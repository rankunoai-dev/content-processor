"""`accounts` on the command line: the first admin, accounts and personal access tokens.

    accounts create-admin --username gaurav --name "Gaurav Doshi"
    accounts add --username anna --name "Anna" --role reviewer
    accounts list
    accounts reset-password --username anna
    accounts token --username gaurav --label "Claude Desktop" [--role viewer]
    accounts tokens [--username gaurav]
    accounts revoke-token --id tok-...

Passwords are typed twice without being shown (or read from standard input when it
is not a terminal, for scripts); they never appear in the command line or the shell
history. `create-admin` and `add` set a password the person must change at their
first sign-in, except the very first admin's (chosen by themselves here). A token is
shown once.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any, cast

from src.core.config import get_settings
from src.modules.accounts.store import ROLES, AccountError, AccountStore, Role

__all__ = ["register"]


def _store(args: argparse.Namespace) -> AccountStore:
    root = Path(args.app_data) if args.app_data else get_settings().cp_app_data_root
    return AccountStore(root / "accounts")


def _password(prompt: str) -> str:
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass(prompt)
    again = getpass.getpass("Type it again: ")
    if first != again:
        msg = "the two passwords differ"
        raise AccountError(msg)
    return first


def _print(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2, default=str) + "\n")


def cmd_accounts(args: argparse.Namespace) -> int:
    """Run one `accounts` action."""
    store = _store(args)
    try:
        if args.action in ("create-admin", "add"):
            if not args.username:
                raise AccountError("give --username")
            first = args.action == "create-admin" and not store.active()
            role: Role = "admin" if args.action == "create-admin" else cast(Role, args.role)
            password = _password(
                "Password (at least 10 characters): " if first else "Temporary password: "
            )
            account = store.create(
                args.username,
                args.name or args.username,
                role,
                password,
                created_by="command line",
                must_change_password=not first,
            )
            _print({"created": account.username, "role": account.role})
            if first:
                sys.stdout.write(
                    "Sign-in is now required for everyone using the app. Sign in with this "
                    "account and add the team on the Accounts page.\n"
                )
        elif args.action == "list":
            _print(
                [
                    {
                        "username": a.username,
                        "name": a.display_name,
                        "role": a.role,
                        "disabled": a.disabled,
                        "must_change_password": a.must_change_password,
                        "last_login_at": a.last_login_at,
                    }
                    for a in store.all()
                ]
            )
        elif args.action == "reset-password":
            account = store.set_password(
                args.username or "", _password("Temporary password: "), must_change=True
            )
            _print({"reset": account.username, "must_change_password": True})
        elif args.action == "token":
            token = store.create_token(args.username or "", args.label or "token", args.token_role)
            sys.stdout.write(
                f"{token}\nKeep it secret: it is shown once. Programs send it as "
                "'Authorization: Bearer <token>'.\n"
            )
        elif args.action == "tokens":
            _print(
                [
                    t.model_dump(mode="json", exclude={"token_hash"})
                    for t in store.tokens(args.username)
                ]
            )
        else:  # revoke-token
            store.revoke_token(args.id or "")
            _print({"revoked": args.id})
    except AccountError as exc:
        sys.stderr.write(f"Not done: {exc}.\n")
        return 1
    return 0


def register(subparsers: Any) -> None:
    """Add `accounts` to the CLI."""
    parser = subparsers.add_parser("accounts", help="team accounts, roles and access tokens")
    parser.add_argument(
        "action",
        choices=[
            "create-admin",
            "add",
            "list",
            "reset-password",
            "token",
            "tokens",
            "revoke-token",
        ],
    )
    parser.add_argument("--username", default=None)
    parser.add_argument("--name", default=None, help="display name")
    parser.add_argument("--role", default="viewer", choices=list(ROLES), help="add: the role")
    parser.add_argument(
        "--token-role", default=None, choices=list(ROLES), help="token: act with less"
    )
    parser.add_argument("--label", default=None, help="token: what it is for")
    parser.add_argument("--id", default=None, help="revoke-token: the token id")
    parser.add_argument("--app-data", default=None, help="app data folder (CP_APP_DATA_ROOT)")
    parser.set_defaults(func=cmd_accounts)
