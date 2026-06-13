"""Streamlit dashboard: MAL watchlist → calendar → Google Calendar sync.

Credentials come from .env; auth dialogs are shown in-UI (no terminal links).
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import streamlit as st
from dotenv import load_dotenv
from streamlit_calendar import calendar

from animutils import gcal, mal_auth
from animutils.mal_client import fetch_currently_airing
from animutils.schedule import EpisodeAir, compute_episodes

load_dotenv()

st.set_page_config(page_title="Anime Episode Calendar", layout="wide")
st.title("📺 Currently Watching — Episode Calendar")

ss = st.session_state
ss.setdefault("episodes", None)
ss.setdefault("entries", None)
ss.setdefault("mal_auth_url", None)
ss.setdefault("mal_complete_fn", None)
ss.setdefault("gcal_auth_url", None)
ss.setdefault("gcal_complete_fn", None)

MAL_CLIENT_ID = os.environ.get("MAL_CLIENT_ID", "").strip()
MAL_CLIENT_SECRET = os.environ.get("MAL_CLIENT_SECRET", "").strip()
MAL_REDIRECT_URI = os.environ.get("MAL_REDIRECT_URI", "http://localhost:8765/callback").strip()
GOOGLE_CLIENT_SECRET_FILE = os.environ.get(
    "GOOGLE_CLIENT_SECRET_FILE", "google_client_secret.json"
).strip()
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary").strip()


def _ensure_google_client_secret() -> bool:
    if gcal.has_client_secret():
        return True
    src = Path(GOOGLE_CLIENT_SECRET_FILE)
    if not src.is_absolute():
        src = Path.cwd() / src
    if not src.exists():
        return False
    gcal.CLIENT_SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, gcal.CLIENT_SECRET_PATH)
    return True


# ---------------------------------------------------------------------------
# Auth dialogs
# ---------------------------------------------------------------------------

@st.dialog("🔐 Authenticate with MyAnimeList", width="large")
def _mal_auth_dialog():
    # Start the flow only once per dialog open.
    if ss.mal_auth_url is None:
        try:
            url, complete_fn = mal_auth.start_auth_flow(
                MAL_CLIENT_ID, MAL_CLIENT_SECRET, MAL_REDIRECT_URI
            )
            ss.mal_auth_url = url
            ss.mal_complete_fn = complete_fn
        except Exception as exc:
            st.error(f"Could not start MAL auth flow: {exc}")
            return

    st.markdown(
        "**Step 1 —** Click the button below to open the MyAnimeList authorization page "
        "(opens in a new tab)."
    )
    st.link_button("Open MyAnimeList Authorization →", ss.mal_auth_url)

    with st.expander("Or copy the URL manually"):
        st.code(ss.mal_auth_url, language=None)

    st.markdown("**Step 2 —** Complete authorization in your browser. This dialog closes automatically.")
    st.divider()

    with st.spinner("Waiting for authorization…"):
        try:
            ss.mal_complete_fn()
        except Exception as exc:
            st.error(f"Authorization failed: {exc}")
        finally:
            ss.mal_auth_url = None
            ss.mal_complete_fn = None

    st.rerun()


@st.dialog("🔐 Authenticate with Google Calendar", width="large")
def _gcal_auth_dialog():
    if ss.gcal_auth_url is None:
        try:
            url, complete_fn = gcal.start_auth_flow()
            ss.gcal_auth_url = url
            ss.gcal_complete_fn = complete_fn
        except Exception as exc:
            st.error(f"Could not start Google auth flow: {exc}")
            return

    st.markdown(
        "**Step 1 —** Click the button below to open the Google authorization page "
        "(opens in a new tab)."
    )
    st.link_button("Open Google Authorization →", ss.gcal_auth_url)

    with st.expander("Or copy the URL manually"):
        st.code(ss.gcal_auth_url, language=None)

    st.markdown("**Step 2 —** Complete authorization in your browser. This dialog closes automatically.")
    st.divider()

    with st.spinner("Waiting for authorization…"):
        try:
            ss.gcal_complete_fn()
        except Exception as exc:
            st.error(f"Authorization failed: {exc}")
        finally:
            ss.gcal_auth_url = None
            ss.gcal_complete_fn = None

    st.rerun()


# ---------------------------------------------------------------------------
# Guard: env vars
# ---------------------------------------------------------------------------

missing_env = [
    name
    for name, val in [
        ("MAL_CLIENT_ID", MAL_CLIENT_ID),
        ("MAL_CLIENT_SECRET", MAL_CLIENT_SECRET),
    ]
    if not val
]
if missing_env:
    st.error(f"Missing in .env: {', '.join(missing_env)}. Fill them in and reload.")
    st.stop()

# ---------------------------------------------------------------------------
# Guard: MAL auth
# ---------------------------------------------------------------------------

if not mal_auth.has_token():
    _mal_auth_dialog()
    st.stop()

# ---------------------------------------------------------------------------
# Guard: Google client secret + auth
# ---------------------------------------------------------------------------

if not _ensure_google_client_secret():
    st.error(
        f"Google client secret not found at `{GOOGLE_CLIENT_SECRET_FILE}`. "
        "Download it from Google Cloud Console (OAuth client → Desktop app) "
        "and place it there (or update GOOGLE_CLIENT_SECRET_FILE in .env)."
    )
    st.stop()

if not gcal.has_token():
    _gcal_auth_dialog()
    st.stop()


# ---------------------------------------------------------------------------
# Sidebar: status + re-auth
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("🔐 Auth status")
    st.success("MyAnimeList ✅")
    st.success(f"Google Calendar ✅  (`{GOOGLE_CALENDAR_ID}`)")
    if st.button("Re-authenticate MAL"):
        mal_auth.clear_token()
        st.rerun()
    if st.button("Re-authenticate Google"):
        gcal.clear_token()
        st.rerun()


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _fetch():
    with st.spinner("Fetching from MyAnimeList…"):
        entries = fetch_currently_airing(MAL_CLIENT_ID, MAL_CLIENT_SECRET)
        ss.entries = entries
        ss.episodes = [asdict(e) for e in compute_episodes(entries)]


if ss.episodes is None:
    try:
        _fetch()
    except Exception as exc:
        st.error(f"Failed to load from MyAnimeList: {exc}")
        st.stop()

col_refresh, col_sync = st.columns([1, 1])

with col_refresh:
    if st.button("🔄 Refresh from MAL", width="stretch"):
        try:
            _fetch()
        except Exception as exc:
            st.error(f"Failed to load from MyAnimeList: {exc}")

episodes = [EpisodeAir(**d) for d in (ss.episodes or [])]

if not episodes:
    st.info("No currently-airing episodes on your Watching list.")
    st.stop()

st.caption(
    f"{len(episodes)} upcoming episodes across {len({e.anime_id for e in episodes})} series."
)

# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

cal_events = [
    {
        "title": f"{e.title} · Ep {e.episode}",
        "start": e.airs_at_local.isoformat(),
        "end": e.airs_at_local.isoformat(),
        "backgroundColor": "#F691B2",
        "borderColor": "#F691B2",
    }
    for e in episodes
]
cal_options = {
    "initialView": "dayGridMonth",
    "firstDay": 1,
    "headerToolbar": {
        "left": "prev,next today",
        "center": "title",
        "right": "dayGridMonth,timeGridWeek,listMonth",
    },
    "height": 720,
    "displayEventTime": True,
}
calendar(events=cal_events, options=cal_options, key="anime-cal")

with st.expander("📋 Episode list", expanded=False):
    st.dataframe(
        [
            {
                "Anime": e.title,
                "Ep": e.episode,
                "Local": e.airs_at_local.strftime("%a %Y-%m-%d %H:%M %Z"),
                "JST": e.airs_at_jst.strftime("%a %Y-%m-%d %H:%M"),
                "Approx?": "yes" if e.approximate else "",
                "MAL": f"https://myanimelist.net/anime/{e.anime_id}",
            }
            for e in episodes
        ],
        width="stretch",
        hide_index=True,
    )

# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

with col_sync:
    sync_clicked = st.button(
        "📅 Sync to Google Calendar (Flamingo, popup at start)",
        type="primary",
        width="stretch",
    )

if sync_clicked:
    final_counts: dict[int, int] = {}
    titles: dict[int, str] = {}
    for entry in ss.entries or []:
        node = entry.get("node") or {}
        anime_id = node.get("id")
        if anime_id is None:
            continue
        titles[anime_id] = node.get("title", str(anime_id))
        total = node.get("num_episodes") or 0
        if total > 0:
            final_counts[anime_id] = total

    with st.spinner("Syncing with Google Calendar…"):
        try:
            result = gcal.sync_episodes(
                episodes,
                final_counts=final_counts,
                titles=titles,
                calendar_id=GOOGLE_CALENDAR_ID,
            )
        except Exception as exc:
            st.error(f"Google Calendar sync failed: {exc}")
        else:
            st.success(
                f"Inserted {len(result['inserted'])}, "
                f"recolored {len(result['recolored'])}, "
                f"deleted {len(result['deleted'])} trailing, "
                f"skipped {len(result['skipped'])} already-present. "
                f"Color: Flamingo (colorId={gcal.FLAMINGO_COLOR_ID}); reminder: popup at event start."
            )
            if result["inserted"]:
                with st.expander(f"➕ Inserted ({len(result['inserted'])})"):
                    st.write(result["inserted"])
            if result["recolored"]:
                with st.expander(f"🎨 Recolored ({len(result['recolored'])})"):
                    st.write(result["recolored"])
            if result["deleted"]:
                with st.expander(f"🗑 Deleted trailing ({len(result['deleted'])})"):
                    st.write(result["deleted"])
            if result["skipped"]:
                with st.expander(f"⏭ Skipped ({len(result['skipped'])})"):
                    st.write(result["skipped"])
