"""Push episode events to Google Calendar (Flamingo, popup at start)."""

from __future__ import annotations

import socket
import threading
import wsgiref.simple_server
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from animutils.schedule import DEFAULT_EPISODE_MINUTES, EpisodeAir

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
TOKEN_PATH = Path("tokens/gcal_token.json")
CLIENT_SECRET_PATH = Path("tokens/google_client_secret.json")
FLAMINGO_COLOR_ID = "4"  # Flamingo


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def has_client_secret() -> bool:
    return CLIENT_SECRET_PATH.exists()


def save_client_secret(content: bytes) -> None:
    CLIENT_SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLIENT_SECRET_PATH.write_bytes(content)


def has_token() -> bool:
    return TOKEN_PATH.exists()


def clear_token() -> None:
    TOKEN_PATH.unlink(missing_ok=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


def start_auth_flow() -> tuple[str, Callable[[], None]]:
    """Return (auth_url, complete_fn).

    Display auth_url in the UI, then call complete_fn() to block until the
    user completes the browser flow. Saves credentials on success.
    """
    if not CLIENT_SECRET_PATH.exists():
        raise RuntimeError("Google client secret JSON not found — upload it first.")

    port = _free_port()
    redirect_uri = f"http://localhost:{port}/"

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
    flow.redirect_uri = redirect_uri
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")

    received: dict[str, str] = {}
    done = threading.Event()

    class _SilentHandler(wsgiref.simple_server.WSGIRequestHandler):
        def log_message(self, *_):
            pass

    def _wsgi_app(environ, start_response):
        qs = environ.get("QUERY_STRING", "")
        path = environ.get("PATH_INFO", "/")
        received["url"] = f"http://localhost:{port}{path}?{qs}"
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        done.set()
        return [
            b"<html><body><h2>Authorization complete.</h2>"
            b"<p>You may close this tab and return to the app.</p></body></html>"
        ]

    server = wsgiref.simple_server.make_server(
        "localhost", port, _wsgi_app, handler_class=_SilentHandler
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def complete() -> None:
        done.wait()
        server.shutdown()
        # google-auth-oauthlib requires the response URL to look like HTTPS
        auth_response = received["url"].replace("http://", "https://", 1)
        flow.fetch_token(authorization_response=auth_response)
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(flow.credentials.to_json())

    return auth_url, complete


def _credentials() -> Credentials:
    if not TOKEN_PATH.exists():
        raise RuntimeError("Google Calendar not authenticated yet.")
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
        else:
            raise RuntimeError("Google credentials invalid — re-authenticate.")
    return creds


def _service():
    return build("calendar", "v3", credentials=_credentials(), cache_discovery=False)


# ---------------------------------------------------------------------------
# Event construction
# ---------------------------------------------------------------------------

def _event_body(ep: EpisodeAir) -> dict:
    start = ep.airs_at_local
    end = start + timedelta(minutes=DEFAULT_EPISODE_MINUTES)
    tz_name = str(start.tzinfo)
    return {
        "summary": f"{ep.title} · Ep {ep.episode}",
        "description": (
            f"MyAnimeList ID: {ep.anime_id}\n"
            f"Airs (JST): {ep.airs_at_jst.isoformat()}\n"
            + ("(Air time approximated — broadcast slot unknown)\n" if ep.approximate else "")
            + f"https://myanimelist.net/anime/{ep.anime_id}"
        ),
        "start": {"dateTime": start.isoformat(), "timeZone": tz_name},
        "end": {"dateTime": end.isoformat(), "timeZone": tz_name},
        "colorId": FLAMINGO_COLOR_ID,
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 0}],
        },
        "extendedProperties": {
            "private": {
                "animutils_anime_id": str(ep.anime_id),
                "animutils_episode": str(ep.episode),
            }
        },
    }


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def _existing_events(svc, calendar_id: str, anime_id: int) -> list[dict]:
    """Return full event dicts for events tagged with this anime."""
    out: list[dict] = []
    page_token = None
    while True:
        resp = (
            svc.events()
            .list(
                calendarId=calendar_id,
                privateExtendedProperty=f"animutils_anime_id={anime_id}",
                showDeleted=False,
                singleEvents=True,
                maxResults=2500,
                pageToken=page_token,
            )
            .execute()
        )
        out.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return out


def sync_episodes(
    episodes: list[EpisodeAir],
    final_counts: dict[int, int] | None = None,
    titles: dict[int, str] | None = None,
    calendar_id: str = "primary",
) -> dict[str, list[str]]:
    """Sync episodes to Google Calendar.

    - Inserts events for new (anime_id, episode) pairs.
    - Patches colorId on existing events whose color differs from FLAMINGO_COLOR_ID.
    - Deletes trailing projected episodes when MAL publishes a final episode count.
    """
    final_counts = final_counts or {}
    titles = titles or {}
    svc = _service()

    cache: dict[int, list[dict]] = {}

    def existing(anime_id: int) -> list[dict]:
        if anime_id not in cache:
            cache[anime_id] = _existing_events(svc, calendar_id, anime_id)
        return cache[anime_id]

    def ep_num(item: dict) -> int | None:
        try:
            return int(
                (item.get("extendedProperties") or {})
                .get("private", {})
                .get("animutils_episode", "")
            )
        except ValueError:
            return None

    inserted: list[str] = []
    skipped: list[str] = []
    deleted: list[str] = []
    recolored: list[str] = []

    # Trim trailing episodes whose anime now has a known final count.
    for anime_id, total in final_counts.items():
        if total <= 0:
            continue
        keep: list[dict] = []
        for item in existing(anime_id):
            n = ep_num(item)
            if n is not None and n > total:
                svc.events().delete(calendarId=calendar_id, eventId=item["id"]).execute()
                deleted.append(f"{titles.get(anime_id, anime_id)} · Ep {n}")
            else:
                keep.append(item)
        cache[anime_id] = keep

    # Recolor existing events that have a different colorId.
    for anime_id, items in cache.items():
        for item in items:
            if item.get("colorId") != FLAMINGO_COLOR_ID:
                svc.events().patch(
                    calendarId=calendar_id,
                    eventId=item["id"],
                    body={"colorId": FLAMINGO_COLOR_ID},
                ).execute()
                item["colorId"] = FLAMINGO_COLOR_ID
                n = ep_num(item)
                recolored.append(f"{titles.get(anime_id, str(anime_id))} · Ep {n}")

    def existing_ep_nums(anime_id: int) -> set[int]:
        return {n for item in existing(anime_id) if (n := ep_num(item)) is not None}

    # Insert new episodes.
    for ep in episodes:
        label = f"{ep.title} · Ep {ep.episode}"
        if ep.episode in existing_ep_nums(ep.anime_id):
            skipped.append(label)
            continue
        created = svc.events().insert(calendarId=calendar_id, body=_event_body(ep)).execute()
        cache[ep.anime_id].append(created)
        inserted.append(label)

    # Recolor any events fetched during insertion that slipped through.
    for anime_id, items in cache.items():
        for item in items:
            if item.get("colorId") != FLAMINGO_COLOR_ID:
                svc.events().patch(
                    calendarId=calendar_id,
                    eventId=item["id"],
                    body={"colorId": FLAMINGO_COLOR_ID},
                ).execute()
                item["colorId"] = FLAMINGO_COLOR_ID
                n = ep_num(item)
                label = f"{titles.get(anime_id, str(anime_id))} · Ep {n}"
                if label not in recolored:
                    recolored.append(label)

    return {"inserted": inserted, "skipped": skipped, "deleted": deleted, "recolored": recolored}
