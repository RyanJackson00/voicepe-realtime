"""CasaTunes music-room configuration and source-selection support.

Public code contains no private room mapping, IP address or credential.
Music search/playback uses the installed HA casatunes.search entity service,
implemented in casatunes_native_search.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import aiohttp

LOG = logging.getLogger(__name__)
_ENTITY_RE = re.compile(r"^media_player[.][a-z0-9_]+$")
_SOURCE_RE = re.compile(r"^[a-zA-Z0-9 _.-]{1,64}$")


@dataclass(frozen=True)
class MusicConfig:
    enabled: bool = False
    allowed_rooms: tuple[str, ...] = ()
    ha_url: str = "http://supervisor/core"
    ha_token: str = ""
    timeout: float = 8.0

    @classmethod
    def from_env(cls) -> "MusicConfig":
        return cls(
            enabled=os.getenv("CASATUNES_MUSIC_ENABLED", "").lower()
                    in ("true", "1", "yes"),
            allowed_rooms=tuple(
                x.strip() for x in os.getenv("CASATUNES_MUSIC_ROOMS", "").split(",")
                if x.strip()
            ),
            ha_url=os.getenv("CASATUNES_MUSIC_HA_URL", "http://supervisor/core").strip().rstrip("/"),
            ha_token=(
                os.getenv("CASATUNES_MUSIC_HA_TOKEN", "").strip()
                or os.getenv("SUPERVISOR_TOKEN", "").strip()
            ),
        )

    def valid_room(self, room: str) -> bool:
        # Do not allow arbitrary target entities or leak the Supervisor token to
        # user-configured external HTTP servers. Standalone HTTPS access can be
        # added after an explicit security review.
        return (
            self.enabled and room in self.allowed_rooms
            and bool(_ENTITY_RE.fullmatch(room))
            and bool(self.ha_token) and self.ha_url == "http://supervisor/core"
        )


class MusicError(Exception):
    pass


class CasaTunesMusic:
    def __init__(self, config: MusicConfig):
        self.config = config

    async def _ha(self, session: aiohttp.ClientSession, method: str,
                  path: str, payload: dict | None = None) -> Any:
        if not path.startswith("/api/") or "?" in path or ".." in path:
            raise MusicError("Invalid Home Assistant API request path")
        async with session.request(
            method, self.config.ha_url + path, json=payload,
            headers={"Authorization":"Bearer " + self.config.ha_token},
            allow_redirects=False,
        ) as response:
            if not 200 <= response.status < 300:
                raise MusicError(f"Home Assistant returned HTTP {response.status}")
            return await response.json()

    async def _room_state(self, session: aiohttp.ClientSession, room: str) -> dict:
        if not self.config.valid_room(room):
            raise MusicError("CasaTunes room is not allowlisted")
        state = await self._ha(session, "GET", "/api/states/" + room)
        if not isinstance(state, dict):
            raise MusicError("Unexpected Home Assistant room state")
        attrs = state.get("attributes") or {}
        if not isinstance(attrs, dict) or not isinstance(attrs.get("source_list"), list):
            raise MusicError("Target is not an identified CasaTunes room")
        return state

    async def assign_source(self, room: str, source: str) -> dict:
        """Assign an existing CasaTunes source, checking the reported outcome."""
        if not self.config.valid_room(room):
            return {"ok":False,"error":"Room not enabled for CasaTunes control"}
        if not _SOURCE_RE.fullmatch(source):
            return {"ok":False,"error":"Invalid source"}
        try:
            timeout=aiohttp.ClientTimeout(total=self.config.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                state=await self._room_state(session,room)
                attrs=state.get("attributes") or {}
                if source not in attrs["source_list"]:
                    return {"ok":False,"error":"Source is not offered by this room"}
                if state.get("state") in ("unknown","unavailable"):
                    return {"ok":False,"error":"Room unavailable"}
                if attrs.get("source")==source:
                    return {"ok":True,"room":room,"source":source,
                            "status":"Source already selected"}
                await self._ha(session,"POST","/api/services/media_player/select_source",
                               {"entity_id":room,"source":source})
                verified=await self._room_state(session,room)
                if (verified.get("attributes") or {}).get("source")!=source:
                    return {"ok":False,"error":"Source selection sent but not verified"}
                return {"ok":True,"room":room,"source":source,
                        "status":"Source assignment verified"}
        except (aiohttp.ClientError,asyncio.TimeoutError,ValueError,MusicError) as exc:
            LOG.warning("CasaTunes source request failed (%s)",type(exc).__name__)
            return {"ok":False,"error":str(exc) if isinstance(exc,MusicError)
                    else "Source update failed or had uncertain outcome"}


def get_music_tool_definitions() -> list[dict[str, Any]]:
    return [{
        "type":"function","name":"casatunes_assign_source",
        "description":"Assign an existing CasaTunes player/source such as Dad or Mom to an allowlisted room. Validates the source and checks the new HA state.",
        "parameters":{"type":"object","properties":{
            "room":{"type":"string","description":"Exact allowed CasaTunes media_player entity ID"},
            "source":{"type":"string","description":"Existing CasaTunes source name"}},
            "required":["room","source"]},
    }]


def register_music_tools(service: Any, music: CasaTunesMusic) -> None:
    async def assign(params):
        args=params.arguments or {}
        await params.result_callback(await music.assign_source(
            str(args.get("room","")),str(args.get("source",""))))
    service.register_function("casatunes_assign_source",assign)
