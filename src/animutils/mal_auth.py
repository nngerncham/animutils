"""MyAnimeList OAuth2 + PKCE flow with on-disk token persistence."""

from __future__ import annotations

import json
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

AUTH_URL = "https://myanimelist.net/v1/oauth2/authorize"
TOKEN_URL = "https://myanimelist.net/v1/oauth2/token"
TOKEN_PATH = Path("tokens/mal_token.json")
REFRESH_LEEWAY_SEC = 60


class _CallbackHandler(BaseHTTPRequestHandler):
    code: str | None = None
    error: str | None = None

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != urlparse(_CallbackHandler.expected_path).path:
            self.send_response(404)
            self.end_headers()
            return
        params = parse_qs(parsed.query)
        if "code" in params:
            _CallbackHandler.code = params["code"][0]
            body = b"MAL auth complete. You can close this tab."
        else:
            _CallbackHandler.error = params.get("error", ["unknown"])[0]
            body = f"MAL auth failed: {_CallbackHandler.error}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):  # silence
        pass


def _wait_for_code(redirect_uri: str) -> str:
    parsed = urlparse(redirect_uri)
    _CallbackHandler.expected_path = redirect_uri
    _CallbackHandler.code = None
    _CallbackHandler.error = None
    server = HTTPServer((parsed.hostname or "localhost", parsed.port or 8765), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        while _CallbackHandler.code is None and _CallbackHandler.error is None:
            time.sleep(0.2)
    finally:
        server.shutdown()
    if _CallbackHandler.error:
        raise RuntimeError(f"MAL auth error: {_CallbackHandler.error}")
    return _CallbackHandler.code  # type: ignore[return-value]


def _save_token(data: dict) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {**data, "expires_at": time.time() + int(data["expires_in"])}
    TOKEN_PATH.write_text(json.dumps(data, indent=2))


def load_token() -> dict | None:
    if not TOKEN_PATH.exists():
        return None
    return json.loads(TOKEN_PATH.read_text())


def clear_token() -> None:
    TOKEN_PATH.unlink(missing_ok=True)


def has_token() -> bool:
    return TOKEN_PATH.exists()


def run_authorization_code_flow(client_id: str, client_secret: str, redirect_uri: str) -> dict:
    code_verifier = secrets.token_urlsafe(64)[:128]
    state = secrets.token_urlsafe(16)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "code_challenge": code_verifier,
        "code_challenge_method": "plain",
        "state": state,
        "redirect_uri": redirect_uri,
    }
    url = f"{AUTH_URL}?{urlencode(params)}"
    print(f"Open this URL to authorize MyAnimeList:\n  {url}")
    webbrowser.open(url)
    code = _wait_for_code(redirect_uri)

    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json()
    _save_token(token)
    return token


def _refresh(client_id: str, client_secret: str, refresh_token: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json()
    _save_token(token)
    return token


def get_access_token(client_id: str, client_secret: str) -> str:
    """Return a valid access token, refreshing on disk if needed.

    Requires a token already saved on disk (run `run_authorization_code_flow`
    once from the UI first).
    """
    token = load_token()
    if token is None:
        raise RuntimeError("MyAnimeList not authenticated yet — run the auth flow first.")
    if token.get("expires_at", 0) - REFRESH_LEEWAY_SEC < time.time():
        token = _refresh(client_id, client_secret, token["refresh_token"])
    return token["access_token"]
