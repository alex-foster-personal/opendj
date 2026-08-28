"""CLI companion to the webui's Google sign-in, for agents and debugging.

    python -m apps.webui.auth_cli whoami
    python -m apps.webui.auth_cli sessions
    python -m apps.webui.auth_cli login [--origin http://127.0.0.1:9418]
    python -m apps.webui.auth_cli logout-all

``whoami`` and ``sessions`` read ``data/state/state.db`` directly, so they
work with the daemon stopped -- useful for answering "is anyone signed in"
without booting anything.

``login`` deliberately does NOT build a consent URL itself. The CSRF state
and PKCE verifier for a sign-in live in the daemon process that will
receive the callback, so a URL minted here would be unredeemable there.
It asks the daemon instead, and says so plainly if the daemon is down.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

from apps.shared.state import db as state_db
from apps.shared.state.paths import STATE_DB
from apps.webui.port_config import PortConfigError, resolve_backend_port


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise SystemExit(
            f"state DB not found at {db_path}; nobody has ever signed in "
            f"(run `python -m apps.shared.state.cli init` to create it)"
        )
    return state_db.open_rw(db_path)


def _cmd_whoami(db_path: Path) -> int:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT u.email, u.name, u.google_sub, COUNT(s.session_token_sha256)
            FROM users AS u
            LEFT JOIN auth_sessions AS s ON s.google_sub = u.google_sub
            GROUP BY u.google_sub
            ORDER BY u.email
            """
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        sys.stdout.write("not signed in (no users in the state DB)\n")
        return 1
    for email, name, sub, session_count in rows:
        sys.stdout.write(
            f"{email}  {name or '(no name)'}  sub={sub}  "
            f"sessions={session_count}\n"
        )
    return 0


def _cmd_sessions(db_path: Path) -> int:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT u.email, substr(s.session_token_sha256, 1, 12),
                   s.created_at, s.last_seen_at, s.expires_at,
                   s.refresh_token IS NOT NULL
            FROM auth_sessions AS s
            JOIN users AS u ON u.google_sub = s.google_sub
            ORDER BY s.created_at DESC
            """
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        sys.stdout.write("no active sessions\n")
        return 1
    for email, prefix, created, seen, expires, has_refresh in rows:
        # The stored value is a hash, so showing a prefix leaks nothing
        # usable -- it is only there to tell two sessions apart.
        sys.stdout.write(
            f"{email}  hash={prefix}...  created={created}  "
            f"last_seen={seen}  expires={expires}  "
            f"refresh_token={'yes' if has_refresh else 'no'}\n"
        )
    return 0


def _cmd_logout_all(db_path: Path) -> int:
    conn = _connect(db_path)
    try:
        deleted = conn.execute("DELETE FROM auth_sessions").rowcount
    finally:
        conn.close()
    sys.stdout.write(f"deleted {deleted} session(s)\n")
    return 0


def _cmd_login(origin: str | None) -> int:
    try:
        port = resolve_backend_port(None)
    except PortConfigError as exc:
        sys.stderr.write(f"cannot resolve the backend port: {exc}\n")
        return 2
    url = f"http://127.0.0.1:{port}/api/v1/auth/login"
    payload = json.dumps({"origin": origin} if origin else {}).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        sys.stderr.write(f"daemon returned {exc.code}:\n{detail}\n")
        return 1
    except urllib.error.URLError as exc:
        sys.stderr.write(
            f"could not reach the webui daemon at {url}: {exc.reason}\n"
            "Start it first -- the sign-in state must be minted by the same "
            "process that will receive Google's callback.\n"
        )
        return 2
    sys.stdout.write(
        f"Open this URL in a browser to sign in:\n\n"
        f"{body['authorization_url']}\n\n"
        f"redirect_uri: {body['redirect_uri']}\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="apps.webui.auth_cli",
        description="Inspect and drive the webui's Google sign-in.",
    )
    parser.add_argument(
        "--data-db", type=Path, default=None,
        help=f"state DB path (default: {STATE_DB})",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("whoami", help="who is signed in (reads the state DB)")
    sub.add_parser("sessions", help="list active sessions")
    sub.add_parser("logout-all", help="delete every session")
    login = sub.add_parser("login", help="ask the daemon for a consent URL")
    login.add_argument(
        "--origin", default=None,
        help="loopback origin the browser will be on, "
             "e.g. http://127.0.0.1:9418",
    )

    args = parser.parse_args(argv)
    db_path = args.data_db if args.data_db is not None else STATE_DB

    if args.command == "whoami":
        return _cmd_whoami(db_path)
    elif args.command == "sessions":
        return _cmd_sessions(db_path)
    elif args.command == "logout-all":
        return _cmd_logout_all(db_path)
    elif args.command == "login":
        return _cmd_login(args.origin)
    raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["main"]
