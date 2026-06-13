"""MyAnimeList OAuth2 + PKCE flow with on-disk token persistence."""

from __future__ import annotations

import json
import secrets
import socket
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

AUTH_URL = "https://myanimelist.net/v1/oauth2/authorize"
TOKEN_URL = "https://myanimelist.net/v1/oauth2/token"
TOKEN_PATH = Path("tokens/mal_token.json")
REFRESH_LEEWAY_SEC = 60


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if "code" in params:
            self.server._code = params["code"][0]
            body = b"MAL auth complete. You can close this tab."
        else:
            self.server._error = params.get("error", ["unknown"])[0]
            body = f"MAL auth failed: {self.server._error}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)
        self.server._done.set()

    def log_message(self, *_):  # silence
        pass


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


def start_auth_flow(
    client_id: str, client_secret: str, redirect_uri: str
) -> tuple[str, Callable[[], None]]:
    """Return (auth_url, complete_fn).

    Display auth_url in the UI, then call complete_fn() to block until the
    user completes the browser flow. Saves the token on success.
    """
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
    auth_url = f"{AUTH_URL}?{urlencode(params)}"

    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8765
    server = HTTPServer((host, port), _CallbackHandler)
    server._code = None  # type: ignore[attr-defined]
    server._error = None  # type: ignore[attr-defined]
    server._done = threading.Event()  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def complete() -> None:
        server._done.wait()
        server.shutdown()
        if server._error:
            raise RuntimeError(f"MAL auth error: {server._error}")
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": server._code,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            timeout=30,
        )
        resp.raise_for_status()
        _save_token(resp.json())

    return auth_url, complete


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
    """Return a valid access token, refreshing on disk if needed."""
    token = load_token()
    if token is None:
        raise RuntimeError("MyAnimeList not authenticated yet — run the auth flow first.")
    if token.get("expires_at", 0) - REFRESH_LEEWAY_SEC < time.time():
        token = _refresh(client_id, client_secret, token["refresh_token"])
    return token["access_token"]
