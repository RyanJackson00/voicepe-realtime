"""Optional CasaTunes TTS delivery for an Office-only voice response bridge.

This module intentionally does not intercept satellite audio.  The calling
response pipeline must explicitly decide whether a reply is eligible and how
to suppress local playback.  Failures are reported so the caller can retain
the satellite audio fallback.  No audio recordings or API tokens are logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from enum import Enum, auto
from urllib.parse import urlsplit

import aiohttp

_LOG = logging.getLogger(__name__)
_SUPERVISOR_URL = "http://supervisor/core"


class DeliveryResult(Enum):
    ACCEPTED = auto()
    REJECTED = auto()
    UNCERTAIN = auto()


def _valid_endpoint(url: str) -> bool:
    try:
        endpoint = urlsplit(url)
        # Accessing port also validates malformed or out-of-range ports.
        if endpoint.port == 0:
            return False
        return bool(
            endpoint.hostname and endpoint.username is None and not endpoint.query
            and not endpoint.fragment and not any(char.isspace() for char in url)
            and (
                endpoint.scheme == "https" or url.rstrip("/") == _SUPERVISOR_URL
                or (endpoint.scheme == "http" and endpoint.hostname in ("127.0.0.1", "::1"))
            )
        )
    except ValueError:
        return False


@dataclass(frozen=True)
class CasaTunesRoute:
    device_id: str
    toggle_entity: str
    service_name: str

    def __post_init__(self):
        if (not self.device_id or not re.fullmatch(r"input_boolean\.[a-z0-9_]+", self.toggle_entity)
                or not re.fullmatch(r"[a-z0-9_]+", self.service_name)):
            raise ValueError("Invalid CasaTunes route")


def _configured_routes() -> tuple[CasaTunesRoute, ...]:
    try:
        entries = json.loads(os.getenv("CASATUNES_ROUTES_JSON", "[]"))
        routes = tuple(CasaTunesRoute(**entry) for entry in entries)
        if len({r.device_id for r in routes}) != len(routes):
            raise ValueError("Duplicate CasaTunes device")
        return routes
    except (TypeError, ValueError):
        _LOG.warning("Invalid CasaTunes routes; dashboard routing is unavailable")
        return ()


@dataclass(frozen=True)
class CasaTunesBridgeConfig:
    enabled: bool = False
    office_device_id: str = ""
    ha_url: str = ""
    ha_token: str = ""
    service_domain: str = "rest_command"
    service_name: str = "casatunes_speak"
    timeout_seconds: float = 8.0
    routes: tuple[CasaTunesRoute, ...] = ()

    @classmethod
    def from_env(cls) -> "CasaTunesBridgeConfig":
        url = os.getenv("CASATUNES_HA_URL", "").strip() or _SUPERVISOR_URL
        token = os.getenv("CASATUNES_HA_TOKEN", "").strip()
        if url.rstrip("/") == _SUPERVISOR_URL:
            token = token or os.getenv("SUPERVISOR_TOKEN", "").strip()
        return cls(
            enabled=os.getenv("CASATUNES_BRIDGE_ENABLED", "").lower() in ("1", "true", "yes"),
            office_device_id=os.getenv("CASATUNES_OFFICE_DEVICE_ID", "").strip(),
            # Home Assistant Apps already receive SUPERVISOR_TOKEN when
            # homeassistant_api is enabled; use the internal Core proxy and
            # avoid a second long-lived secret in the public fork.
            ha_url=url,
            ha_token=token,
            routes=_configured_routes(),
        )


class CasaTunesBridge:
    """Send one finalized text response to Home Assistant's REST command.

    A definitive rejection permits satellite fallback. An uncertain delivery
    may already be playing and must not be retried or spoken again locally.
    """

    def __init__(self, config: CasaTunesBridgeConfig):
        self.config = config

    def route_for(self, device_id: str | None) -> CasaTunesRoute | None:
        return next((route for route in self.config.routes if route.device_id == device_id), None)

    def eligible(self, device_id: str | None) -> bool:
        c = self.config
        return bool(
            c.enabled and device_id
            and (self.route_for(device_id) is not None or device_id == c.office_device_id)
            and _valid_endpoint(c.ha_url)
            and c.ha_token and c.service_domain and c.service_name
        )

    async def selected(self, device_id: str | None) -> bool:
        if not self.eligible(device_id):
            return False
        route = self.route_for(device_id)
        if route is None:
            return True
        config = self.config
        url = f"{config.ha_url.rstrip('/')}/api/states/{route.toggle_entity}"
        try:
            timeout = aiohttp.ClientTimeout(total=0.35)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url, headers={"Authorization": "Bearer " + config.ha_token},
                    allow_redirects=False,
                ) as response:
                    if response.status == 200:
                        state = await response.json()
                        return isinstance(state, dict) and state.get("state") == "on"
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            _LOG.debug("CasaTunes reply toggle unavailable; using satellite")
        return False

    async def speak(self, *, device_id: str | None, text: str) -> DeliveryResult:
        text = text.strip()
        if not self.eligible(device_id) or not text or len(text) > 1500:
            return DeliveryResult.REJECTED
        try:
            route = self.route_for(device_id)
            if route is None:
                return await self._post(text)
            return await self._post(text, service_name=route.service_name)
        except (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError) as exc:
            _LOG.warning("CasaTunes connection failed (%s)", type(exc).__name__)
            return DeliveryResult.REJECTED
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
            _LOG.warning("CasaTunes delivery outcome uncertain (%s)", type(exc).__name__)
            return DeliveryResult.UNCERTAIN

    async def _post(self, text: str, *, service_name: str | None = None) -> DeliveryResult:
        c = self.config
        url = (
            c.ha_url.rstrip("/") + "/api/services/"
            + c.service_domain + "/" + (service_name or c.service_name)
        )
        timeout = aiohttp.ClientTimeout(total=c.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url,
                params={"return_response": ""},
                json={"message": text},
                headers={"Authorization": "Bearer " + c.ha_token},
                allow_redirects=False,
            ) as response:
                if not 200 <= response.status < 300:
                    # A proxy failure or timeout can follow a successful dispatch.
                    if response.status in (400, 401, 403, 404, 405, 429) or 300 <= response.status < 400:
                        return DeliveryResult.REJECTED
                    return DeliveryResult.UNCERTAIN
                result = await response.json()
                service_response = (
                    result.get("service_response") if isinstance(result, dict) else None
                )
                status = (
                    service_response.get("status") if isinstance(service_response, dict) else None
                )
                if isinstance(status, int) and 200 <= status < 300:
                    return DeliveryResult.ACCEPTED
                if isinstance(status, int) and 400 <= status < 600 and status not in (408, 504):
                    return DeliveryResult.REJECTED
                return DeliveryResult.UNCERTAIN
