"""
``opencraig`` command-line entry point.

Usage:

    opencraig serve [--config myconfig.yaml] [--host 0.0.0.0] [--port 8000]
    opencraig ask   "question" [--server URL] [--path /scope] [--no-kg]
    opencraig ingest <file> [--folder /destination]
    opencraig health [--server URL]
    opencraig version

The ``serve`` subcommand is a shim for the existing ``main.py`` launcher
so installing the package exposes a proper console script; everything
else is a thin wrapper over ``opencraig.client.Client``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence


def _cmd_serve(args: argparse.Namespace) -> int:
    # Delegate to the existing main.py launcher (config resolution, uvicorn, etc.).
    import main as _main

    # main.main() reads CLI args via parse_args(); re-invoke by building argv.
    argv = ["main.py"]
    if args.config:
        argv += ["--config", str(args.config)]
    if args.host:
        argv += ["--host", args.host]
    if args.port:
        argv += ["--port", str(args.port)]
    if args.reload:
        argv += ["--reload"]
    if args.workers:
        argv += ["--workers", str(args.workers)]
    if args.log_level:
        argv += ["--log-level", args.log_level]
    _orig, sys.argv = sys.argv, argv
    try:
        return _main.main() or 0
    finally:
        sys.argv = _orig


def _cmd_ask(args: argparse.Namespace) -> int:
    from .client import Client

    overrides: dict = {}
    if args.no_kg:
        overrides["kg_path"] = False
    if args.no_tree:
        overrides["tree_path"] = False
    if args.no_qu:
        overrides["query_understanding"] = False
    if args.no_rerank:
        overrides["rerank"] = False

    with Client(args.server) as c:
        if args.stream:
            for event, data in c.ask_stream(
                args.query,
                path_filter=args.path,
                overrides=overrides or None,
            ):
                if event == "delta":
                    print(data.get("text", ""), end="", flush=True)
                elif event == "done":
                    print()  # newline
                elif event == "error":
                    print(f"\n[error] {data}", file=sys.stderr)
                    return 1
        else:
            ans = c.ask(args.query, path_filter=args.path, overrides=overrides or None)
            print(ans.text)
            if ans.citations_used:
                print(f"\n— {len(ans.citations_used)} citation(s) —", file=sys.stderr)
                for c_ in ans.citations_used:
                    print(f"  [{c_.citation_id}] {c_.doc_id} p.{c_.page_no}", file=sys.stderr)
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    from .client import Client

    with Client(args.server) as c:
        resp = c.upload(args.file, folder_path=args.folder, doc_id=args.doc_id)
    print(json.dumps(resp, indent=2, ensure_ascii=False))
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    from .client import Client

    with Client(args.server) as c:
        h = c.health()
    print(json.dumps(h, indent=2, ensure_ascii=False))
    return 0 if h.get("status") == "ok" else 1


def _cmd_version(_args: argparse.Namespace) -> int:
    from . import __version__

    print(__version__)
    return 0


# ---------------------------------------------------------------------------
# Auth subcommands
# ---------------------------------------------------------------------------


def _cmd_auth(args: argparse.Namespace) -> int:
    sub = getattr(args, "auth_action", None)
    if sub == "reset-password":
        return _auth_reset_password(args)
    if sub == "list-tokens":
        return _auth_list_tokens(args)
    if sub == "create-token":
        return _auth_create_token(args)
    if sub == "revoke-token":
        return _auth_revoke_token(args)
    if sub == "whoami":
        return _auth_whoami(args)
    if sub == "logout":
        return _auth_logout(args)
    print("unknown auth subcommand — see `opencraig auth --help`", file=sys.stderr)
    return 2


def _auth_reset_password(args: argparse.Namespace) -> int:
    """Direct DB write — bypasses HTTP, useful if admin locked themselves out."""
    from getpass import getpass

    from sqlalchemy import select

    from api.auth.primitives import hash_password
    from config.loader import load_config
    from persistence.models import AuthUser
    from persistence.store import Store

    cfg = load_config(args.config)
    new_pw = args.new_password or getpass("New password: ")
    if len(new_pw) < 4:
        print("refusing: password too short (< 4 chars)", file=sys.stderr)
        return 2

    store = Store(cfg.persistence.relational)
    store.connect()
    store.ensure_schema()
    with store.transaction() as sess:
        user = sess.execute(select(AuthUser).where(AuthUser.username == args.username)).scalar_one_or_none()
        if user is None:
            print(f"user {args.username!r} not found", file=sys.stderr)
            return 2
        user.password_hash = hash_password(new_pw)
        user.must_change_password = False
    print(f"password reset for {args.username}")
    return 0


def _auth_whoami(args: argparse.Namespace) -> int:
    from .client import Client

    with Client(args.server) as c:
        r = c._client.get("/api/v1/auth/me")
        if r.status_code != 200:
            print(f"HTTP {r.status_code}: {r.text}", file=sys.stderr)
            return 1
        print(json.dumps(r.json(), indent=2, ensure_ascii=False))
    return 0


def _auth_list_tokens(args: argparse.Namespace) -> int:
    from .client import Client

    with Client(args.server) as c:
        r = c._client.get("/api/v1/auth/tokens")
        r.raise_for_status()
        rows = r.json()
    if not rows:
        print("(no tokens)")
        return 0
    print(f"{'ID':18}  {'NAME':24}  {'PREFIX':8}  {'STATUS':10}  CREATED")
    for t in rows:
        status = "revoked" if t.get("revoked_at") else "active"
        print(f"{t['token_id']:18}  {t['name']:24}  {t['hash_prefix']:8}  {status:10}  {t.get('created_at', '')}")
    return 0


def _auth_create_token(args: argparse.Namespace) -> int:
    from .client import Client

    body = {"name": args.name}
    if args.expires_days:
        body["expires_days"] = args.expires_days
    with Client(args.server) as c:
        r = c._client.post("/api/v1/auth/tokens", json=body)
        r.raise_for_status()
        d = r.json()
    print(f"Name:      {d['name']}")
    print(f"Token:     {d['token']}    (save it now — will not be shown again)")
    print(f"Prefix:    {d['hash_prefix']}")
    if d.get("expires_at"):
        print(f"Expires:   {d['expires_at']}")
    return 0


def _auth_revoke_token(args: argparse.Namespace) -> int:
    from .client import Client

    with Client(args.server) as c:
        r = c._client.delete(f"/api/v1/auth/tokens/{args.token_id}")
        if r.status_code >= 400:
            print(f"HTTP {r.status_code}: {r.text}", file=sys.stderr)
            return 1
    print(f"revoked {args.token_id}")
    return 0


def _auth_logout(args: argparse.Namespace) -> int:
    from .client import Client

    with Client(args.server) as c:
        r = c._client.post("/api/v1/auth/logout")
    print("logged out" if r.status_code == 200 else f"HTTP {r.status_code}")
    return 0 if r.status_code == 200 else 1


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="opencraig", description="OpenCraig CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    # serve
    s = sub.add_parser("serve", help="Run the FastAPI server")
    s.add_argument("--config", default=None)
    s.add_argument("--host", default=None)
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--reload", action="store_true")
    s.add_argument("--workers", type=int, default=None)
    s.add_argument("--log-level", default=None)
    s.set_defaults(func=_cmd_serve)

    # ask
    a = sub.add_parser("ask", help="Send a query to a running server")
    a.add_argument("query")
    a.add_argument("--server", default="http://localhost:8000")
    a.add_argument("--path", default=None, help="Scope to folder path (path_filter)")
    a.add_argument("--stream", action="store_true")
    a.add_argument("--no-kg", action="store_true", help="Disable KG path")
    a.add_argument("--no-tree", action="store_true", help="Disable tree navigation")
    a.add_argument("--no-qu", action="store_true", help="Disable query understanding")
    a.add_argument("--no-rerank", action="store_true", help="Disable reranker")
    a.set_defaults(func=_cmd_ask)

    # ingest
    i = sub.add_parser("ingest", help="Upload + queue a file for ingestion")
    i.add_argument("file")
    i.add_argument("--server", default="http://localhost:8000")
    i.add_argument("--folder", default="/")
    i.add_argument("--doc-id", default=None)
    i.set_defaults(func=_cmd_ingest)

    # health
    h = sub.add_parser("health", help="Probe server /health")
    h.add_argument("--server", default="http://localhost:8000")
    h.set_defaults(func=_cmd_health)

    # version
    v = sub.add_parser("version", help="Print OpenCraig version")
    v.set_defaults(func=_cmd_version)

    # auth
    au = sub.add_parser("auth", help="Auth utilities")
    au_sub = au.add_subparsers(dest="auth_action", required=True)

    rp = au_sub.add_parser("reset-password", help="Reset a user's password (direct DB)")
    rp.add_argument("username", nargs="?", default="admin")
    rp.add_argument("--new-password", default=None, help="New password; prompt if omitted")
    rp.add_argument("--config", default=None, help="yaml path (for DB connection)")

    wh = au_sub.add_parser("whoami", help="GET /auth/me using $OPENCRAIG_API_TOKEN")
    wh.add_argument("--server", default="http://localhost:8000")

    lt = au_sub.add_parser("list-tokens", help="List SKs owned by current user")
    lt.add_argument("--server", default="http://localhost:8000")

    ct = au_sub.add_parser("create-token", help="Create a new SK")
    ct.add_argument("name")
    ct.add_argument("--expires-days", type=int, default=None)
    ct.add_argument("--server", default="http://localhost:8000")

    rv = au_sub.add_parser("revoke-token", help="Revoke an SK by token_id")
    rv.add_argument("token_id")
    rv.add_argument("--server", default="http://localhost:8000")

    lo = au_sub.add_parser("logout", help="Revoke current session cookie")
    lo.add_argument("--server", default="http://localhost:8000")

    au.set_defaults(func=_cmd_auth)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
