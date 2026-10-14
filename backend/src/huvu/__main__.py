"""`uv run huvu` starts the backend with huvu.toml (or $HUVU_CONFIG).

Users and devices are managed from here too:

    uv run huvu user add mom --name "Mom"   asks for a password and her Hermes profile's key
                                            (or reads HUVU_PASSWORD and HUVU_HERMES_KEY)
    uv run huvu user passwd mom
    uv run huvu user hermes-key mom
    uv run huvu user list
    uv run huvu user remove mom
    uv run huvu user alias me mom "mom"     what one user calls another, for the model and
                                            on messages; "" goes back to their name
    uv run huvu user aliases me             what one user calls everyone else
    uv run huvu group add beach --name "Beach house"   asks for the group profile's key
    uv run huvu group join home sitter --guest
    uv run huvu group leave home sitter
    uv run huvu group list
    uv run huvu device list
    uv run huvu device revoke d_1a2b3c4d5e
    uv run huvu tracker add mom              prints a QR code and link that set up OwnTracks on her phone
    uv run huvu tracker add me --browser     prints the address and token for huvu's Chrome extension
    uv run huvu tracker add me --mail        prints the address and token for huvu's Email Worker
    uv run huvu wipe mom                     deletes her triggers, conversations and history (see wipe.py)
    uv run huvu wipe --group home            the same for a group's shared devices
    uv run huvu skills                       writes every source's Hermes skill and exits (setup.sh
                                             pins them, so a fresh install runs this first)

A new user joins the default group (home) as a member, unless told
otherwise with --group, --guest or --no-group.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import uvicorn

from . import config as config_module
from .accounts import Accounts, AuthError
from .db import connect


def main() -> None:
    parser = argparse.ArgumentParser(prog="huvu")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="run the backend (the default)")

    user = commands.add_parser("user", help="manage users").add_subparsers(dest="action", required=True)
    add = user.add_parser("add", help="add a user, with a Hermes profile of the same name")
    add.add_argument("id")
    add.add_argument("--name")
    add.add_argument("--quiet", action="store_true", help="print only the profile's MCP token")
    add.add_argument("--group", help="the group to join (default: the default group)")
    add.add_argument("--guest", action="store_true", help="join as a guest, not a member")
    add.add_argument("--no-group", action="store_true", help="join no group")
    user.add_parser("passwd", help="set a user's password").add_argument("id")
    user.add_parser("hermes-key", help="set the API key of a user's Hermes profile").add_argument("id")
    user.add_parser("list", help="list users")
    user.add_parser("remove", help="remove a user and log out their devices").add_argument("id")
    alias = user.add_parser("alias", help="set what one user calls another")
    alias.add_argument("owner")
    alias.add_argument("person")
    alias.add_argument("alias", help='what the owner calls them; "" goes back to their name')
    user.add_parser("aliases", help="show what one user calls everyone else").add_argument("owner")

    group = commands.add_parser("group", help="manage groups").add_subparsers(dest="action", required=True)
    add_group = group.add_parser("add", help="add a group, with a Hermes profile of the same name")
    add_group.add_argument("id")
    add_group.add_argument("--name")
    add_group.add_argument("--quiet", action="store_true", help="print only the profile's MCP token")
    join = group.add_parser("join", help="put a user in a group, or change their role there")
    join.add_argument("group")
    join.add_argument("user")
    join.add_argument("--guest", action="store_true", help="as a guest, not a member")
    leave = group.add_parser("leave", help="take a user out of a group")
    leave.add_argument("group")
    leave.add_argument("user")
    group.add_parser("list", help="list groups and who is in them")

    device = commands.add_parser("device", help="manage devices").add_subparsers(dest="action", required=True)
    device.add_parser("list", help="list devices")
    device.add_parser("revoke", help="log a device out").add_argument("id")

    tracker = commands.add_parser("tracker", help="manage location trackers (OwnTracks)").add_subparsers(dest="action", required=True)
    add_tracker = tracker.add_parser("add", help="add OwnTracks on a user's phone, and print the link that sets it up")
    add_tracker.add_argument("user")
    add_tracker.add_argument("--name", help="what to call the phone (default: <user>'s phone, <user>'s Chrome, or <user>'s mail)")
    poster = add_tracker.add_mutually_exclusive_group()
    poster.add_argument("--browser", action="store_true", help="for huvu's Chrome extension (browser-extension/), not OwnTracks")
    poster.add_argument("--mail", action="store_true", help="for huvu's Email Worker (mail-worker/), not OwnTracks")

    commands.add_parser("skills", help="write every source's Hermes skill, then exit")

    wipe = commands.add_parser("wipe", help="delete what huvu keeps for a user or a group; the backend must be stopped")
    whom = wipe.add_mutually_exclusive_group(required=True)
    whom.add_argument("user", nargs="?")
    whom.add_argument("--group")

    args = parser.parse_args()
    config = config_module.load()
    if args.command in (None, "serve"):
        _serve(config)
    elif args.command == "skills":
        _skills(config)
    else:
        try:
            _manage(config, args)
        except AuthError as err:
            sys.exit(f"huvu: {err}")


def _skills(config) -> None:
    from .kernel import Kernel

    config.data_dir.mkdir(parents=True, exist_ok=True)
    kernel = Kernel(config)
    kernel.write_skills()
    kernel.data.close()
    kernel.db.close()


def _serve(config) -> None:
    from .http import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # A push stream never ends by itself, so without a limit a stop (s6-svc
    # -d, as wipe.sh needs) waits for every app to close.
    uvicorn.run(create_app(config), host=config.host, port=config.port, timeout_graceful_shutdown=5)


def _manage(config, args) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(config.data_dir / "huvu.db")
    tz = ZoneInfo(config.timezone)
    accounts = Accounts(conn, lambda: datetime.now(tz), household_key=config.hermes_api_key, household_mcp_token=config.mcp_token)
    match (args.command, getattr(args, "action", None)):
        case ("user", "add"):
            password = os.environ.get("HUVU_PASSWORD") or _new_password()
            key = os.environ.get("HUVU_HERMES_KEY") or getpass.getpass(f"API key of Hermes profile {args.id!r} (its API_SERVER_KEY): ")
            token = accounts.add_user(args.id, args.name or args.id, password, key)
            if not args.no_group:
                accounts.join(args.group or accounts.default_group(), args.id, "guest" if args.guest else "member")
            if args.quiet:
                print(token)
            else:
                print(f"Added {args.id}. Put HUVU_MCP_TOKEN={token} in their Hermes profile's .env (shown once).")
                print("The rest of the profile's setup is in hermes-plugin/README.md, or use setup.sh at the repo root.")
        case ("user", "passwd"):
            accounts.set_password(args.id, _new_password())
        case ("user", "hermes-key"):
            accounts.set_hermes_key(args.id, getpass.getpass(f"API key of Hermes profile {args.id!r}: "))
        case ("user", "list"):
            for u in accounts.users():
                groups = ", ".join(f"{g} ({role})" for g, role in accounts.memberships(u.id).items()) or "no group"
                print(f"{u.id}\t{u.name}\t{groups}\t{u.created_at:%Y-%m-%d}")
        case ("user", "remove"):
            accounts.remove_user(args.id)
        case ("user", "alias"):
            accounts.set_alias(args.owner, args.person, args.alias)
        case ("user", "aliases"):
            if accounts.user(args.owner) is None:
                raise AuthError(f"no user {args.owner!r}")
            for u in accounts.users():
                if u.id != args.owner:
                    print(f"{u.id}\t{accounts.alias(args.owner, u.id)}")
        case ("group", "add"):
            key = os.environ.get("HUVU_HERMES_KEY") or getpass.getpass(f"API key of Hermes profile {args.id!r} (its API_SERVER_KEY): ")
            token = accounts.add_group(args.id, args.name or args.id, key)
            if args.quiet:
                print(token)
            else:
                print(f"Added {args.id}. Put HUVU_MCP_TOKEN={token} in its Hermes profile's .env (shown once).")
                print("Set the profile up like a user's (hermes-plugin/README.md), then add members with `huvu group join`.")
        case ("group", "join"):
            accounts.join(args.group, args.user, "guest" if args.guest else "member")
        case ("group", "leave"):
            accounts.leave(args.group, args.user)
        case ("group", "list"):
            for g in accounts.groups():
                people = ", ".join(f"{u.id} ({accounts.role(g.id, u.id)})" for u in accounts.people(g.id)) or "nobody"
                print(f"{g.id}\t{g.name}{' (default)' if g.is_default else ''}\t{people}")
        case ("device", "list"):
            for d in accounts.devices():
                print(f"{d.id}\t{d.kind}\t{d.user or f'group {d.group}'}\t{d.name}\t{d.created_at:%Y-%m-%d}")
        case ("device", "revoke"):
            accounts.revoke(args.id)
        case ("tracker", "add") if args.browser:
            _add_browser(config, accounts, args.user, args.name or f"{args.user}'s Chrome")
        case ("tracker", "add") if args.mail:
            _add_mail(config, accounts, args.user, args.name or f"{args.user}'s mail")
        case ("tracker", "add"):
            _add_tracker(config, accounts, args.user, args.name or f"{args.user}'s phone")
        case ("wipe", _):
            _wipe(config, conn, accounts, args.user, args.group)
    conn.close()


def _wipe(config, conn, accounts: Accounts, user: str | None, group: str | None) -> None:
    import socket

    from .wipe import wipe

    with socket.socket() as probe:
        probe.settimeout(0.5)
        if probe.connect_ex((config.host if config.host not in ("0.0.0.0", "::") else "127.0.0.1", config.port)) == 0:
            sys.exit(f"huvu: the backend is running on port {config.port}; stop it first")
    if user is not None and accounts.user(user) is None:
        sys.exit(f"huvu: no user {user!r}")
    if group is not None and accounts.group(group) is None:
        sys.exit(f"huvu: no group {group!r}")
    from .data import FILE

    counts = wipe(conn, config.data_dir / FILE, config.data_dir / "scripts", sources=config.data_dir / "sources", user=user, group=group, default_group=group == accounts.default_group())
    print(", ".join(f"{n} {table}" for table, n in counts.items()))


def _add_tracker(config, accounts: Accounts, user: str, name: str) -> None:
    """Adds a tracker and prints what sets OwnTracks up on the phone: a QR
    code to scan, and the same link to open on the phone itself."""
    import segno

    from huvu_drivers.location import setup_link

    source = next((s.name for s in config.sources if s.driver.endswith("location:LocationSource")), None)
    if source is None:
        raise AuthError("no location source in huvu.toml; add one first (see huvu_drivers/location.py)")
    if "localhost" in config.public_url or "127.0.0.1" in config.public_url:
        raise AuthError(f"public_url is {config.public_url}, which a phone can't reach; set the backend's public URL in huvu.toml first")
    device, token = accounts.add_tracker(user, name)
    link = setup_link(f"{config.public_url}/api/ingest/{source}", user, device.id, token)
    segno.make(link, error="l").terminal(compact=True)
    print(f"Added tracker {device.id} for {user}. On their phone, install OwnTracks, then scan this code or open the link (it holds the token, shown once):")
    print(link)


def _add_browser(config, accounts: Accounts, user: str, name: str) -> None:
    """Adds a tracker for huvu's Chrome extension, and prints what goes in
    the extension's options."""
    source = next((s.name for s in config.sources if s.driver.endswith("browser:BrowserSource")), None)
    if source is None:
        raise AuthError("no browser source in huvu.toml; add one first (see huvu_drivers/browser.py)")
    device, token = accounts.add_tracker(user, name)
    print(f"Added tracker {device.id} for {user}. In the huvu extension's options in their Chrome, set (the token is shown once):")
    print(f"  address  {config.public_url}")
    print(f"  token    {token}")


def _add_mail(config, accounts: Accounts, user: str, name: str) -> None:
    """Adds a tracker for huvu's Email Worker, and prints what goes in its
    settings. One worker serves one address, so each person who forwards
    mail gets an address and a worker of their own."""
    source = next((s.name for s in config.sources if s.driver.endswith("mail:MailSource")), None)
    if source is None:
        raise AuthError("no mail source in huvu.toml; add one first (see huvu_drivers/mail.py)")
    device, token = accounts.add_tracker(user, name)
    print(f"Added tracker {device.id} for {user}. Set these on the Email Worker (mail-worker/; the token is shown once):")
    print(f"  HUVU_URL    {config.public_url}/api/ingest/{source}")
    print(f"  HUVU_TOKEN  {token}  (a secret: wrangler secret put HUVU_TOKEN)")


def _new_password() -> str:
    password = getpass.getpass("Password: ")
    if len(password) < 8:
        raise AuthError("a password needs at least 8 characters")
    if getpass.getpass("Again: ") != password:
        raise AuthError("the passwords don't match")
    return password


if __name__ == "__main__":
    main()
