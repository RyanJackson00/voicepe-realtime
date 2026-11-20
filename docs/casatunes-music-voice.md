# CasaTunes music commands for Jarvis (development branch)

Status: **developer preview, disabled by default**. No production Home Assistant or CasaTunes settings were modified. This feature is independent of the Office ceiling-speaker announcement bridge.

## User stories

- “Hey Jarvis, play Greta Van Fleet in the living room from Amazon Music.”
- “Play Highway Tune in the kitchen.”
- “Set the office source to Dad.”
- Future: add to queue, play next, grouping, queue editing, resume music after announcements.

## Verified environment from private owner conversation

Home Assistant already exposes CasaTunes zone `media_player` entities and existing group entities; their `source_list` contains configured player names. CasaTunes Amazon Music browsing is accessible through `media_player.browse_media`, including Playlists, Stations, Charts, Recommended, New and My Music. The players **do not advertise native Home Assistant search_media** in `supported_features`.

The public CasaTunes documentation confirms a REST API at `/api/v1` (server-specific API sandbox at `/casadev`) and supports searching within one or multiple music services. **The detailed search route, parameter names, response shape, and playback semantics for this particular server remain unverified.** Never invent those details or claim an artist search succeeded when the configured contract is missing.

## Architecture

There are three distinct objects:

1. Music **service**: Amazon Music, Spotify Browser, TuneIn, etc.
2. Music **source/player**: CasaTunes configured names such as Dad or Mom. A source has a music queue.
3. **Room/zone**: a Home Assistant CasaTunes `media_player`, configured to listen to a source. Multiple rooms may share one source.

A requested music service is not itself a room source. In a complete implementation, Jarvis must resolve a source, locate music within its selected service, queue/play that music on the source, route the source to the room, and verify state. Changing music for a source used by multiple rooms changes what all those rooms hear; do not overwrite another room's session silently.

## New Python module

`app/casatunes_music_tool.py` provides (opt-in):

- `casatunes_search_music(room, query, service)` — query a configured CasaTunes search API route, validate response, return selection keys. **Fails closed by default**, because the exact CasaDev search contract is not yet known.
- `casatunes_play_music(room, selection)` — submit a prior verified search selection to Home Assistant's `media_player.play_media`. A response confirms HA accepted a request, not that the speaker played.
- `casatunes_assign_source(room, source)` — check the room's actual available source list, send `select_source`, and verify the new reported source (which can lag). No arbitrary source name accepted.

These are registered for Realtime and GPT-Live only with the explicit opt-in flag. The feature intentionally does not emit a false spoken “Playing now” when no verified search API exists.

## Environment (development only, not production instructions)

- `CASATUNES_MUSIC_ENABLED=false` default. Explicit `true` is required.
- `CASATUNES_MUSIC_ROOMS`: comma-separated **exact**, owner-configured HA CasaTunes `media_player` IDs. No room is allowed by default.
- `CASATUNES_MUSIC_HA_URL`: defaults to internal `http://supervisor/core`; prefer internal Supervisor token.
- `CASATUNES_MUSIC_HA_TOKEN`: optional explicit token; falls back to `SUPERVISOR_TOKEN`.
- `CASATUNES_MUSIC_API_URL`: trusted CasaTunes API base, e.g. `http://your-casa-server:8735`. Never hardcode a household address in this public repo.
- `CASATUNES_MUSIC_SEARCH_ROUTE`: verified relative route beginning with `/api/v1/`. **NOT KNOWN YET.** Placeholder endpoints in code tests are not deployment instructions.

The current adapter expects a JSON top-level object with `results`, where each candidate has `title`, `media_content_id` and `media_content_type`. That is an **internal adapter interface, not a documented CasaTunes native response**. Before enabling, implement a version-specific mapper from the actual CasaDev search response to this shape. Confirm whether query parameter is `query`, source/service scoping, authentication, and whether Home Assistant `play_media` accepts the resulting IDs. Add fixture tests using **sanitized** actual server responses.

## Before production enablement (required)

1. In a local browser on the home network, inspect `http://<your-CasaTunes-host>/casadev`. Obtain the complete **Search** and **Play / Queue** method signatures and benign example JSON output. Redact addresses, credentials, private playlist names and anything household-identifying.
2. Identify source instances and exact zone/source assignment. Verify the target *source* has a separate queue and is not serving another occupied room; add collision-prevention logic.
3. Add adapter conversion tests from actual CasaDev responses, service filtering, invalid queries, zero/ambiguous results, duplicates, unauthorized access, service unavailable, and idempotency.
4. Validate a read-only artist search for Greta Van Fleet on Amazon Music. Do not issue playback during discovery.
5. Add tests ensuring an explicit user request for playback, choose a matching artist without false-positive autocomplete, then route and play safely.
6. Verify playback result via actual entity state / API status and a user-heard Office or Living Room canary. Require explicit approval before making a live CasaTunes request that changes playback.
7. Keep both CasaTunes voice feature branches in draft until tests and rollout gates pass. Confirm compatibility when combining with the separate announcement bridge.

### Why this is partial

A server-specific API contract cannot be established from the public generic REST overview alone. Implementing an undocumented endpoint and publishing it as working would be unsafe. The code and registration here form a safe starting point, **not completed artist search/voice playback**.
