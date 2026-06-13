"""Push episode events to Google Calendar (red, popup at start)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from animutils.schedule import DEFAULT_EPISODE_MINUTES, EpisodeAir

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
TOKEN_PATH = Path("tokens/gcal_token.json")
CLIENT_SECRET_PATH = Path("tokens/google_client_secret.json")
FLAMINGO_COLOR_ID = "4"  # Flamingo


def has_client_secret() -> bool:
    return CLIENT_SECRET_PATH.exists()


def save_client_secret(content: bytes) -> None:
    CLIENT_SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLIENT_SECRET_PATH.write_bytes(content)


def has_token() -> bool:
    return TOKEN_PATH.exists()


def clear_token() -> None:
    TOKEN_PATH.unlink(missing_ok=True)


def authenticate() -> None:
    """Run the installed-app OAuth flow (opens a browser)."""
    if not CLIENT_SECRET_PATH.exists():
        raise RuntimeError("Upload your Google OAuth client_secret JSON first.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json())


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
                label = f"{titles.get(anime_id, str(anime_id))} · Ep {n}"
                recolored.append(label)

    # Collect existing episodes across all cached anime (may include anime not
    # yet fetched for insertion — fetch on demand below).
    def existing_ep_nums(anime_id: int) -> set[int]:
        return {n for item in existing(anime_id) if (n := ep_num(item)) is not None}

    # Insert any new episodes, recoloring is already handled above.
    for ep in episodes:
        label = f"{ep.title} · Ep {ep.episode}"
        if ep.episode in existing_ep_nums(ep.anime_id):
            skipped.append(label)
            continue
        created = svc.events().insert(calendarId=calendar_id, body=_event_body(ep)).execute()
        cache[ep.anime_id].append(created)
        inserted.append(label)

    # Recolor newly fetched anime (fetched during insertion but not yet recolored).
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
