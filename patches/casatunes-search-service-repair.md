# CasaTunes search service repair (draft; not installed)

This is a source-level repair for the installed custom component at
`/config/custom_components/casatunes/media_player.py`.

## Diagnosis, verified from the live HA traceback

- The installed entity service accepts a required plain string `keyword`.
- Its handler calls `self.coordinator.data.search_media(self.zone_id, keyword)`.
- The installed `pycasatunes.search_media` expects a mapping with `artist`,
  `album`, and `track` keys and immediately calls `query.get("artist")`.
- That produces `AttributeError: 'str' object has no attribute 'get'`.
- The integration's `services.yaml` advertises `keyword_artist`,
  `keyword_album`, `keyword_track_name`, and `mode`, unlike its registered
  service schema. A client using those advertised fields is rejected with 400.
- `search_media` only returns a best-match media object; it does not play it.
  The integration must queue/play the returned item ID and refresh state.

## Proposed changes to `media_player.py`

The integration's public source was inspected, but do not assume byte-for-byte
equivalence with the owner's installed copy. Apply only after comparing the live
file and taking a backup.

1. Change `from .const import ATTR_KEYWORD, DOMAIN, SERVICE_SEARCH` to
   `from .const import DOMAIN, SERVICE_SEARCH`.
2. Replace the existing `SEARCH_SCHEMA` assignment with:

```python
SEARCH_MODES = {"playNow", "playShuffle", "playUnshuffle", "add", "addplay"}
SEARCH_SCHEMA = {
    vol.Optional("keyword_artist", default=""): vol.All(str, vol.Length(max=120)),
    vol.Optional("keyword_album", default=""): vol.All(str, vol.Length(max=120)),
    vol.Optional("keyword_track_name", default=""): vol.All(str, vol.Length(max=120)),
    vol.Optional("mode", default="add"): vol.In(SEARCH_MODES),
}
```

3. Replace the original `async def search(self, keyword):` implementation:

```python
    async def search(
        self,
        keyword_artist="",
        keyword_album="",
        keyword_track_name="",
        mode="add",
    ):
        """Search and queue a CasaTunes result using the library contract."""
        query = {
            "artist": keyword_artist.strip(),
            "album": keyword_album.strip(),
            "track": keyword_track_name.strip(),
        }
        if not any(query.values()):
            raise ValueError("Specify an artist, album, or track")
        if mode not in SEARCH_MODES:
            raise ValueError("Invalid CasaTunes queue mode")

        item = await self.coordinator.data.search_media(self.zone_id, query)
        if not isinstance(item, dict) or not item.get("ID"):
            raise ValueError("CasaTunes returned no playable match")

        await self.coordinator.data.queue_media(self.zone_id, item["ID"], mode)
        await self.coordinator.async_refresh()
```

## Safety and acceptance checks

- This is a **candidate fix**, not a completed test of the local API or
  a deployed repair. The underlying `queue_media` request takes zone ID,
  media ID and queue mode; mode semantics should be checked on the owner's
  installed library before any queue-changing canary.
- Search may match several music providers. Do not claim Amazon Music provenance.
- A search result (or HTTP 2xx) does not establish audible playback; require
  a readback of Office `state`, `source`, `media_artist`, `media_title`.
- Preserve other rooms' queues; the Dad source can be shared even if other
  zones are presently off.
- Save the original file, use a syntax check and an integration reload (or,
  only with separate user approval, a full Home Assistant restart). Never
  overwrite the current file without reading it first.
- Confirm the Office-only Creed test against the live state, then update
  Jarvis's draft integration to use the corrected fields.
