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

### Option 1: Pre-built App Bundle (Recommended)

1) Open the **Releases** page of the public repo: `csilvertooth/music_app_server`.
2) Download the latest `Music-App-Server-macos-<arch>.zip` (`arm64` for Apple Silicon, `x86_64` for Intel).
3) Unzip and move **Music App Server.app** to `/Applications`.
4) Launch it. On the first run it will give you two options - Done or Move to Trash.  Click Done and then open System Settings -> Privacy and Security.  Scroll to the bottom and allow Music App Server.
5) Now re-run the app. MacOS will ask for **Automation** permissions to control "Music". Allow them.
6) Open the UI: http://127.0.0.1:7766/ui (default port is 7766). Change the port in Settings if needed.
7) Optional: add the app to macOS Login Items to launch at login.

Notes
- The app launches the Music application on startup if not running.
- Settings are stored at: `~/Library/Application Support/Music App Server/config.json`.

### Option 2: Run Directly via Python3

If you prefer to run the server directly from source:

1) Ensure Python 3.8+ is installed on your macOS system.
2) Install dependencies:
   ```
   pip3 install flask pillow
   ```
   Or from the provided requirements.txt:
   ```
   pip3 install -r requirements.txt
   ```
3) Grant permissions:
   - Open System Settings -> Privacy and Security -> Automation.
   - Allow Python (or your terminal app) to control "Music".
4) Run the server:
   ```
   python3 music_app_server.py
   ```
5) The server will start on http://127.0.0.1:7766 (default port). It will automatically open your browser to the UI at `/ui`.
6) To stop the server, press Ctrl+C in the terminal.

Notes
- The script launches the Music application on startup if not running.
- Settings are stored at: `~/Library/Application Support/Music App Server/config.json`.
- For testing, you can run the server and access the API endpoints directly or via the web UI.

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

## Media Player Integration for Home Assistant

The server now exposes each AirPlay device as an individual media player entity for Home Assistant. This allows integration with maxi-media-player or other HA components that work with media players.

### Media Player Endpoints

- `GET /media_players` → List of available AirPlay media players
- `GET /media_player/airplay_<device_slug>` → Get status of specific media player
- `POST /media_player/airplay_<device_slug>/play` → Select device and start playback
- `POST /media_player/airplay_<device_slug>/pause` → Pause playback
- `POST /media_player/airplay_<device_slug>/stop` → Stop playback
- `POST /media_player/airplay_<device_slug>/volume` body: `{volume_level:0..1}` → Set device volume
- `POST /media_player/airplay_<device_slug>/next` → Next track
- `POST /media_player/airplay_<device_slug>/previous` → Previous track

Each media player represents an AirPlay device with:
- `entity_id`: `media_player.airplay_<device_slug>` (e.g., `media_player.airplay_Kitchen_Speaker`)
- `state`: "playing" if device is active and master playback is playing, "paused" otherwise
- `volume_level`: 0.0-1.0 (device volume level)
- `friendly_name`: "AirPlay: <device_name>"
- `supported_features`: ["play", "pause", "stop", "volume_set", "volume_step", "next_track", "previous_track"]

### Integration with maxi-media-player

maxi-media-player groups media players that are already in HA. To use AirPlay devices:

1. **Create HA REST Media Player Sensors:**
   For each AirPlay device, create REST sensors in HA that poll the server endpoints.

   Example `configuration.yaml` (adjust for your devices and server address):

   ```yaml
   media_player:
     - platform: rest
       resource: http://192.168.1.100:7766/media_player/airplay_Kitchen_Speaker
       resource_template: "{{ state_url }}"
       method: GET
       body_on: true
       body_off: true
       headers:
         Accept: application/json
         Content-Type: application/json
       name: "Kitchen Speaker"
       unique_id: "kitchen_speaker_rest_mp"
       commands:
         play: !include
           - http://192.168.1.100:7766/media_player/airplay_Kitchen_Speaker/play
           - method: POST
         pause: !include
           - http://192.168.1.100:7766/media_player/airplay_Kitchen_Speaker/pause
           - method: POST
         stop: !include
           - http://192.168.1.100:7766/media_player/airplay_Kitchen_Speaker/stop
           - method: POST
         next: !include
           - http://192.168.1.100:7766/media_player/airplay_Kitchen_Speaker/next
           - method: POST
         previous: !include
           - http://192.168.1.100:7766/media_player/airplay_Kitchen_Speaker/previous
           - method: POST
         volume_set: !include
           - http://192.168.1.100:7766/media_player/airplay_Kitchen_Speaker/volume
           - method: POST
           - headers:
               Content-Type: application/json
           - payload: '{"volume_level": {{ volume_level }}}'

   # Add similar blocks for each AirPlay device (Living_Room_Speaker, etc.)
   ```

2. **Configure maxi-media-player:**
   Once the REST media players are available in HA, configure maxi-media-player:

   ```yaml
   maxi_media_player:
     group_members:
       - media_player.kitchen_speaker
       - media_player.living_room_speaker
       # Add other AirPlay device media players
   ```

3. **Dynamic Device Discovery:**
   To automatically discover AirPlay devices, you might create a script that polls `/media_players` and generates the HA config dynamically.

### Notes

- Device slug is created from device name with spaces replaced by underscores
- When "play" is called on a media player, it selects that device as the output and starts/resumes master playback
- All transport controls (play/pause/stop/next/previous) affect the global playback state, not individual device states
- Volume controls are per-device
- Multiple devices can be playing simultaneously if you start playback on different devices

## Home Assistant (HA) Companion

Use the HA integration to embed the controller in Lovelace and automate from HA. See `apple_music_server_haos/README.md` for installation and dashboard examples.
