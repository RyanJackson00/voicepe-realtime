"""Opt-in response gate for the *Realtime* (gpt-realtime-2) Pipecat pipeline.

Unlike GPT-Live's complete-reply admission, Realtime streams audio and text
through Pipecat. This gate holds the original frames through the response-end
bracket before choosing CasaTunes TTS or the untouched satellite PCM fallback.

This is a canary implementation, disabled by default. A REST 2xx is NOT
evidence of actual CasaTunes playback, and it cannot cancel already-started
ceiling-speaker announcements.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from time import monotonic
from typing import Optional

from pipecat.frames.frames import (
    CancelFrame,
    ControlFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    OutputAudioRawFrame,
    StartFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.background import spawn
from app.casatunes_bridge import CasaTunesBridge, CasaTunesBridgeConfig, DeliveryResult
from app.phase_emitter import ExternalReplyFrame

_LOG = logging.getLogger(__name__)
# A cautious initial canary: don't route confirmations or security actions
# until device-playback evidence/cancellation is implemented.
_CONFIRMATION_RE = re.compile(
    r"\b(confirm|confirmation|are you sure|yes or no|shall i|may i|"
    r"unlock|lock|garage|gate|alarm|disarm|arm the|open the door)\b",
    re.IGNORECASE,
)
_SPEECH_FRAMES = (TTSTextFrame, LLMTextFrame, OutputAudioRawFrame, TTSStartedFrame, TTSStoppedFrame)
_SATELLITE_AUDIO = (OutputAudioRawFrame, TTSStartedFrame, TTSStoppedFrame)


@dataclass
class CasaTunesCompletedFrame(ControlFrame):
    """The server completed a response that has no pending confirmation."""

    completed_at: float = field(default_factory=monotonic)


class CasaTunesRealtimeOutput(FrameProcessor):
    """Hold a complete Realtime reply; CasaTunes on REST accept, PCM on failure.

    This processor follows the model and assistant transcript tap and precedes
    context aggregation, phase emission and transport output. Its filter uses the
    actual websocket client's device ID, never a guessed room label.
    """

    def __init__(
        self, device_id: str,
        bridge: Optional[CasaTunesBridge] = None,
        max_audio_bytes: int = 3_000_000,
        max_hold_seconds: float = 18.0,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.device_id = device_id
        self.bridge = bridge or CasaTunesBridge(CasaTunesBridgeConfig.from_env())
        self.max_audio_bytes = max_audio_bytes
        self.max_hold_seconds = max_hold_seconds
        self._buffering = False
        self._bypass_until_end = False
        self._drop_until_start = False
        self._completed_at: Optional[float] = None
        self._held: list[Frame] = []
        self._audio_bytes = 0
        self._frame_lock = asyncio.Lock()
        self._generation = 0
        self._watchdog: Optional[asyncio.Task] = None
        self._delivery: Optional[asyncio.Task] = None

    def _cancel_watchdog(self):
        if self._watchdog is not None and self._watchdog is not asyncio.current_task():
            self._watchdog.cancel()
        self._watchdog = None

    def _reset(self):
        self._generation += 1
        self._cancel_watchdog()
        if self._delivery is not None:
            self._delivery.cancel()
            self._delivery = None
        self._held = []
        self._audio_bytes = 0
        self._buffering = False
        self._bypass_until_end = False
        self._completed_at = None

    def interrupt(self):
        self._reset()
        self._drop_until_start = True

    async def cleanup(self):
        self._reset()
        await super().cleanup()

    async def _release(self, frames: list[Frame], generation: int, direction: FrameDirection):
        for frame in frames:
            if generation != self._generation:
                return
            await self.push_frame(frame, direction)

    async def _give_up(self, generation: int, direction: FrameDirection):
        if generation != self._generation or not self._buffering:
            return
        frames, self._held = self._held, []
        self._audio_bytes = 0
        self._buffering = False
        self._bypass_until_end = True
        self._cancel_watchdog()
        _LOG.warning("CasaTunes Realtime hold timed out or reached audio limit; falling back")
        await self._release(frames, generation, direction)

    def _arm_watchdog(self, generation: int, direction: FrameDirection):
        async def watch():
            await asyncio.sleep(self.max_hold_seconds)
            async with self._frame_lock:
                await self._give_up(generation, direction)
        self._watchdog = spawn(watch(), "casatunes-hold")

    async def _deliver(self, frames: list[Frame], completed_at: Optional[float], generation: int,
                       direction: FrameDirection):
        spoken = "".join(
            frame.text for frame in frames
            if isinstance(frame, (TTSTextFrame, LLMTextFrame)) and frame.text
        ).strip()
        eligible = (
            completed_at is not None and spoken and len(spoken) <= 1500
            and any(isinstance(frame, OutputAudioRawFrame) and frame.audio for frame in frames)
            and not _CONFIRMATION_RE.search(spoken)
            and self.bridge.eligible(self.device_id)
        )
        result = DeliveryResult.REJECTED
        if eligible:
            dispatched_at = monotonic()
            result = await self.bridge.speak(device_id=self.device_id, text=spoken)
            _LOG.info(
                "CasaTunes delivery timing result=%s model_complete_to_dispatch_ms=%d "
                "service_roundtrip_ms=%d",
                result.name.lower(), round((dispatched_at - completed_at) * 1000),
                round((monotonic() - dispatched_at) * 1000),
            )
        if generation != self._generation:
            return
        if result is not DeliveryResult.REJECTED:
            # Preserve transcript framing without claiming local playout.
            outgoing = [f for f in frames if not isinstance(f, _SATELLITE_AUDIO)]
            outgoing.append(ExternalReplyFrame())
            _LOG.info("CasaTunes Realtime result %s; satellite PCM withheld", result.name.lower())
        else:
            outgoing = frames
            if eligible:
                _LOG.warning("CasaTunes Realtime reply failed; satellite PCM fallback")
        await self._release(outgoing, generation, direction)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        enabled = self.bridge.eligible(self.device_id)
        if enabled and isinstance(frame, (InterruptionFrame, UserStartedSpeakingFrame)):
            self.interrupt()
        elif enabled and isinstance(frame, (CancelFrame, StartFrame, EndFrame)):
            self._reset()
            self._drop_until_start = False
        await super().process_frame(frame, direction)
        if not enabled or direction != FrameDirection.DOWNSTREAM or not isinstance(
            frame, (*_SPEECH_FRAMES, LLMFullResponseStartFrame, LLMFullResponseEndFrame,
                    CasaTunesCompletedFrame)
        ):
            await self.push_frame(frame, direction)
            return
        generation = self._generation
        async with self._frame_lock:
            if generation == self._generation:
                await self._process_reply_frame(frame, direction)

    async def _process_reply_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, LLMFullResponseStartFrame):
            self._reset()
            self._drop_until_start = False
            self._buffering = True
            self._held = [frame]
            self._arm_watchdog(self._generation, direction)
            return
        if self._drop_until_start:
            return
        if isinstance(frame, CasaTunesCompletedFrame):
            self._completed_at = frame.completed_at if self._buffering else None
            return
        if self._bypass_until_end:
            if isinstance(frame, LLMFullResponseEndFrame):
                self._bypass_until_end = False
            await self.push_frame(frame, direction)
            return
        if not self._buffering:
            await self.push_frame(frame, direction)
            return
        self._held.append(frame)
        if isinstance(frame, OutputAudioRawFrame):
            self._audio_bytes += len(frame.audio)
            if self._audio_bytes > self.max_audio_bytes:
                await self._give_up(self._generation, direction)
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._buffering = False
            self._cancel_watchdog()
            frames, self._held = self._held, []
            self._delivery = spawn(
                self._deliver(frames, self._completed_at, self._generation, direction),
                "casatunes-delivery",
            )
