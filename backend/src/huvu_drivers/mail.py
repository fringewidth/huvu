"""Mail a person forwards to huvu: bills, receipts, whatever they choose.

huvu has no access to anyone's inbox. A person sets a filter in their mail
to forward what they want huvu to have to an address of huvu's own, such
as bills@mail.hrishik.me. Cloudflare Email Routing hands that address's
mail to an Email Worker (mail-worker/ at the repo root), which posts each
mail as it came to POST /api/ingest/mail, with a tracker's token (`huvu
tracker add <user> --mail`). So huvu sees only what passed the filter,
never the OTPs and reset links in the rest of the inbox.

Each mail is a row of `mail_messages`, and each file attached to it a row
of `mail_attachments`, both their person's alone. A file is kept in the
source's folder, and its row holds the absolute path, so Hermes's browser
can upload it and its file tools can read it. A PDF's text is in its row
too. Mail is written by strangers, so both tables are labelled `external`.

Anyone can send mail to the address and claim any sender. So each mail is
checked for DKIM signatures here, and `signed_by` names the domains whose
signature checked out. Forwarding keeps the original signature: Gmail's
filter forwards the message as it came, and the headers Gmail and
Cloudflare add on the way aren't signed. A mail whose sender's domain
isn't in `signed_by` may be forged.

    [[sources]]
    name = "mail"
    driver = "huvu_drivers.mail:MailSource"
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import logging
import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

import dkim
import dkim.dnsplug
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from huvu.prompts import MAIL_GUIDE, TEXT
from huvu.source import Source, SourceError
from huvu.spec import Caller, ColumnSpec, SourceSpec, TableSpec

log = logging.getLogger("huvu.source.mail")

# Cloudflare Email Routing takes mail up to 25 MiB.
MAX_RAW = 25 * 1024 * 1024
TEXT_MAX = 20_000
SUBJECT_MAX = 500
NAME_MAX = 200
DNS_TIMEOUT = 5


def _column(table: str, name: str, type: str) -> ColumnSpec:
    return ColumnSpec(name=name, type=type, description=TEXT[f"mail.table.{table}.column.{name}"])


TABLES = [
    TableSpec(
        name="messages",
        description=TEXT["mail.table.messages"],
        columns=[
            _column("messages", "message_id", "text"),
            _column("messages", "sender", "text"),
            _column("messages", "sender_name", "text"),
            _column("messages", "subject", "text"),
            _column("messages", "sent_at", "text"),
            _column("messages", "signed_by", "text"),
            _column("messages", "forwarded_by", "text"),
            _column("messages", "text", "text"),
            _column("messages", "attachments", "integer"),
        ],
        labels=["external"],
        indexes=[["message_id"], ["sender", "ts"]],
    ),
    TableSpec(
        name="attachments",
        description=TEXT["mail.table.attachments"],
        columns=[
            _column("attachments", "message", "integer"),
            _column("attachments", "name", "text"),
            _column("attachments", "type", "text"),
            _column("attachments", "size", "integer"),
            _column("attachments", "path", "text"),
            _column("attachments", "text", "text"),
        ],
        labels=["external"],
        indexes=[["message"]],
    ),
]


class MailSource(Source):
    def describe(self) -> SourceSpec:
        return SourceSpec(name="mail", description=TEXT["mail.description"], guide=MAIL_GUIDE, tables=TABLES)

    async def ingest(self, body: Any, caller: Caller) -> Any:
        """Takes one mail as it came, `{"raw": base64 of the message, "from":
        the envelope sender}`, from the Email Worker, and answers whether it
        kept it. A mail huvu has (by its Message-ID) is skipped, so a
        retried post keeps one copy."""
        user = caller.user
        if user is None:
            raise SourceError("mail needs a person's tracker")
        if not isinstance(body, dict) or not isinstance(body.get("raw"), str):
            raise SourceError('post {"raw": "<the message, base64>", "from": "<the envelope sender>"}')
        try:
            raw = base64.b64decode(body["raw"], validate=True)
        except (binascii.Error, ValueError) as err:
            raise SourceError(f"raw isn't base64: {err}") from err
        if len(raw) > MAX_RAW:
            raise SourceError(f"a mail can be at most {MAX_RAW // (1024 * 1024)} MiB")
        message: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)  # type: ignore[assignment]
        message_id = str(message.get("Message-ID") or "").strip() or f"<sha256:{hashlib.sha256(raw).hexdigest()}>"
        if self.ctx.rows("messages", "user_id = ? AND message_id = ?", (user, message_id), limit=1):
            return {"kept": False, "why": "already here"}
        signed_by = await asyncio.to_thread(_signed_by, raw)
        sender_name, sender = _sender(message)
        files = [part for part in message.iter_attachments() if _is_file(part)]
        row = {
            "message_id": message_id,
            "sender": sender,
            "sender_name": sender_name,
            "subject": str(message.get("Subject") or "").strip()[:SUBJECT_MAX],
            "sent_at": _sent_at(message),
            "signed_by": ", ".join(signed_by) or None,
            "forwarded_by": str(body.get("from") or "").strip() or None,
            "text": _body_text(message),
            "attachments": len(files),
        }
        row_id = await self.ctx.record("messages", row, user=user)
        folder = self.ctx.data_dir / user / str(row_id)
        names: set[str] = set()
        for n, part in enumerate(files):
            content = part.get_payload(decode=True) or b""
            name = _file_name(part.get_filename(), n)
            name = name if name not in names else f"{n + 1}-{name}"
            names.add(name)
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / name
            path.write_bytes(content)
            kind = part.get_content_type()
            text = _pdf_text(content) if kind == "application/pdf" or name.lower().endswith(".pdf") else None
            attachment = {"message": row_id, "name": name, "type": kind, "size": len(content), "path": str(path), "text": text}
            await self.ctx.record("attachments", attachment, user=user)
        return {"kept": True, "attachments": len(files)}


def _is_file(part: EmailMessage) -> bool:
    """A file the sender attached: not a picture the mail's own layout
    shows, and not a forwarded mail, whose text is in the body."""
    if part.get_content_maintype() in ("multipart", "message"):
        return False
    return part.get_content_disposition() == "attachment" or part.get_content_maintype() != "image"


def _sender(message: EmailMessage) -> tuple[str | None, str | None]:
    found = getaddresses([str(message.get("From") or "")])
    if not found or not found[0][1]:
        return None, None
    name, address = found[0]
    return name or None, address.lower()


def _sent_at(message: EmailMessage) -> str | None:
    try:
        return parsedate_to_datetime(str(message["Date"])).isoformat() if message["Date"] else None
    except (TypeError, ValueError):
        return None


def _signed_by(raw: bytes) -> list[str]:
    """The domains whose DKIM signature on the mail checked out, in the
    order the signatures come."""
    count = len(re.findall(rb"^DKIM-Signature:", raw.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0], re.IGNORECASE | re.MULTILINE))
    domains: list[str] = []
    for idx in range(count):
        checker = dkim.DKIM(raw, timeout=DNS_TIMEOUT)
        try:
            if checker.verify(idx=idx, dnsfunc=_dns_txt):
                domain = checker.domain.decode().lower() if isinstance(checker.domain, bytes) else str(checker.domain).lower()
                if domain not in domains:
                    domains.append(domain)
        except Exception as err:
            log.info("a DKIM signature didn't check out: %s", err)
    return domains


def _dns_txt(name: bytes | str, timeout: int = DNS_TIMEOUT) -> bytes | None:
    """A DNS TXT record, for DKIM's public keys. Tests replace it."""
    return dkim.dnsplug.get_txt(name, timeout=timeout)


def _body_text(message: EmailMessage) -> str | None:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return None
    try:
        content = part.get_content()
    except (LookupError, ValueError):
        return None
    text = content if part.get_content_subtype() == "plain" else _html_text(content)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    return text[:TEXT_MAX] or None


class _Text(HTMLParser):
    BLOCKS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}
    SKIP = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self._skipping += 1
        elif tag in self.BLOCKS:
            self.out.append("\n")
        elif tag == "td":
            self.out.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skipping = max(0, self._skipping - 1)
        elif tag in self.BLOCKS:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.out.append(data)


def _html_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    parser.close()
    return "".join(parser.out)


def _file_name(name: str | None, n: int) -> str:
    """A file name safe to write: no folders, no leading dot."""
    name = re.sub(r"[^\w.\- ()]", "_", (name or "").replace("\\", "/").rsplit("/", 1)[-1]).strip(" .")
    return name[:NAME_MAX] or f"attachment-{n + 1}"


def _pdf_text(content: bytes) -> str | None:
    try:
        reader = PdfReader(io.BytesIO(content))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except (PyPdfError, ValueError, KeyError, TypeError, OSError) as err:
        log.info("couldn't read a PDF's text: %s", err)
        return None
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text[:TEXT_MAX] or None
