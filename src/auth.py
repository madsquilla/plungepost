"""Password gate for PlungePost.

The dashboard and the phone app both expose everything: your accounts, your
Facebook tokens, and an Anthropic key that costs money to use. That was fine
when this only ever listened on a LAN. Hosted on the public internet it is
not, so every route sits behind a single shared password.

    PLUNGEPOST_PASSWORD=some-long-passphrase

Set it and the whole app requires a login (one that sticks for 30 days, so
the phone app asks once). Leave it unset and nothing changes: the app is open,
exactly as it always was, which is fine on a trusted LAN and not fine
anywhere else. Set a password before you tunnel it, proxy it, or forward a
port to it.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import logging
import os
import threading
import time

from flask import (
    Blueprint, Response, jsonify, redirect, render_template_string, request,
    session, url_for,
)

logger = logging.getLogger("plungepost.auth")

SESSION_KEY = "authed"
SESSION_DAYS = 30

# Endpoints reachable without a login. The manifest, worker and icons stay
# public so the app still installs and shows its icon at the login screen;
# none of them expose any account data.
_PUBLIC = {"auth.login", "pwa.manifest", "pwa.service_worker", "pwa.icon"}

# Login throttle: a shared password on the open internet needs to not be
# brute-forceable. Failures are counted per client IP, in memory.
_MAX_FAILURES = 8
_LOCKOUT_SECONDS = 300
_failures: dict[str, list] = {}
_failures_lock = threading.Lock()


def password() -> str:
    return os.environ.get("PLUNGEPOST_PASSWORD", "").strip()


def enabled() -> bool:
    return bool(password())


def secret_key() -> str:
    """A stable session secret.

    FLASK_SECRET wins when set. Otherwise it is derived from the password, so
    a hosted instance gets an unguessable secret with one env var instead of
    two, and sessions survive restarts. Changing the password logs everyone
    out, which is what you want anyway.
    """
    explicit = os.environ.get("FLASK_SECRET", "").strip()
    if explicit:
        return explicit
    pw = password()
    if pw:
        return hashlib.sha256(("plungepost-session:" + pw).encode()).hexdigest()
    return "plungepost-local-dashboard"


def _client_ip() -> str:
    # Fly/most proxies put the real client first in X-Forwarded-For.
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "?"


def _locked_out(ip: str) -> int:
    """Seconds remaining on this IP's lockout, or 0."""
    now = time.time()
    with _failures_lock:
        recent = [t for t in _failures.get(ip, []) if now - t < _LOCKOUT_SECONDS]
        _failures[ip] = recent
        if len(recent) >= _MAX_FAILURES:
            return int(_LOCKOUT_SECONDS - (now - recent[0])) + 1
    return 0


def _record_failure(ip: str) -> None:
    with _failures_lock:
        _failures.setdefault(ip, []).append(time.time())


def _clear_failures(ip: str) -> None:
    with _failures_lock:
        _failures.pop(ip, None)


def is_authed() -> bool:
    if not enabled():
        return True          # no password configured: the gate is off
    return bool(session.get(SESSION_KEY))


def install(app) -> None:
    """Put every route behind the gate and register /login."""
    app.secret_key = secret_key()
    app.permanent_session_lifetime = datetime.timedelta(days=SESSION_DAYS)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Behind Fly/any TLS proxy the cookie must be https-only. Locally
        # (plain http) that flag would stop the cookie being stored at all.
        SESSION_COOKIE_SECURE=os.environ.get("PLUNGEPOST_HTTPS", "") == "1",
    )
    app.register_blueprint(bp)

    @app.before_request
    def _gate():
        if request.endpoint in _PUBLIC or request.endpoint == "static":
            return None
        if is_authed():
            return None
        return _unauthorized("Sign in to continue.")

    if enabled():
        logger.info("Password protection is ON.")
    else:
        logger.warning("PLUNGEPOST_PASSWORD is not set -- the dashboard and "
                       "phone app are open to anyone who can reach this port. "
                       "Fine on a trusted LAN; set a password before exposing "
                       "it any further.")


def _unauthorized(message: str):
    """JSON for the app's API, a redirect to the login screen for a browser."""
    if request.path.startswith("/api/"):
        return jsonify({"error": message, "auth_required": True}), 401
    return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))


bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if is_authed():
        return redirect(request.args.get("next") or "/app")
    if not enabled():
        return Response(render_template_string(
            _PAGE, error="", note="No password is set on this instance. Set "
            "PLUNGEPOST_PASSWORD and restart to sign in from anywhere.",
            locked=True), status=503, mimetype="text/html")

    ip = _client_ip()
    error = ""
    if request.method == "POST":
        wait = _locked_out(ip)
        if wait:
            error = f"Too many attempts. Try again in {wait // 60 + 1} minute(s)."
        elif hmac.compare_digest(request.form.get("password", ""), password()):
            session.permanent = True
            session[SESSION_KEY] = True
            _clear_failures(ip)
            logger.info("Signed in from %s", ip)
            return redirect(request.args.get("next") or "/app")
        else:
            _record_failure(ip)
            logger.warning("Failed sign-in from %s", ip)
            error = "That password is not right."
    return Response(render_template_string(_PAGE, error=error, note="", locked=False),
                    status=401 if error else 200, mimetype="text/html")


@bp.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


_PAGE = """<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>PlungePost</title>
<link rel="icon" href="/icons/icon-192.png">
<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png">
<meta name="theme-color" content="#0e2740">
<style>
 *{box-sizing:border-box;}
 body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;
   background:radial-gradient(1000px 500px at 50% -10%,#17456f 0%,transparent 60%),#0b1a2b;
   color:#eaf1f8;font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;}
 .box{width:100%;max-width:360px;text-align:center;}
 img{width:64px;height:64px;border-radius:16px;margin-bottom:18px;}
 h1{font-size:21px;margin:0 0 6px;letter-spacing:-.01em;}
 p.sub{color:#9db0c4;margin:0 0 24px;font-size:14.5px;}
 input{width:100%;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.18);
   color:#eaf1f8;border-radius:13px;padding:15px 16px;font-size:16px;font-family:inherit;}
 input:focus{outline:none;border-color:#2faa46;box-shadow:0 0 0 3px rgba(47,170,70,.22);}
 button{width:100%;margin-top:12px;border:none;border-radius:13px;padding:16px;
   background:#2faa46;color:#fff;font-size:16px;font-weight:700;font-family:inherit;cursor:pointer;}
 button:active{background:#38c254;}
 .err{background:rgba(226,96,122,.14);border:1px solid rgba(226,96,122,.4);color:#f6b6c3;
   border-radius:12px;padding:12px 14px;font-size:14px;margin-bottom:16px;}
 .note{color:#e6b15c;font-size:14px;line-height:1.55;}
</style></head><body>
<div class="box">
  <img src="/icons/icon-192.png" alt="">
  <h1>PlungePost</h1>
  <p class="sub">Sign in to make posts.</p>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  {% if locked %}<p class="note">{{ note }}</p>
  {% else %}
  <form method="post">
    <input type="password" name="password" placeholder="Password" autocomplete="current-password"
           autofocus required>
    <button type="submit">Sign in</button>
  </form>
  {% endif %}
</div></body></html>
"""
