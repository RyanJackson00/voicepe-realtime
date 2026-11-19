"""Unit tests for opt-in Pipecat Realtime CasaTunes response buffering."""
import asyncio
import unittest
from unittest.mock import AsyncMock

from pipecat.frames.frames import (
    AggregationType,
    InterruptionFrame,
    LLMFullResponseStartFrame,
    LLMFullResponseEndFrame,
    TTSAudioRawFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from app.casatunes_bridge import CasaTunesBridge, CasaTunesBridgeConfig, DeliveryResult
from app.casatunes_realtime import CasaTunesCompletedFrame, CasaTunesRealtimeOutput
from app.phase_emitter import ExternalReplyFrame


def bridge(office="office-id", enabled=True):
    config = CasaTunesBridgeConfig(
        enabled=enabled,
        office_device_id=office,
        ha_url="https://homeassistant.example",
        ha_token="test-only",
    )
    obj = CasaTunesBridge(config)
    obj.speak = AsyncMock(return_value=DeliveryResult.ACCEPTED)
    return obj


def audio():
    return TTSAudioRawFrame(audio=b"\x00\x01" * 10, sample_rate=24000, num_channels=1)


class RealtimeCasaTunesTests(unittest.IsolatedAsyncioTestCase):
    async def make_processor(self, device_id="office-id", result=DeliveryResult.ACCEPTED, enabled=True):
        b = bridge(enabled=enabled)
        b.speak.return_value = result
        gate = CasaTunesRealtimeOutput(device_id=device_id, bridge=b, enable_direct_mode=True)
        gate.push_frame = AsyncMock()
        return gate, b

    async def reply(self, gate, text="Hello from Jarvis", *, completed=True, wait=True):
        for frame in (
            LLMFullResponseStartFrame(),
            TTSTextFrame(text, aggregated_by=AggregationType.SENTENCE),
            audio(),
            *((CasaTunesCompletedFrame(),) if completed else ()),
            LLMFullResponseEndFrame(),
        ):
            await gate.process_frame(frame, FrameDirection.DOWNSTREAM)
        if wait and gate._delivery is not None:
            await gate._delivery

    async def test_office_success_withholds_satellite_audio(self):
        gate, b = await self.make_processor()
        await self.reply(gate)
        b.speak.assert_awaited_once_with(device_id="office-id", text="Hello from Jarvis")
        emitted = [call.args[0] for call in gate.push_frame.await_args_list]
        self.assertFalse(any(isinstance(f, TTSAudioRawFrame) for f in emitted))
        self.assertTrue(any(isinstance(f, TTSTextFrame) for f in emitted))

    async def test_service_failure_releases_original_pcm(self):
        gate, b = await self.make_processor(result=DeliveryResult.REJECTED)
        await self.reply(gate)
        b.speak.assert_awaited_once()
        emitted = [call.args[0] for call in gate.push_frame.await_args_list]
        self.assertTrue(any(isinstance(f, TTSAudioRawFrame) for f in emitted))

    async def test_uncertain_delivery_never_replays_pcm(self):
        gate, b = await self.make_processor(result=DeliveryResult.UNCERTAIN)
        await self.reply(gate)
        b.speak.assert_awaited_once()
        emitted = [call.args[0] for call in gate.push_frame.await_args_list]
        self.assertFalse(any(isinstance(f, TTSAudioRawFrame) for f in emitted))
        self.assertIsInstance(emitted[-1], ExternalReplyFrame)

    async def test_unconfirmed_completion_stays_on_satellite(self):
        gate, b = await self.make_processor()
        await self.reply(gate, completed=False)
        b.speak.assert_not_awaited()
        self.assertTrue(any(isinstance(c.args[0], TTSAudioRawFrame)
                            for c in gate.push_frame.await_args_list))

    async def test_stop_and_new_reply_do_not_release_stale_audio(self):
        entered = asyncio.Event()
        stopped = asyncio.Event()

        async def delivery(**kwargs):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        office, b = await self.make_processor()
        living, other = await self.make_processor(device_id="living-room")
        b.speak.side_effect = delivery
        await self.reply(office, wait=False)
        await asyncio.wait_for(entered.wait(), 1)
        await self.reply(living)
        other.speak.assert_not_awaited()
        self.assertTrue(any(isinstance(c.args[0], TTSAudioRawFrame)
                            for c in living.push_frame.await_args_list))

        office.interrupt()
        for late in (audio(), CasaTunesCompletedFrame(), LLMFullResponseEndFrame()):
            await office.process_frame(late, FrameDirection.DOWNSTREAM)
        office.push_frame.assert_not_awaited()
        await asyncio.wait_for(stopped.wait(), 1)

        b.speak.side_effect = None
        await self.reply(office, text="next reply")
        self.assertEqual(b.speak.await_count, 2)
        self.assertIsInstance(office.push_frame.await_args.args[0], ExternalReplyFrame)

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
        for frame in (
            LLMFullResponseStartFrame(),
            TTSTextFrame("stale", aggregated_by=AggregationType.SENTENCE),
            audio(),
        ):
            await gate.process_frame(frame, FrameDirection.DOWNSTREAM)
        await gate.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
        self.assertFalse(gate._buffering)
        self.assertEqual(gate._held, [])
        b.speak.assert_not_awaited()

    async def test_audio_cap_releases_audio_without_casatunes(self):
        gate, b = await self.make_processor()
        gate.max_audio_bytes = 2
        for frame in (
            LLMFullResponseStartFrame(),
            TTSTextFrame("long", aggregated_by=AggregationType.SENTENCE),
            audio(),
            LLMFullResponseEndFrame(),
        ):
            await gate.process_frame(frame, FrameDirection.DOWNSTREAM)
        b.speak.assert_not_awaited()
        emitted = [call.args[0] for call in gate.push_frame.await_args_list]
        self.assertTrue(any(isinstance(f, TTSAudioRawFrame) for f in emitted))

    async def test_watchdog_releases_buffered_pcm(self):
        gate, b = await self.make_processor()
        gate.max_hold_seconds = 0
        emitted = []

        async def push(frame, direction):
            await asyncio.sleep(0)
            emitted.append(frame)

        gate.push_frame.side_effect = push
        start, pcm = LLMFullResponseStartFrame(), audio()
        for frame in (start, pcm):
            await gate.process_frame(frame, FrameDirection.DOWNSTREAM)
        await gate._watchdog
        end = LLMFullResponseEndFrame()
        await gate.process_frame(end, FrameDirection.DOWNSTREAM)
        self.assertEqual(emitted, [start, pcm, end])
        b.speak.assert_not_awaited()

    async def test_text_only_reply_does_not_trigger_audio(self):
        gate, b = await self.make_processor()
        frames = [
            LLMFullResponseStartFrame(),
            TTSTextFrame("text only", aggregated_by=AggregationType.SENTENCE),
            LLMFullResponseEndFrame(),
        ]
        for frame in frames:
            await gate.process_frame(frame, FrameDirection.DOWNSTREAM)
        await gate._delivery
        b.speak.assert_not_awaited()
        self.assertEqual([call.args[0] for call in gate.push_frame.await_args_list], frames)
