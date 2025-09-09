# Apple Music Server for macOS

Lightweight local HTTP server that automates the Apple Music app on macOS via AppleScript and exposes a clean web UI plus a JSON API. Used standalone or with the Home Assistant (HA) companion.

## Features

- Web UI to control playback, AirPlay devices, and master volume
- Push updates via Server‑Sent Events (SSE)
- Album/artist/playlist artwork and thumbnails
- Browse/search library helpers (playlists, albums, artists, songs)
- Settings with optional auto‑open browser on launch
- Process control: restart and quit endpoints

## Install

1) Open the **Releases** page of the public repo: `csilvertooth/music_app_server`.
2) Download the latest `Music-App-Server-macos-<arch>.zip` (`arm64` for Apple Silicon, `x86_64` for Intel).
3) Unzip and move **Music App Server.app** to `/Applications`.
4) Launch it. On the first run it will give you two options - Done or Move to Trash.  Click Done and then open System Settings -> Privacy and Security.  Scroll to the bottom and allow Music App Server.
5) Now re-run the app. MacOS will ask for **Automation** permissions to control “Music”. Allow them.
6) Open the UI: http://127.0.0.1:7766/ui (default port is 7766). Change the port in Settings if needed.
7) Optional: add the app to macOS Login Items to launch at login.

Notes
- The app launches the Music application on startup if not running.
- Settings are stored at: `~/Library/Application Support/Music App Server/config.json`.

## Web UI

Open `http://<mac-host>:<port>/ui` for the bundled controller:
- Now playing, transport controls, artwork
- AirPlay devices list (select devices, per‑device volume)
- Master volume slider
- Settings (port, polling intervals, open‑browser on start)
- Buttons: Reload, Save & Restart, Quit

## API Reference

Base URL: `http://<mac-host>:<port>` (default port 7766)

General
- `GET /` → redirects to `/ui`
- `GET /status` → basic health; includes shuffle state and endpoint list
- `GET /ui` → web UI
- `GET /events` → Server‑Sent Events stream of updates `{event, data, ts}`

Settings
- `GET /settings` → returns settings + `config_path`
- `POST /settings` body: `{port?, open_browser?, poll_now_ms?, poll_devices_ms?, poll_master_ms?}` → returns `{ok, restart:false, settings}`
  - Note: The UI now calls `/restart` explicitly after saving when needed.

Playback
- `POST /playpause`
- `POST /play` body: `{playlist|album|artist|song}` (server queues/plays accordingly if implemented)
- `POST /pause`
- `POST /stop`
- `POST /next`
- `POST /previous`
- `POST /resume` body: `{devices?: "Dev A,Dev B"}` (optionally sets AirPlay devices, then plays)

Now Playing & Volume
- `GET /now_playing` → `{state,title,artist,album,position,duration,shuffle,volume}`
- `GET /master_volume` → integer 0–100
- `POST /master_volume` body: `{level:0..100}`
- `POST /set_volume` body: `{volume:0..100}` (sets app master volume)

Shuffle
- `GET /shuffle` → `{enabled}`
- `POST /shuffle` body: `{enabled:true|false}`

AirPlay Devices
- `GET /devices` → `string[]` of device names (deduped)
- `GET /current_devices` → `string[]` currently active devices
- `GET /device_volumes` → `{ name: volume }` map
- `POST /set_devices` body: `{devices:"Dev A,Dev B"}` → `{status, applied: string[]}`
- `POST /set_device_volume` body: `{device:"Name", level:0..100}`
- `GET /airplay_full` → `[{name, volume, active}]` (sorted active first)
- `GET /airplay_debug` → raw AppleScript results for troubleshooting

Browse & Search
- `GET /playlists` → `string[]`
- `GET /albums` → `string[]`
- `GET /artists` → `string[]`
- `GET /songs/<playlist>` → `string[]`
- `GET /songs_by_album/<album>` → `string[]` (in album order)
- `GET /songs_by_artist/<artist>` → `string[]`
- `GET /albums_by_artist/<artist>` → `string[]`
- `GET /search?q=term&types=album,artist,playlist,song&limit=25` → `{albums,artists,playlists,songs,tracks}`

Artwork
- `GET /artwork` → current track artwork bytes (image)
- `GET /artwork_album/<album>` → image bytes
- `GET /artwork_playlist/<playlist>` → image bytes
- `GET /artwork_artist/<artist>` → image bytes
- Thumbnails (server resizes with sips):
  - `GET /artwork_album_thumb/<size>/<album>` → image bytes
  - `GET /artwork_playlist_thumb/<size>/<playlist>` → image bytes
  - `GET /artwork_artist_thumb/<size>/<artist>` → image bytes
- Metadata (etag, ctype) for caching:
  - `GET /artwork_album_meta/<album>`
  - `GET /artwork_playlist_meta/<playlist>`
  - `GET /artwork_artist_meta/<artist>`
  - `GET /artwork_album_thumb_meta/<size>/<album>`
  - `GET /artwork_playlist_thumb_meta/<size>/<playlist>`
  - `GET /artwork_artist_thumb_meta/<size>/<artist>`

System Control
- `POST /restart` (also supports GET) → schedules short‑delay relaunch
- `POST /quit` (also supports GET) → exits process

## Example Requests

Play/pause
```
curl -X POST http://127.0.0.1:7766/playpause
```

Set shuffle off
```
curl -X POST http://127.0.0.1:7766/shuffle -H 'Content-Type: application/json' \
  -d '{"enabled": false}'
```

Set AirPlay devices and per‑device volume
```
curl -X POST http://127.0.0.1:7766/set_devices -H 'Content-Type: application/json' \
  -d '{"devices": "Living Room,Office"}'

curl -X POST http://127.0.0.1:7766/set_device_volume -H 'Content-Type: application/json' \
  -d '{"device": "Living Room", "level": 40}'
```

Get now playing
```
curl http://127.0.0.1:7766/now_playing
```

## Home Assistant (HA) Companion

Use the HA integration to embed the controller in Lovelace and automate from HA. See `apple_music_server_haos/README.md` for installation and dashboard examples.

