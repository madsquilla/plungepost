"""PlungePost Quick -- URL in, finished Facebook post out.

The PWA's engine. Give it a website address and it will, in one background
job: read the site and learn the brand (only the first time for a given
domain), write a post in that brand's voice, and render the branded graphic.

Accounts are keyed by domain, so pasting the same site again reuses the brand
that was already learned instead of relearning it, and the post lands in that
account's normal review queue -- the PWA and the dashboard share one library.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import cards
import generate as gen
import onboard
import publish as pub
import store
import tenants

logger = logging.getLogger("plungepost.quick")

# Jobs live in memory only: a job is a few seconds of work whose result is
# persisted to the account's queue, so losing the job list on restart is fine.
_JOBS: dict[str, dict] = {}
_LOCK = threading.RLock()
_MAX_JOBS = 40


def _host(url: str) -> str:
    host = urlparse(onboard._normalize_url(url)).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def find_account(url: str) -> str | None:
    """The existing account for this website's domain, if we have one."""
    target = _host(url)
    if not target:
        return None
    for t in tenants.list_tenants():
        if _host(tenants.account(t["slug"]).get("website", "")) == target:
            return t["slug"]
    return None


def _set(job_id: str, **fields) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            job.update(fields)


def get(job_id: str) -> dict | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def _trim() -> None:
    """Keep only the most recent jobs so a long-running server can't grow."""
    if len(_JOBS) <= _MAX_JOBS:
        return
    for jid in sorted(_JOBS, key=lambda j: _JOBS[j]["started_at"])[:-_MAX_JOBS]:
        _JOBS.pop(jid, None)


def describe(item: dict, slug: str) -> dict:
    """A queue item flattened into what the PWA needs to render it."""
    acct = tenants.account(slug)
    card = item.get("card_path", "")
    name = card.rsplit("/", 1)[-1] if card else ""
    return {
        "id": item["id"],
        "slug": slug,
        "business": acct.get("name", slug),
        "website": acct.get("website", ""),
        "accent": acct.get("accent", "#2ecc71"),
        "headline": item.get("image_headline", ""),
        "kicker": item.get("image_kicker", ""),
        "theme": item.get("theme", ""),
        "caption": item.get("caption", ""),
        "post_text": item.get("post_text", ""),
        # What actually gets pasted into Facebook: body + link + hashtags.
        "message": pub.compose_message(item),
        "link": item.get("link", ""),
        "card_url": f"/media/{slug}/{name}" if name else "",
    }


def _run(job_id: str, url: str, topic: str) -> None:
    def progress(msg: str) -> None:
        _set(job_id, message=msg)

    try:
        slug = find_account(url)
        if slug:
            progress(f"Using the brand we already learned for {_host(url)}...")
        else:
            progress("Reading the website...")
            # A bare URL is all we need: build_account reads the business name
            # off the site (og:site_name / <title>, else the domain).
            slug = onboard.build_account(website=url, progress=progress)
        # A worker thread starts with no account bound; bind it or generation
        # would silently target whichever account happens to be first.
        tenants.set_current(slug)
        _set(job_id, slug=slug, business=tenants.account(slug).get("name", slug))

        progress("Writing the post...")
        item = (gen.generate_custom(topic) if topic else gen.generate_post())

        progress("Designing the graphic...")
        cards.build_card(item)

        with store.LOCK:
            store.append_pending(item)
        _set(job_id, message="Done", result=describe(item, slug))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Quick post failed for %s", url)
        _set(job_id, error=True, message=friendly_error(exc))
    finally:
        _set(job_id, active=False)


def friendly_error(exc: Exception) -> str:
    """Turn a raw exception into something worth showing on a phone."""
    msg = str(exc)
    low = msg.lower()
    if "credit balance" in low:
        return "Anthropic API is out of credits. Add credits at console.anthropic.com."
    if "authentication" in low or "x-api-key" in low or "401" in low:
        return "Anthropic API key was rejected. Check ANTHROPIC_API_KEY in .env."
    if "anthropic_api_key" in low:
        return "No Anthropic API key is set. Add ANTHROPIC_API_KEY to .env."
    if "no themes" in low or "brand" in low and "missing" in low:
        return "Couldn't read enough from that website to build a brand."
    return msg[:180] if msg else "Something went wrong."


def start(url: str, topic: str = "") -> str:
    """Kick off a quick post and return its job id."""
    job_id = uuid.uuid4().hex[:12]
    with _LOCK:
        _JOBS[job_id] = {
            "id": job_id, "active": True, "error": False,
            "message": "Starting...", "url": url, "topic": topic,
            "slug": "", "business": "", "result": None,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        _trim()
    threading.Thread(target=_run, args=(job_id, url, topic), daemon=True).start()
    return job_id
