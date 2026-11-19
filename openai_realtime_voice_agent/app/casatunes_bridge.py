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

import aiohttp

_LOG = logging.getLogger(__name__)


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
        return cls(
            enabled=os.getenv("CASATUNES_BRIDGE_ENABLED", "").lower() in ("1", "true", "yes"),
            office_device_id=os.getenv("CASATUNES_OFFICE_DEVICE_ID", "").strip(),
            # Home Assistant Apps already receive SUPERVISOR_TOKEN when
            # homeassistant_api is enabled; use the internal Core proxy and
            # avoid a second long-lived secret in the public fork.
            ha_url=os.getenv("CASATUNES_HA_URL", "http://supervisor/core").strip(),
            ha_token=(os.getenv("CASATUNES_HA_TOKEN", "").strip()
                      or os.getenv("SUPERVISOR_TOKEN", "").strip()),
        )


class CasaTunesBridge:
    """Send one finalized text response to Home Assistant's REST command.

    Returns False for all failures, including unknown source device.  The
    integration MUST retain the existing satellite speaker on False.
    """

    def __init__(self, config: CasaTunesBridgeConfig):
        self.config = config

    def eligible(self, device_id: str | None) -> bool:
        c = self.config
        return bool(
            c.enabled and c.office_device_id and device_id
            and device_id == c.office_device_id
            and c.ha_url.startswith(("http://", "https://"))
            and c.ha_token and c.service_domain and c.service_name
        )

    async def speak(self, *, device_id: str | None, text: str) -> bool:
        if not self.eligible(device_id) or not text.strip():
            return False
        try:
            return await self._post(text.strip())
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
            _LOG.warning("CasaTunes response bridge unavailable (%s)", type(exc).__name__)
            return False

    async def _post(self, text: str) -> bool:
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
                    return False
                result = await response.json()
                service_response = (
                    result.get("service_response") if isinstance(result, dict) else None
                )
                status = (
                    service_response.get("status") if isinstance(service_response, dict) else None
                )
                return isinstance(status, int) and 200 <= status < 300
