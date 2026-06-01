"""Compute per-episode air datetimes from MAL broadcast + start_date."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from tzlocal import get_localzone

JST = ZoneInfo("Asia/Tokyo")
DEFAULT_EPISODE_MINUTES = 30
FORWARD_HORIZON_WEEKS = 26  # how far to project ongoing shows

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


@dataclass
class EpisodeAir:
    anime_id: int
    title: str
    episode: int
    airs_at_jst: datetime
    airs_at_local: datetime
    image_url: str | None
    approximate: bool  # True when broadcast time was unknown


def _first_airing(start: date, weekday_idx: int, t: time) -> datetime:
    delta = (weekday_idx - start.weekday()) % 7
    first_date = start + timedelta(days=delta)
    return datetime.combine(first_date, t, tzinfo=JST)


def episodes_for_entry(entry: dict[str, Any]) -> list[EpisodeAir]:
    """Project upcoming episode airings.

    Episode N's airdate = first_airing + (N-1) weeks. We always emit a forward
    horizon starting from "today" — that way long-running shows whose
    `start_date` is years ago still show up.
    """
    node = entry["node"]
    broadcast = node.get("broadcast") or {}
    start_str = node.get("start_date")
    if not start_str:
        return []

    start = date.fromisoformat(start_str)
    weekday_name = (broadcast.get("day_of_the_week") or "").lower()
    weekday_idx = WEEKDAYS.get(weekday_name, start.weekday())
    start_time_str = broadcast.get("start_time")
    approximate = start_time_str is None or weekday_name == ""
    start_time = (
        time.fromisoformat(start_time_str) if start_time_str else time(0, 0)
    )

    first = _first_airing(start, weekday_idx, start_time)
    num_episodes = node.get("num_episodes") or 0  # 0 = unknown / ongoing

    local_tz = get_localzone()
    now_local = datetime.now(local_tz)
    image = (node.get("main_picture") or {}).get("medium")

    # Episode number of the next airing on/after now (1-indexed).
    delta_weeks = (now_local.astimezone(JST) - first).total_seconds() / (7 * 86400)
    next_ep = max(1, int(delta_weeks) + 1)
    # If the show already finished but we haven't fetched the latest list,
    # cap at num_episodes; otherwise project up to the horizon.
    last_ep = (
        num_episodes
        if num_episodes and num_episodes < next_ep + FORWARD_HORIZON_WEEKS
        else next_ep + FORWARD_HORIZON_WEEKS - 1
    )

    out: list[EpisodeAir] = []
    for n in range(next_ep, last_ep + 1):
        air_jst = first + timedelta(days=7 * (n - 1))
        air_local = air_jst.astimezone(local_tz)
        if air_local < now_local - timedelta(days=1):
            continue
        out.append(
            EpisodeAir(
                anime_id=node["id"],
                title=node["title"],
                episode=n,
                airs_at_jst=air_jst,
                airs_at_local=air_local,
                image_url=image,
                approximate=approximate,
            )
        )
    return out


def compute_episodes(entries: list[dict[str, Any]]) -> list[EpisodeAir]:
    all_eps: list[EpisodeAir] = []
    for entry in entries:
        all_eps.extend(episodes_for_entry(entry))
    all_eps.sort(key=lambda e: e.airs_at_local)
    return all_eps
