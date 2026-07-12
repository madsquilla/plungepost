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

logger = logging.getLogger("plungepost.onboard")

DEFAULT_MODEL = "claude-opus-4-8"
MAX_TOKENS = 4096
REQUEST_TIMEOUT = 20
_UA = {"User-Agent": "Mozilla/5.0 (compatible; PlungePostOnboard/1.0)"}

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
        self.logos: list[str] = []               # <img> that look like a logo
        self.icons: list[str] = []               # apple-touch-icon / rel=icon
        self.og_images: list[str] = []           # og:image (often a screenshot)
        self.theme_colors: list[str] = []        # explicit brand color signals
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
        if tag == "link":
            rel = (a.get("rel") or "").lower()
            href = a.get("href") or ""
            # apple-touch-icon and rel=icon are almost always the real brand
            # mark (square), unlike og:image which is usually a hero screenshot.
            if href and ("apple-touch-icon" in rel or "icon" in rel):
                self.icons.append(href)
        if tag == "meta":
            name = (a.get("name") or "").lower()
            if a.get("property") == "og:image" and a.get("content"):
                self.og_images.append(a["content"])
            # <meta name="theme-color"> / msapplication-TileColor are the site's
            # declared brand color -- the strongest single signal.
            if name in ("theme-color", "msapplication-tilecolor") and a.get("content"):
                self.theme_colors.append(a["content"])

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
    icons = list(ex.icons)
    og_images = list(ex.og_images)
    theme_colors = list(ex.theme_colors)
    # Also scan the raw homepage CSS/markup for the most-used saturated hex
    # colors -- a good secondary brand-color signal when there is no meta tag.
    theme_colors += _hex_colors_in(home_html)

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

    return {"base": base, "pages": pages, "logos": logos,
            "icons": icons, "og_images": og_images,
            "theme_colors": theme_colors}


# ---------------------------------------------------------------------------
# Brand-color detection (site theme-color, CSS, and the logo image)
# ---------------------------------------------------------------------------
_HEX_RE = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")


def _to_rgb(h: str):
    h = (h or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return None
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return None


def _rgb_to_hex(rgb) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _saturation_value(rgb):
    import colorsys
    h, s, v = colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
    return h, s, v


def _is_brandable(rgb) -> bool:
    """Skip near-white, near-black, and greys -- not usable as an accent."""
    _, s, v = _saturation_value(rgb)
    return s >= 0.28 and 0.18 <= v <= 0.95


def _hex_colors_in(html: str) -> list[str]:
    """Most-frequent brandable hex colors in a page's raw HTML/CSS, by count."""
    counts: dict[tuple, int] = {}
    for m in _HEX_RE.finditer(html or ""):
        rgb = _to_rgb(m.group(0))
        if rgb and _is_brandable(rgb):
            counts[rgb] = counts.get(rgb, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [_rgb_to_hex(rgb) for rgb, _ in ordered[:8]]


def _dominant_logo_colors(logo_bytes: bytes) -> list[str]:
    """The dominant brandable colors in the logo image, most-common first."""
    from io import BytesIO

    from PIL import Image
    try:
        img = Image.open(BytesIO(logo_bytes)).convert("RGBA")
    except Exception:
        return []
    # Composite over mid-grey so transparent PNGs don't bias toward white/black.
    bg = Image.new("RGBA", img.size, (128, 128, 128, 255))
    img = Image.alpha_composite(bg, img).convert("RGB").resize((96, 96))
    q = img.quantize(colors=16, method=Image.Quantize.FASTOCTREE).convert("RGB")
    counts: dict[tuple, int] = {}
    for px in q.getdata():
        counts[px] = counts.get(px, 0) + 1
    scored = []
    for rgb, n in counts.items():
        if not _is_brandable(rgb):
            continue
        _, s, _v = _saturation_value(rgb)
        scored.append((s * (n ** 0.5), rgb))     # weight vividness x frequency
    scored.sort(reverse=True)
    return [_rgb_to_hex(rgb) for _, rgb in scored[:6]]


def _hue_far(a: str, b: str, min_deg: float = 25.0) -> bool:
    """True if two hex colors differ enough in hue to read as two accents."""
    ra, rb = _to_rgb(a), _to_rgb(b)
    if not ra or not rb:
        return True
    ha = _saturation_value(ra)[0] * 360
    hb = _saturation_value(rb)[0] * 360
    diff = abs(ha - hb) % 360
    return min(diff, 360 - diff) >= min_deg


def detect_accents(scrape: dict, logo_bytes: bytes | None,
                   fallback1: str, fallback2: str) -> tuple[str, str]:
    """Pick two brand accents from the site's declared colors + logo, in order
    of trust: <meta theme-color>, dominant logo colors, then frequent CSS hues.
    Falls back to the provided defaults if the site yields nothing usable."""
    candidates: list[str] = []
    for c in scrape.get("theme_colors", []):
        rgb = _to_rgb(c if c.startswith("#") else "#" + c)
        if rgb and _is_brandable(rgb):
            candidates.append(_rgb_to_hex(rgb))
    if logo_bytes:
        candidates += _dominant_logo_colors(logo_bytes)
    # De-dupe, preserving order.
    seen, ordered = set(), []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    if not ordered:
        return fallback1, fallback2
    accent1 = ordered[0]
    accent2 = next((c for c in ordered[1:] if _hue_far(c, accent1)), None)
    if accent2 is None:
        # Only one distinct hue found: derive a darker companion for accent2.
        r, g, b = _to_rgb(accent1)
        accent2 = _rgb_to_hex((int(r * 0.6), int(g * 0.6), int(b * 0.6)))
    return accent1, accent2


# ---------------------------------------------------------------------------
# Model: build brand + themes from the scraped text
# ---------------------------------------------------------------------------
_SYSTEM = (
    "You are a brand strategist. You are given a business name, its website URL, "
    "the list of real page URLs on the site, and the visible text scraped from "
    "those pages. Produce a JSON object that captures the brand accurately for a "
    "social-media post generator. Use ONLY facts supported by the text; do not "
    "invent services, stats, or claims. If a field is unknown, use an empty "
    "string or empty list. Never use em dashes.\n\n"
    "CRITICAL: match the voice and topics to what this business actually is and "
    "who its customers are. A local consumer or home service (cleaning, "
    "landscaping, plumbing, dental, salon, restaurant) should sound warm, "
    "friendly, neighborly, and benefit-focused for everyday customers, and its "
    "themes should be about real customer concerns (a clean home, saving time, "
    "trust, local pride, before/after results, seasonal needs). Do NOT write it "
    "like a B2B software or tech company: avoid words like 'solution', "
    "'platform', 'leverage', 'built a tool', 'onboarding', 'deploy', and other "
    "corporate/SaaS jargon unless the business genuinely is a tech company. "
    "Write the voice_rules and themes so a reader would immediately feel this is "
    "THAT kind of business.\n\n"
    "You also pick the visual DESIGN SYSTEM that best fits this brand's "
    "personality, so its posts look right automatically. Choose exactly one "
    "'design' id and a 'mood':\n"
    "- soft-rounded  : warm, friendly, approachable (home cleaning, childcare, "
    "pet care, wellness, cafes). mood=bright\n"
    "- friendly-round: playful, cheerful, family-facing (kids, parties, ice "
    "cream, community). mood=bright\n"
    "- elegant-serif : refined, upscale, boutique (interior design, salon, spa, "
    "jewelry, fine dining, law, real estate). mood=bright\n"
    "- bold-impact   : energetic, strong, high-impact (gym, auto, construction, "
    "sports, BBQ, events). mood=bright\n"
    "- modern-grotesk: clean, modern, minimal (agencies, studios, modern "
    "services, startups, e-commerce). mood=bright\n"
    "- tech-condensed: serious, technical, enterprise (IT, cybersecurity, "
    "finance, B2B software, industrial). mood=dark\n"
    "Pick the single best fit for THIS business."
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
  "design": "one of: soft-rounded | friendly-round | elegant-serif | bold-impact | modern-grotesk | tech-condensed",
  "mood": "bright or dark (dark only for tech-condensed)",
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


_VALID_DESIGNS = {"soft-rounded", "friendly-round", "elegant-serif",
                  "bold-impact", "modern-grotesk", "tech-condensed"}


def build_brand_and_themes(name: str, base: str, scrape: dict) -> tuple[dict, list, str, str]:
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
    design = (data.get("design") or "").strip()
    if design not in _VALID_DESIGNS:
        design = ""      # fall back to auto-by-slug if the model picked garbage
    mood = "dark" if (data.get("mood") or "").strip().lower() == "dark" else "bright"
    if design == "tech-condensed":
        mood = "dark"
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
    return brand, themes, design, mood


# ---------------------------------------------------------------------------
# Logo: download from the site, else render a wordmark
# ---------------------------------------------------------------------------
def _looks_like_screenshot(content: bytes) -> bool:
    """A wide landscape raster (like an og:image hero) is a screenshot, not a
    logo. Real logos/marks are square-ish or a horizontal wordmark."""
    from io import BytesIO

    from PIL import Image
    try:
        w, h = Image.open(BytesIO(content)).size
    except Exception:
        return True
    if h == 0:
        return True
    ar = w / h
    # og:image heroes are ~1.9:1 and large; reject clearly screenshot-shaped.
    return (w >= 900 and 1.4 <= ar <= 2.4) or ar > 6


def _download_logo(scrape: dict, base: str | None = None) -> bytes | None:
    """Find the site's real logo. Prefers <img> logos and apple-touch/rel icons
    (actual brand marks) over og:image, and rejects screenshot-shaped images."""
    base = base or scrape.get("base", "")
    # Priority: header/nav <img> logos, then apple-touch/rel icons, then (only
    # as a last resort) og:image, which is usually a hero screenshot.
    ordered = (scrape.get("logos", []) + scrape.get("icons", [])
               + scrape.get("og_images", []))
    fallback: bytes | None = None
    for src in ordered:
        url = urljoin(base + "/", src)
        if url.lower().split("?")[0].endswith(".svg"):
            continue  # Pillow can't open SVG without extra deps
        try:
            resp = requests.get(url, headers=_UA, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            continue
        if resp.status_code != 200 or len(resp.content) < 800:
            continue
        ctype = resp.headers.get("content-type", "")
        if "image" not in ctype or "svg" in ctype:
            continue
        if _looks_like_screenshot(resp.content):
            fallback = fallback or resp.content   # keep as last-ditch option
            continue
        return resp.content
    return fallback


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
                  auto_colors: bool = True, progress=None) -> str:
    progress = progress or _noop
    base = _normalize_url(website)
    scrape = scrape_site(base, progress)

    progress("Studying the brand and writing content themes...")
    brand, themes, design, mood = build_brand_and_themes(name, base, scrape)

    progress("Fetching the logo...")
    logo_bytes = _download_logo(scrape, base)

    # Derive the brand colors from the site + logo unless the user chose to
    # set them by hand. The passed accents are the fallback either way.
    if auto_colors:
        accent, accent2 = detect_accents(scrape, logo_bytes, accent, accent2)
        logger.info("Detected brand colors for '%s': %s / %s", name, accent, accent2)

    if not logo_bytes:
        logo_bytes = _render_wordmark(name, accent)

    progress("Creating the account...")
    slug = tenants.create_tenant(
        name, name, base, brand, themes,
        fb_page_id=fb_page_id, fb_token=fb_token,
        accent=accent, accent2=accent2, style=mood, design=design,
        logo_bytes=logo_bytes,
    )
    logger.info("Onboarded '%s' (%s): %d themes, design=%s, mood=%s",
                name, slug, len(themes), design or "auto", mood)
    return slug


def rebuild_content(slug: str, auto_colors: bool = True, progress=None) -> str:
    """Re-scrape an existing account's website and regenerate its brand voice,
    themes, logo, and (optionally) colors, WITHOUT touching its Facebook creds,
    style, or post queues. Used by the account settings 'Rebuild' button."""
    progress = progress or _noop
    acct = tenants.account(slug)
    name = acct.get("name", slug)
    base = _normalize_url(acct.get("website", ""))
    if not base:
        raise RuntimeError("This account has no website to rebuild from.")

    scrape = scrape_site(base, progress)
    progress("Studying the brand and writing content themes...")
    # Keep the account's existing design/mood on rebuild (respect any manual
    # override); only brand voice, themes, colors and logo are refreshed.
    brand, themes, _design, _mood = build_brand_and_themes(name, base, scrape)

    progress("Fetching the logo...")
    logo_bytes = _download_logo(scrape, base)

    accent = acct.get("accent", "#2ecc71")
    accent2 = acct.get("accent2", "#2b6cc4")
    if auto_colors:
        accent, accent2 = detect_accents(scrape, logo_bytes, accent, accent2)

    tenants.save_brand(brand, slug)
    tenants.save_themes(themes, slug)
    acct["accent"], acct["accent2"] = accent, accent2
    tenants.save_account(acct, slug)
    if logo_bytes:
        (tenants.tenant_dir(slug) / "logo_full.png").write_bytes(logo_bytes)
    logger.info("Rebuilt content for '%s' (%s): %d themes, colors %s/%s",
                name, slug, len(themes), accent, accent2)
    return slug
