# CasaTunes voice bridge

The bridge is experimental and disabled by default. It routes eligible completed replies from one Office satellite to the CasaTunes ceiling-speaker announcement service in Home Assistant. CasaTunes synthesizes the speech in its own voice. Living Room and other satellites use their usual speakers.

## Configuration

In the add-on's Configuration tab, `casatunes_bridge_enabled` defaults to `false` and `casatunes_office_device_id` defaults to empty. The device ID must match the Office client's actual websocket device ID.

The add-on uses its existing `SUPERVISOR_TOKEN` and `http://supervisor/core` by default. It posts `{"message":"..."}` to `/api/services/rest_command/casatunes_speak?return_response` through that proxy. Confirm the Home Assistant command's template accepts `message` and targets the Office zone before enabling the add-on option.

For standalone development, `CASATUNES_BRIDGE_ENABLED` and `CASATUNES_OFFICE_DEVICE_ID` provide the same options. `CASATUNES_HA_URL` and `CASATUNES_HA_TOKEN` allow a separate development Home Assistant endpoint and credential. Keep tokens out of Git.

## Reply handling

- GPT-Live uses the existing `LiveReplyAdmission` buffer. The bridge leaves confirmation prompts on the satellite and disables streaming delivery for the eligible device so its original audio is available for fallback.
- Pipecat Realtime buffers the original audio and transcript through the response-end frame. Confirmation-like replies and text-only responses stay on the original path. An interruption discards held frames. The hold timeout and audio limit release the original frames.
- Satellite PCM is withheld only when both Home Assistant and the REST command's returned CasaTunes status indicate HTTP success. Request failures release the original PCM when the reply is still current. The bridge does not follow redirects.

The returned status confirms the endpoint accepted the request. Audible playback, music resumption, and cancellation after dispatch still require hardware validation. The Realtime confirmation filter is heuristic, and remote playback does not provide the same timing signals as the satellite.

## Office canary

After rollout approval, verify the exact device ID, confirm the deployed command's payload, and test on an isolated Office canary. Check one audible reply with no duplicate satellite audio, Living Room playback, confirmations, missing transcripts, interruptions, follow-up timing, service failure, and music resumption. Disable `casatunes_bridge_enabled` to return to satellite output and restart the canary add-on to apply the option.
