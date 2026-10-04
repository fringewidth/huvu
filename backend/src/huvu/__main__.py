"""`uv run huvu` starts the backend with huvu.toml (or $HUVU_CONFIG).

Users and devices are managed from here too:

    uv run huvu user add mom --name "Mom"   asks for a password and her Hermes profile's key
                                            (or reads HUVU_PASSWORD and HUVU_HERMES_KEY)
    uv run huvu user passwd mom
    uv run huvu user hermes-key mom
    uv run huvu user list
    uv run huvu user remove mom
    uv run huvu group add beach --name "Beach house"   asks for the group profile's key
    uv run huvu group join home sitter --guest
    uv run huvu group leave home sitter
    uv run huvu group list
    uv run huvu device list
    uv run huvu device revoke d_1a2b3c4d5e

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

    args = parser.parse_args()
    config = config_module.load()
    if args.command in (None, "serve"):
        _serve(config)
    else:
        try:
            _manage(config, args)
        except AuthError as err:
            sys.exit(f"huvu: {err}")


def _serve(config) -> None:
    from .http import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(config), host=config.host, port=config.port)


def _manage(config, args) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(config.data_dir / "huvu.db")
    accounts = Accounts(conn, lambda: datetime.now().astimezone(), household_key=config.hermes_api_key, household_mcp_token=config.mcp_token)
    match (args.command, args.action):
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
    conn.close()


def _new_password() -> str:
    password = getpass.getpass("Password: ")
    if len(password) < 8:
        raise AuthError("a password needs at least 8 characters")
    if getpass.getpass("Again: ") != password:
        raise AuthError("the passwords don't match")
    return password


if __name__ == "__main__":
    main()
