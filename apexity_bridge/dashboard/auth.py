"""Dashboard security: session auth, CSRF, origin checks, login throttling.

The dashboard is localhost-only by default. Even then, browsers are a vector
(other apps/tabs could POST to 127.0.0.1), so every state-changing request
requires a session + CSRF token + same-host Origin/Referer.
"""
from __future__ import annotations

import functools
import logging
import secrets as secrets_mod
import time

from flask import abort, jsonify, redirect, request, session, url_for

from ..security.passwords import verify_password

log = logging.getLogger("apexity_bridge.dashboard.auth")

MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 60
_failed_logins: dict[str, list[float]] = {}

SETTINGS_PASSWORD_HASH = "dashboard_admin_password_hash"
SETTINGS_USERNAME = "dashboard_admin_username"
SETTINGS_SECRET_KEY = "dashboard_secret_key"


def ensure_secret_key(app, settings_repo) -> None:
    key = settings_repo.get(SETTINGS_SECRET_KEY)
    if not key:
        key = secrets_mod.token_hex(32)
        settings_repo.set(SETTINGS_SECRET_KEY, key, is_secret=True)
    app.secret_key = key


def admin_username(settings_repo) -> str:
    return settings_repo.get(SETTINGS_USERNAME, "admin")


def password_is_configured(settings_repo) -> bool:
    return bool(settings_repo.get(SETTINGS_PASSWORD_HASH))


def check_credentials(settings_repo, username: str, password: str) -> bool:
    stored = settings_repo.get(SETTINGS_PASSWORD_HASH)
    if not stored:
        return False
    if username != admin_username(settings_repo):
        return False
    return verify_password(password, stored)


def _throttle_key() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "local")


def login_allowed() -> tuple[bool, int]:
    """(allowed, seconds_remaining) — lockout after repeated failures."""
    fails = [t for t in _failed_logins.get(_throttle_key(), [])
             if time.time() - t < LOCKOUT_SECONDS]
    _failed_logins[_throttle_key()] = fails
    if len(fails) >= MAX_FAILED_LOGINS:
        remaining = int(LOCKOUT_SECONDS - (time.time() - fails[0])) or 1
        return False, remaining
    return True, 0


def record_login_failure() -> None:
    _failed_logins.setdefault(_throttle_key(), []).append(time.time())


def clear_login_failures() -> None:
    _failed_logins.pop(_throttle_key(), None)


# -------------------------------------------------------------------------- --
def login_required(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "authentication required"}), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def csrf_protect(app) -> None:
    """before_request guard for every mutating method."""
    SAFE = {"GET", "HEAD", "OPTIONS"}

    @app.before_request
    def _guard():
        if request.method in SAFE:
            # issue a per-session token
            if session.get("admin") and "csrf_token" not in session:
                session["csrf_token"] = secrets_mod.token_hex(32)
            return None
        if request.endpoint in ("login", "setup", "static"):
            return None
        if not session.get("admin"):
            return jsonify({"error": "authentication required"}), 401

        # Origin/Referer must be same-host (blocks cross-site form posts).
        host = request.host
        for header in ("Origin", "Referer"):
            value = request.headers.get(header)
            if value:
                from urllib.parse import urlparse
                if urlparse(value).netloc != host:
                    log.warning("blocked cross-origin %s from %s", request.path, value)
                    abort(403, description="cross-origin request refused")
                break

        token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
        if not token or token != session.get("csrf_token"):
            abort(403, description="invalid CSRF token")
        return None


def current_actor() -> str:
    return session.get("admin") or "unknown"
