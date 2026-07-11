"""Multi-account (multi-tenant) support.

Each account ("tenant") lives under tenants/<slug>/ with its own brand, themes,
logo, Facebook credentials, and data (queues + cards):

    tenants/<slug>/
        account.json    name, website, accent colors, fb_page_id, fb_token
        brand.json      the brand-facts dict (see content.DEFAULT_BRAND shape)
        themes.json     content themes with per-theme deep links
        logo_full.png   brand logo (and optional logo_mark.png)
        data/           pending.json approved.json history.json settings.json cards/

The "current" account is a thread-local, so web requests (one per account, set
from the session) and the background scheduler (which loops every account) never
step on each other. Anthropic + Pexels keys stay global (in .env).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
TENANTS_DIR = _ROOT / "tenants"
_local = threading.local()

# Fallback brand accent (SkySystems green / blue) if an account omits them.
_DEFAULT_ACCENT = "#2ecc71"
_DEFAULT_ACCENT2 = "#2b6cc4"


# --- json helpers ----------------------------------------------------------

def _read_json(path: Path, default):
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "account"


# --- listing / current -----------------------------------------------------

def list_tenants() -> list[dict[str, str]]:
    """[{slug, name}] for every account, sorted by name."""
    out = []
    if TENANTS_DIR.exists():
        for d in TENANTS_DIR.iterdir():
            if d.is_dir() and (d / "account.json").exists():
                acct = _read_json(d / "account.json", {})
                out.append({"slug": d.name, "name": acct.get("name", d.name)})
    return sorted(out, key=lambda t: t["name"].lower())


def exists(slug: str) -> bool:
    return (TENANTS_DIR / slug / "account.json").exists()


def set_current(slug: str) -> None:
    _local.slug = slug


def current() -> str:
    slug = getattr(_local, "slug", None)
    if slug and exists(slug):
        return slug
    ts = list_tenants()
    return ts[0]["slug"] if ts else "default"


# --- per-account paths + config -------------------------------------------

def tenant_dir(slug: str | None = None) -> Path:
    return TENANTS_DIR / (slug or current())


def data_dir(slug: str | None = None) -> Path:
    return tenant_dir(slug) / "data"


def cards_dir(slug: str | None = None) -> Path:
    return data_dir(slug) / "cards"


def logo_full(slug: str | None = None) -> Path:
    return tenant_dir(slug) / "logo_full.png"


def logo_mark(slug: str | None = None) -> Path:
    p = tenant_dir(slug) / "logo_mark.png"
    return p if p.exists() else logo_full(slug)


def account(slug: str | None = None) -> dict[str, Any]:
    return _read_json(tenant_dir(slug) / "account.json", {})


def save_account(data: dict, slug: str | None = None) -> None:
    _write_json(tenant_dir(slug) / "account.json", data)


def brand(slug: str | None = None) -> dict[str, Any]:
    return _read_json(tenant_dir(slug) / "brand.json", {})


def save_brand(data: dict, slug: str | None = None) -> None:
    _write_json(tenant_dir(slug) / "brand.json", data)


def themes(slug: str | None = None) -> list[dict[str, Any]]:
    return _read_json(tenant_dir(slug) / "themes.json", [])


def save_themes(data: list, slug: str | None = None) -> None:
    _write_json(tenant_dir(slug) / "themes.json", data)


def website(slug: str | None = None) -> str:
    return account(slug).get("website", "").strip()


def style(slug: str | None = None) -> str:
    """Card visual style: 'bright' (light/clean, default for new accounts) or
    'dark' (navy premium). Missing -> 'dark' to preserve the original look."""
    return (account(slug).get("style") or "dark").lower()


def domain(slug: str | None = None) -> str:
    """Bare domain for the card footer, e.g. 'skyusa.us'."""
    w = website(slug)
    w = re.sub(r"^https?://", "", w).rstrip("/")
    w = re.sub(r"^www\.", "", w)
    return w or "your-site.com"


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = (h or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except (ValueError, IndexError):
        return (46, 204, 113)


def accent_colors(slug: str | None = None) -> list[tuple[int, int, int]]:
    a = account(slug)
    return [
        _hex_to_rgb(a.get("accent", _DEFAULT_ACCENT)),
        _hex_to_rgb(a.get("accent2", _DEFAULT_ACCENT2)),
    ]


def fb_creds(slug: str | None = None) -> tuple[str, str]:
    a = account(slug)
    return a.get("fb_page_id", ""), a.get("fb_token", "")


# --- create / seed ---------------------------------------------------------

def create_tenant(slug, name, website, brand_dict, themes_list, *,
                  fb_page_id="", fb_token="", accent=_DEFAULT_ACCENT,
                  accent2=_DEFAULT_ACCENT2, style="dark", logo_bytes=None,
                  mark_bytes=None, seed_data_from: Path | None = None) -> str:
    """Create a new account. Returns its slug."""
    slug = slugify(slug or name)
    tdir = TENANTS_DIR / slug
    (tdir / "data" / "cards").mkdir(parents=True, exist_ok=True)
    # If this account already exists (e.g. re-adding to refresh brand/themes),
    # keep its saved Facebook credentials when the caller passes blanks, so a
    # rebuild never wipes a token the user already connected.
    prev = _read_json(tdir / "account.json", {})
    save_account({
        "name": name, "website": website,
        "fb_page_id": fb_page_id or prev.get("fb_page_id", ""),
        "fb_token": fb_token or prev.get("fb_token", ""),
        "accent": accent, "accent2": accent2,
        "style": style,
    }, slug)
    save_brand(brand_dict or {}, slug)
    save_themes(themes_list or [], slug)
    if logo_bytes:
        (tdir / "logo_full.png").write_bytes(logo_bytes)
    if mark_bytes:
        (tdir / "logo_mark.png").write_bytes(mark_bytes)
    # Seed empty queues if not seeding from an existing folder.
    for q in ("pending.json", "approved.json", "history.json"):
        p = tdir / "data" / q
        if not p.exists():
            _write_json(p, [])
    return slug
