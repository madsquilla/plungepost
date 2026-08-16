"""Build self-contained HTML/CSS post graphics, themed per brand + design.

Each account has a design system (font + shapes + motif) and brand colors; each
post picks one of many layouts. We assemble an HTML document and hand it to
htmlrender to screenshot at 1080x1080. HTML/CSS gives real typography and
layout, so cards look professionally designed rather than pixel-drawn.

Public API:
    render_card(item, out_path, photo_path=None, avoid=None) -> layout_name
"""

from __future__ import annotations

import base64
import colorsys
import html as _html
import logging
import random
import re
from pathlib import Path

import tenants

logger = logging.getLogger("plungepost.htmlcards")

_ASSETS = Path(__file__).resolve().parent.parent / "assets"
_FONT_DIR = _ASSETS / "fonts"


def _furl(name: str) -> str:
    return (_FONT_DIR / name).resolve().as_uri()


# --- colors ------------------------------------------------------------------
def _to_rgb(h):
    h = (h or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except (ValueError, IndexError):
        return (46, 204, 113)


def _hex(rgb):
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(c))) for c in rgb))


def _chroma(hexc):
    """0..1 saturation of a hex colour. Used to tell a brand colour from a grey."""
    r, g, b = (x / 255 for x in _to_rgb(hexc))
    return colorsys.rgb_to_hsv(r, g, b)[1]


def _ui_accent(hexc):
    """Vivid, readable brand accent (pale/grey inputs get saturated/darkened)."""
    r, g, b = (x / 255 for x in _to_rgb(hexc))
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if s < 0.12:
        v = min(v, 0.34)
    else:
        s = max(s, 0.5)
        v = min(max(v, 0.42), 0.74)
    return _hex(tuple(int(x * 255) for x in colorsys.hsv_to_rgb(h, s, v)))


def _pick_accents(a1, a2):
    """Order a brand's two accents so the more colourful one leads.

    Onboarding scrapes colours off a website and often lands on a border or
    background grey. Feeding that to _ui_accent only darkens it -- it can never
    become colourful -- so the whole feed renders greyscale while the brand's
    real colour sits unused in accent2. Lead with whichever actually has chroma.
    """
    a1 = a1 or "#2ecc71"
    a2 = a2 or "#2b6cc4"
    if _chroma(a2) > _chroma(a1) + 0.18:
        return a2, a1
    return a1, a2


def _darken(hexc, f=0.72):
    return _hex(tuple(int(c * f) for c in _to_rgb(hexc)))


def _lighten(hexc, f=0.5):
    return _hex(tuple(int(c + (255 - c) * f) for c in _to_rgb(hexc)))


def _mix(hexc, other, f):
    a, b = _to_rgb(hexc), _to_rgb(other)
    return _hex(tuple(a[i] + (b[i] - a[i]) * f for i in range(3)))


def _luma(hexc):
    r, g, b = _to_rgb(hexc)
    return 0.299 * r + 0.587 * g + 0.114 * b


# --- design systems ----------------------------------------------------------
# family label -> (font file, css weight). Variable fonts accept any weight.
_FONTS = {
    "quicksand": ("Quicksand.ttf", 700),
    "baloo": ("Baloo2.ttf", 700),
    "fraunces": ("Fraunces.ttf", 600),
    "anton": ("Anton.ttf", 400),
    "grotesk": ("SpaceGrotesk.ttf", 600),
    "rajdhani": ("Rajdhani-Bold.ttf", 700),
    "nunito": ("NunitoSans.ttf", 400),
    "bricolage": ("BricolageGrotesque.woff2", 700),
    "inter": ("Inter.woff2", 400),
}

_DESIGNS = {
    # Warm home-services identity: a characterful serif for trust, a rounded
    # sans for approachability, daylight for atmosphere, and the sweep arc as
    # the signature. Built for brands where a stranger is let into your house.
    "warm-home": dict(head="fraunces", hw=600, body="nunito", case="none",
                      radius=22, kicker="pill", motif="sunwash", tracking=".02em",
                      mood="bright", warm="#f0c177", sweep=True, paper="#fbf8f3",
                      ink="#16302c", sub="#5c6f68", foot=False),
    # Same warm palette and sweep signature, but a rounded display face --
    # reads neighbourly rather than premium. For brands whose pitch is "locally
    # owned, not a franchise".
    "warm-home-round": dict(head="baloo", hw=700, body="nunito", case="none",
                            radius=26, kicker="pill", motif="sunwash", tracking="0",
                            mood="bright", warm="#f0c177", sweep=True,
                            paper="#fbf8f3", ink="#16302c", sub="#5c6f68", foot=False),
    "soft-rounded": dict(head="quicksand", hw=700, body="nunito", case="none",
                         radius=30, kicker="pill", motif="blobs", tracking="0",
                         mood="bright"),
    "friendly-round": dict(head="baloo", hw=700, body="nunito", case="none",
                           radius=34, kicker="pill", motif="blobs", tracking="0",
                           mood="bright"),
    "elegant-serif": dict(head="fraunces", hw=600, body="nunito", case="none",
                          radius=8, kicker="plain", motif="none", tracking=".16em",
                          mood="bright"),
    "bold-impact": dict(head="anton", hw=400, body="grotesk", case="upper",
                        radius=4, kicker="tab", motif="stripe", tracking=".01em",
                        mood="bright"),
    "modern-grotesk": dict(head="grotesk", hw=700, body="nunito", case="none",
                           radius=10, kicker="underline", motif="none",
                           tracking="0", mood="bright"),
    "tech-condensed": dict(head="rajdhani", hw=700, body="nunito", case="upper",
                           radius=3, kicker="tab", motif="grid", tracking=".04em",
                           mood="dark"),
    # Editorial trust identity for a solo senior engineer's practice: quiet
    # authority rather than loud cybersecurity-vendor marketing. Sentence
    # case (the brand's own headline is "I.T. You Can Actually Trust", not
    # shouted caps), an underline eyebrow instead of a filled badge, and the
    # same display/body pairing as the brand's own site.
    "trust-editorial": dict(head="bricolage", hw=700, body="inter", case="none",
                            radius=14, kicker="underline", motif="glow",
                            tracking="-.01em", mood="dark"),
}
_BRIGHT = ["warm-home", "soft-rounded", "friendly-round", "elegant-serif",
           "bold-impact", "modern-grotesk"]

# Layout pool per design (text + photo + list). Photo layouts are used only when
# a photo is available; checklist/stat only when the copy has a list/number.
_BASE = ["hero", "centered", "corner", "frame", "bold-color", "quote",
         "photo-full", "photo-top", "photo-side", "photo-card", "checklist", "stat"]
_POOLS = {
    "warm-home": ["hero", "photo-side", "stat", "photo-card", "quote"] + _BASE,
    "warm-home-round": ["hero", "photo-side", "stat", "photo-card", "centered"] + _BASE,
    "soft-rounded": ["hero", "photo-card", "centered", "photo-top"] + _BASE,
    "friendly-round": ["centered", "photo-card", "hero", "frame"] + _BASE,
    "elegant-serif": ["frame", "photo-side", "quote", "centered"] + _BASE,
    "bold-impact": ["bold-color", "photo-full", "corner", "hero"] + _BASE,
    "modern-grotesk": ["corner", "photo-side", "hero", "frame"] + _BASE,
    "tech-condensed": ["hero", "photo-full", "bold-color", "corner", "stat", "quote"],
    # No bold-color (a full-bleed accent wash reads as a vendor ad, not a
    # senior engineer's own note) and no photo-full/-top/-card (this brand
    # sells direct personal access, not stock-photo server-room imagery).
    # Weighted toward the layouts that carry a credibility number, a direct
    # trust statement, or a scannable tip list -- what actually earns
    # engagement for a B2B/MSP account instead of reading as a sales pitch.
    "trust-editorial": ["stat", "quote", "checklist", "hero", "corner",
                        "photo-side", "centered"],
}


def _design_id():
    try:
        acct = tenants.account()
    except Exception:
        acct = {}
    d = acct.get("design")
    if d in _DESIGNS:
        return d
    if tenants.style() == "dark":
        return "tech-condensed"
    slug = tenants.current()
    return _BRIGHT[sum(ord(c) for c in slug) % len(_BRIGHT)]


# --- assets ------------------------------------------------------------------
def _logo_data_uri():
    p = tenants.logo_full()
    if p.exists():
        b = p.read_bytes()
        return "data:image/png;base64," + base64.b64encode(b).decode()
    return None


def _logo_is_badge():
    """True when the logo is a squarish icon rather than a horizontal wordmark.

    A square icon alone reads as an unexplained sticker in the footer, so it
    gets paired with the company name; a wordmark already says the name and
    stands on its own.
    """
    p = tenants.logo_full()
    if not p.exists():
        return False
    try:
        from PIL import Image
        with Image.open(p) as im:
            w, h = im.size
        return h > 0 and (w / h) < 1.7
    except Exception:
        return False


def _photo_uri(photo_path):
    if not photo_path:
        return None
    p = Path(photo_path)
    if not p.exists():
        return None
    return p.resolve().as_uri()


# --- text helpers ------------------------------------------------------------
_LIST_RE = re.compile(r"^\s*(\d+)[.)]\s+(.*)")
# A REAL statistic: a percentage, money, multiplier, 24/7, a multi-digit number
# (>=2 digits, optional +), or an ordinal. A bare single digit (a list marker
# like "1.") is intentionally NOT a stat.
_NUM_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?\s?%"        # 60%, 12.5%
    r"|\$\s?\d[\d,]*(?:\.\d+)?"       # $1,000
    r"|\b\d+x\b"                      # 3x
    r"|\b\d{1,3}(?:/\d{1,3})\b"       # 24/7
    r"|\b\d{2,}[\d,]*\+?\b"           # 500, 500+, 13
    r"|\b\d+(?:st|nd|rd|th)\b)"       # 1st, 25th
)
_SENT_RE = re.compile(r"(.{18,150}?[.!?])(?:\s|$)")


def _esc(s):
    return _html.escape((s or "").strip())


# An inline numbered-list marker, e.g. the " 1. " in "...we make: 1. Rushed...".
# Not preceded by a digit (so "3.5" is not a marker).
_INLINE_MARK = re.compile(r"(?<!\d)\s*\b\d{1,2}[.)]\s+")


def _lead(text):
    # Drop line-based numbered-list lines first.
    lines = [ln for ln in (text or "").splitlines() if not _LIST_RE.match(ln.strip())]
    t = " ".join(" ".join(lines).split())
    if not t:
        t = " ".join((text or "").split())
    # If an INLINE numbered list follows (2+ markers), keep only the intro.
    marks = list(_INLINE_MARK.finditer(t))
    if len(marks) >= 2:
        intro = t[:marks[0].start()].rstrip(" :-–—,")
        if len(intro) >= 12:
            t = intro
    m = _SENT_RE.match(t)
    return (m.group(1) if m else t[:150]).strip()


def _list_items(text):
    # Line-based numbered list.
    out = []
    for ln in (text or "").splitlines():
        m = _LIST_RE.match(ln.strip())
        if m:
            out.append(m.group(2).strip())
    if out:
        return out
    # Inline numbered list ("intro: 1. a. 2. b. 3. c").
    t = " ".join((text or "").split())
    marks = list(_INLINE_MARK.finditer(t))
    if len(marks) >= 2:
        items = []
        for i, mk in enumerate(marks):
            start = mk.end()
            end = marks[i + 1].start() if i + 1 < len(marks) else len(t)
            piece = t[start:end].strip()
            if piece:
                items.append(piece)
        return items
    return []


def _first_stat(text, headline):
    for s in (headline or "", text or ""):
        m = _NUM_RE.search(s)
        if m:
            return m.group(1).strip()
    return None


# Words that follow a number but are not its unit, so "40 pounds" keeps "pounds"
# while "4 of" or "500 and" keeps nothing.
_NOT_UNIT = {
    "of", "and", "or", "the", "a", "an", "to", "in", "on", "for", "with",
    "that", "which", "is", "are", "was", "were", "per", "out", "off", "at",
}


def _stat_unit(text, headline, stat):
    """The unit belonging to `stat`, e.g. "pounds" from "40 pounds a year".

    A bare numeral reads as a non-sequitur on a card -- "40" tells you nothing
    until you read the paragraph. Pairing it with its unit makes the stat
    self-contained, which is the whole point of a stat layout.
    """
    if not stat or stat.endswith("%") or stat.startswith("$"):
        return ""
    for s in (headline or "", text or ""):
        idx = s.find(stat)
        if idx < 0:
            continue
        tail = s[idx + len(stat):].lstrip()
        word = re.split(r"[^A-Za-z-]+", tail, maxsplit=1)[0]
        low = word.lower()
        if word and low not in _NOT_UNIT and 2 <= len(word) <= 12:
            return word
    return ""


# --- CSS ---------------------------------------------------------------------
def _fontfaces():
    faces = []
    for label, (fname, _w) in _FONTS.items():
        fmt = "woff2" if fname.endswith(".woff2") else "truetype"
        faces.append(
            "@font-face{font-family:'%s';src:url('%s') format('%s');"
            "font-weight:100 1000;font-display:block;}" % (label, _furl(fname), fmt))
    return "".join(faces)


def _theme(design, accent, accent2, mood):
    d = _DESIGNS[design]
    head_fam = d["head"]
    body_fam = d["body"]
    # Per-design paper/ink so a warm identity is not forced onto the cool
    # blue-grey defaults that every design shared before.
    ink = d.get("ink", "#16232f")
    paper = d.get("paper", "#f5f7fa")
    sub = d.get("sub", "#5a6b7a")
    if mood == "dark":
        ink = "#f3f7fc"
        paper = "#0d1826"
        sub = "#9fb2c6"
    r, g, b = _to_rgb(accent)
    r2, g2, b2 = _to_rgb(accent2)
    acc_lt = _mix(accent, paper, 0.82)
    if mood == "dark":
        # Rich gradient-mesh navy with accent glows -- the premium dark look.
        cardbg = (
            f"radial-gradient(60% 52% at 82% 6%, rgba({r},{g},{b},0.30), transparent 60%),"
            f"radial-gradient(55% 48% at 8% 96%, rgba({r2},{g2},{b2},0.24), transparent 62%),"
            f"radial-gradient(40% 40% at 30% 50%, rgba({r},{g},{b},0.10), transparent 70%),"
            "linear-gradient(158deg,#122238 0%,#0a1524 55%,#06101c 100%)")
        # A light accent tint, not acc_lt -- acc_lt is mixed toward the dark
        # paper for subtle background fills, and a 3-line headline reaches
        # far enough down the gradient that its last line landed on a tone
        # nearly matching the card background (unreadable). Mixing toward
        # white instead keeps every line legible regardless of line count.
        head_grad_end = _mix(accent, "#ffffff", 0.45)
        head_grad = f"linear-gradient(180deg,#f6fbff 0%,{head_grad_end} 118%)"
    else:
        cardbg = paper
        head_grad = ""
    # Warm daylight tint for the sunwash motif, and a hairline that anchors the
    # footer instead of leaving it floating in the dead band.
    warm = d.get("warm", "#f0c177")
    wr, wg, wb = _to_rgb(warm)
    warm_glow = (f"rgba({wr},{wg},{wb},0.30)" if mood != "dark"
                 else f"rgba({wr},{wg},{wb},0.12)")
    hair = _mix(ink, paper, 0.88 if mood != "dark" else 0.80)
    vars_ = {
        "ACCENT": accent, "ACCENT2": accent2,
        "ACC_DK": _darken(accent, 0.72), "ACC_LT": acc_lt,
        "ACC_GLOW": f"rgba({r},{g},{b},0.14)",
        "ACC_DOT": f"rgba({r},{g},{b},0.16)",
        "WARM": warm, "WARM_GLOW": warm_glow, "HAIR": hair,
        "INK": ink, "PAPER": paper, "SUB": sub, "CARDBG": cardbg,
        "HEAD": head_fam, "BODY": body_fam,
        "HW": str(d["hw"]), "RADIUS": str(d["radius"]) + "px",
        "TRACK": d["tracking"],
        "TT": "uppercase" if d["case"] == "upper" else "none",
    }
    css = _BASE_CSS
    if mood == "dark":
        css += _DARK_CSS.replace("{HEAD_GRAD}", head_grad)
    for k, v in vars_.items():
        css = css.replace("{" + k + "}", v)
    return css, d


_BASE_CSS = """
*{margin:0;padding:0;box-sizing:border-box;}
html,body{width:1080px;height:1080px;}
#card{width:1080px;height:1080px;background:{CARDBG};color:{INK};position:relative;
  overflow:hidden;font-family:'{BODY}',sans-serif;-webkit-font-smoothing:antialiased;}
.pad{position:absolute;inset:0;padding:96px 92px 172px;display:flex;flex-direction:column;}
.center-v{justify-content:center;}
.head{font-family:'{HEAD}';font-weight:{HW};color:{INK};line-height:1.06;
  letter-spacing:-.01em;text-transform:{TT};text-wrap:balance;max-width:100%;}
.kicker{align-self:flex-start;font-family:'{HEAD}';font-weight:600;font-size:24px;
  letter-spacing:.14em;text-transform:uppercase;margin-bottom:26px;}
.k-pill{background:{ACCENT};color:#fff;padding:12px 24px;border-radius:999px;}
.k-tab{background:{ACCENT};color:#fff;padding:11px 20px;}
.k-plain{color:{ACCENT};}
.k-underline{color:{ACCENT};border-bottom:4px solid {ACCENT};padding-bottom:6px;}
.rule{width:96px;height:8px;background:{ACCENT};border-radius:4px;margin:30px 0 26px;}
/* Clamped so a long lead can never spill into the footer -- copy length varies
   per post and the card has no scrollbar to save it. */
.sub{font-size:35px;line-height:1.45;color:{SUB};max-width:88%;overflow:hidden;
  display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;}
.footer{position:absolute;left:92px;right:92px;bottom:70px;display:flex;
  align-items:center;justify-content:space-between;gap:20px;padding-top:30px;
  border-top:2px solid {HAIR};}
.logo{height:94px;max-width:58%;object-fit:contain;object-position:left center;}
.lockup{display:flex;align-items:center;gap:20px;min-width:0;}
.badge{width:78px;height:78px;flex:0 0 auto;border-radius:18px;object-fit:cover;
  box-shadow:0 6px 18px rgba(9,20,18,.16);}
.lockup-name{font-family:'{HEAD}';font-weight:{HW};font-size:33px;color:{INK};
  line-height:1.1;letter-spacing:-.005em;white-space:nowrap;}
/* In a half-width column the name and the domain cannot both fit; the name
   wins because it is the brand. */
.footer.narrow .dom{display:none;}
.footer.narrow .lockup-name{font-size:29px;}
.footer.narrow .badge{width:64px;height:64px;border-radius:15px;}
.logo-wm{font-family:'{HEAD}';font-weight:{HW};font-size:34px;color:{ACCENT};}
.dom{font-size:24px;color:{SUB};opacity:.85;white-space:nowrap;}
.bg{position:absolute;inset:0;pointer-events:none;overflow:hidden;}
.blob{position:absolute;border-radius:50%;pointer-events:none;}
.stripe{position:absolute;top:-80px;bottom:-80px;width:300px;right:-30px;
  transform:skewX(-11deg);background:{ACCENT};opacity:.07;}
.ring{position:absolute;border-radius:50%;border:44px solid {ACCENT};opacity:.06;}
.glow{position:absolute;inset:0;background:radial-gradient(60% 55% at 82% 12%,
  {ACC_GLOW} 0%, transparent 60%);}
.dots{position:absolute;inset:0;background-image:radial-gradient({ACC_DOT} 3px,transparent 3px);
  background-size:46px 46px;-webkit-mask-image:linear-gradient(120deg,#000 0%,transparent 62%);
  mask-image:linear-gradient(120deg,#000 0%,transparent 62%);}
.grid{position:absolute;inset:0;background-image:linear-gradient({ACC_DOT} 1px,transparent 1px),
  linear-gradient(90deg,{ACC_DOT} 1px,transparent 1px);background-size:60px 60px;
  -webkit-mask-image:radial-gradient(70% 70% at 78% 18%,#000,transparent);
  mask-image:radial-gradient(70% 70% at 78% 18%,#000,transparent);}
.wave{position:absolute;left:-10%;right:-10%;bottom:-46%;height:80%;border-radius:50%;
  background:{ACC_LT};}
.cornerblock{position:absolute;top:0;right:0;width:44%;height:40%;background:{ACC_LT};
  border-bottom-left-radius:44px;}
.photo{position:absolute;inset:0;background-size:cover;background-position:center;}
.scrim{position:absolute;inset:0;background:linear-gradient(to top,
  rgba(8,20,18,.94) 0%, rgba(8,20,18,.72) 30%, rgba(8,20,18,.18) 58%,
  rgba(8,20,18,0) 78%);}
.on-photo{color:#fff;}
.on-photo .head{color:#fff;text-shadow:0 2px 18px rgba(0,0,0,.35);}
.on-photo .sub{color:#e7edf3;max-width:86%;}
.on-photo .dom{color:#dfe8f0;}
.on-photo .logo{filter:brightness(0) invert(1);}
.on-photo .lockup-name{color:#fff;}
.on-photo-f .lockup-name{color:#fff;}
.on-photo-f .dom{color:#dfe8f0;}
.numlist{margin-top:14px;display:flex;flex-direction:column;gap:20px;}
.numrow{display:flex;align-items:flex-start;gap:22px;}
.numbadge{flex:0 0 auto;width:52px;height:52px;border-radius:50%;background:{ACCENT};
  color:#fff;font-family:'{HEAD}';font-weight:700;font-size:26px;display:flex;
  align-items:center;justify-content:center;}
.numtext{font-size:29px;line-height:1.35;color:{INK};padding-top:6px;}
.statnum{font-family:'{HEAD}';font-weight:{HW};color:{ACCENT};font-size:250px;
  line-height:.86;letter-spacing:-.02em;}
.statrow{display:flex;align-items:baseline;gap:22px;flex-wrap:wrap;}
.statunit{font-family:'{HEAD}';font-weight:{HW};color:{ACCENT};font-size:76px;
  line-height:1;letter-spacing:-.01em;opacity:.92;}

/* Signature: a shallow arc in place of the straight rule -- the arc of a cloth
   passing over a surface. Applied per design via the d-sweep class. */
#card.d-sweep .rule{width:158px;height:40px;background:transparent;border:0;
  border-bottom:8px solid {ACCENT};border-radius:0 0 58% 58% / 0 0 100% 100%;
  transform:rotate(-3deg);margin:26px 0 32px;}
#card.d-sweep .k-pill{border-radius:999px;}
/* Warm daylight falling across the card -- the atmosphere the brand sells. */
.sunwash{position:absolute;inset:0;background:
  radial-gradient(58% 46% at 12% 4%, {WARM_GLOW} 0%, transparent 64%),
  radial-gradient(46% 40% at 96% 88%, {ACC_GLOW} 0%, transparent 66%);}
/* display:flex so .kicker's align-self:flex-start applies -- in a block panel
   the pill stretched to the full panel width. */
.card-panel{position:absolute;left:60px;right:60px;bottom:60px;background:{PAPER};
  border-radius:{RADIUS};padding:52px 56px 44px;box-shadow:0 26px 70px rgba(9,20,18,.26);
  display:flex;flex-direction:column;align-items:flex-start;}
.card-panel .sub{-webkit-line-clamp:3;}
/* width:100% because the panel is align-items:flex-start, which would
   otherwise shrink this row to its content and collide name with domain. */
.panel-foot{display:flex;align-items:center;justify-content:space-between;
  width:100%;margin-top:34px;padding-top:26px;border-top:2px solid {HAIR};gap:20px;}
/* The photo-top band is only ~520px tall; the shared 4-line lead does not fit. */
/* Stops above the footer rather than at the card edge, so the lead can never
   run underneath the brand lockup. */
.band{position:absolute;left:0;right:0;top:560px;bottom:186px;padding:30px 92px 0;
  display:flex;flex-direction:column;justify-content:center;align-items:flex-start;}
.band .sub{-webkit-line-clamp:2;font-size:31px;}
.band .rule{margin:18px 0 20px;}
.band .kicker{margin-bottom:16px;}
/* Must out-specify #card.d-sweep .rule, which would otherwise keep its taller
   margins and push the lead out of the band. */
#card .band .rule{margin:14px 0 16px;height:30px;}
#card .band{overflow:hidden;}
/* With no brand footer the copy can use the full canvas instead of leaving a
   dead band at the bottom. */
#card.nofoot .pad{padding-bottom:100px;}
#card.nofoot .band{bottom:92px;}
#card.nofoot .card-panel{padding-bottom:52px;}
.frame-bg{position:absolute;inset:0;background:{ACC_LT};}
.frame-card{position:absolute;inset:60px;border-radius:26px;background:{PAPER};
  border:2px solid {ACCENT};display:flex;flex-direction:column;justify-content:center;
  padding:0 76px;}
"""

# Premium dark-mode enhancements: gradient headline, glowing accents, film grain.
_DARK_CSS = """
#card .head{background:{HEAD_GRAD};-webkit-background-clip:text;background-clip:text;
  color:transparent;}
#card .sub{color:#aebfd2;}
#card .rule{box-shadow:0 0 22px {ACCENT};}
#card .kicker.k-tab,#card .kicker.k-pill{box-shadow:0 6px 26px rgba(0,0,0,.35);}
#card .dom{color:#8fa4bb;}
#card .statnum{text-shadow:0 0 60px {ACCENT};}
#card .card-panel{background:#0f1d30;box-shadow:0 30px 80px rgba(0,0,0,.5);}
#card .frame-bg{background:transparent;}
#card .frame-card{background:rgba(15,29,48,.72);backdrop-filter:blur(2px);}
#card .numtext{color:#cdd9e6;}
#card .glow{background:radial-gradient(60% 55% at 82% 12%,rgba(255,255,255,.05) 0%,transparent 60%);}
.grain{position:absolute;inset:0;pointer-events:none;opacity:.05;mix-blend-mode:overlay;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");}
"""


# --- kicker/footer fragments -------------------------------------------------
def _kicker_html(d, kicker):
    cls = {"pill": "k-pill", "tab": "k-tab", "plain": "k-plain",
           "underline": "k-underline"}.get(d["kicker"], "k-pill")
    return f'<div class="kicker {cls}">{_esc(kicker)}</div>' if kicker else ""


def _lockup_html(logo_uri, wm_name, badge):
    """The brand mark for the footer: badge + name, wordmark alone, or text."""
    if logo_uri and badge:
        return (f'<div class="lockup"><img class="badge" src="{logo_uri}">'
                f'<div class="lockup-name">{_esc(wm_name)}</div></div>')
    if logo_uri:
        return f'<img class="logo" src="{logo_uri}">'
    return f'<div class="logo-wm">{_esc(wm_name)}</div>'


def _footer_html(logo_uri, domain, wm_name, badge=False):
    left = _lockup_html(logo_uri, wm_name, badge)
    return f'<div class="footer">{left}<div class="dom">{_esc(domain)}</div></div>'


# A palette of tasteful background treatments. Each design allows a few; one is
# picked per post (rotating) so a brand's feed varies instead of repeating.
_BG_SETS = {
    "warm-home": ["sunwash", "sunwash", "wave", "glow"],
    "warm-home-round": ["sunwash", "sunwash", "wave", "blobs"],
    "soft-rounded": ["blobs", "glow", "dots", "wave"],
    "friendly-round": ["blobs", "dots", "wave", "glow"],
    "elegant-serif": ["minimal", "glow", "ring", "minimal"],
    "bold-impact": ["stripe", "cornerblock", "glow", "minimal"],
    "modern-grotesk": ["minimal", "dots", "glow", "cornerblock"],
    "tech-condensed": ["grid", "glow", "minimal", "grid"],
    # No grid: a literal tech-lattice pattern is the cybersecurity-vendor
    # cliche this brand explicitly avoids. Quiet glow and negative space
    # read as confidence instead.
    "trust-editorial": ["glow", "minimal", "ring", "glow"],
}


def _bg_html(variant, accent, accent2):
    if variant == "blobs":
        return ('<div class="bg">'
                f'<div class="blob" style="width:460px;height:460px;top:-150px;'
                f'right:-110px;background:{accent};opacity:.09;"></div>'
                f'<div class="blob" style="width:300px;height:300px;bottom:-120px;'
                f'left:-110px;background:{accent2};opacity:.08;"></div></div>')
    if variant == "sunwash":
        return '<div class="bg"><div class="sunwash"></div></div>'
    if variant == "glow":
        return '<div class="bg"><div class="glow"></div></div>'
    if variant == "dots":
        return '<div class="bg"><div class="dots"></div></div>'
    if variant == "grid":
        return '<div class="bg"><div class="grid"></div></div>'
    if variant == "stripe":
        return '<div class="bg"><div class="stripe"></div></div>'
    if variant == "wave":
        return '<div class="bg"><div class="wave"></div></div>'
    if variant == "ring":
        return ('<div class="bg"><div class="ring" style="width:620px;height:620px;'
                'top:-220px;right:-180px;"></div></div>')
    if variant == "cornerblock":
        return '<div class="bg"><div class="cornerblock"></div></div>'
    return ""  # minimal


# --- layouts (return inner #card HTML) --------------------------------------
def _hero(ctx):
    return f"""<div class="pad center-v">
      {ctx['motif']}{_kicker_html(ctx['d'], ctx['kicker'])}
      <div class="head" style="font-size:{ctx['hsize']}px">{ctx['headline']}</div>
      <div class="rule"></div>
      <div class="sub">{ctx['lead']}</div>
      {ctx['footer']}</div>"""


def _centered(ctx):
    return f"""<div class="pad center-v" style="align-items:center;text-align:center;">
      {ctx['motif']}
      <div class="kicker {_kcls(ctx['d'])}">{_esc(ctx['kicker'])}</div>
      <div class="head" style="font-size:{ctx['hsize']}px">{ctx['headline']}</div>
      <div class="rule" style="margin-left:auto;margin-right:auto;"></div>
      <div class="sub" style="max-width:82%;">{ctx['lead']}</div>
      {ctx['footer']}</div>"""


def _corner(ctx):
    return f"""<div class="pad" style="justify-content:flex-end;padding-bottom:{ctx['pb_corner']};">
      {ctx['motif']}
      <div style="position:absolute;top:104px;left:96px;">{_kicker_html(ctx['d'], ctx['kicker'])}</div>
      <div class="head" style="font-size:{ctx['hsize']}px">{ctx['headline']}</div>
      <div class="rule"></div>
      <div class="sub">{ctx['lead']}</div>
      {ctx['footer']}</div>"""


def _frame(ctx):
    return f"""<div class="frame-bg"></div>
      <div class="frame-card">
        {_kicker_html(ctx['d'], ctx['kicker'])}
        <div class="head" style="font-size:{min(ctx['hsize'],80)}px">{ctx['headline']}</div>
        <div class="rule"></div>
        <div class="sub" style="max-width:92%;">{ctx['lead']}</div>
      </div>
      {ctx['footer_frame']}"""


def _bold_color(ctx):
    a = ctx['accent']
    return f"""<div id="paint" style="position:absolute;inset:0;
        background:linear-gradient(150deg,{a},{_darken(a,0.72)});"></div>
      <div class="pad center-v on-photo" style="color:#fff;">
        <div class="kicker" style="color:#fff;letter-spacing:.14em;">{_esc(ctx['kicker'])}</div>
        <div class="head" style="font-size:{ctx['hsize']}px;color:#fff;">{ctx['headline']}</div>
        <div class="rule" style="background:#fff;"></div>
        <div class="sub" style="color:#eef4fa;">{ctx['lead']}</div>
        {ctx['footer']}</div>"""


def _quote(ctx):
    # A large-statement layout with a bold accent bar (no decorative quote glyph
    # -- the copy is rarely an actual quotation, so the mark looked random).
    return f"""<div class="pad center-v" style="padding-left:130px;">
      {ctx['motif']}
      <div style="position:absolute;left:96px;top:50%;transform:translateY(-50%);
        width:10px;height:300px;border-radius:6px;background:{ctx['accent']};"></div>
      {_kicker_html(ctx['d'], ctx['kicker'])}
      <div class="head" style="font-size:{min(ctx['hsize'],80)}px;">{ctx['headline']}</div>
      <div class="rule"></div>
      <div class="sub">{ctx['lead']}</div>
      {ctx['footer']}</div>"""


def _stat(ctx):
    stat = ctx['stat'] or ""
    unit = ctx.get('stat_unit') or ""
    unit_html = f'<div class="statunit">{_esc(unit)}</div>' if unit else ""
    return f"""<div class="pad center-v">
      {ctx['motif']}{_kicker_html(ctx['d'], ctx['kicker'])}
      <div class="statrow"><div class="statnum">{_esc(stat)}</div>{unit_html}</div>
      <div class="head" style="font-size:58px;margin-top:14px;">{ctx['headline']}</div>
      <div class="rule"></div>
      <div class="sub">{ctx['lead']}</div>
      {ctx['footer']}</div>"""


def _checklist(ctx):
    rows = "".join(
        f'<div class="numrow"><div class="numbadge">{i}</div>'
        f'<div class="numtext">{_esc(it)}</div></div>'
        for i, it in enumerate(ctx['items'][:4], 1))
    return f"""<div class="pad center-v">
      {ctx['motif']}{_kicker_html(ctx['d'], ctx['kicker'])}
      <div class="head" style="font-size:56px;">{ctx['headline']}</div>
      <div class="numlist">{rows}</div>
      {ctx['footer']}</div>"""


def _photo_full(ctx):
    return f"""<div class="photo" style="background-image:url('{ctx['photo']}');"></div>
      <div class="scrim"></div>
      <div class="pad on-photo" style="justify-content:flex-end;padding-bottom:{ctx['pb_photo']};">
        {_kicker_html(ctx['d'], ctx['kicker'])}
        <div class="head" style="font-size:{ctx['hsize']}px;">{ctx['headline']}</div>
        <div class="sub" style="margin-top:22px;">{ctx['lead']}</div>
      </div>
      {ctx['footer_photo_block']}"""


def _photo_top(ctx):
    return f"""<div class="photo" style="top:0;bottom:auto;height:560px;
        background-image:url('{ctx['photo']}');"></div>
      <div class="band">
        {_kicker_html(ctx['d'], ctx['kicker'])}
        <div class="head" style="font-size:{min(ctx['hsize'],64)}px;">{ctx['headline']}</div>
        <div class="rule"></div>
        <div class="sub">{ctx['lead']}</div>
      </div>
      {ctx['footer_frame']}"""


def _photo_side(ctx):
    # An off-centre split with a soft shadow at the join. An exact 540px halving
    # with a hard edge read as two rectangles pasted together.
    return f"""<div class="photo" style="width:600px;background-image:url('{ctx['photo']}');"></div>
      <div style="position:absolute;left:600px;top:0;bottom:0;width:56px;
        background:linear-gradient(90deg,rgba(9,20,18,.16),rgba(9,20,18,0));"></div>
      <div style="position:absolute;left:600px;right:0;top:0;bottom:0;padding:104px 64px;
        display:flex;flex-direction:column;justify-content:center;">
        {_kicker_html(ctx['d'], ctx['kicker'])}
        <div class="head" style="font-size:{min(ctx['hsize'],58)}px;">{ctx['headline']}</div>
        <div class="rule"></div>
        <div class="sub" style="max-width:100%;font-size:31px;">{ctx['lead']}</div>
      </div>
      {ctx['footer_narrow']}"""


def _photo_card(ctx):
    return f"""<div class="photo" style="background-image:url('{ctx['photo']}');"></div>
      <div class="card-panel">
        {_kicker_html(ctx['d'], ctx['kicker'])}
        <div class="head" style="font-size:{min(ctx['hsize'],58)}px;">{ctx['headline']}</div>
        <div class="rule"></div>
        <div class="sub" style="max-width:100%;">{ctx['lead']}</div>
        {ctx['panel_foot']}
      </div>"""


_LAYOUTS = {
    "hero": _hero, "centered": _centered, "corner": _corner, "frame": _frame,
    "bold-color": _bold_color, "quote": _quote, "stat": _stat,
    "checklist": _checklist, "photo-full": _photo_full, "photo-top": _photo_top,
    "photo-side": _photo_side, "photo-card": _photo_card,
}
_PHOTO_LAYOUTS = {"photo-full", "photo-top", "photo-side", "photo-card"}


def _kcls(d):
    return {"pill": "k-pill", "tab": "k-tab", "plain": "k-plain",
            "underline": "k-underline"}.get(d["kicker"], "k-pill")


def _hsize(headline_text):
    # Short/medium headlines run bigger than before: on a 1080-square canvas a
    # modest headline plus one short sentence of lead left huge dead margins
    # above and below the centered block. Bigger type makes the same short
    # copy read as a confident, deliberate statement instead of unfinished.
    n = len(headline_text)
    if n <= 16:
        return 112
    if n <= 26:
        return 100
    if n <= 40:
        return 84
    return 68


# --- entry point -------------------------------------------------------------
def render_card(item, out_path, photo_path=None, avoid=None, seed=None, layout=None):
    """Render one post card. `layout` forces a specific layout (previews/tests);
    normally the layout is chosen from the design's pool."""
    import htmlrender

    design = _design_id()
    d = _DESIGNS[design]
    acct = tenants.account()
    _a1, _a2 = _pick_accents(acct.get("accent"), acct.get("accent2"))
    accent = _ui_accent(_a1)
    accent2 = _ui_accent(_a2)
    mood = d["mood"]
    domain = tenants.domain()
    logo_uri = _logo_data_uri()
    wm = (tenants.account().get("name") or "").strip()
    photo = _photo_uri(photo_path)

    headline = _esc(item.get("image_headline") or "")
    kicker = item.get("image_kicker") or ""
    lead = _esc(_lead(item.get("post_text", "")))
    items = _list_items(item.get("post_text", ""))
    stat = _first_stat(item.get("post_text", ""), item.get("image_headline", ""))
    stat_unit = _stat_unit(item.get("post_text", ""), item.get("image_headline", ""), stat)

    rng = random.Random(seed)
    pool = []
    seen = set()
    for t in _POOLS.get(design, _BASE):
        if t in seen:
            continue
        seen.add(t)
        if t in _PHOTO_LAYOUTS and not photo:
            continue
        if t == "checklist" and not items:
            continue
        if t == "stat" and not stat:
            continue
        pool.append(t)
    if not pool:
        pool = ["hero"]
    # A single short sentence of lead copy leaves the text-only layouts mostly
    # empty on a 1080-square canvas -- kicker, headline and one line of lead
    # centered in a square leaves huge dead margins above and below. When a
    # photo is available, prefer the layouts that actually use it: they fill
    # the frame properly, and photo posts draw more engagement anyway.
    _SPARSE = {"hero", "centered", "corner", "quote", "frame", "bold-color"}
    if photo and len(lead) < 90:
        rich = [t for t in pool if t not in _SPARSE]
        if rich:
            pool = rich
    fresh = [t for t in pool if t not in (avoid or set())]
    if layout and layout in _LAYOUTS:
        if layout in _PHOTO_LAYOUTS and not photo:
            layout = rng.choice(fresh or pool)
    else:
        layout = rng.choice(fresh or pool)

    theme_css, d = _theme(design, accent, accent2, mood)
    # Designs can drop the brand footer entirely. The caption already carries the
    # link, so a logo + domain lockup is duplicated furniture that eats the
    # bottom third of the canvas.
    show_foot = d.get("foot", True)
    if show_foot:
        badge = _logo_is_badge()
        footer = _footer_html(logo_uri, domain, wm, badge)
        _lock = _lockup_html(logo_uri, wm, badge)
        _inner = _lock + f'<div class="dom">{_esc(domain)}</div>'
        footer_frame = f'<div class="footer">{_inner}</div>'
        footer_narrow = f'<div class="footer narrow" style="left:664px;right:56px;">{_inner}</div>'
        footer_photo_block = f'<div class="footer on-photo-f">{_inner}</div>'
        panel_foot = f'<div class="panel-foot">{_inner}</div>'
    else:
        footer = footer_frame = footer_narrow = footer_photo_block = panel_foot = ""

    bg_set = _BG_SETS.get(design, ["glow", "minimal"])
    bg_variant = rng.choice(bg_set)
    ctx = dict(d=d, kicker=kicker, headline=headline, lead=lead, items=items,
               stat=stat, stat_unit=stat_unit, accent=accent, accent2=accent2, photo=photo,
               footer=footer, footer_frame=footer_frame, footer_narrow=footer_narrow,
               footer_photo_block=footer_photo_block, panel_foot=panel_foot,
               pb_corner="158px" if show_foot else "96px",
               pb_photo="200px" if show_foot else "118px",
               motif=_bg_html(bg_variant, accent, accent2), hsize=_hsize(headline))

    inner = _LAYOUTS[layout](ctx)
    grain = '<div class="grain"></div>' if mood == "dark" else ""
    _cls = ([] if not d.get("sweep") else ["d-sweep"]) + ([] if show_foot else ["nofoot"])
    card_cls = f" class='{' '.join(_cls)}'" if _cls else ""
    doc = ("<!doctype html><html><head><meta charset='utf-8'><style>"
           + _fontfaces() + theme_css + "</style></head><body><div id='card'"
           + card_cls + ">" + inner + grain + "</div></body></html>")
    htmlrender.render_html_to_png(doc, out_path)
    logger.info("Rendered %s card (design=%s) -> %s", layout, design, out_path)
    return layout
