"""
Login: one shared password (APP_PASSWORD), then a random session token in a
cookie. Everything except the login page requires a valid session.

Sessions live in this process's memory, so nothing about a login is saved to
disk and restarting the server logs everyone out. Run a single server process:
a second worker would not know about the first one's sessions.
"""

import os
import secrets
import time

from fastapi import Request
from fastapi.responses import RedirectResponse

COOKIE_NAME = "session"
SESSION_IDLE_SECONDS = 30 * 60
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 5 * 60
MIN_PASSWORD_LENGTH = 12  # also rejects the "change_me" placeholder in .env.example
PUBLIC_PATHS = {"/login", "/static/style.css"}  # everything else needs a session

NOT_CONFIGURED = (f"Login isn't set up yet. Set APP_PASSWORD in your .env file (at least "
                  f"{MIN_PASSWORD_LENGTH} characters) and restart the server.")

_sessions = {}  # token -> time it expires
_failures = {}  # client address -> (failed attempts, time of the last one)


# ------------------------------------------------------------------
# Password checks
# ------------------------------------------------------------------

def is_configured():
    """With no usable password set, nobody can log in (the app fails closed)."""
    return len(os.getenv("APP_PASSWORD", "")) >= MIN_PASSWORD_LENGTH


def password_matches(attempt):
    return is_configured() and secrets.compare_digest(attempt.encode(), os.environ["APP_PASSWORD"].encode())


def is_locked_out(address):
    count, last = _failures.get(address, (0, 0.0))
    return count >= MAX_FAILED_LOGINS and time.monotonic() - last < LOCKOUT_SECONDS


def record_failure(address):
    now = time.monotonic()
    for stale in [a for a, (_, last) in _failures.items() if now - last >= LOCKOUT_SECONDS]:
        del _failures[stale]
    count, _ = _failures.get(address, (0, 0.0))
    _failures[address] = (count + 1, now)


def clear_failures(address):
    _failures.pop(address, None)


# ------------------------------------------------------------------
# Sessions
# ------------------------------------------------------------------

def start_session():
    now = time.monotonic()
    for expired in [t for t, expires in _sessions.items() if expires <= now]:
        del _sessions[expired]
    token = secrets.token_urlsafe(32)
    _sessions[token] = now + SESSION_IDLE_SECONDS
    return token


def session_is_valid(token):
    """True for a live session; each valid check restarts its idle timer."""
    now = time.monotonic()
    if token is None or _sessions.get(token, 0.0) <= now:
        _sessions.pop(token, None)
        return False
    _sessions[token] = now + SESSION_IDLE_SECONDS
    return True


def end_session(token):
    _sessions.pop(token, None)


def set_session_cookie(response, request, token):
    """
    A browser-session cookie that scripts can't read and other sites can't
    trigger on a form post. It's HTTPS-only unless the app is on this computer.
    """
    response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", path="/",
                        secure=request.url.hostname not in ("127.0.0.1", "localhost"))


class AuthMiddleware:
    """Sends anyone without a session to the login page, for every route and file."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] not in PUBLIC_PATHS:
            if not session_is_valid(Request(scope).cookies.get(COOKIE_NAME)):
                response = RedirectResponse("/login", status_code=303)
                return await response(scope, receive, send)
        await self.app(scope, receive, send)
