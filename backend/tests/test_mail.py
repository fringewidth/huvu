"""The mail source: forwarded mail with its files, DKIM, and privacy
between people."""

from __future__ import annotations

import base64
from email.message import EmailMessage

import dkim
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from huvu.config import Config
from huvu.http import create_app
from huvu.kernel import Kernel
from huvu.spec import Caller
from huvu_drivers import mail as mail_driver
from huvu_drivers.mail import MailSource

MY_MAIL = Caller(kind="device", user="me", device="t_me")
ME = Caller(kind="hermes", session="s-me", user="me")
MOM = Caller(kind="hermes", session="s-mom", user="mom")

pytestmark = pytest.mark.usefixtures("family")

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
PUBLIC = base64.b64encode(KEY.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    """The shop's DKIM key, as its DNS would serve it."""
    records = {b"s1._domainkey.shop.example.": b"v=DKIM1; k=rsa; p=" + PUBLIC}

    def txt(name, timeout=5):
        return records.get(name if isinstance(name, bytes) else name.encode())

    monkeypatch.setattr(mail_driver, "_dns_txt", txt)


@pytest.fixture
async def kernel(tmp_path, clock, home, hermes):
    k = Kernel(Config(data_dir=tmp_path), hermes=hermes, sources={"home": home, "mail": MailSource()}, clock=clock)
    await home.run()
    await k.triggers.load()
    yield k
    await k.triggers.stop()
    await k.conversations.close()
    k.data.close()
    k.db.close()


@pytest.fixture
def source(kernel) -> MailSource:
    return kernel.registry.get("mail")


def pdf(text: str) -> bytes:
    """A one-page PDF with `text` on it."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def bill(sender: str = "orders@shop.example", subject: str = "Your order", message_id: str = "<o1@shop.example>", sign: bool = True) -> bytes:
    message = EmailMessage()
    message["From"] = f"Shop <{sender}>"
    message["To"] = "me@example.com"
    message["Subject"] = subject
    message["Date"] = "Wed, 14 Oct 2026 19:02:00 +0530"
    message["Message-ID"] = message_id
    message.set_content("Your order is on its way.")
    message.add_alternative("<html><head><style>p{}</style></head><body><p>Your order</p><table><tr><td>Total</td><td>245.00</td></tr></table></body></html>", subtype="html")
    message.get_payload()[1].add_related(b"\x89PNG\r\n\x1a\nlogo", maintype="image", subtype="png", cid="<logo>", filename="logo.png")
    message.add_attachment(pdf("Invoice total 245.00"), maintype="application", subtype="pdf", filename="../invoice.pdf")
    raw = message.as_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    if sign:
        raw = dkim.sign(raw, b"s1", b"shop.example", PRIVATE, include_headers=[b"from", b"subject", b"date", b"message-id"]) + raw
    # What Gmail and Cloudflare add on the way, which no signature covers.
    return b"X-Forwarded-To: bills@mail.example\r\nReceived: by cloudflare\r\n" + raw


def post(raw: bytes) -> dict:
    return {"raw": base64.b64encode(raw).decode(), "from": "me+caf_=bills@example.com"}


async def sql(kernel, caller: Caller, query: str):
    return await kernel.tools.call("data_sql", {"sql": query}, caller)


async def test_a_signed_bill_is_kept_with_its_pdf(source, kernel):
    assert await source.ingest(post(bill()), MY_MAIL) == {"kept": True, "attachments": 1}
    row = (await sql(kernel, ME, "SELECT sender, sender_name, subject, sent_at, signed_by, forwarded_by, text, attachments FROM mail_messages")).data["rows"][0]
    assert row[:6] == ["orders@shop.example", "Shop", "Your order", "2026-10-14T19:02:00+05:30", "shop.example", "me+caf_=bills@example.com"]
    # The plain text wins over the HTML when the mail has both.
    assert row[6] == "Your order is on its way." and row[7] == 1
    files = (await sql(kernel, ME, "SELECT a.name, a.type, a.path, a.text FROM mail_attachments a JOIN mail_messages m ON a.message = m.id")).data["rows"]
    # The layout's logo isn't a file the sender attached, and a name can't
    # climb out of the source's folder.
    assert [f[:2] for f in files] == [["invoice.pdf", "application/pdf"]]
    path = files[0][2]
    assert path.startswith(str(kernel.config.data_dir)) and path.endswith("/me/1/invoice.pdf")
    assert open(path, "rb").read().startswith(b"%PDF") and "Invoice total 245.00" in files[0][3]


async def test_a_mail_huvu_has_is_kept_once(source):
    assert (await source.ingest(post(bill()), MY_MAIL))["kept"]
    assert await source.ingest(post(bill()), MY_MAIL) == {"kept": False, "why": "already here"}


async def test_a_forged_or_altered_mail_has_no_signer(source, kernel):
    await source.ingest(post(bill(message_id="<o2@shop.example>", sign=False)), MY_MAIL)
    altered = bill(message_id="<o3@shop.example>").replace(b"Subject: Your order", b"Subject: Your refund")
    await source.ingest(post(altered), MY_MAIL)
    rows = (await sql(kernel, ME, "SELECT subject, signed_by FROM mail_messages ORDER BY id")).data["rows"]
    assert rows == [["Your order", None], ["Your refund", None]]


async def test_html_only_mail_is_made_plain():
    message = EmailMessage()
    message["From"] = "a@b.example"
    message.set_content("<p>Order <b>42</b></p><script>alert(1)</script><table><tr><td>Total</td><td>99</td></tr></table>", subtype="html")
    assert mail_driver._body_text(message) == "Order 42\nTotal 99"


async def test_mail_is_private_and_labelled_external(source, kernel):
    await source.ingest(post(bill()), MY_MAIL)
    mine = await sql(kernel, ME, "SELECT count(*) FROM mail_messages")
    assert mine.data["rows"] == [[1]] and "external" in mine.labels
    assert (await sql(kernel, MOM, "SELECT count(*) FROM mail_messages")).data["rows"] == [[0]]
    assert (await sql(kernel, MOM, "SELECT count(*) FROM mail_attachments")).data["rows"] == [[0]]


async def test_the_worker_posts_with_a_trackers_token(kernel):
    _, token = kernel.accounts.add_tracker("me", "my mail")
    app = create_app(kernel.config, kernel)
    auth = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/ingest/mail", json=post(bill()), headers=auth)
        assert res.status_code == 200 and res.json() == {"kept": True, "attachments": 1}
        assert (await client.post("/api/ingest/mail", json={"raw": "not base64!"}, headers=auth)).status_code == 400
        assert (await client.post("/api/ingest/mail", json=post(bill()))).status_code == 401
