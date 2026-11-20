# Jarvis → CasaTunes music and source control (draft)

> Status: **implemented in draft PR #2, not deployed, not hardware-validated**. Independent of PR #1 (CasaTunes text-to-speech over ceiling speakers). Tested Home Assistant service definitions by read-only inspection; no music was played or queue modified during development.

## What the user wants

Say, “Hey Jarvis, play Greta Van Fleet in the living room from Amazon Music.” Jarvis should find the requested artist/album/track, choose a suitable CasaTunes source, assign it to the living room if needed, queue and start playback, and report the **actual** result. Follow-up commands should support adding songs/playlists, source switching, grouping, volume and stop.

**Distinctions:** Amazon Music is a catalog/service. "Dad", "Mom", "Piper", etc. are CasaTunes sources or stream players. Living Room, Kitchen, Office, etc. are zones. A music queue belongs to a source; assigning that source to a room makes the room hear it. If two rooms share Dad, replacing Dad's queue changes both rooms' music.

## Verified capabilities, 2026-11-19

The owner's Home Assistant exposes `media_player` entities for CasaTunes zones, with source list and supported browsing. Read-only `media_player.browse_media` returned Amazon Music, Spotify Browser, CasaTunes Playlists and other services, and Amazon Music categories Playlists, Stations, Charts, Recommended, New and My Music.

**Key discovery:** Home Assistant's active service registry also exposes the CasaTunes integration's dedicated `casatunes.search` entity action:

- `entity_id`: exact CasaTunes `media_player.*` room
- `keyword_artist`: artist (e.g., "Greta Van Fleet")
- `keyword_album`: album title
- `keyword_track_name`: song title
- `mode`:
  - `playNow` — replace queue and play
  - `playShuffle` — replace queue and shuffle
  - `playUnshuffle` — replace queue and play unshuffled
  - `add` — append to queue
  - `addplay` — append and start newly added music

This is an official service **registered by the installed CasaTunes Home Assistant integration**, and is preferable to guessing undocumented direct REST search routes. The external CasaTunes documentation confirms it supports searching configured services, but **this Home Assistant action does not expose a service-specific selector**. It is NOT established that `keyword_artist` will search only Amazon Music or prioritize Amazon rather than another connected service. Do not claim it does until tested.

The integration's public source for an earlier version registers a `search` entity service that internally calls its CasaTunes library client. The schema observed in the owner's active Home Assistant instance is richer, so use the **live registry** as authority and verify actual behavior on the installed version. No live search action was executed because it can modify the queue.

## New implementation (feature branch)

- `app/casatunes_music_tool.py`: opt-in config, exact room allowlist and Supervisor proxy-only HA access; CasaTunes room source assignment with valid source check and post-call state verification.
- `app/casatunes_native_search.py`: callable `casatunes_request_music(room,artist,album,track,mode,expected_source)`. Calls the installed `casatunes.search` Home Assistant service with the registered keyword and mode fields; rejects invalid/missing searches, unavailable rooms, mismatched expected source, and replace-queue requests when another enabled zone is already playing from that source.
- `app/main.py`: Realtime and GPT-Live register these two tools only when enabled. Uses the existing `GuardedToolsMixin` shared by both voice runtimes.
- `config.yaml` and `root/run.sh`: optional no-default `casatunes_music_enabled` and `casatunes_music_rooms` settings, passed into runtime; **existing installs remain unchanged**.
- Unit tests run without reaching either Home Assistant or CasaTunes.

### Proposed add-on options once approved for canary

In the private Home Assistant add-on configuration (never in public GitHub):

```yaml
casatunes_music_enabled: true
casatunes_music_rooms: "media_player.YOUR_OFFICE_ENTITY,media_player.YOUR_LIVING_ROOM_ENTITY"
```

The IDs above are **illustrations**, not the actual installation. Use verified live CasaTunes `media_player` entity IDs from the private Home Assistant instance; allow only selected room(s) initially.

No additional CasaTunes server URL or credential is needed for this path; the add-on uses Home Assistant's existing Supervisor proxy and the installed CasaTunes integration.

### Return semantics

An HTTP success from `casatunes.search` means Home Assistant accepted a service request. It does **not** prove:
- a matching artist/song was found;
- which underlying music service supplied the match;
- a source's current queue actually changed;
- the intended room is on and playing;
- audio is physically audible.

The current tool correctly returns a status plus explicit warning about those gaps. **The end-to-end voice command is not complete until result verification is added.** The model must never say “Playing Greta Van Fleet from Amazon Music now” without confirmation.

## Rollout/acceptance gates before production

1. Run Python compilation, relevant unit tests, full suite, code/security checks on the draft PR head. Repair any introduced failures.
2. Review the active `casatunes.search` schema against local integration code to confirm mode handling and exact keyword fields; validate “from Amazon Music” scoping or add a true service-selective CasaTunes REST API call using the local `/casadev` schema.
3. Verify source collision and queue replacement behavior with a harmless, private, dedicated test source, **with user approval**. No impact to music in other rooms.
4. Read room state after action (source, playback state, track name/artist) and add bounded verification, handling async propagation without blindly retrying side effects.
5. Add source auto-selection policy: choose an idle source, preserve active queues, and make explicit confirmation for replacing other rooms' music. Avoid a language model supplying its own authorization flag.
6. Test requested content on Amazon, a nonexistent artist, ambiguous hits, source stopped/off, multiple rooms sharing source, and app/network failures.
7. Test pause/stop, add-to-queue, queue display and grouping separately. Add new tools only when supported and tested.
8. Canary on one explicitly approved room; leave existing PR #1 and all production CasaTunes/TTS settings intact.

## Local testing notes

CI will run without access to the user's CasaTunes LAN. Do **not** add private IPs, household names, album titles from personal libraries, access tokens, raw transcripts or production service responses to public CI artifacts.

## Reference

- [CasaTunes developer API documentation](https://www.casatunes.com/support/docs/rest-api) and local `/casadev` sandbox.
- [CasaTunes search settings](https://www.casatunes.com/support/docs/casasetup).
- [CasaTunes Home Assistant integration](https://github.com/jonkristian/casatunes), noting the installed version may differ from public repository source.
