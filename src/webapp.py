"""Web review dashboard for the SkySystems USA auto-poster.

Browser UI to generate (random or custom/holiday posts), review, approve,
schedule, and publish posts -- plus a built-in scheduler that auto-publishes
scheduled posts and an optional daily auto-pilot.

Run locally:
    python src/webapp.py            # http://localhost:8080

Security: this dashboard can publish to your Facebook Page and has NO login.
Keep it on localhost or a trusted LAN. Do not expose it to the internet.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from flask import (
    Flask, abort, flash, redirect, render_template_string, request,
    send_from_directory, url_for,
)

import cards
import generate as gen
import publish as pub
import store

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CARDS_DIR = _REPO_ROOT / "data" / "cards"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("skysystems.web")

if load_dotenv is not None:
    load_dotenv(_REPO_ROOT / ".env")

# Make scheduling use the configured local time zone (TZ env, e.g.
# America/Chicago). tzset() exists on Unix only; Windows uses system time.
if hasattr(time, "tzset"):
    time.tzset()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "skysystems-local-dashboard")

# Guards every queue read-modify-write so the web routes and the background
# scheduler never corrupt the JSON files by writing at the same time.
_LOCK = threading.RLock()


DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_OPTIONS = list(zip(DAYS, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]))


def _meta_ready() -> bool:
    return bool(os.environ.get("META_PAGE_ID") and os.environ.get("META_PAGE_ACCESS_TOKEN"))


def _pop(items, item_id):
    found, rest = None, []
    for it in items:
        if it.get("id") == item_id and found is None:
            found = it
        else:
            rest.append(it)
    return found, rest


def _delete_card(item) -> None:
    cp = item.get("card_path")
    if cp:
        p = Path(cp)
        if not p.is_absolute():
            p = _REPO_ROOT / cp
        try:
            p.unlink()
        except OSError:
            pass


def _do_publish(item) -> str:
    """Publish one item and append it to history. Returns the FB post id."""
    post_id = pub.publish_post(item)
    item["status"] = "posted"
    item["posted_at"] = datetime.now(timezone.utc).isoformat()
    item["facebook_post_id"] = post_id
    store.append_history(item)
    return post_id


# --- background scheduler --------------------------------------------------

def _scheduler_tick() -> None:
    """One pass: publish any due scheduled posts, then run the daily auto-pilot."""
    if not _meta_ready():
        return
    now = datetime.now()  # local time (set TZ env on the container)
    with _LOCK:
        approved = store.read_approved()
        remaining, changed = [], False
        for it in approved:
            sched = it.get("scheduled_at")
            due = False
            if sched:
                try:
                    due = datetime.fromisoformat(sched) <= now
                except ValueError:
                    due = False
            if due:
                try:
                    pid = _do_publish(it)
                    logger.info("Scheduler published %s (fb=%s)", it["id"], pid)
                    changed = True
                    continue
                except Exception as exc:  # noqa: BLE001
                    logger.error("Scheduled publish failed for %s: %s", it["id"], exc)
            remaining.append(it)
        if changed:
            store.write_approved(remaining)

        # Daily auto-pilot: publish one approved post per configured time slot,
        # on the selected days. Multiple times per day are supported.
        s = store.read_settings()
        if s.get("auto_pilot_enabled") and DAYS[now.weekday()] in s.get("auto_pilot_days", []):
            today = now.strftime("%Y-%m-%d")
            fired = list(s.get("fired_slots", []))
            fired_set = set(fired)
            new_fired = False
            for t in s.get("auto_pilot_times", []):
                slot = f"{today} {t}"
                if slot in fired_set:
                    continue
                try:
                    hh, mm = (int(x) for x in t.split(":"))
                except ValueError:
                    continue
                if (now.hour, now.minute) < (hh, mm):
                    continue
                approved = store.read_approved()
                pool = sorted(
                    [a for a in approved if not a.get("scheduled_at")],
                    key=lambda i: i.get("generated_at", ""),
                )
                if pool:
                    item = pool[0]
                    try:
                        pid = _do_publish(item)
                        logger.info("Auto-pilot published %s at %s (fb=%s)", item["id"], t, pid)
                        store.write_approved([a for a in approved if a.get("id") != item["id"]])
                    except Exception as exc:  # noqa: BLE001
                        logger.error("Auto-pilot publish failed: %s", exc)
                else:
                    logger.info("Auto-pilot %s: nothing approved to publish.", t)
                fired.append(slot)
                fired_set.add(slot)
                new_fired = True
            if new_fired:
                s["fired_slots"] = fired[-80:]  # keep recent slots only
                store.write_settings(s)


def _scheduler_loop() -> None:
    logger.info("Background scheduler started.")
    while True:
        try:
            _scheduler_tick()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Scheduler tick error: %s", exc)
        time.sleep(60)


# --- routes ----------------------------------------------------------------

@app.route("/")
def index():
    approved = store.read_approved()
    scheduled = sorted(
        [a for a in approved if a.get("scheduled_at")],
        key=lambda i: i.get("scheduled_at", ""),
    )
    ready = [a for a in approved if not a.get("scheduled_at")]
    return render_template_string(
        TEMPLATE,
        pending=store.read_pending(),
        scheduled=scheduled,
        ready=ready,
        history=list(reversed(store.read_history()))[:12],
        settings=store.read_settings(),
        formats=gen.POST_FORMATS,
        day_options=DAY_OPTIONS,
        meta_ready=_meta_ready(),
        now_local=datetime.now().strftime("%Y-%m-%dT%H:%M"),
    )


@app.route("/card/<path:name>")
def card(name):
    if not name.endswith(".png") or "/" in name or "\\" in name:
        abort(404)
    return send_from_directory(_CARDS_DIR, name)


@app.route("/generate", methods=["POST"])
def generate():
    count = max(1, min(int(request.form.get("count", 1)), 10))
    try:
        items = gen.generate_batch(count) if count > 1 else [gen.generate_post()]
        with _LOCK:
            for item in items:
                cards.build_card(item)
                store.append_pending(item)
        flash(f"Generated {count} post(s) into review.", "ok")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Generate failed")
        flash(f"Generation failed: {exc}", "err")
    return redirect(url_for("index"))


@app.route("/generate-custom", methods=["POST"])
def generate_custom():
    topic = (request.form.get("topic") or "").strip()
    fmt_id = request.form.get("format") or ""
    if not topic:
        flash("Type what the post should be about first.", "err")
        return redirect(url_for("index"))
    try:
        fmt = gen.get_format(fmt_id) if fmt_id else None
        item = gen.generate_custom(topic, fmt=fmt)
        with _LOCK:
            cards.build_card(item)
            store.append_pending(item)
        flash("Custom post generated into review.", "ok")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Custom generate failed")
        flash(f"Custom generation failed: {exc}", "err")
    return redirect(url_for("index"))


@app.route("/approve/<item_id>", methods=["POST"])
def approve(item_id):
    with _LOCK:
        item, rest = _pop(store.read_pending(), item_id)
        if item:
            item["status"] = "approved"
            store.write_pending(rest)
            store.write_approved(store.read_approved() + [item])
            flash("Approved.", "ok")
    return redirect(url_for("index"))


@app.route("/unapprove/<item_id>", methods=["POST"])
def unapprove(item_id):
    with _LOCK:
        item, rest = _pop(store.read_approved(), item_id)
        if item:
            item.pop("scheduled_at", None)
            item["status"] = "pending"
            store.write_approved(rest)
            store.write_pending(store.read_pending() + [item])
            flash("Moved back to review.", "ok")
    return redirect(url_for("index"))


@app.route("/discard/<item_id>", methods=["POST"])
def discard(item_id):
    with _LOCK:
        item, rest = _pop(store.read_pending(), item_id)
        if item:
            store.write_pending(rest)
        else:
            item, rest = _pop(store.read_approved(), item_id)
            if item:
                store.write_approved(rest)
        if item:
            _delete_card(item)
            flash("Discarded.", "ok")
    return redirect(url_for("index"))


@app.route("/schedule/<item_id>", methods=["POST"])
def schedule(item_id):
    when = (request.form.get("when") or "").strip()
    try:
        dt = datetime.fromisoformat(when)
    except ValueError:
        flash("Pick a valid date and time.", "err")
        return redirect(url_for("index"))
    with _LOCK:
        approved = store.read_approved()
        for it in approved:
            if it.get("id") == item_id:
                it["scheduled_at"] = dt.isoformat(timespec="minutes")
                store.write_approved(approved)
                flash(f"Scheduled for {dt.strftime('%a %b %d, %I:%M %p')}.", "ok")
                break
    return redirect(url_for("index"))


@app.route("/unschedule/<item_id>", methods=["POST"])
def unschedule(item_id):
    with _LOCK:
        approved = store.read_approved()
        for it in approved:
            if it.get("id") == item_id:
                it.pop("scheduled_at", None)
                store.write_approved(approved)
                flash("Schedule cleared.", "ok")
                break
    return redirect(url_for("index"))


@app.route("/publish/<item_id>", methods=["POST"])
def publish_one(item_id):
    with _LOCK:
        item, rest = _pop(store.read_approved(), item_id)
        if not item:
            flash("That post is no longer approved.", "err")
            return redirect(url_for("index"))
        try:
            pid = _do_publish(item)
            store.write_approved(rest)
            flash(f"Published! Facebook post id {pid}", "ok")
        except pub.TokenExpiredError as exc:
            flash(f"Token problem: {exc}", "err")
        except pub.PublishError as exc:
            flash(f"Publish failed: {exc}", "err")
    return redirect(url_for("index"))


@app.route("/settings", methods=["POST"])
def settings_save():
    s = store.read_settings()
    s["auto_pilot_enabled"] = request.form.get("auto_pilot_enabled") == "on"
    s["auto_pilot_days"] = [d for d in DAYS if request.form.get("day_" + d) == "on"]
    times = []
    for i in (1, 2, 3):
        t = (request.form.get(f"time_{i}") or "").strip()
        if t:
            times.append(t)
    s["auto_pilot_times"] = sorted(set(times)) or ["09:00"]
    store.write_settings(s)
    flash("Auto-pilot settings saved.", "ok")
    return redirect(url_for("index"))


TEMPLATE = r"""
{% macro render_post(p, kind, meta_ready, now_local) %}
<div class="post">
  {% if p.card_path %}<a href="{{ url_for('card', name=p.id + '.png') }}" target="_blank" title="Click to view full size"><img src="{{ url_for('card', name=p.id + '.png') }}" alt="card"></a>{% endif %}
  <div class="meta">
    <div>
      <span class="tag">{{ p.image_kicker }}</span>
      {% if p.format %}<span class="tag b">{{ p.format }}</span>{% endif %}
      {% if p.custom_topic %}<span class="tag amber">custom</span>{% endif %}
      {% if kind == "scheduled" %}<span class="tag amber">{{ p.scheduled_at.replace("T"," ") }}</span>{% endif %}
    </div>
    <div class="cap">{{ p.caption }}</div>
    {% if kind != "history" %}<div class="body">{{ p.post_text }}</div>{% endif %}
    <div class="row">
      {% if kind == "pending" %}
        <form method="post" action="{{ url_for('approve', item_id=p.id) }}"><button class="green">Approve</button></form>
        <form method="post" action="{{ url_for('discard', item_id=p.id) }}" onsubmit="return confirm('Discard this post?')"><button class="danger">Discard</button></form>
      {% elif kind == "approved" %}
        <form method="post" action="{{ url_for('publish_one', item_id=p.id) }}"><button class="blue" {{ 'disabled' if not meta_ready }}>Publish now</button></form>
        <form method="post" action="{{ url_for('schedule', item_id=p.id) }}">
          <input type="datetime-local" name="when" value="{{ now_local }}"><button class="ghost">Schedule</button></form>
        <form method="post" action="{{ url_for('unapprove', item_id=p.id) }}"><button class="ghost">Back to review</button></form>
        <form method="post" action="{{ url_for('discard', item_id=p.id) }}" onsubmit="return confirm('Discard this post?')"><button class="danger">Discard</button></form>
      {% elif kind == "scheduled" %}
        <form method="post" action="{{ url_for('publish_one', item_id=p.id) }}"><button class="blue" {{ 'disabled' if not meta_ready }}>Publish now</button></form>
        <form method="post" action="{{ url_for('unschedule', item_id=p.id) }}"><button class="ghost">Cancel schedule</button></form>
        <form method="post" action="{{ url_for('discard', item_id=p.id) }}" onsubmit="return confirm('Discard this post?')"><button class="danger">Discard</button></form>
      {% elif kind == "history" %}
        {% if p.facebook_post_id %}<a class="fb" target="_blank" href="https://www.facebook.com/{{ p.facebook_post_id }}">View on Facebook &rarr;</a>{% endif %}
      {% endif %}
    </div>
  </div>
</div>
{% endmacro %}
<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>SkySystems Poster</title>
<style>
  :root{--bg:#0a0e14;--panel:#0f1722;--card:#111c29;--green:#34ae4c;--blue:#2b6cc4;
        --muted:#9fb3c8;--line:#1d2a3a;--text:#eef3f9;}
  *{box-sizing:border-box;} body{margin:0;background:var(--bg);color:var(--text);
    font-family:'Segoe UI',system-ui,sans-serif;}
  header{display:flex;align-items:center;justify-content:space-between;padding:16px 30px;
    border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:10;}
  .brand{font-size:22px;font-weight:800;} .brand .g{color:var(--green);}
  .brand small{color:var(--muted);font-weight:400;font-size:13px;margin-left:8px;}
  .wrap{max-width:1240px;margin:0 auto;padding:22px 30px 70px;}
  h2{font-size:13px;text-transform:uppercase;letter-spacing:.16em;color:var(--muted);
    margin:30px 0 14px;border-bottom:1px solid var(--line);padding-bottom:8px;}
  .panels{display:grid;grid-template-columns:1fr 1fr;gap:16px;}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;}
  .panel h3{margin:0 0 12px;font-size:15px;}
  label{display:block;font-size:12px;color:var(--muted);margin:10px 0 5px;text-transform:uppercase;letter-spacing:.06em;}
  input,select,textarea{width:100%;background:#0b121c;border:1px solid var(--line);color:#fff;
    padding:9px 11px;border-radius:7px;font-size:14px;font-family:inherit;}
  textarea{min-height:74px;resize:vertical;}
  .inline{display:flex;gap:10px;align-items:flex-end;}
  .inline>div{flex:1;}
  button{cursor:pointer;border:none;border-radius:7px;padding:10px 16px;font-weight:700;font-size:13px;}
  .green{background:var(--green);color:#06210d;} .blue{background:var(--blue);color:#fff;}
  .ghost{background:transparent;border:1px solid var(--line);color:var(--muted);}
  .danger{background:transparent;border:1px solid #7a2230;color:#ff8a9b;}
  .post{display:flex;gap:22px;background:var(--card);border:1px solid var(--line);
    border-radius:12px;padding:18px;margin-bottom:18px;}
  .post img{width:520px;min-width:520px;max-width:48%;height:auto;border-radius:9px;border:1px solid var(--line);display:block;cursor:zoom-in;transition:opacity .12s;}
  .post a:hover img{opacity:.85;}
  .meta{flex:1;min-width:0;}
  .tag{display:inline-block;font-size:11px;text-transform:uppercase;letter-spacing:.07em;
    color:var(--green);border:1px solid var(--green);border-radius:99px;padding:2px 10px;margin:0 6px 6px 0;}
  .tag.b{color:var(--blue);border-color:var(--blue);}
  .tag.amber{color:#e8c777;border-color:#7a611e;}
  .cap{color:var(--muted);font-size:13.5px;white-space:pre-wrap;margin:8px 0;line-height:1.5;}
  .body{font-size:15px;white-space:pre-wrap;line-height:1.6;margin-top:6px;}
  .row{margin-top:16px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;}
  .row form{display:inline-flex;gap:6px;align-items:center;margin:0;}
  .row input[type=datetime-local]{width:auto;padding:7px 9px;font-size:12px;}
  .empty{color:var(--muted);font-style:italic;padding:6px 0;}
  .flash{padding:11px 16px;border-radius:8px;margin-bottom:12px;font-size:14px;}
  .flash.ok{background:#10261a;border:1px solid #1f5b33;color:#86e0a3;}
  .flash.err{background:#2a1115;border:1px solid #6a2230;color:#ff9aa8;}
  .warn{background:#241c0c;border:1px solid #5a4516;color:#e8c777;padding:11px 16px;border-radius:8px;margin-bottom:12px;font-size:14px;}
  .toggle{display:flex;align-items:center;gap:8px;margin-top:10px;}
  .toggle input{width:auto;}
  .days{display:flex;gap:6px;flex-wrap:wrap;margin-top:4px;}
  .daybox{display:flex;align-items:center;gap:5px;background:#0b121c;border:1px solid var(--line);
    border-radius:7px;padding:7px 11px;font-size:13px;color:var(--text);text-transform:none;
    letter-spacing:0;margin:0;cursor:pointer;}
  .daybox input{width:auto;}
  a.fb{color:var(--blue);font-size:13px;text-decoration:none;}
  @media(max-width:900px){.panels{grid-template-columns:1fr;}.post{flex-direction:column;}.post img{width:100%;min-width:0;max-width:100%;}}
</style></head><body>
<header>
  <div class="brand">Sky<span class="g">Systems</span><small>Post Studio</small></div>
</header>
<div class="wrap">
  {% with msgs = get_flashed_messages(with_categories=true) %}
    {% for cat,m in msgs %}<div class="flash {{cat}}">{{m}}</div>{% endfor %}
  {% endwith %}
  {% if not meta_ready %}<div class="warn">META_PAGE_ID / META_PAGE_ACCESS_TOKEN not set - you can generate, review, and schedule, but publishing is disabled.</div>{% endif %}

  <div class="panels">
    <div class="panel">
      <h3>Create a custom post</h3>
      <form method="post" action="{{ url_for('generate_custom') }}">
        <label>What should this post be about?</label>
        <textarea name="topic" placeholder="e.g. Memorial Day: we're honoring those who served. Office closed Monday. -- or -- Limited summer offer: free security assessment for new Austin clients booked in June."></textarea>
        <div class="inline" style="margin-top:10px;">
          <div>
            <label>Style (optional)</label>
            <select name="format">
              <option value="">Auto (let it pick)</option>
              {% for f in formats %}<option value="{{f.id}}">{{f.id}}</option>{% endfor %}
            </select>
          </div>
          <button class="green" type="submit">Create custom post</button>
        </div>
      </form>
    </div>

    <div class="panel">
      <h3>Generate from the theme rotation</h3>
      <form method="post" action="{{ url_for('generate') }}">
        <label>How many posts?</label>
        <div class="inline">
          <div><input type="number" name="count" value="1" min="1" max="10"></div>
          <button class="green" type="submit">Generate</button>
        </div>
      </form>
      <h3 style="margin-top:22px;">Auto-pilot schedule</h3>
      <form method="post" action="{{ url_for('settings_save') }}">
        <div class="toggle">
          <input type="checkbox" id="ap" name="auto_pilot_enabled" {{ 'checked' if settings.auto_pilot_enabled }}>
          <label for="ap" style="margin:0;text-transform:none;letter-spacing:0;color:var(--text);">Auto-publish approved posts on this schedule</label>
        </div>
        <label>Post on these days</label>
        <div class="days">
          {% for d, lbl in day_options %}
          <label class="daybox"><input type="checkbox" name="day_{{ d }}" {{ 'checked' if d in settings.auto_pilot_days }}> {{ lbl }}</label>
          {% endfor %}
        </div>
        <label>At these times (one post per time slot; leave blank to skip)</label>
        <div class="inline">
          <div><input type="time" name="time_1" value="{{ settings.auto_pilot_times[0] if settings.auto_pilot_times|length > 0 else '' }}"></div>
          <div><input type="time" name="time_2" value="{{ settings.auto_pilot_times[1] if settings.auto_pilot_times|length > 1 else '' }}"></div>
          <div><input type="time" name="time_3" value="{{ settings.auto_pilot_times[2] if settings.auto_pilot_times|length > 2 else '' }}"></div>
          <button class="ghost" type="submit">Save</button>
        </div>
        <div style="color:var(--muted);font-size:12px;margin-top:8px;">Tip: set two times (e.g. 09:00 and 15:00) to post twice a day. It pulls from your approved queue, so keep a few approved.</div>
      </form>
    </div>
  </div>

  {% if scheduled %}
  <h2>Scheduled ({{ scheduled|length }})</h2>
  {% for p in scheduled %}{{ render_post(p, "scheduled", meta_ready, now_local) }}{% endfor %}
  {% endif %}

  <h2>Approved &middot; ready to publish ({{ ready|length }})</h2>
  {% if not ready %}<div class="empty">Nothing waiting. Approve posts below or create one above.</div>{% endif %}
  {% for p in ready %}{{ render_post(p, "approved", meta_ready, now_local) }}{% endfor %}

  <h2>In review ({{ pending|length }})</h2>
  {% if not pending %}<div class="empty">Queue is empty.</div>{% endif %}
  {% for p in pending %}{{ render_post(p, "pending", meta_ready, now_local) }}{% endfor %}

  <h2>Recently posted ({{ history|length }})</h2>
  {% if not history %}<div class="empty">No posts published yet.</div>{% endif %}
  {% for p in history %}{{ render_post(p, "history", meta_ready, now_local) }}{% endfor %}
</div>
</body></html>
"""


def _start_scheduler():
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()


if __name__ == "__main__":
    _start_scheduler()
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
