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
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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
            ha_url=os.getenv("CASATUNES_HA_URL", "").strip(),
            ha_token=os.getenv("CASATUNES_HA_TOKEN", "").strip(),
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
            return await asyncio.to_thread(self._post, text.strip())
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            _LOG.warning("CasaTunes response bridge unavailable (%s)", type(exc).__name__)
            return False

    def _post(self, text: str) -> bool:
        c = self.config
        url = (
            c.ha_url.rstrip("/") + "/api/services/"
            + c.service_domain + "/" + c.service_name
        )
        payload = json.dumps({"message": text}).encode("utf-8")
        request = Request(
            url, data=payload, method="POST",
            headers={
                "Authorization": "Bearer " + c.ha_token,
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=c.timeout_seconds) as response:
            return 200 <= response.status < 300
