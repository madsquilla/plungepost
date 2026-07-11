"""Publish a post to the SkySystems USA Facebook Page via the Graph API.

Public API:
    publish_post(item) -> facebook_post_id

Reads META_PAGE_ID and META_PAGE_ACCESS_TOKEN from the environment. The token
must be a long-lived Page access token (generated separately; see README).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import requests

import tenants

_REPO_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger("skysystems.publish")

GRAPH_API_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
REQUEST_TIMEOUT = 30  # seconds


class PublishError(RuntimeError):
    """Raised when publishing fails for any reason."""


class TokenExpiredError(PublishError):
    """Raised specifically when the Page access token is invalid/expired."""


def _credentials() -> tuple[str, str]:
    # Per-account Page credentials (from the current tenant's account.json).
    page_id, token = tenants.fb_creds()
    # Fall back to env for a single-account / legacy setup.
    page_id = page_id or os.environ.get("META_PAGE_ID", "")
    token = token or os.environ.get("META_PAGE_ACCESS_TOKEN", "")
    if not page_id or not token:
        raise PublishError(
            "This account has no Facebook Page ID / access token set. Add them "
            "in the account's settings (or META_PAGE_ID / "
            "META_PAGE_ACCESS_TOKEN in the environment) to publish."
        )
    return page_id, token


def _resolve_card(item: dict[str, Any]) -> Path | None:
    """Return the local card image path if it exists, else None."""
    rel = (item.get("card_path") or "").strip()
    if not rel:
        return None
    path = Path(rel)
    if not path.is_absolute():
        path = _REPO_ROOT / rel
    return path if path.exists() else None


def publish_post(item: dict[str, Any]) -> str:
    """Publish the post. Posts a photo (branded card) when item['card_path']
    exists, otherwise a plain text post. Returns the resulting feed post id.
    """
    card = _resolve_card(item)
    if card is not None:
        return _publish_photo(item, card)
    return _publish_text(item)


def _publish_text(item: dict[str, Any]) -> str:
    page_id, token = _credentials()

    # Caption (lead + clickable link + hashtags) is the Facebook message;
    # the post_text body lives on the image itself.
    message = (item.get("caption") or item.get("post_text") or "").strip()
    if not message:
        raise PublishError("Refusing to publish an empty post.")

    url = f"{GRAPH_BASE}/{page_id}/feed"
    payload = {"message": message, "access_token": token}
    link = (item.get("link") or "").strip()
    if link:
        payload["link"] = link

    logger.info("Publishing TEXT post id=%s to Page %s", item.get("id"), page_id)
    try:
        resp = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise PublishError(f"Network error calling Graph API: {exc}") from exc

    data = _handle_response(resp)
    post_id = data.get("id")
    if not post_id:
        raise PublishError(f"Graph API 200 but no post id in response: {data}")
    logger.info("Published successfully. Facebook post id=%s", post_id)
    return post_id


def _publish_photo(item: dict[str, Any], card: Path) -> str:
    """Upload the branded card to /photos with the post text as the caption."""
    page_id, token = _credentials()

    # Caption (lead + clickable link + hashtags) is the Facebook message;
    # the post_text body lives on the image itself.
    message = (item.get("caption") or item.get("post_text") or "").strip()
    if not message:
        raise PublishError("Refusing to publish an empty post.")

    url = f"{GRAPH_BASE}/{page_id}/photos"
    logger.info(
        "Publishing PHOTO post id=%s (card=%s) to Page %s",
        item.get("id"),
        card.name,
        page_id,
    )
    try:
        with open(card, "rb") as fh:
            resp = requests.post(
                url,
                data={"message": message, "access_token": token, "published": "true"},
                files={"source": fh},
                timeout=REQUEST_TIMEOUT,
            )
    except requests.RequestException as exc:
        raise PublishError(f"Network error uploading photo to Graph API: {exc}") from exc

    data = _handle_response(resp)
    # /photos returns {"id": <photo_id>, "post_id": <feed_post_id>}.
    post_id = data.get("post_id") or data.get("id")
    if not post_id:
        raise PublishError(f"Graph API 200 but no post id in response: {data}")
    logger.info("Published photo successfully. Facebook post id=%s", post_id)
    return post_id


def _handle_response(resp: requests.Response) -> dict[str, Any]:
    """Raise a clear error on non-200, else return the parsed JSON body."""
    if resp.status_code != 200:
        detail = _extract_error(resp)
        code = detail.get("code")
        # 190 = invalid/expired access token; OAuthException covers token issues.
        if code == 190 or detail.get("type") == "OAuthException":
            raise TokenExpiredError(
                "Facebook rejected the Page access token (it is likely expired "
                "or revoked). Regenerate a long-lived Page access token and "
                "update META_PAGE_ACCESS_TOKEN. Graph said: "
                f"{detail.get('message', resp.text)}"
            )
        raise PublishError(
            f"Graph API returned HTTP {resp.status_code}: "
            f"{detail.get('message', resp.text)}"
        )
    return resp.json()


def _extract_error(resp: requests.Response) -> dict[str, Any]:
    """Pull the {error: {...}} envelope out of a Graph error response."""
    try:
        body = resp.json()
    except ValueError:
        return {"message": resp.text}
    error = body.get("error")
    if isinstance(error, dict):
        return error
    return {"message": resp.text}
