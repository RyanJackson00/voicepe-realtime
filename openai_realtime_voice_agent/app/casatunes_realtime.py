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
from typing import Optional

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    OutputAudioRawFrame,
    StartFrame,
    StartInterruptionFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.casatunes_bridge import CasaTunesBridge, CasaTunesBridgeConfig

_LOG = logging.getLogger(__name__)
# A cautious initial canary: don't route confirmations or security actions
# until device-playback evidence/cancellation is implemented.
_CONFIRMATION_RE = re.compile(
    r"\\b(confirm|confirmation|are you sure|yes or no|shall i|may i|"
    r"unlock|lock|garage|gate|alarm|disarm|arm the|open the door)\\b",
    re.IGNORECASE,
)


class CasaTunesRealtimeOutput(FrameProcessor):
    """Hold a complete Realtime reply; CasaTunes on REST accept, PCM on failure.

    This processor is inserted *after* the model and assistant transcript tap,
    *before* phase emission and transport output. Its device filter uses the
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
        self._held: list[Frame] = []
        self._audio_bytes = 0
        self._generation = 0
        self._watchdog: Optional[asyncio.Task] = None
        self._delivery: Optional[asyncio.Task] = None

    def _cancel_watchdog(self):
        if self._watchdog is not None:
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
            try:
                await asyncio.sleep(self.max_hold_seconds)
                await self._give_up(generation, direction)
            except asyncio.CancelledError:
                return
        self._watchdog = asyncio.create_task(watch())

    async def _deliver(self, frames: list[Frame], generation: int, direction: FrameDirection):
        spoken = "".join(
            frame.text for frame in frames
            if isinstance(frame, (TTSTextFrame, LLMTextFrame)) and frame.text
        ).strip()
        eligible = (
            spoken and len(spoken) <= 1500
            and not _CONFIRMATION_RE.search(spoken)
            and self.bridge.eligible(self.device_id)
        )
        accepted = False
        if eligible:
            accepted = await self.bridge.speak(device_id=self.device_id, text=spoken)
        if generation != self._generation:
            return
        if accepted:
            # Retain response framing and transcript for context / logging.
            # Drop *only* the audio that would duplicate the CasaTunes speech.
            outgoing = [f for f in frames if not isinstance(f, OutputAudioRawFrame)]
            _LOG.info("CasaTunes accepted Realtime reply; satellite PCM withheld")
        else:
            outgoing = frames
            if eligible:
                _LOG.warning("CasaTunes Realtime reply failed; satellite PCM fallback")
        await self._release(outgoing, generation, direction)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if not self.bridge.eligible(self.device_id):
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, (StartInterruptionFrame, CancelFrame, StartFrame, EndFrame)):
            self._reset()
            await self.push_frame(frame, direction)
            return
        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, LLMFullResponseStartFrame):
            self._reset()
            self._buffering = True
            self._held = [frame]
            self._arm_watchdog(self._generation, direction)
            return
        if self._bypass_until_end:
            if isinstance(frame, LLMFullResponseEndFrame):
                self._bypass_until_end = False
            await self.push_frame(frame, direction)
            return
        if not self._buffering:
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, (TTSTextFrame, LLMTextFrame, OutputAudioRawFrame, LLMFullResponseEndFrame)):
            self._held.append(frame)
            if isinstance(frame, OutputAudioRawFrame):
                self._audio_bytes += len(frame.audio)
                if self._audio_bytes > self.max_audio_bytes:
                    await self._give_up(self._generation, direction)
                    return
            if isinstance(frame, LLMFullResponseEndFrame):
                self._buffering = False
                self._cancel_watchdog()
                frames, self._held = self._held, []
                gen = self._generation
                self._delivery = asyncio.create_task(self._deliver(frames, gen, direction))
            return
        # Control and metrics frames must not be delayed by the speech hold.
        await self.push_frame(frame, direction)
