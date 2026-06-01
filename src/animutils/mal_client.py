"""MyAnimeList API client — just the bits we need."""

from __future__ import annotations

from typing import Any

import requests

from animutils.mal_auth import get_access_token

API_BASE = "https://api.myanimelist.net/v2"
WATCHING_FIELDS = (
    "list_status,"
    "node(id,title,main_picture,status,start_date,broadcast,num_episodes)"
)


def _headers(client_id: str, client_secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {get_access_token(client_id, client_secret)}"}


def fetch_watching(client_id: str, client_secret: str) -> list[dict[str, Any]]:
    """Return all entries on @me's list with status='watching'."""
    items: list[dict[str, Any]] = []
    url: str | None = (
        f"{API_BASE}/users/@me/animelist"
        f"?status=watching&limit=1000&nsfw=true&fields={WATCHING_FIELDS}"
    )
    headers = _headers(client_id, client_secret)
    while url:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        items.extend(payload.get("data", []))
        url = payload.get("paging", {}).get("next")
    return items


def fetch_currently_airing(client_id: str, client_secret: str) -> list[dict[str, Any]]:
    """Subset of watching list whose anime is currently_airing."""
    return [
        entry
        for entry in fetch_watching(client_id, client_secret)
        if (entry.get("node") or {}).get("status") == "currently_airing"
    ]
