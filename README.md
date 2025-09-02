# Music App Server (macOS)

A lightweight Flask + AppleScript service that controls the **Music** app on macOS. It powers the companion Home Assistant integration (“Music App Controller”), exposes a simple web UI, and provides a REST API for playback, browsing, search, artwork, and AirPlay device control.

> **Note:** This project is community-built and not affiliated with Apple Inc. See **Trademarks** below.

---

## Features
- Mini **web UI** at `/ui` (play/pause/next/prev, master volume, AirPlay device selection with per-device volumes)
- **REST API** for playlists, albums, artists, songs, search, and playback
- **Precise track playback**: disambiguates same-title songs using album/artist or playlist+index
- **Artwork** endpoint (with format auto-detection) and small SVG icons for browse categories
- **AirPlay**: list devices, apply outputs immediately, get per-device volumes
- **Settings** persisted at: `~/Library/Application Support/Music App Server/config.json`
  - Port, open-browser, and **polling** intervals (now playing / devices / master volume)

---

## Quick Start

### A) Download the macOS app (recommended)
1. Open the **Releases** page of the public repo: `csilvertooth/music_app_server`.
2. Download the latest `Music-App-Server-macos-<arch>.zip` (`arm64` for Apple Silicon, `x86_64` for Intel).
3. Unzip and move **Music App Server.app** to `/Applications`.
4. Launch it. On the first run it will give you two options - Done or Move to Trash.  Click Done and then open System Settings -> Privacy and Security.  Scroll to the bottom and allow Music App Server.
5. Now re-run the app. MacOS will ask for **Automation** permissions to control “Music”. Allow them.
5. Open the UI at <http://localhost:7766/ui> (or your configured host/port).

> The first time you change AirPlay outputs or volumes, macOS may prompt for permissions—allow them.

### B) Run from source
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python music_app_server.py