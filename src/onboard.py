"""Auto-onboard a new account by reading its website.

Given a business name + website (+ optional Facebook Page creds), this:
  1. Scrapes the homepage and a few key internal pages (about / services / etc).
  2. Asks the model to build a brand-facts dict (content.DEFAULT_BRAND shape)
     and a set of content themes with deep links to real pages on the site.
  3. Finds the site's logo (or renders a clean wordmark as a fallback).
  4. Creates the tenant via tenants.create_tenant().

Public API:
    build_account(name, website, fb_page_id="", fb_token="",
                  accent="#2ecc71", accent2="#2b6cc4", progress=None) -> slug
"""

from __future__ import annotations

import json
import logging
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import anthropic
import requests

import tenants

logger = logging.getLogger("skysystems.onboard")

DEFAULT_MODEL = "claude-opus-4-8"
MAX_TOKENS = 4096
REQUEST_TIMEOUT = 20
_UA = {"User-Agent": "Mozilla/5.0 (compatible; SkySystemsOnboard/1.0)"}

# Internal-link keywords worth reading beyond the homepage.
_KEY_PAGES = (
    "about", "service", "solution", "product", "industr", "who-we-serve",
    "what-we-do", "capabilities", "pricing", "team", "company",
)


def _noop(_msg: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    """Collect visible text + internal links + candidate logo image URLs."""

    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self.chunks: list[str] = []
        self.links: list[tuple[str, str]] = []   # (href, anchor text)
        self.logos: list[str] = []
        self._cur_href: str | None = None
        self._cur_anchor: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
        if tag == "a" and a.get("href"):
            self._cur_href = a["href"]
            self._cur_anchor = []
        if tag == "img":
            src = a.get("src") or ""
            hint = (a.get("alt", "") + " " + src + " " + a.get("class", "")).lower()
            if "logo" in hint and src:
                self.logos.append(src)
        if tag == "meta" and a.get("property") == "og:image" and a.get("content"):
            self.logos.append(a["content"])

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1
        if tag == "a" and self._cur_href is not None:
            self.links.append((self._cur_href, " ".join(self._cur_anchor).strip()))
            self._cur_href = None

    def handle_data(self, data):
        if self._skip:
            return
        text = data.strip()
        if text:
            self.chunks.append(text)
            if self._cur_href is not None:
                self._cur_anchor.append(text)


def _fetch(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=_UA, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", ""):
            return resp.text
    except requests.RequestException as exc:
        logger.info("Fetch failed for %s: %s", url, exc)
    return None


def _normalize_url(website: str) -> str:
    website = (website or "").strip()
    if not re.match(r"^https?://", website):
        website = "https://" + website
    return website.rstrip("/")


def scrape_site(website: str, progress=_noop) -> dict:
    """Return {text, pages:[{url,text}], links, logos} for the site."""
    base = _normalize_url(website)
    host = urlparse(base).netloc
    progress("Reading the homepage...")
    home_html = _fetch(base) or _fetch(base + "/") or ""
    ex = _TextExtractor()
    ex.feed(home_html)
    pages = [{"url": base, "text": " ".join(ex.chunks)}]
    logos = list(ex.logos)

    # Rank internal links by how well the URL/anchor matches a key-page keyword.
    seen = {base}
    candidates: list[str] = []
    for href, anchor in ex.links:
        full = urljoin(base + "/", href)
        if urlparse(full).netloc != host:
            continue
        full = full.split("#")[0].rstrip("/")
        if full in seen:
            continue
        hint = (full + " " + anchor).lower()
        if any(k in hint for k in _KEY_PAGES):
            seen.add(full)
            candidates.append(full)

    for url in candidates[:4]:
        progress(f"Reading {urlparse(url).path or '/'} ...")
        html = _fetch(url)
        if not html:
            continue
        ex2 = _TextExtractor()
        ex2.feed(html)
        pages.append({"url": url, "text": " ".join(ex2.chunks)})
        logos.extend(ex2.logos)

    return {"base": base, "pages": pages, "logos": logos}


# ---------------------------------------------------------------------------
# Model: build brand + themes from the scraped text
# ---------------------------------------------------------------------------
_SYSTEM = (
    "You are a brand strategist. You are given a business name, its website URL, "
    "the list of real page URLs on the site, and the visible text scraped from "
    "those pages. Produce a JSON object that captures the brand accurately for a "
    "social-media post generator. Use ONLY facts supported by the text; do not "
    "invent services, stats, or claims. If a field is unknown, use an empty "
    "string or empty list. Never use em dashes."
)


def _build_prompt(name: str, base: str, scrape: dict) -> str:
    page_urls = [p["url"] for p in scrape["pages"]]
    corpus = ""
    for p in scrape["pages"]:
        corpus += f"\n\n=== {p['url']} ===\n{p['text'][:3500]}"
    corpus = corpus[:14000]
    return f"""Business name: {name}
Website: {base}
Real page URLs on the site (use these EXACT urls for theme deep links):
{json.dumps(page_urls, indent=2)}

Scraped page text:
{corpus}

Return ONLY a single JSON object, no prose, no code fences, with this shape:
{{
  "brand": {{
    "company": "the legal/display company name",
    "what": "a short phrase: what the business is and where, e.g. 'a residential and commercial cleaning company in Dallas, Texas'",
    "headline": "a short brand headline",
    "positioning": "one sentence positioning statement",
    "website": "{base}",
    "tagline": "a short tagline if the site has one, else ''",
    "differentiator": "what sets them apart, from the site",
    "mission": "their mission if stated, else ''",
    "stats": ["credibility facts actually stated, e.g. '15 years in business'"],
    "service_pillars": ["the real services/offerings, one per item"],
    "verticals": ["the industries/audiences they serve, if any"],
    "compliance": ["any standards/certifications they mention, else omit"],
    "signature_stat": "one memorable stat to use sparingly, else ''",
    "voice_rules": [
      "A short list of voice rules fitting this brand. Always include: 'Never use em dashes.' Keep it professional, human, mobile-friendly."
    ]
  }},
  "themes": [
    {{
      "id": "kebab-case-id",
      "description": "what this recurring post theme is about",
      "angle": "how to write it: the specific angle and tone",
      "vertical": null,
      "link": "one of the EXACT page urls above that best matches this theme (deep link, not just the homepage)"
    }}
  ]
}}

Generate 12 to 18 varied, on-brand themes that cover their real services and audiences. Every theme's link MUST be one of the exact page urls listed above."""


def _parse_json(raw: str) -> dict:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))


def _uses_adaptive_thinking(model: str) -> bool:
    return any(t in model for t in ("opus-4-8", "opus-4-7", "fable-5", "mythos-5"))


def build_brand_and_themes(name: str, base: str, scrape: dict) -> tuple[dict, list]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
    client = anthropic.Anthropic(api_key=api_key)
    req: dict = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": _SYSTEM,
        "messages": [{"role": "user", "content": _build_prompt(name, base, scrape)}],
    }
    if _uses_adaptive_thinking(model):
        req["thinking"] = {"type": "adaptive"}
        req["output_config"] = {"effort": "medium"}
    else:
        req["temperature"] = 0.7
    resp = client.messages.create(**req)
    text = "\n".join(
        b.text for b in resp.content if getattr(b, "type", "") == "text"
    ).strip()
    data = _parse_json(text)
    brand = data.get("brand") or {}
    themes = data.get("themes") or []
    brand.setdefault("website", base)
    # Guarantee the no-em-dash rule is present.
    rules = brand.get("voice_rules") or []
    if not any("em dash" in r.lower() or "em-dash" in r.lower() for r in rules):
        rules.append("Never use em dashes. Use commas, periods, or colons instead.")
    brand["voice_rules"] = rules
    # Only keep themes whose link is a real page on the site.
    valid_urls = {p["url"] for p in scrape["pages"]}
    for t in themes:
        if t.get("link") not in valid_urls:
            t["link"] = base
        t.setdefault("vertical", None)
    return brand, themes


# ---------------------------------------------------------------------------
# Logo: download from the site, else render a wordmark
# ---------------------------------------------------------------------------
def _download_logo(logos: list[str], base: str) -> bytes | None:
    for src in logos:
        url = urljoin(base + "/", src)
        if url.lower().endswith(".svg"):
            continue  # Pillow can't open SVG without extra deps
        try:
            resp = requests.get(url, headers=_UA, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200 and len(resp.content) > 800:
                ctype = resp.headers.get("content-type", "")
                if "image" in ctype and "svg" not in ctype:
                    return resp.content
        except requests.RequestException:
            continue
    return None


def _render_wordmark(name: str, accent: str) -> bytes:
    """Fallback logo: the business name set in the brand accent on transparent."""
    from io import BytesIO

    from PIL import Image, ImageDraw, ImageFont

    def _hex(h):
        h = (h or "#2ecc71").lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

    color = _hex(accent)
    font_dir = Path(__file__).resolve().parent.parent / "assets" / "fonts"
    text = (name or "Brand").strip()
    try:
        font = ImageFont.truetype(str(font_dir / "Rajdhani-Bold.ttf"), 120)
    except Exception:
        font = ImageFont.load_default()
    tmp = Image.new("RGBA", (10, 10))
    d = ImageDraw.Draw(tmp)
    bbox = d.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 30
    img = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((pad - bbox[0], pad - bbox[1]), text, font=font, fill=color)
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def build_account(name: str, website: str, fb_page_id: str = "", fb_token: str = "",
                  accent: str = "#2ecc71", accent2: str = "#2b6cc4",
                  progress=None) -> str:
    progress = progress or _noop
    base = _normalize_url(website)
    scrape = scrape_site(base, progress)

    progress("Studying the brand and writing content themes...")
    brand, themes = build_brand_and_themes(name, base, scrape)

    progress("Fetching the logo...")
    logo_bytes = _download_logo(scrape.get("logos", []), base)
    if not logo_bytes:
        logo_bytes = _render_wordmark(name, accent)

    progress("Creating the account...")
    slug = tenants.create_tenant(
        name, name, base, brand, themes,
        fb_page_id=fb_page_id, fb_token=fb_token,
        accent=accent, accent2=accent2,
        logo_bytes=logo_bytes,
    )
    logger.info("Onboarded new account '%s' (%s) with %d themes",
                name, slug, len(themes))
    return slug
