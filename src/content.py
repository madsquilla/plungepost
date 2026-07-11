"""Brand facts, theme loading, and system-prompt assembly (per account).

Brand facts and themes are now per-account (see tenants.py). `brand()` returns
the current account's brand dict (falling back to the SkySystems seed), and
`build_system_prompt()` assembles a GENERIC prompt from whatever brand fields
are present, so it works for any business, not just an MSP.
"""

from __future__ import annotations

from typing import Any

import tenants

# Generic voice rules used as a default / for newly-onboarded accounts.
DEFAULT_VOICE_RULES = [
    "Professional, plain-English, reassuring. Never fear-monger for its own sake.",
    "Educational first, sales second. Build credibility, do not hard-sell.",
    "Sound human-written. No buzzword soup, no 'unlock the power of synergy'.",
    "Never use em dashes (the long dash). Use commas, periods, or colons instead.",
    "Short paragraphs, 1 to 3 short sentences each. Easy to read on mobile.",
    "Light, tasteful emoji use is OK (0 to 2 per post), not every line.",
    "End most posts with a soft CTA, not a pushy one, or invite a conversation.",
    "Include 2 to 4 relevant hashtags max. Do not overdo it.",
    "Do not stack every credibility stat into one post. Rotate, use accurately.",
]

# ---------------------------------------------------------------------------
# SkySystems seed brand (used to migrate the first account + as a fallback).
# ---------------------------------------------------------------------------
DEFAULT_BRAND: dict[str, Any] = {
    "company": "SkySystems USA Corporation",
    "what": "a managed IT, cybersecurity, and cloud provider (an MSP) based in Austin, Texas",
    "headline": "Your Managed IT & Cybersecurity Partner in Austin, Texas.",
    "positioning": (
        "Secure, fully managed IT, cybersecurity, and cloud for Austin-area "
        "businesses and government, backed by 24/7 support."
    ),
    "website": "https://skyusa.us",
    "tagline": "We Brought Enterprise IT Security to Small Business. On Purpose.",
    "differentiator": (
        "An Austin-based partner that brings enterprise-grade security, "
        "management, and 24/7 support to small and mid-sized businesses, made "
        "accessible and affordable, without the enterprise price tag."
    ),
    "mission": (
        "Bring enterprise-grade IT security and management to small and "
        "mid-sized businesses and government. Make robust protection accessible "
        "and understandable, without the enterprise price tag."
    ),
    "stats": [
        "13+ years in business", "70+ professionals", "7 datacenters",
        "24/7 support",
    ],
    "service_pillars": [
        "Cybersecurity & Compliance",
        "Managed IT & Helpdesk (a dedicated IT department, real people answer)",
        "Microsoft 365 & Microsoft Azure",
        "Cloud Hosting & AWS",
        "Backup & Disaster Recovery (Veeam)",
        "Network Security & WatchGuard Firewalls",
        "Networking & WiFi",
        "Business Phones / VoIP (3CX)",
        "AI Implementation (governed, business-ready AI)",
    ],
    "verticals": [
        "Public Safety & Municipalities (CJIS, dispatch/CAD uptime, ransomware defense)",
        "Ministries & Non-Profits (donor data, broadcast-quality streaming)",
        "Mega Churches & Multi-Campus (broadcast production, multi-site, AI-ready)",
        "Financial Services (regulator-grade security, exam-ready compliance, fraud defense)",
        "Professional Services (law, accounting, consulting; privileged data, safe AI)",
        "Retail (PCI-aligned multi-site networks, POS uptime)",
        "Smaller Cities & Towns (CJIS, ransomware defense, public-budget pricing)",
        "Technology, Media & Telecom (cloud, DevOps, high-throughput infra, AI)",
    ],
    "compliance": ["NIST", "HIPAA", "CJIS", "SEC", "PCI"],
    "signature_stat": (
        "~60% of small businesses close within 6 months of a cyberattack "
        "(use sparingly, not every post)."
    ),
    "voice_rules": [
        "Never mention Germany, Europe, transatlantic, overseas/offshore teams, "
        "or 'follow the sun' support. Present the company strictly as an Austin, "
        "Texas company with a US team.",
        *DEFAULT_VOICE_RULES,
    ],
}


# ---------------------------------------------------------------------------
# Post formats + lengths (shared across all accounts).
# ---------------------------------------------------------------------------
POST_FORMATS = [
    {"id": "listicle", "instruction": (
        "Structure as a short numbered list of 2 to 4 tight, specific points, "
        "with a one-line intro before the list and a one-line takeaway after.")},
    {"id": "myth-reality", "instruction": (
        "Structure as Myth vs Reality: state a common misconception in one "
        "line, then correct it plainly and reassuringly.")},
    {"id": "scenario", "instruction": (
        "Open with a tiny real-world scenario (two or three sentences of story), "
        "then draw out the lesson.")},
    {"id": "question-led", "instruction": (
        "Open with one direct question to the reader. Answer it simply, close.")},
    {"id": "stat-led", "instruction": (
        "Lead with one specific, accurate number or statistic, explain why it "
        "matters, then reassure. Do not stack multiple stats.")},
    {"id": "how-to", "instruction": (
        "Give a short, practical how-to in exactly 3 plain steps the reader "
        "could act on this week.")},
    {"id": "one-bold-idea", "instruction": (
        "Make one bold, clear statement up front, then back it up in 2 to 3 "
        "short sentences. Keep the whole thing punchy and brief.")},
    {"id": "human-angle", "instruction": (
        "Tell it from a human, behind-the-scenes angle: what the team actually "
        "does, or a relatable frustration a customer feels. Warm, candid.")},
    {"id": "quick-tip", "instruction": (
        "Share one single, specific, immediately useful tip. Short, no filler.")},
    {"id": "comparison", "instruction": (
        "Frame as a simple before/after or this-vs-that comparison.")},
]

POST_LENGTHS = [
    {"id": "short", "instruction": (
        "Short and scannable: 2 to 3 short sentences that still make a real, "
        "informative point. Roughly 40 to 65 words.")},
    {"id": "medium", "instruction": (
        "A bit fuller: 3 to 4 short sentences. Roughly 65 to 90 words.")},
    {"id": "list", "instruction": (
        "A short numbered list of 3 brief points with a one-line intro. Roughly "
        "55 to 85 words.")},
]


def brand() -> dict[str, Any]:
    """The current account's brand facts (or the SkySystems seed)."""
    b = tenants.brand()
    return b if b else DEFAULT_BRAND


def load_themes() -> list[dict[str, Any]]:
    """The current account's content themes."""
    themes = tenants.themes()
    if not isinstance(themes, list) or not themes:
        raise ValueError("This account has no themes.json yet.")
    return themes


def _bullet(items) -> str:
    return "\n".join(f"- {item}" for item in (items or []))


def build_system_prompt() -> str:
    """Assemble a generic brand-voice prompt from the current account's brand."""
    b = brand()
    company = b.get("company", "the business")
    what = b.get("what", "a local business")
    website = b.get("website", "")
    P: list[str] = [
        f"You write short, professional social media posts for the official "
        f"Facebook Page of {company}, {what}.",
        "",
        "BRAND POSITIONING",
    ]
    if b.get("headline"):
        P.append(f"- Headline: {b['headline']}")
    if b.get("positioning"):
        P.append(f"- {b['positioning']}")
    if website:
        P.append(f"- Website: {website}")
    if b.get("tagline"):
        P.append(f"- Tagline: {b['tagline']}")
    if b.get("differentiator"):
        P.append(f"- What sets them apart: {b['differentiator']}")
    if b.get("mission"):
        P += ["", "MISSION", b["mission"]]
    if b.get("stats"):
        P += ["", "CREDIBILITY (rotate, use accurately, do NOT stack all in one post)",
              _bullet(b["stats"])]
    if b.get("service_pillars"):
        P += ["", "SERVICES / OFFERINGS (rotate across these)", _bullet(b["service_pillars"])]
    if b.get("verticals"):
        P += ["", "WHO THEY SERVE / INDUSTRIES", _bullet(b["verticals"])]
    if b.get("compliance"):
        P += ["", "Standards / compliance you may reference accurately: "
              + ", ".join(b["compliance"]) + "."]
    if b.get("signature_stat"):
        P.append("Signature stat to use sparingly: " + b["signature_stat"])
    P += ["", "VOICE RULES (follow strictly)",
          _bullet(b.get("voice_rules") or DEFAULT_VOICE_RULES)]
    P += ["", f"""WRITING QUALITY (aim higher than generic)
- The post_text is shown IN FULL on the image graphic, so keep it informative
  but scannable. Make a real, useful point; cut filler and obvious statements.
- Open with a specific, concrete hook, not a vague generality.
- Reference the REAL services and audiences listed above accurately. Name
  concrete offerings when relevant. Never invent services the business does
  not offer.
- Vary sentence and paragraph structure so posts never feel templated. No two
  posts should open the same way.
- You will be given a FORMAT and LENGTH for THIS post. Follow them.
- Vary emoji use (often none) and change up the hashtag set.

THE TWO PIECES OF TEXT
1. post_text -> the MESSAGE shown IN FULL on the image graphic. Informative but
   scannable. You MAY end with a short VERBAL soft CTA. Do NOT put any URL or
   hashtags in post_text (the image is not clickable). Emojis fine in moderation.
2. caption -> the short text shown ABOVE the image. This is where the clickable
   link and hashtags belong. Format: one short sentence that complements the
   message, then the EXACT page link you are given for this post (it deep-links
   to the specific service/page the post is about, e.g. {website}/services),
   then 2 to 4 relevant hashtags. Use the provided link verbatim.

Plus three helper fields for the graphic:
- image_headline: a short, bold 3 to 7 word title shown large on the graphic.
- image_kicker: a 2 to 4 word Title Case label above the headline.
- image_query: a 2 to 4 word concrete, visual, professional stock-photo search.

FORMATTING THE post_text (rendered on the graphic, so structure helps)
- Numbered steps: each on its own line starting "1. ", "2. ", "3. ".
- Myth/Reality or Before/After: start the line with the label and a colon.
- Separate distinct thoughts into short paragraphs (blank line between).

OUTPUT FORMAT
Return ONLY a single JSON object, no prose, no code fences, with exactly:
{{
  "post_text": "the message shown ON the image (no URL, no hashtags)",
  "caption": "short lead sentence, then the link, then 2 to 4 hashtags",
  "theme": "the theme id you were asked to write for",
  "image_headline": "short bold 3 to 7 word title",
  "image_kicker": "2 to 4 word Title Case label",
  "image_query": "2 to 4 word concrete photo search"
}}"""]
    return "\n".join(P)
