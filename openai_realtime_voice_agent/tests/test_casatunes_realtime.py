"""Unit tests for opt-in Pipecat Realtime CasaTunes response buffering."""
import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from pipecat.frames.frames import (
    AggregationType,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseStartFrame,
    LLMFullResponseEndFrame,
    TTSAudioRawFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from app.casatunes_bridge import CasaTunesBridge, CasaTunesBridgeConfig, CasaTunesRoute, DeliveryResult
from app.casatunes_realtime import CasaTunesCompletedFrame, CasaTunesRealtimeOutput
from app.device_registry import DeviceConnection
from app.phase_emitter import ExternalReplyFrame, TurnLiveness
from app.raw_audio_serializer import RawAudioSerializer
from app.session_manager import SessionManager
from app.turn_timeline import TurnTimeline
from app.wake_events import CaptureConfig, WakeEventStore
from app.websocket_handler import WebSocketHandler
from tests.live_harness import Tap
from tests.test_handler_wiring import ForwardingService, RunnableTransport


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


class RealtimeCasaTunesPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def run_reply(self, result):
        # Exercise build_pipeline with Pipecat's real assistant aggregator.
        # It consumes the response brackets and text after updating context.
        device_id = "office-id"
        backend = bridge()
        backend.speak.return_value = result
        sink = Tap()
        transport = RunnableTransport()
        transport._output = sink
        connection = DeviceConnection(
            device_id=device_id, websocket=object(), authenticated=True,
            serializer=RawAudioSerializer(device_id), transport=transport,
            turn_timeline=TurnTimeline(device_id), turn_liveness=TurnLiveness(),
            openai_service=ForwardingService(),
        )
        with tempfile.TemporaryDirectory() as directory:
            store = WakeEventStore(CaptureConfig(mode="off", probe_dir=directory), instance="w")
            handler = WebSocketHandler(session_manager=SessionManager(), wake_events=store)
            with patch("app.websocket_handler.OpenAIRealtimeLLMService", ForwardingService), \
                 patch("app.casatunes_realtime.CasaTunesBridge", return_value=backend):
                _, runner, task = handler.build_pipeline(connection)
            self.assertTrue(connection.openai_service.suppress_progress_ack)
            worker = asyncio.create_task(runner.run(task))
            try:
                await asyncio.wait_for(sink.started.wait(), 3)
                pcm = audio()
                await task.queue_frames([
                    InterruptionFrame(),
                    LLMFullResponseStartFrame(),
                    TTSTextFrame("fixture reply", aggregated_by=AggregationType.SENTENCE),
                    pcm,
                    CasaTunesCompletedFrame(),
                    LLMFullResponseEndFrame(),
                ])
                await sink.wait_for(LLMContextFrame, timeout=3)
                backend.speak.assert_awaited_once_with(device_id=device_id, text="fixture reply")
                messages = sink.of(LLMContextFrame)[-1].context.get_messages()
                self.assertIn({"role": "assistant", "content": "fixture reply"}, messages)
                self.assertEqual(sink.of(TTSAudioRawFrame),
                                 [pcm] if result is DeliveryResult.REJECTED else [])
            finally:
                await connection.phase_emitter.close()
                await task.cancel()
                await asyncio.wait_for(worker, 5)

    async def test_accepted_reply_retains_context_without_satellite_audio(self):
        await self.run_reply(DeliveryResult.ACCEPTED)

    async def test_rejected_reply_retains_context_and_satellite_audio(self):
        await self.run_reply(DeliveryResult.REJECTED)


class RealtimeCasaTunesTests(unittest.IsolatedAsyncioTestCase):
    async def make_processor(self, device_id="office-id", result=DeliveryResult.ACCEPTED, enabled=True):
        b = bridge(enabled=enabled)
        b.speak.return_value = result
        gate = CasaTunesRealtimeOutput(device_id=device_id, bridge=b, enable_direct_mode=True)
        gate.push_frame = AsyncMock()
        return gate, b

    async def reply(self, gate, text="Hello from Jarvis", *, completed=True,
                    started_tool_call=False, wait=True):
        completion = CasaTunesCompletedFrame(started_tool_call=started_tool_call)
        for frame in (
            LLMFullResponseStartFrame(),
            TTSTextFrame(text, aggregated_by=AggregationType.SENTENCE),
            audio(),
            *((completion,) if completed else ()),
            LLMFullResponseEndFrame(),
        ):
            await gate.process_frame(frame, FrameDirection.DOWNSTREAM)
        if wait and gate._delivery is not None:
            await gate._delivery

    async def test_route_toggle_is_sampled_again_for_the_next_reply(self):
        route = CasaTunesRoute("office-id", "input_boolean.office_replies", "office_speak")
        backend = CasaTunesBridge(CasaTunesBridgeConfig(
            enabled=True, ha_url="https://homeassistant.example", ha_token="test",
            routes=(route,),
        ))
        backend.selected = AsyncMock(side_effect=(False, True))
        backend.speak = AsyncMock(return_value=DeliveryResult.ACCEPTED)
        gate = CasaTunesRealtimeOutput(device_id="office-id", bridge=backend, enable_direct_mode=True)
        gate.push_frame = AsyncMock()

        await self.reply(gate)
        backend.speak.assert_not_awaited()
        self.assertTrue(any(isinstance(c.args[0], TTSAudioRawFrame)
                            for c in gate.push_frame.await_args_list))

        gate.push_frame.reset_mock()
        await self.reply(gate)
        backend.speak.assert_awaited_once()
        self.assertFalse(any(isinstance(c.args[0], TTSAudioRawFrame)
                             for c in gate.push_frame.await_args_list))
        self.assertEqual(backend.selected.await_count, 2)

    async def test_office_success_withholds_satellite_audio(self):
        gate, b = await self.make_processor()
        with self.assertLogs("app.casatunes_realtime", level="INFO") as captured:
            await self.reply(gate)
        self.assertRegex(
            captured.output[0],
            r"CasaTunes delivery timing result=accepted model_complete_to_dispatch_ms=\d+ "
            r"service_roundtrip_ms=\d+$",
        )
        self.assertNotIn("Hello from Jarvis", "\n".join(captured.output))
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

    async def test_clarification_uses_local_audio_and_follow_up(self):
        for started_tool_call in (False, True):
            with self.subTest(started_tool_call=started_tool_call):
                gate, b = await self.make_processor()
                await self.reply(gate, "Which city should I check?",
                                 started_tool_call=started_tool_call)
                b.speak.assert_not_awaited()
                emitted = [call.args[0] for call in gate.push_frame.await_args_list]
                self.assertTrue(any(isinstance(f, TTSAudioRawFrame) for f in emitted))
                self.assertFalse(any(isinstance(f, ExternalReplyFrame) for f in emitted))

    async def test_tool_call_speech_is_silent_until_final_answer(self):
        gate, b = await self.make_processor()
        await self.reply(gate, "Let me look that up.", started_tool_call=True)
        b.speak.assert_not_awaited()
        emitted = [call.args[0] for call in gate.push_frame.await_args_list]
        self.assertEqual([type(frame) for frame in emitted],
                         [LLMFullResponseStartFrame, LLMFullResponseEndFrame])

        await self.reply(gate, "The forecast is clear.")
        b.speak.assert_awaited_once_with(device_id="office-id", text="The forecast is clear.")
        self.assertIsInstance(gate.push_frame.await_args.args[0], ExternalReplyFrame)

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
