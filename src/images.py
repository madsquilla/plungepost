"""Fetch a topic-relevant stock photo from Pexels (free API).

Public API:
    fetch_stock_photo(query, out_path) -> Path | None

Reads PEXELS_API_KEY from the environment. If the key is missing, the request
fails, or there are no results, returns None so the caller can fall back to a
text-only branded card. Never raises for an ordinary "no photo" outcome.
"""

from __future__ import annotations

import logging
import os
import random
from pathlib import Path

import requests

logger = logging.getLogger("skysystems.images")

PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"
TIMEOUT = 30


def fetch_stock_photo(query: str, out_path: str | Path) -> Path | None:
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        logger.info("PEXELS_API_KEY not set; will use a text-only card.")
        return None

    query = (query or "").strip() or "cybersecurity technology"
    try:
        resp = requests.get(
            PEXELS_SEARCH_URL,
            headers={"Authorization": key},
            params={
                "query": query,
                "orientation": "landscape",
                "size": "large",
                "per_page": 15,
            },
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("Pexels request error for '%s': %s", query, exc)
        return None

    if resp.status_code == 401:
        logger.warning("Pexels rejected the API key (401). Check PEXELS_API_KEY.")
        return None
    if resp.status_code != 200:
        logger.warning("Pexels search failed (%s): %s", resp.status_code, resp.text[:200])
        return None

    photos = resp.json().get("photos", [])
    if not photos:
        logger.info("No Pexels results for '%s'.", query)
        return None

    # Random among the top results -> varied imagery across posts on a theme.
    photo = random.choice(photos[: min(10, len(photos))])
    src = photo.get("src", {})
    img_url = src.get("large2x") or src.get("large") or src.get("original")
    if not img_url:
        return None

    try:
        img_resp = requests.get(img_url, timeout=TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("Failed to download Pexels image: %s", exc)
        return None
    if img_resp.status_code != 200:
        logger.warning("Pexels image download failed (%s).", img_resp.status_code)
        return None

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(img_resp.content)
    logger.info(
        "Stock photo for '%s' by %s (Pexels).", query, photo.get("photographer", "?")
    )
    return out_path
