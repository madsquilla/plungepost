"""PlungePost PWA -- the installable phone app.

A mobile-first surface over the same engine the dashboard uses: paste a
website address, get a finished on-brand Facebook post (graphic + text) you
can share straight into the Facebook app or Meta Business Suite.

Mounted on the dashboard app as a blueprint (see webapp.py), so both share
one library of accounts and posts.

Routes:
    /app                      the app shell (installable, works offline)
    /manifest.webmanifest     PWA manifest
    /sw.js                    service worker (root scope)
    /icons/<name>.png         app icons, resized from assets/icon.png
    /media/<slug>/<name>.png  a post graphic, by account
    /api/*                    JSON the shell runs on
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Blueprint, Response, abort, jsonify, request, send_file,
    send_from_directory,
)

import quick
import store
import tenants

logger = logging.getLogger("plungepost.pwa")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WEB_DIR = _REPO_ROOT / "web"
_ASSETS_DIR = _REPO_ROOT / "assets"

bp = Blueprint("pwa", __name__)

APP_NAME = "PlungePost"
THEME_COLOR = "#0e2740"
ACCENT_COLOR = "#2faa46"

# Icon sizes the manifest asks for, plus the iOS home-screen icon.
_ICON_SIZES = {"icon-192.png": 192, "icon-512.png": 512,
               "icon-maskable-512.png": 512, "apple-touch-icon.png": 180}
_MASKABLE = {"icon-maskable-512.png"}
_icon_cache: dict[str, bytes] = {}


def _asset_version() -> str:
    """Short hash of the shell + worker, so a redeploy busts the SW cache."""
    h = hashlib.sha256()
    for name in ("app.html", "sw.js"):
        path = _WEB_DIR / name
        h.update(path.read_bytes() if path.exists() else b"")
    return h.hexdigest()[:12]


def _no_store(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


# --- shell, manifest, worker, icons ---------------------------------------

@bp.route("/app")
def app_shell():
    """The shell is static (all data arrives over /api), so the service worker
    can cache it wholesale and the app opens instantly and offline."""
    resp = send_from_directory(_WEB_DIR, "app.html", mimetype="text/html")
    return _no_store(resp)


@bp.route("/manifest.webmanifest")
def manifest():
    data = {
        "name": f"{APP_NAME} -- website to Facebook post",
        "short_name": APP_NAME,
        "description": "Paste a website address and get an on-brand Facebook "
                       "post graphic and caption.",
        "start_url": "/app",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": THEME_COLOR,
        "theme_color": THEME_COLOR,
        "categories": ["business", "productivity"],
        "icons": [
            {"src": "/icons/icon-192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "any"},
            {"src": "/icons/icon-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "any"},
            {"src": "/icons/icon-maskable-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
        # Lets the phone's share sheet send a URL straight into the app.
        "share_target": {
            "action": "/app",
            "method": "GET",
            "params": {"url": "url", "text": "text", "title": "title"},
        },
    }
    return Response(json.dumps(data, indent=2),
                    mimetype="application/manifest+json")


@bp.route("/sw.js")
def service_worker():
    src = (_WEB_DIR / "sw.js").read_text(encoding="utf-8")
    resp = Response(src.replace("%CACHE_VERSION%", _asset_version()),
                    mimetype="application/javascript")
    # Served from the root so the worker can control the whole origin.
    resp.headers["Service-Worker-Allowed"] = "/"
    return _no_store(resp)


@bp.route("/icons/<name>")
def icon(name):
    if name not in _ICON_SIZES:
        abort(404)
    if name not in _icon_cache:
        _icon_cache[name] = _render_icon(name)
    return send_file(io.BytesIO(_icon_cache[name]), mimetype="image/png",
                     max_age=86400)


def _render_icon(name: str) -> bytes:
    from PIL import Image

    size = _ICON_SIZES[name]
    src = Image.open(_ASSETS_DIR / "icon.png").convert("RGBA")
    if name in _MASKABLE:
        # Maskable icons get cropped to a circle by the launcher, so inset the
        # mark and paint the brand background out to the edges.
        pad = int(size * 0.14)
        inner = src.resize((size - pad * 2, size - pad * 2), Image.LANCZOS)
        canvas = Image.new("RGBA", (size, size), THEME_COLOR)
        canvas.alpha_composite(inner, (pad, pad))
    else:
        canvas = src.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    canvas.save(buf, "PNG")
    return buf.getvalue()


@bp.route("/media/<slug>/<name>")
def media(slug, name):
    """A post graphic, addressed by account -- unlike the dashboard's /card,
    this does not depend on which account the session happens to be on."""
    if not name.endswith(".png") or not tenants.exists(slug):
        abort(404)
    return send_from_directory(tenants.cards_dir(slug), name, max_age=31536000)


# --- api -------------------------------------------------------------------

@bp.route("/api/accounts")
def api_accounts():
    """Sites we already know, so returning users tap instead of typing."""
    out = []
    for t in tenants.list_tenants():
        acct = tenants.account(t["slug"])
        out.append({"slug": t["slug"], "name": acct.get("name", t["slug"]),
                    "website": acct.get("website", ""),
                    "accent": acct.get("accent", ACCENT_COLOR)})
    return jsonify({"accounts": out})


@bp.route("/api/quick", methods=["POST"])
def api_quick():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    topic = (body.get("topic") or "").strip()
    if not url or "." not in url:
        return jsonify({"error": "Enter a website address, like acme.com."}), 400
    return jsonify({"job": quick.start(url, topic)})


@bp.route("/api/quick/<job_id>")
def api_quick_status(job_id):
    job = quick.get(job_id)
    if job is None:
        return jsonify({"error": "That job has expired. Make a new post."}), 404
    return _no_store(jsonify(job))


@bp.route("/api/library")
def api_library():
    """Posts waiting in review, newest first, across every account."""
    items = []
    for t in tenants.list_tenants():
        slug = t["slug"]
        tenants.set_current(slug)
        try:
            pending = store.read_pending()
        except Exception:  # noqa: BLE001 -- one bad account must not blank the list
            logger.exception("Could not read the queue for %s", slug)
            continue
        for item in pending:
            items.append(quick.describe(item, slug))
    items.reverse()
    return _no_store(jsonify({"posts": items[:40]}))


@bp.route("/api/posted/<slug>/<item_id>", methods=["POST"])
def api_posted(slug, item_id):
    """Once you've posted it yourself, file it under history."""
    if not tenants.exists(slug):
        abort(404)
    tenants.set_current(slug)
    with store.LOCK:
        pending = store.read_pending()
        item = next((i for i in pending if i.get("id") == item_id), None)
        if item is None:
            return jsonify({"error": "That post is no longer in review."}), 404
        store.write_pending([i for i in pending if i.get("id") != item_id])
        item["status"] = "posted"
        item["posted_at"] = datetime.now(timezone.utc).isoformat()
        store.append_history(item)
    return jsonify({"ok": True})


@bp.route("/api/discard/<slug>/<item_id>", methods=["POST"])
def api_discard(slug, item_id):
    if not tenants.exists(slug):
        abort(404)
    tenants.set_current(slug)
    with store.LOCK:
        pending = store.read_pending()
        if not any(i.get("id") == item_id for i in pending):
            return jsonify({"error": "That post is already gone."}), 404
        store.write_pending([i for i in pending if i.get("id") != item_id])
    return jsonify({"ok": True})
