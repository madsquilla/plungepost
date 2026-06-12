r"""Helper: turn a short-lived USER token into a long-lived PAGE token.

You run this once (whenever the token needs regenerating). It:
  1. Exchanges your short-lived user token for a long-lived user token.
  2. Calls /me/accounts to get the Page token for your Page.
  3. Prints the long-lived Page access token (paste it into .env).

A Page token derived from a long-lived user token does not expire as long as
you stay an admin and the password/app are unchanged -- ideal for a daily job.

USAGE (PowerShell, from the project folder):
    .\.venv\Scripts\python.exe tools\get_page_token.py

It will prompt you for three values (nothing is stored or sent anywhere except
to Facebook's Graph API over HTTPS):
    - App ID        (Meta for Developers -> your app -> Settings -> Basic)
    - App Secret    (same page; click "Show")
    - Short-lived USER token (from Graph API Explorer, see README step)

You can also pass them as flags to avoid the prompts:
    python tools\get_page_token.py --app-id 123 --app-secret abc --user-token EAA...
"""

from __future__ import annotations

import argparse
import getpass
import sys

import requests

GRAPH = "https://graph.facebook.com/v21.0"
TARGET_PAGE_ID = "106257649231904"  # SkySystems USA Corporation
REQUIRED_PERMS = {"pages_manage_posts", "pages_read_engagement", "pages_show_list"}


def _die(msg: str) -> "None":
    print(f"\nERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _read_env() -> dict:
    """Read META_APP_ID / META_APP_SECRET / FB_USER_TOKEN from .env if present.

    dotenv strips surrounding whitespace and quotes, which avoids the most
    common copy-paste failure (a stray trailing space on the App Secret).
    """
    from pathlib import Path

    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return {}
    try:
        from dotenv import dotenv_values
    except ImportError:
        return {}
    return {k: v for k, v in dotenv_values(str(env_path)).items() if v}


def main() -> int:
    ap = argparse.ArgumentParser(description="Get a long-lived Page token.")
    ap.add_argument("--app-id")
    ap.add_argument("--app-secret")
    ap.add_argument("--user-token", help="Short-lived USER token from Graph Explorer")
    ap.add_argument("--page-id", default=TARGET_PAGE_ID)
    args = ap.parse_args()

    # Easiest path: read from .env keys (paste into Notepad, no hidden prompts).
    # Falls back to flags, then interactive prompts.
    env = _read_env()
    app_id = args.app_id or env.get("META_APP_ID") or input("App ID: ").strip()
    app_secret = (
        args.app_secret
        or env.get("META_APP_SECRET")
        or getpass.getpass("App Secret (hidden): ").strip()
    )
    user_token = (
        args.user_token
        or env.get("FB_USER_TOKEN")
        or getpass.getpass("Short-lived USER token (hidden): ").strip()
    )

    if not (app_id and app_secret and user_token):
        _die("All three of App ID, App Secret, and User token are required.")

    # 0) Sanity: check the user token's permissions up front for a clear message.
    perms_resp = requests.get(
        f"{GRAPH}/me/permissions", params={"access_token": user_token}, timeout=30
    )
    if perms_resp.status_code == 200:
        granted = {
            d["permission"]
            for d in perms_resp.json().get("data", [])
            if d.get("status") == "granted"
        }
        missing = REQUIRED_PERMS - granted
        if missing:
            _die(
                "Your user token is missing required permissions: "
                + ", ".join(sorted(missing))
                + ".\nRegenerate it in Graph API Explorer with these added, then "
                "run this again."
            )

    # 1) Exchange short-lived user token -> long-lived user token.
    print("\n[1/2] Exchanging for a long-lived user token...")
    exch = requests.get(
        f"{GRAPH}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": user_token,
        },
        timeout=30,
    )
    if exch.status_code != 200:
        _die(f"Token exchange failed: {exch.json().get('error', exch.text)}")
    long_user_token = exch.json().get("access_token")
    if not long_user_token:
        _die(f"No long-lived user token returned: {exch.json()}")
    print("      OK.")

    # 2) Get the Page token for the target Page using the long-lived user token.
    print("[2/2] Fetching the long-lived Page token from /me/accounts...")
    accounts = requests.get(
        f"{GRAPH}/me/accounts",
        params={"fields": "id,name,access_token", "access_token": long_user_token},
        timeout=30,
    )
    if accounts.status_code != 200:
        _die(f"/me/accounts failed: {accounts.json().get('error', accounts.text)}")

    pages = accounts.json().get("data", [])
    match = next((p for p in pages if p.get("id") == args.page_id), None)
    if not match:
        names = ", ".join(f"{p.get('name')} ({p.get('id')})" for p in pages) or "none"
        _die(
            f"Page id {args.page_id} not found among the Pages you manage: {names}"
        )

    page_token = match.get("access_token")
    if not page_token:
        _die(
            "No access_token returned for the Page. The user token likely lacks "
            "pages_manage_posts / pages_read_engagement."
        )

    print("\n" + "=" * 70)
    print("SUCCESS. Long-lived Page token for:", match.get("name"))
    print("=" * 70)
    print("\nPaste these two lines into your .env (replace the existing values):\n")
    print(f"META_PAGE_ID={match.get('id')}")
    print(f"META_PAGE_ACCESS_TOKEN={page_token}")
    print(
        "\nThen verify with:  .\\.venv\\Scripts\\python.exe tools\\verify_token.py\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
