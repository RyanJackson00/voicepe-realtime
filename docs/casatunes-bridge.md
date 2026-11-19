# CasaTunes voice bridge — development plan

Status: **experimental GPT-Live response wiring committed, not production tested**. Still disabled by default. Do not deploy to the running add-on without a controlled Office-only test.

## Goal

Route finished Jarvis speech **from the Office satellite only** to the CasaTunes Office ceiling-speaker announcement endpoint. Keep Living Room on normal satellite audio and prevent double playback. Preserve a working satellite audio fallback and interruption handling.

The existing Home Assistant `rest_command.casatunes_speak` announcement was previously tested in the Office. This fork's adapter calls that Home Assistant REST service; it **does not reproduce the original Marin audio voice**. Verify the service's request variables before enabling.

## First commit: delivery adapter

`app/casatunes_bridge.py` exposes `CasaTunesBridge.speak(device_id, text)`. It returns success/failure and is **disabled by default**. It is now invoked at the GPT-Live complete-reply admission stage. The ordinary Realtime/Pipecat path remains unchanged.

Optional environment settings:

- `CASATUNES_BRIDGE_ENABLED=true` (default disabled)
- `CASATUNES_OFFICE_DEVICE_ID=<exact satellite id>`
- `CASATUNES_HA_URL=http://<home-assistant-host>:8123`
- `CASATUNES_HA_TOKEN=<Home Assistant long-lived access token>`

Never commit credentials to this public fork. Use supervised add-on secrets/environment configuration. The adapter currently posts `{"message":"..."}` to `/api/services/rest_command/casatunes_speak`; verify the actual REST command template uses the `message` parameter.

## Wiring implemented for GPT-Live complete replies

The GPT-Live `LiveReplyAdmission` stage collects verified transcript and PCM before release. When the Office satellite matches the configured ID, ordinary completed answers are sent to the Home Assistant CasaTunes command first. On HTTP success, Jarvis withholds satellite PCM and marks the answer handled. On HTTP failure, the original PCM is played through the satellite. Streaming complete replies are disabled for an eligible Office satellite so they cannot leak audio before the CasaTunes choice. Confirmation prompts are kept on the original satellite path because a REST success does not prove playback. Other device IDs, including Living Room, use the existing pipeline.

**Unresolved risks:** A successful REST response only confirms service acceptance, not actual audible playback or music resume. A user interruption while CasaTunes is processing a REST request cannot stop a remote announcement. The direct Realtime/Pipecat path is not yet bridged. The complete Live path needs hardware-in-the-loop tests before enablement.

## Integration still required

1. Verify the exact Office satellite ID exposed by the active websocket session and configure it securely. Do not infer it solely from room names or transcript contents.
2. Validate on an Office canary that LiveReplyAdmission holds PCM until the finalized transcript and that speech is never duplicated. Its existing buffering is reused rather than recreated.
3. Validate that premature acknowledgements, suppressed replies, interrupted answers, and incomplete/ineligible responses never route. The GPT-Live path uses `session.output_transcript.delta`, `session.output_audio.delta`, and specialized admission/gating; the classic Pipecat path has different events.
4. Test service success, timeout, disconnect, stale responses, and server failure against the fallback. Verify the existing admission bookkeeping does not start an incorrect follow-up timer.
5. New speech/interruption must cancel or supersede pending announcements safely. A successful HA REST response means the command was accepted, **not** that ceiling-speaker playback completed; further CasaTunes state observation is needed to certify user-heard delivery.
6. Preserve the Living Room route unmodified. Test Office and Living Room concurrently.
7. Add end-to-end tests for successful delivery, silent replies, rapid follow-ups, barge-in, service failure, missing transcript, and restart/rollback.

## Rollout

Keep the currently installed production add-on unchanged. Implement and test the full integration in this feature branch; review a pull request before changing the installed add-on. Start with Office-only opt-in canary tests and retain an immediate flag-based rollback.
