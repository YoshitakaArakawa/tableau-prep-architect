"""Shared authentication helper for tableau-prep-architect skills.

Loads .env from the nearest ancestor directory and returns a configured
tableauserverclient Server + Auth pair using Personal Access Token.

Required .env variables: SERVER, SITE_NAME, PAT_NAME, PAT_VALUE

Usage:

    from tableau_auth import sign_in_server
    server, auth = sign_in_server()
    with server.auth.sign_in(auth):
        ...
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import tableauserverclient as TSC
except ImportError:
    sys.exit("ERROR: tableauserverclient is required. Install with: pip install -r requirements.txt")

try:
    from dotenv import load_dotenv
except ImportError:
    sys.exit("ERROR: python-dotenv is required. Install with: pip install -r requirements.txt")


def find_env_file(start: Path | None = None) -> Path | None:
    """Look for .env in start (or cwd) and ancestors (up to 6 levels)."""
    cur = (start or Path.cwd()).resolve()
    for _ in range(6):
        candidate = cur / ".env"
        if candidate.exists():
            return candidate
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def load_credentials() -> dict:
    env_path = find_env_file()
    if env_path:
        load_dotenv(env_path)
        print(f"[auth] Loaded .env from: {env_path}", file=sys.stderr)
    else:
        print("[auth] WARNING: No .env file found. Relying on environment only.", file=sys.stderr)

    creds = {
        "server_url": os.environ.get("SERVER"),
        "site_id":    os.environ.get("SITE_NAME", ""),
        "pat_name":   os.environ.get("PAT_NAME"),
        "pat_secret": os.environ.get("PAT_VALUE"),
    }
    missing = [k for k, v in [
        ("SERVER",    creds["server_url"]),
        ("PAT_NAME",  creds["pat_name"]),
        ("PAT_VALUE", creds["pat_secret"]),
    ] if not v]
    if missing:
        sys.exit(f"ERROR: Missing required env vars: {', '.join(missing)}")
    return creds


def sign_in_server():
    """Return (server, auth) ready for `with server.auth.sign_in(auth):`."""
    creds = load_credentials()
    auth = TSC.PersonalAccessTokenAuth(
        token_name=creds["pat_name"],
        personal_access_token=creds["pat_secret"],
        site_id=creds["site_id"],
    )
    server = TSC.Server(creds["server_url"], use_server_version=True)
    return server, auth
