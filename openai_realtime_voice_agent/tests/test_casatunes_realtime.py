"""Unit tests for opt-in Pipecat Realtime CasaTunes response buffering."""
import asyncio
import unittest
from unittest.mock import AsyncMock

from pipecat.frames.frames import (
    LLMFullResponseStartFrame,
    LLMFullResponseEndFrame,
    StartInterruptionFrame,
    TTSAudioRawFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from app.casatunes_bridge import CasaTunesBridge, CasaTunesBridgeConfig
from app.casatunes_realtime import CasaTunesRealtimeOutput


def bridge(office="office-id", enabled=True):
    config = CasaTunesBridgeConfig(
        enabled=enabled,
        office_device_id=office,
        ha_url="http://homeassistant.local:8123",
        ha_token="test-only",
    )
    obj = CasaTunesBridge(config)
    obj.speak = AsyncMock(return_value=True)
    return obj


def audio():
    return TTSAudioRawFrame(audio=b"\\x00\\x01" * 10, sample_rate=24000, num_channels=1)


class RealtimeCasaTunesTests(unittest.IsolatedAsyncioTestCase):
    async def make_processor(self, device_id="office-id", success=True, enabled=True):
        b = bridge(enabled=enabled)
        b.speak.return_value = success
        gate = CasaTunesRealtimeOutput(device_id=device_id, bridge=b)
        gate.push_frame = AsyncMock()
        return gate, b

    async def reply(self, gate, text="Hello from Jarvis"):
        for frame in (
            LLMFullResponseStartFrame(),
            TTSTextFrame(text),
            audio(),
            LLMFullResponseEndFrame(),
        ):
            await gate.process_frame(frame, FrameDirection.DOWNSTREAM)
        if gate._delivery is not None:
            await gate._delivery

    async def test_office_success_withholds_satellite_audio(self):
        gate, b = await self.make_processor()
        await self.reply(gate)
        b.speak.assert_awaited_once()
        emitted = [call.args[0] for call in gate.push_frame.await_args_list]
        self.assertFalse(any(isinstance(f, TTSAudioRawFrame) for f in emitted))
        self.assertTrue(any(isinstance(f, TTSTextFrame) for f in emitted))

    async def test_service_failure_releases_original_pcm(self):
        gate, b = await self.make_processor(success=False)
        await self.reply(gate)
        b.speak.assert_awaited_once()
        emitted = [call.args[0] for call in gate.push_frame.await_args_list]
        self.assertTrue(any(isinstance(f, TTSAudioRawFrame) for f in emitted))

    async def test_living_room_passthrough_never_calls_casatunes(self):
        gate, b = await self.make_processor(device_id="living-room")
        await self.reply(gate)
        b.speak.assert_not_awaited()
        emitted = [call.args[0] for call in gate.push_frame.await_args_list]
        self.assertTrue(any(isinstance(f, TTSAudioRawFrame) for f in emitted))

    async def test_disabled_passthrough(self):
        gate, b = await self.make_processor(enabled=False)
        await self.reply(gate)
        b.speak.assert_not_awaited()
        emitted = [call.args[0] for call in gate.push_frame.await_args_list]
        self.assertTrue(any(isinstance(f, TTSAudioRawFrame) for f in emitted))

    async def test_confirmation_stays_on_satellite(self):
        gate, b = await self.make_processor()
        await self.reply(gate, "Are you sure you want to unlock the door?")
        b.speak.assert_not_awaited()
        emitted = [call.args[0] for call in gate.push_frame.await_args_list]
        self.assertTrue(any(isinstance(f, TTSAudioRawFrame) for f in emitted))

    async def test_interruption_drops_buffered_reply(self):
        gate, b = await self.make_processor()
        for frame in (LLMFullResponseStartFrame(), TTSTextFrame("Stale"), audio()):
            await gate.process_frame(frame, FrameDirection.DOWNSTREAM)
        await gate.process_frame(StartInterruptionFrame(), FrameDirection.DOWNSTREAM)
        self.assertFalse(gate._buffering)
        self.assertEqual(gate._held, [])
        b.speak.assert_not_awaited()

    async def test_audio_cap_releases_audio_without_casatunes(self):
        gate, b = await self.make_processor()
        gate.max_audio_bytes = 2
        for frame in (LLMFullResponseStartFrame(), TTSTextFrame("Long"), audio(), LLMFullResponseEndFrame()):
            await gate.process_frame(frame, FrameDirection.DOWNSTREAM)
        b.speak.assert_not_awaited()
        emitted = [call.args[0] for call in gate.push_frame.await_args_list]
        self.assertTrue(any(isinstance(f, TTSAudioRawFrame) for f in emitted))
