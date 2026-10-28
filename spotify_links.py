"""Resolve Spotify album, playlist and artist links into playable tracks.

Since February 2026 Spotify no longer gives new developer apps playlist contents or artist top tracks,
and LavaSrc's album loading relies on a batch endpoint that was removed (LavaSrc issue #351).
Spotify's public embed player still lists the tracks (title, artist, length), so we read the list there
and find each song on YouTube Music ourselves. Loading every track link through the Spotify API instead
quickly hits the low rate limit Spotify gives new apps.
"""

import asyncio
import json
import re
from typing import NamedTuple

import aiohttp
import wavelink

LINK_RE = re.compile(r"https?://open\.spotify\.com/(?:intl-[a-z-]+/)?(album|playlist|artist)/([A-Za-z0-9]+)")
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', re.S)
MAX_CONCURRENT_LOADS = 6
CANDIDATES = 5  # YouTube results considered per song
LENGTH_TOLERANCE_MS = 4000  # a result this close to Spotify's length counts as the same recording


class SpotifyTrack(NamedTuple):
    title: str
    artist: str
    length_ms: int


async def fetch_collection(query: str) -> tuple[str, list[SpotifyTrack]] | None:
    """If query is a Spotify album/playlist/artist link, return its name and tracks; otherwise None."""
    match = LINK_RE.match(query.strip())
    if not match:
        return None
    kind, spotify_id = match.groups()

    url = f"https://open.spotify.com/embed/{kind}/{spotify_id}"
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as http:
        async with http.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            html = await resp.text()

    page = NEXT_DATA_RE.search(html)
    if not page:
        raise LookupError("Spotify's page format changed")
    entity = json.loads(page.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    name = entity.get("name") or entity.get("title") or "Spotify"
    if kind == "artist":
        name = f"{name} — Top Tracks"
    tracks = [
        SpotifyTrack(t["title"], t.get("subtitle", ""), int(t.get("duration") or 0))
        for t in entity.get("trackList", [])
        if t.get("uri", "").startswith("spotify:track:") and t.get("title")
    ]
    return name, tracks


async def _search(query: str, source: wavelink.TrackSource) -> list[wavelink.Playable]:
    try:
        results = await wavelink.Playable.search(query, source=source)
    except wavelink.LavalinkLoadException:
        return []
    return results[:CANDIDATES] if isinstance(results, list) else []


async def find_on_youtube(track: SpotifyTrack) -> wavelink.Playable | None:
    """Find the song on YouTube Music (falling back to YouTube).

    The top result is usually right; the length check skips covers, remixes and live versions that rank first.
    """
    query = f"{track.title} {track.artist}"
    candidates = await _search(query, wavelink.TrackSource.YouTubeMusic) or await _search(
        query, wavelink.TrackSource.YouTube
    )
    if not candidates:
        return None
    if not track.length_ms:
        return candidates[0]
    for candidate in candidates:
        if abs(candidate.length - track.length_ms) <= LENGTH_TOLERANCE_MS:
            return candidate
    return min(candidates, key=lambda c: abs(c.length - track.length_ms))


async def find_all_on_youtube(tracks: list[SpotifyTrack]) -> list[wavelink.Playable]:
    """Find several songs concurrently, keeping their order and dropping ones that weren't found."""
    limit = asyncio.Semaphore(MAX_CONCURRENT_LOADS)

    async def find(track: SpotifyTrack) -> wavelink.Playable | None:
        async with limit:
            return await find_on_youtube(track)

    found = await asyncio.gather(*(find(t) for t in tracks))
    return [t for t in found if t is not None]
