"""Opt-in CasaTunes music lookup and playback tools for Jarvis.

The public add-on deliberately has no household-specific URLs, entities, or
credentials. Search requires an explicitly configured, verified CasaTunes
search route from the owner's CasaDev schema; no endpoint is guessed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp

LOG = logging.getLogger(__name__)
_ENTITY_RE = re.compile(r"^media_player[.][a-z0-9_]+$")
_SOURCE_RE = re.compile(r"^[a-zA-Z0-9 _.-]{1,64}$")
_MAX_RESULTS = 10


@dataclass(frozen=True)
class MusicConfig:
    enabled: bool = False
    allowed_rooms: tuple[str, ...] = ()
    ha_url: str = "http://supervisor/core"
    ha_token: str = ""
    casa_url: str = ""
    # Search contract is installation/version dependent; only use verified
    # CasaDev route provided by the administrator. A leading /api/v1 path.
    search_route: str = ""
    timeout: float = 8.0

    @classmethod
    def from_env(cls) -> "MusicConfig":
        return cls(
            enabled=os.getenv("CASATUNES_MUSIC_ENABLED", "").lower() in ("true", "1", "yes"),
            allowed_rooms=tuple(x.strip() for x in
                                os.getenv("CASATUNES_MUSIC_ROOMS", "").split(",") if x.strip()),
            ha_url=os.getenv("CASATUNES_MUSIC_HA_URL", "http://supervisor/core").strip().rstrip("/"),
            ha_token=(os.getenv("CASATUNES_MUSIC_HA_TOKEN", "").strip()
                      or os.getenv("SUPERVISOR_TOKEN", "").strip()),
            casa_url=os.getenv("CASATUNES_MUSIC_API_URL", "").strip().rstrip("/"),
            search_route=os.getenv("CASATUNES_MUSIC_SEARCH_ROUTE", "").strip(),
        )

    def valid_room(self, room: str) -> bool:
        return (self.enabled and room in self.allowed_rooms
                and bool(_ENTITY_RE.fullmatch(room)) and bool(self.ha_token)
                and self.ha_url.startswith(("http://", "https://")))

    def search_ready(self) -> bool:
        """Require a fixed base and route, never accept a model-provided URL."""
        u = urlsplit(self.casa_url)
        return (self.enabled and bool(self.search_route)
                and self.search_route.startswith("/api/v1/")
                and ".." not in self.search_route and "?" not in self.search_route
                and u.scheme in ("http", "https") and bool(u.hostname)
                and not u.username and not u.password and not u.fragment)


class MusicError(Exception):
    pass


class CasaTunesMusic:
    def __init__(self, config: MusicConfig):
        self.config = config
        # Search candidates are short-lived server-side selections, never
        # arbitrary media identifiers suggested by a language model.
        self._candidates: dict[str, dict[str, str]] = {}
        self._candidate_room: str | None = None

    async def _ha(self, session: aiohttp.ClientSession, method: str,
                  path: str, payload: dict | None = None) -> dict:
        url = self.config.ha_url + path
        headers = {"Authorization": "Bearer " + self.config.ha_token}
        async with session.request(method, url, json=payload, headers=headers,
                                   allow_redirects=False) as response:
            if not 200 <= response.status < 300:
                raise MusicError(f"Home Assistant returned HTTP {response.status}")
            data = await response.json()
            if not isinstance(data, dict) and method == "GET":
                raise MusicError("Unexpected Home Assistant response")
            return data

    async def _room_state(self, session: aiohttp.ClientSession, room: str) -> dict:
        state = await self._ha(session, "GET", "/api/states/" + room)
        attrs = state.get("attributes") or {}
        if not isinstance(attrs, dict) or "source_list" not in attrs:
            raise MusicError("Room is not recognized as a CasaTunes player")
        return state

    async def search(self, room: str, query: str, service: str = "Amazon Music") -> dict:
        self._candidates.clear()
        self._candidate_room = None
        if not self.config.valid_room(room):
            return {"ok": False, "error": "CasaTunes music control is not enabled for this room"}
        query = query.strip()
        if not 2 <= len(query) <= 120:
            return {"ok": False, "error": "Specify an artist, album, or song"}
        if not self.config.search_ready():
            return {"ok": False, "error": (
                "CasaTunes search is not configured yet. Read the server's "
                "CasaDev API search schema, then configure the verified endpoint. "
                "Do not claim music was found or started."
            )}
        if not _SOURCE_RE.fullmatch(service):
            return {"ok": False, "error": "Invalid music service"}
        timeout = aiohttp.ClientTimeout(total=self.config.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                room_state = await self._room_state(session, room)
                source = (room_state.get("attributes") or {}).get("source")
                # Configured route must be a fixed path. The query is encoded
                # by aiohttp, never spliced into the URL.
                async with session.get(
                    self.config.casa_url + self.config.search_route,
                    params={"query": query, "service": service, "source": source or ""},
                    allow_redirects=False,
                ) as response:
                    if not 200 <= response.status < 300:
                        raise MusicError(f"CasaTunes returned HTTP {response.status}")
                    payload = await response.json()
                # The shape here is a guarded adapter contract, to be verified
                # against CasaDev before enabling it.
                matches = payload.get("results") if isinstance(payload, dict) else None
                if not isinstance(matches, list):
                    raise MusicError("Search result shape does not match the configured CasaDev adapter")
                candidates = []
                for item in matches:
                    if not isinstance(item, dict):
                        continue
                    mid = item.get("media_content_id")
                    typ = item.get("media_content_type")
                    title = item.get("title")
                    if not all(isinstance(x, str) and x.strip() for x in (mid,typ,title)):
                        continue
                    if len(mid) > 2048 or len(typ) > 80:
                        continue
                    key = str(len(candidates) + 1)
                    self._candidates[key] = {"id":mid,"type":typ,"title":title}
                    candidates.append({"selection":key,"title":title,
                                       "artist":str(item.get("artist") or "")[:120],
                                       "type":str(item.get("media_class") or typ)[:80]})
                    if len(candidates) >= _MAX_RESULTS:
                        break
                self._candidate_room = room
                return {"ok": True, "room": room, "source": source,
                        "service": service, "matches": candidates,
                        "instruction": "Call casatunes_play_music with a selection from this result only"}
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, MusicError) as exc:
            LOG.warning("CasaTunes music search unavailable (%s)", type(exc).__name__)
            return {"ok": False, "error": str(exc) if isinstance(exc, MusicError)
                    else "CasaTunes search is unavailable"}

    async def assign_source(self, room: str, source: str) -> dict:
        """Assign an existing CasaTunes source to a room; never guess a source."""
        if not self.config.valid_room(room):
            return {"ok": False, "error": "Room is not enabled for CasaTunes music control"}
        if not _SOURCE_RE.fullmatch(source):
            return {"ok": False, "error": "Invalid source name"}
        timeout = aiohttp.ClientTimeout(total=self.config.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                state = await self._room_state(session, room)
                attrs = state.get("attributes") or {}
                source_list = attrs.get("source_list")
                if not isinstance(source_list, list) or source not in source_list:
                    return {"ok": False, "error": "The requested CasaTunes source is not available"}
                if state.get("state") == "unavailable":
                    return {"ok": False, "error": "Room is unavailable"}
                if attrs.get("source") == source:
                    return {"ok": True, "room": room, "source": source,
                            "status": "Source was already assigned"}
                await self._ha(session, "POST", "/api/services/media_player/select_source",
                               {"entity_id": room, "source": source})
                confirmed = await self._room_state(session, room)
                if (confirmed.get("attributes") or {}).get("source") != source:
                    return {"ok": False, "room": room,
                            "error": "Source command accepted but new source was not yet verified"}
                return {"ok": True, "room": room, "source": source,
                        "status": "Source assignment verified"}
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, MusicError) as exc:
            LOG.warning("CasaTunes source change unavailable (%s)", type(exc).__name__)
            return {"ok": False, "error": str(exc) if isinstance(exc, MusicError)
                    else "Source request failed or verification was unavailable"}

    async def play(self, room: str, selection: str) -> dict:
        if not self.config.valid_room(room) or room != self._candidate_room:
            return {"ok":False,"error":"The room must match the latest search"}
        picked = self._candidates.pop(selection, None)
        self._candidates.clear()
        self._candidate_room = None
        if picked is None:
            return {"ok":False,"error":"Search again and select one of the returned results"}
        timeout = aiohttp.ClientTimeout(total=self.config.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                room_state = await self._room_state(session, room)
                if room_state.get("state") == "unavailable":
                    raise MusicError("Room is unavailable")
                await self._ha(session, "POST", "/api/services/media_player/play_media",
                               {"entity_id": room,
                                "media_content_id": picked["id"],
                                "media_content_type": picked["type"]})
                return {"ok":True,"status":"Home Assistant accepted playback request",
                        "room":room,"title":picked["title"],
                        "warning":"Audible playback has not been verified"}
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, MusicError) as exc:
            LOG.warning("CasaTunes playback request unavailable (%s)", type(exc).__name__)
            return {"ok":False,"error":str(exc) if isinstance(exc,MusicError)
                    else "Playback request failed or had an uncertain result; do not retry automatically"}


def get_music_tool_definitions() -> list[dict[str, Any]]:
    # Only verified HA service contracts are offered to the model.
    return [{
        "type":"function","name":"casatunes_assign_source",
        "description":"Assign an existing CasaTunes player/source such as Dad or Mom to an allowed room. Validate source against room's source_list; returns verified status.",
        "parameters":{"type":"object","properties":{
            "room":{"type":"string","description":"Exact allowlisted CasaTunes media_player entity ID"},
            "source":{"type":"string","description":"Existing CasaTunes source name"}},
            "required":["room","source"]},
    }]


def register_music_tools(service: Any, music: CasaTunesMusic) -> None:
    async def assign(params):
        args = params.arguments or {}
        await params.result_callback(await music.assign_source(
            str(args.get("room","")), str(args.get("source",""))))
    service.register_function("casatunes_assign_source", assign)
