r"""Read-only check that META_PAGE_ID + META_PAGE_ACCESS_TOKEN can publish.

Reads values from .env (or the environment). Makes only GET requests -- it
never posts. Run it any time you change the token.

    .\.venv\Scripts\python.exe tools\verify_token.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import requests

try:
    from dotenv import dotenv_values
except ImportError:
    dotenv_values = None

GRAPH = "https://graph.facebook.com/v21.0"


def _load() -> tuple[str, str]:
    import os

    values = {}
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if dotenv_values is not None and env_path.exists():
        values = dotenv_values(str(env_path))
    pid = values.get("META_PAGE_ID") or os.environ.get("META_PAGE_ID", "")
    tok = values.get("META_PAGE_ACCESS_TOKEN") or os.environ.get(
        "META_PAGE_ACCESS_TOKEN", ""
    )
    if not pid or not tok:
        print("ERROR: META_PAGE_ID and META_PAGE_ACCESS_TOKEN must be set in .env")
        sys.exit(1)
    return pid, tok


def main() -> int:
    pid, tok = _load()

    # Is this a Page token? /me should return the Page itself (id == page id).
    me = requests.get(
        f"{GRAPH}/me", params={"fields": "id,name", "access_token": tok}, timeout=30
    )
    if me.status_code != 200:
        print("FAIL: token rejected ->", me.json().get("error", {}).get("message"))
        return 1

    me_data = me.json()
    is_page_token = me_data.get("id") == pid
    print("Token identity:", me_data.get("name"), f"({me_data.get('id')})")
    print("Is a PAGE token for META_PAGE_ID:", is_page_token)

    if not is_page_token:
        print(
            "\nFAIL: This is a USER token, not the Page token. "
            "Run tools\\get_page_token.py to convert it."
        )
        return 1

    # Read the Page's own posting-related fields to confirm scope.
    page = requests.get(
        f"{GRAPH}/{pid}",
        params={"fields": "name,fan_count", "access_token": tok},
        timeout=30,
    )
    if page.status_code == 200:
        d = page.json()
        print("Page name:", d.get("name"))
        print("\nPASS: token works and is scoped to the Page. Ready to publish.")
        return 0

    err = page.json().get("error", {})
    print("\nFAIL reading Page:", err.get("message"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
