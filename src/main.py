"""Entry point: argument parsing, logging setup, and mode dispatch.

Run-once-and-exit. Designed to be triggered on a schedule (e.g. Unraid User
Scripts) once per day.

Modes:
    stage             (default) generate tomorrow's post -> pending.json
    publish-approved  publish the oldest approved item -> history.json
    generate-batch    generate N posts at once -> pending.json

Flags:
    --dry-run         generate + print + write to a local file, never call Meta
    --count N         number of posts for generate-batch (default 5)

Exit codes:
    0  success
    1  a recoverable/operational error (API failure, nothing to publish, etc.)
    2  bad usage / configuration
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Allow running both as `python src/main.py` and `python -m main` from src/.
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional at runtime
    load_dotenv = None

import cards
import generate as gen
import publish as pub
import store

logger = logging.getLogger("skysystems")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = _REPO_ROOT / "logs"
_DRYRUN_DIR = _REPO_ROOT / "data" / "dry_runs"
_CARDS_DIR = _REPO_ROOT / "data" / "cards"


def _force_utf8_stdio() -> None:
    """Ensure stdout/stderr use UTF-8 so emojis in posts never crash a run.

    Linux containers are already UTF-8; Windows consoles default to cp1252 and
    would raise UnicodeEncodeError when printing a post that contains an emoji.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def setup_logging() -> None:
    """Log to stdout (for container logs) and a rotating local file."""
    _force_utf8_stdio()
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    root = logging.getLogger("skysystems")
    root.setLevel(logging.INFO)
    root.handlers.clear()

    stdout = logging.StreamHandler(stream=sys.stdout)
    stdout.setFormatter(fmt)
    root.addHandler(stdout)

    file_handler = RotatingFileHandler(
        _LOG_DIR / "poster.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def _build_card(item: dict) -> None:
    """Fetch a topic photo and render the branded card (shared with the web UI)."""
    cards.build_card(item)


def _print_post(item: dict) -> None:
    bar = "=" * 70
    print(bar)
    print(f"THEME : {item.get('theme')}")
    print(f"ID    : {item.get('id')}")
    print(f"CARD  : {item.get('card_path') or '(none)'}  [{item.get('image_style', '?')}]")
    print(f"KICKER: {item.get('image_kicker')}")
    print(bar)
    print("ON-IMAGE TEXT:")
    print(item.get("post_text", ""))
    print("-" * 70)
    print("CAPTION (shown above the image, has the link + hashtags):")
    print(item.get("caption", ""))
    print(bar)


def _write_dry_run(items: list[dict]) -> Path:
    _DRYRUN_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = _DRYRUN_DIR / f"dry-run-{stamp}.json"
    path.write_text(
        json.dumps(items, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


# --- Mode handlers ---------------------------------------------------------

def mode_stage(dry_run: bool) -> int:
    """Generate one post. Dry-run prints + files it; otherwise stage to pending."""
    item = gen.generate_post()
    _build_card(item)
    _print_post(item)

    if dry_run:
        path = _write_dry_run([item])
        logger.info("DRY RUN: wrote generated post to %s (nothing staged)", path)
        return 0

    store.append_pending(item)
    logger.info("Staged post id=%s to pending.json for your review.", item["id"])
    return 0


def mode_generate_batch(count: int, dry_run: bool) -> int:
    items = gen.generate_batch(count)
    for item in items:
        _build_card(item)
        _print_post(item)

    if dry_run:
        path = _write_dry_run(items)
        logger.info(
            "DRY RUN: wrote %d generated posts to %s (nothing staged)",
            len(items),
            path,
        )
        return 0

    for item in items:
        store.append_pending(item)
    logger.info("Staged %d posts to pending.json for your review.", len(items))
    return 0


def mode_publish_approved(dry_run: bool) -> int:
    approved = store.read_approved()
    # Oldest unposted approved item first.
    queue = [i for i in approved if i.get("status") != "posted"]
    if not queue:
        logger.error("No approved, unposted items in approved.json. Nothing to do.")
        return 1

    queue.sort(key=lambda i: i.get("generated_at", ""))
    item = queue[0]
    _print_post(item)

    if dry_run:
        path = _write_dry_run([item])
        logger.info(
            "DRY RUN: would publish post id=%s. Wrote it to %s. Meta NOT called.",
            item["id"],
            path,
        )
        return 0

    post_id = pub.publish_post(item)

    # Record success in history, then remove the item from approved.json.
    item = dict(item)
    item["status"] = "posted"
    item["posted_at"] = datetime.now(timezone.utc).isoformat()
    item["facebook_post_id"] = post_id
    store.append_history(item)

    remaining = [i for i in approved if i.get("id") != item.get("id")]
    store.write_approved(remaining)

    logger.info(
        "Published post id=%s (facebook id=%s) and moved it to history.json.",
        item["id"],
        post_id,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skysystems-poster",
        description="Generate and publish SkySystems USA Facebook posts.",
    )
    parser.add_argument(
        "--mode",
        choices=["stage", "publish-approved", "generate-batch"],
        default="stage",
        help="Operation mode (default: stage).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate and print/file the post but never call the Facebook API.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=5,
        help="Number of posts to generate in generate-batch mode (default: 5).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Load .env if present (no-op in container where env is injected directly).
    if load_dotenv is not None:
        load_dotenv(_REPO_ROOT / ".env")

    setup_logging()
    logger.info(
        "Starting run: mode=%s dry_run=%s count=%s", args.mode, args.dry_run, args.count
    )

    try:
        if args.mode == "stage":
            rc = mode_stage(args.dry_run)
        elif args.mode == "generate-batch":
            rc = mode_generate_batch(args.count, args.dry_run)
        elif args.mode == "publish-approved":
            rc = mode_publish_approved(args.dry_run)
        else:  # pragma: no cover -- argparse restricts choices
            logger.error("Unknown mode: %s", args.mode)
            rc = 2
    except gen.GenerationError as exc:
        logger.error("Generation failed: %s", exc)
        rc = 1
    except pub.TokenExpiredError as exc:
        logger.error("ACTION NEEDED -- token problem: %s", exc)
        rc = 1
    except pub.PublishError as exc:
        logger.error("Publishing failed: %s", exc)
        rc = 1
    except Exception as exc:  # noqa: BLE001 -- top-level guard for clean exit code
        logger.exception("Unexpected error: %s", exc)
        rc = 1

    logger.info("Run finished with exit code %d", rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
