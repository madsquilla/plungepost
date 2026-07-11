"""Read/write the pending / approved / history JSON queues.

Each queue is a JSON array of "item" dicts. An item looks like:

    {
        "id": "20260611-153012-threat-of-the-week",
        "generated_at": "2026-06-11T15:30:12+00:00",
        "theme": "threat-of-the-week",
        "post_text": "....",
        "suggested_image_concept": "....",
        "status": "pending" | "approved" | "posted",
        "link": "https://skyusa.us",          # optional
        "posted_at": "....",                   # set when published
        "facebook_post_id": "...."             # set when published
    }

Files are read defensively (a missing or empty file is treated as an empty
list) and written atomically (write to a temp file, then replace) so an
interrupted run cannot corrupt a queue.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import tenants


def _p(name: str) -> Path:
    """Path to a queue/settings file in the CURRENT account's data dir."""
    return tenants.data_dir() / name


DEDUP_WINDOW_DAYS = 30

_DEFAULT_SETTINGS = {
    "auto_pilot_enabled": False,
    "auto_pilot_days": ["mon", "tue", "wed", "thu", "fri"],  # which weekdays
    "auto_pilot_times": ["09:00"],     # one or more local HH:MM slots per day
    "fired_slots": [],                 # "YYYY-MM-DD HH:MM" slots already posted
}


def read_settings() -> dict[str, Any]:
    settings = dict(_DEFAULT_SETTINGS)
    if _p("settings.json").exists():
        text = _p("settings.json").read_text(encoding="utf-8").strip()
        if text:
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    settings.update(data)
            except json.JSONDecodeError:
                pass
    return settings


def write_settings(settings: dict[str, Any]) -> None:
    _p("settings.json").parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(_p("settings.json").parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, _p("settings.json"))
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read(path: Path) -> list[dict[str, Any]]:
    """Read a JSON array, tolerating a missing or empty file."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array in {path}, got {type(data).__name__}")
    return data


def _write(path: Path, items: list[dict[str, Any]]) -> None:
    """Atomically write a JSON array to path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(items, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        # Clean up the temp file if the replace never happened.
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


# --- Public read helpers ---------------------------------------------------

def read_pending() -> list[dict[str, Any]]:
    return _read(_p("pending.json"))


def read_approved() -> list[dict[str, Any]]:
    return _read(_p("approved.json"))


def read_history() -> list[dict[str, Any]]:
    return _read(_p("history.json"))


# --- Public write helpers --------------------------------------------------

def append_pending(item: dict[str, Any]) -> None:
    items = read_pending()
    items.append(item)
    _write(_p("pending.json"), items)


def write_pending(items: list[dict[str, Any]]) -> None:
    _write(_p("pending.json"), items)


def write_approved(items: list[dict[str, Any]]) -> None:
    _write(_p("approved.json"), items)


def append_history(item: dict[str, Any]) -> None:
    items = read_history()
    items.append(item)
    _write(_p("history.json"), items)


# --- Dedup support ---------------------------------------------------------

def recent_topics(window_days: int = DEDUP_WINDOW_DAYS) -> list[dict[str, str]]:
    """Return {theme, post_text} for items posted within the last window_days.

    Pulls from history (already posted) plus anything still staged in pending
    or approved, so the generator never repeats a theme that is queued up but
    not yet live.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    recent: list[dict[str, str]] = []

    def _consider(items: list[dict[str, Any]], date_key: str) -> None:
        for item in items:
            stamp = item.get(date_key) or item.get("generated_at")
            keep = True
            if stamp:
                try:
                    when = datetime.fromisoformat(stamp)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    keep = when >= cutoff
                except ValueError:
                    keep = True  # unparseable date -> keep it, be conservative
            if keep:
                recent.append(
                    {
                        "theme": item.get("theme", ""),
                        "format": item.get("format", ""),
                        "post_text": item.get("post_text", ""),
                    }
                )

    _consider(read_history(), "posted_at")
    _consider(read_pending(), "generated_at")
    _consider(read_approved(), "generated_at")
    return recent
