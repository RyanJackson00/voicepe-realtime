"""Optional CasaTunes TTS delivery for an Office-only voice response bridge.

This module intentionally does not intercept satellite audio.  The calling
response pipeline must explicitly decide whether a reply is eligible and how
to suppress local playback.  Failures are reported so the caller can retain
the satellite audio fallback.  No audio recordings or API tokens are logged.
"""
from __future__ import annotations

import asyncio
import logging
import os
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
class CasaTunesBridgeConfig:
    enabled: bool = False
    office_device_id: str = ""
    ha_url: str = ""
    ha_token: str = ""
    service_domain: str = "rest_command"
    service_name: str = "casatunes_speak"
    timeout_seconds: float = 8.0

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
        )


class CasaTunesBridge:
    """Send one finalized text response to Home Assistant's REST command.

    A definitive rejection permits satellite fallback. An uncertain delivery
    may already be playing and must not be retried or spoken again locally.
    """

    def __init__(self, config: CasaTunesBridgeConfig):
        self.config = config

    def eligible(self, device_id: str | None) -> bool:
        c = self.config
        return bool(
            c.enabled and c.office_device_id and device_id
            and device_id == c.office_device_id
            and _valid_endpoint(c.ha_url)
            and c.ha_token and c.service_domain and c.service_name
        )

    async def speak(self, *, device_id: str | None, text: str) -> DeliveryResult:
        text = text.strip()
        if not self.eligible(device_id) or not text or len(text) > 1500:
            return DeliveryResult.REJECTED
        try:
            return await self._post(text)
        except (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError) as exc:
            _LOG.warning("CasaTunes connection failed (%s)", type(exc).__name__)
            return DeliveryResult.REJECTED
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
            _LOG.warning("CasaTunes delivery outcome uncertain (%s)", type(exc).__name__)
            return DeliveryResult.UNCERTAIN

    async def _post(self, text: str) -> DeliveryResult:
        c = self.config
        url = (
            c.ha_url.rstrip("/") + "/api/services/"
            + c.service_domain + "/" + c.service_name
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
