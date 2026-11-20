# CasaTunes voice bridge

The bridge is experimental and disabled by default. It routes eligible completed replies from one Office satellite to the CasaTunes ceiling-speaker announcement service in Home Assistant. CasaTunes synthesizes the speech in its own voice. Living Room and other satellites use their usual speakers.

## Configuration

In the add-on's Configuration tab, `casatunes_bridge_enabled` defaults to `false` and `casatunes_office_device_id` defaults to empty. Routing requires both the exact websocket device ID and a token-authenticated connection. A shared device token establishes membership in the trusted set of satellites.

The add-on uses its existing `SUPERVISOR_TOKEN` and `http://supervisor/core` by default. It posts `{"message":"..."}` to `/api/services/rest_command/casatunes_speak?return_response` through that proxy. Confirm the Home Assistant command's template accepts `message` and targets the Office zone before enabling the add-on option.

For standalone development, `CASATUNES_BRIDGE_ENABLED` and `CASATUNES_OFFICE_DEVICE_ID` provide the same options. A custom `CASATUNES_HA_URL` requires its own `CASATUNES_HA_TOKEN` and HTTPS, with HTTP allowed on loopback for tests. The Supervisor token stays scoped to the built-in Core proxy. Keep tokens out of Git.

## Reply handling

- GPT-Live uses the existing `LiveReplyAdmission` buffer. The bridge leaves confirmation prompts on the satellite and disables streaming delivery for the eligible device so its original audio is available for fallback.
- Pipecat Realtime buffers audio and transcript until the server confirms a completed response. Questions that end with a question mark and confirmation prompts stay on the satellite so its follow-up microphone can reopen. Realtime discards other spoken output from a response that starts a new tool call and waits for the later answer. Text-only responses stay local. A device stop or barge-in invalidates held frames and any outstanding local request. The hold timeout and audio limit release the original frames in order.
- The bridged Realtime connection skips local spoken progress prompts. Other connections keep their configured progress prompts. Audible errors and confirmation prompts still have their normal paths.
- Successful nested CasaTunes status suppresses satellite PCM. A definitive rejection releases PCM when the reply is still current. After an ambiguous timeout, malformed response, or proxy error, the bridge skips both retry and PCM replay because the original announcement may already be playing.
- After external delivery or an uncertain outcome, the satellite returns to idle without opening follow-up listening. A new wake starts the next turn.

The returned status confirms endpoint acceptance. Audible playback, music resumption, and cancellation after dispatch still require hardware validation. Local interruption cannot revoke an already dispatched announcement.

The Realtime delivery log reports milliseconds from model completion to service dispatch and from dispatch to the service result. Neither measurement uses a speaker playback signal.

## Office canary

After rollout approval, verify the exact device ID, confirm the deployed command's payload, and test on an isolated Office canary. Check one audible reply with no duplicate satellite audio, Living Room playback, confirmations, missing transcripts, interruptions, follow-up timing, service failure, and music resumption. Disable `casatunes_bridge_enabled` to return to satellite output and restart the canary add-on to apply the option.
