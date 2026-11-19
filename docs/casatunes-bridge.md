# CasaTunes voice bridge — development plan

Status: **prototype adapter only, not wired to live audio**. Do not deploy as-is expecting speech redirection.

## Goal

Route finished Jarvis speech **from the Office satellite only** to the CasaTunes Office ceiling-speaker announcement endpoint. Keep Living Room on normal satellite audio and prevent double playback. Preserve a working satellite audio fallback and interruption handling.

The existing Home Assistant `rest_command.casatunes_speak` announcement was previously tested in the Office. This fork's adapter calls that Home Assistant REST service; it **does not reproduce the original Marin audio voice**. Verify the service's request variables before enabling.

## First commit: delivery adapter

`app/casatunes_bridge.py` exposes `CasaTunesBridge.speak(device_id, text)`. It returns success/failure and is **disabled by default**. It does not yet connect to the streaming response pipeline.

Optional environment settings:

- `CASATUNES_BRIDGE_ENABLED=true` (default disabled)
- `CASATUNES_OFFICE_DEVICE_ID=<exact satellite id>`
- `CASATUNES_HA_URL=http://<home-assistant-host>:8123`
- `CASATUNES_HA_TOKEN=<Home Assistant long-lived access token>`

Never commit credentials to this public fork. Use supervised add-on secrets/environment configuration. The adapter currently posts `{"message":"..."}` to `/api/services/rest_command/casatunes_speak`; verify the actual REST command template uses the `message` parameter.

## Integration still required

1. Identify the Office source device in the active websocket session. Do not infer it solely from room names or transcript contents.
2. **Buffer** response audio for the eligible Office route until there is an eligible *final, accepted* assistant transcript. Audio is streamed before the final text; suppression only after a successful announcement would be too late to prevent duplicate speech.
3. Do not route premature acknowledgements, suppressed replies, interrupted answers, or incomplete/ineligible responses. The GPT-Live path uses `session.output_transcript.delta`, `session.output_audio.delta`, and specialized admission/gating; the classic Pipecat path has different events.
4. Send final text to CasaTunes. Only after the Home Assistant service confirms success should the buffered satellite audio be discarded. On any HTTP failure or timeout, release the buffered PCM in order as fallback. Ensure the response does not hang indefinitely.
5. New speech/interruption must cancel or supersede pending announcements safely. A successful HA REST response means the command was accepted, **not** that ceiling-speaker playback completed; further CasaTunes state observation is needed to certify user-heard delivery.
6. Preserve the Living Room route unmodified. Test Office and Living Room concurrently.
7. Add end-to-end tests for successful delivery, silent replies, rapid follow-ups, barge-in, service failure, missing transcript, and restart/rollback.

## Rollout

Keep the currently installed production add-on unchanged. Implement and test the full integration in this feature branch; review a pull request before changing the installed add-on. Start with Office-only opt-in canary tests and retain an immediate flag-based rollback.
