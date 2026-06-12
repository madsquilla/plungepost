"""Shared card-building: fetch a topic photo and render the branded landscape
card for an item. Used by both the CLI (main.py) and the web dashboard
(webapp.py)."""

from __future__ import annotations

import logging
from pathlib import Path

import images
import imagecard

logger = logging.getLogger("skysystems.cards")

_REPO_ROOT = Path(__file__).resolve().parent.parent
CARDS_DIR = _REPO_ROOT / "data" / "cards"


def build_card(item: dict) -> dict:
    """Render the post's branded card. Sets item['card_path'] + ['image_style'].

    Never raises: a post must still go out even if imagery fails (it falls back
    to a text-only card, or no card at all on hard failure).
    """
    kicker = item.get("image_kicker") or "Austin, Texas"
    query = item.get("image_query") or item.get("theme") or "cybersecurity technology"
    out = CARDS_DIR / f"{item['id']}.png"
    try:
        src_photo = CARDS_DIR / f"_src_{item['id']}.jpg"
        photo = images.fetch_stock_photo(query, src_photo)
        imagecard.render_landscape_card(
            item["post_text"], out, kicker=kicker, photo_path=photo,
            headline=item.get("image_headline", ""),
        )
        item["image_style"] = "landscape-photo" if photo else "landscape"
        if photo is not None:
            try:
                Path(photo).unlink()
            except OSError:
                pass
        item["card_path"] = str(out.relative_to(_REPO_ROOT)).replace("\\", "/")
        logger.info("Built card (%s) -> %s", item["image_style"], item["card_path"])
    except Exception as exc:  # noqa: BLE001 -- imagery must never block a post
        logger.warning("Card rendering failed (%s); post will be text-only.", exc)
        item["card_path"] = ""
        item["image_style"] = "none"
    return item
