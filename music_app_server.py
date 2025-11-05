import subprocess
import json
import os
import hashlib
import sys
import logging
import tempfile
import threading
import time
import io

from queue import Queue
from flask import Flask, request, jsonify, Response, render_template_string, redirect

import base64

try:
    from PIL import Image  # type: ignore
except Exception:  # Pillow may not be available; fall back to sips/JPEG/PNG
    Image = None
WEBP_ENABLED = Image is not None

logging.basicConfig(level=logging.DEBUG)  # Enable debug logging for requests
LIB_LIST_DEBUG = os.getenv("AM_LIB_LIST_DEBUG", "0") == "1"

def _guess_image_mime(data: bytes) -> str:
    """Best-effort guess for artwork bytes without external deps."""
    if not data:
        return "application/octet-stream"
    header = data[:16]
    # WEBP: RIFF....WEBP
    try:
        if header.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
            return "image/webp"
    except Exception:
        pass
    if header.startswith(b"\xFF\xD8\xFF"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"GIF87a") or header.startswith(b"GIF89a"):
        return "image/gif"
    if header.startswith(b"BM"):
        return "image/bmp"
    if header.startswith(b"II*\x00") or header.startswith(b"MM\x00*"):
        return "image/tiff"
    return "image/jpeg"

def _convert_to_webp(data: bytes, max_size: int | None = None) -> tuple[bytes, str] | None:
    """Convert image bytes to WEBP (optionally resizing to <=max_size). Returns (bytes, mime) or None if unavailable."""
    if not data or Image is None:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            try:
                resample = getattr(Image, 'LANCZOS', Image.BICUBIC)
            except Exception:
                resample = Image.BICUBIC
            if max_size and max_size > 0:
                im.thumbnail((int(max_size), int(max_size)), resample=resample)
            buf = io.BytesIO()
            save_kwargs = {"format": "WEBP", "quality": 85, "method": 4}
            try:
                im.save(buf, **save_kwargs)
            except Exception:
                # Try without method if unsupported
                save_kwargs.pop("method", None)
                im.save(buf, **save_kwargs)
            return buf.getvalue(), "image/webp"
    except Exception:
        return None


app = Flask(__name__)

# ---- SSE pub/sub for push updates ----
_subscribers = set()
_sub_lock = threading.Lock()
_last_snapshot = {"now": None, "airplay": None, "master": None, "shuffle": None, "art_tok": int(time.time()), "art_hash": None}
_BLANK_PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAoMBgQ2QY1QAAAAASUVORK5CYII="
)


def _sse_subscribe():
    q = Queue(maxsize=100)
    with _sub_lock:
        _subscribers.add(q)
    return q


def _sse_unsubscribe(q):
    with _sub_lock:
        _subscribers.discard(q)


def _sse_publish(event: str, data):
    payload = {"event": event, "data": data, "ts": int(time.time() * 1000)}
    # Surface artwork token at the top-level for convenience
    try:
        if isinstance(data, dict):
            if event == 'now' and 'artwork_token' in data:
                payload['artwork_token'] = data.get('artwork_token')
            elif event == 'snapshot' and 'artwork_token' in data:
                payload['artwork_token'] = data.get('artwork_token')
            # Also surface an artwork_etag if known
            if 'artwork_etag' in data:
                payload['artwork_etag'] = data.get('artwork_etag')
            else:
                try:
                    if _last_snapshot.get('art_hash'):
                        payload['artwork_etag'] = _last_snapshot.get('art_hash')
                except Exception:
                    pass
    except Exception:
        pass
    msg = json.dumps(payload, ensure_ascii=False)
    dead = []
    with _sub_lock:
        for q in list(_subscribers):
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            _subscribers.discard(q)


# ---- Minimal state readers ----

def _get_now_playing_dict():
    # Use the exact robust script shape as /now_playing (no PID), then adapt to a compact dict
    script = '''
    tell application "Music"
        set pstate to player state as text
        set shuf to false
        try
            set shuf to shuffle enabled
        end try
        set rep to song repeat
        set vol to 0
        try
            set vol to sound volume
        end try
        if pstate is "stopped" then
            set AppleScript's text item delimiters to linefeed
            return pstate & "\n" & "" & "\n" & "" & "\n" & "" & "\n" & "0" & "\n" & (shuf as text) & "\n" & (rep as text) & "\n" & (vol as text) & "\n" & "0"
        end if
        set nm to ""
        set ar to ""
        set al to ""
        set pos to 0
        set dur to 0
        try
            set pos to player position
        end try
        try
            set t to current track
        on error
            set t to missing value
        end try
        if t is not missing value then
            try
                set nm to (name of t as text)
            end try
            try
                set ar to (artist of t as text)
            end try
            try
                set al to (album of t as text)
            end try
            try
                set dur to (duration of t)
            end try
        end if
        set AppleScript's text item delimiters to linefeed
        return pstate & "\n" & nm & "\n" & ar & "\n" & al & "\n" & (pos as text) & "\n" & (shuf as text) & "\n" & (rep as text) & "\n" & (vol as text) & "\n" & (dur as text)
    end tell
    '''
    r = run_applescript(script)
    if isinstance(r, dict):
        return {"state": "unknown"}
    if not isinstance(r, str) or not r:
        return {"state": "unknown"}
    lines = r.splitlines()
    while len(lines) < 9:
        lines.append("")
    state, title, artist, album, position, shuffle_txt, repeat_txt, volume_txt, duration = lines[:9]
    def _to_float(s):
        try:
            return float(s)
        except Exception:
            return 0.0
    def _to_bool(s):
        s = (s or "").strip().lower()
        return s in ("true", "yes", "1")
    out = {
        "state": state or "unknown",
        "title": title or "",
        "artist": artist or "",
        "album": album or "",
        "pid": "",
        "position": _to_float(position),
        "is_playing": (state or "").lower().startswith("play"),
        "shuffle": _to_bool(shuffle_txt) if shuffle_txt != '' else None,
        "repeat": _to_bool(repeat_txt) if repeat_txt != '' else None,
    }
    return out


def _get_master_volume_percent():
    script = '''
    tell application "Music"
        try
            return (sound volume as integer)
        on error
            return -1
        end try
    end tell
    '''
    r = run_applescript(script)
    try:
        v = int(r)
        return max(0, min(100, v))
    except Exception:
        return -1

### AirPlay device status.
def _read_airplay_full():
    """Return list of {name, active} for AirPlay devices."""
    script_primary = '''
    tell application "Music"
        set outLines to {}
        try
            repeat with d in AirPlay devices
                set nm to ""
                set isSel to false
                try
                    set nm to (name of d as text)
                end try
                try
                    set isSel to (selected of d)
                end try
                set end of outLines to nm & tab & (isSel as text)
            end repeat
        on error errm number errn
            return "ERROR:" & errn & ":" & errm
        end try
        set AppleScript's text item delimiters to linefeed
        return outLines as text
    end tell
    '''
    result = run_applescript(script_primary)
    if isinstance(result, dict) or (isinstance(result, str) and result.startswith("ERROR:")):
        app.logger.warning(f"/airplay_full primary failure: {result if isinstance(result, dict) else result}")
        script_fallback = '''
        tell application "Music"
            set outLines to {}
            set nameList to {}
            try
                set nameList to name of AirPlay devices
            on error
                set nameList to {}
            end try
            repeat with nm in nameList
                set isSel to false
                try
                    set d = (first AirPlay device whose name is (nm as text))
                    try
                        set isSel to (selected of d)
                    end try
                end try
                set end of outLines to (nm as text) & tab & (isSel as text)
            end repeat
            set AppleScript's text item delimiters to linefeed
            return outLines as text
        end tell
        '''
        result = run_applescript(script_fallback)
        if isinstance(result, dict):
            app.logger.error(f"/airplay_full fallback AppleScript error: {result.get('error')}")
            return []

    items = []
    if isinstance(result, str) and result:
        for line in result.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                name = parts[0].strip()
                sel = parts[1].strip().lower()
                if name:
                    items.append({"name": name, "active": sel in ("true", "yes", "1")})
    try:
        items.sort(key=lambda d: (not bool(d.get("active")), str(d.get("name", "")).casefold()))
    except Exception:
        pass
    return items


def _get_airplay_volumes():
    """Return dict of device name -> volume (0-100) for AirPlay devices."""
    script = '''
    tell application "Music"
        try
            set devNames to name of AirPlay devices
        on error
            try
                set devNames to name of current AirPlay devices
            on error
                set devNames to {}
            end try
        end try
        set outLines to {}
        repeat with nm in devNames
            set volTxt to "-1"
            try
                set v to sound volume of (first AirPlay device whose name is (nm as text))
                set volTxt to (v as text)
            end try
            set end of outLines to ((nm as text) & tab & volTxt)
        end repeat
        set AppleScript's text item delimiters to linefeed
        return outLines as text
    end tell
    '''
    result = run_applescript(script)
    volumes = {}
    if isinstance(result, str) and result:
        for line in result.splitlines():
            if "\t" in line:
                name, vol = line.split("\t", 1)
                name = name.strip()
                try:
                    v = int(float(vol.strip()))
                    volumes[name] = max(0, min(100, v))
                except Exception:
                    volumes[name] = None
    return volumes


def _set_airplay_device_volume(device, level):
    """Set volume for a specific AirPlay device."""
    if not device or level is None:
        return False
    try:
        level = int(level)
    except Exception:
        return False
    level = max(0, min(100, level))
    device_safe = applescript_escape(device)
    script = f'''
    tell application "Music"
        try
            set sound volume of (first AirPlay device whose name is "{device_safe}") to {level}
            return "ok"
        on error errm number errn
            return "ERROR:" & errn & ":" & errm
        end try
    end tell
    '''
    result = run_applescript(script)
    if isinstance(result, str) and result.startswith('ERROR:'):
        return False
    if isinstance(result, dict) and 'error' in result:
        return False
    return True


def _current_snapshot():
    now = _get_now_playing_dict()
    shuffle = bool(get_shuffle_enabled())
    master = _get_master_volume_percent()
    air_status = _read_airplay_full()
    air_volumes = _get_airplay_volumes()
    for item in air_status:
        name = item['name']
        item['volume'] = air_volumes.get(name, None)
    air = air_status
    return {
        "now": now,
        "shuffle": shuffle,
        "master": master,
        "airplay": air,
        "artwork_token": _last_snapshot.get("art_tok", int(time.time()))
    }


# ---- Background watchers ----
_watchers_started = False


def _start_watchers_once():
    global _watchers_started
    if _watchers_started:
        return
    _watchers_started = True
    threading.Thread(target=_watch_now_loop, daemon=True).start()
    threading.Thread(target=_watch_airplay_loop, daemon=True).start()
    threading.Thread(target=_watch_master_loop, daemon=True).start()


def _watch_now_loop():
    last_pid = None
    last_state = None
    last_meta_key = None  # title|artist|album
    last_pos = None
    while True:
        s = load_settings()
        itv = max(0.8, (s.get('poll_now_ms', 1500) / 1000.0))
        try:
            now = _get_now_playing_dict()
            pid = now.get('pid') or ''
            st = now.get('state')
            title = (now.get('title') or '').strip()
            artist = (now.get('artist') or '').strip()
            album = (now.get('album') or '').strip()
            meta_key = f"{title}|{artist}|{album}"
            pos = None
            try:
                pos = float(now.get('position') or 0.0)
            except Exception:
                pos = None

            changed = (pid != last_pid) or (st != last_state)
            # If persistent ID is unreliable/missing, detect track change by metadata
            meta_changed = (meta_key != last_meta_key) and bool(meta_key)
            # Detect restart (position dropped significantly)
            restarted = False
            try:
                if pos is not None and last_pos is not None and (pos + 1.5) < last_pos:
                    restarted = True
            except Exception:
                restarted = False

            if changed or meta_changed or restarted:
                last_pid = pid
                last_state = st
                last_meta_key = meta_key
                last_pos = pos if pos is not None else last_pos
                # Bump artwork token on track change or restart
                prev = (_last_snapshot.get('now') or {})
                prev_pid = prev.get('pid') or ''
                prev_key = f"{(prev.get('title') or '').strip()}|{(prev.get('artist') or '').strip()}|{(prev.get('album') or '').strip()}"
                if (pid and pid != prev_pid) or (not pid and meta_key and meta_key != prev_key) or restarted:
                    _last_snapshot['art_tok'] = int(time.time() * 1000)
                _last_snapshot['now'] = now
                _sse_publish('now', {**now, 'artwork_token': _last_snapshot['art_tok']})
                # Prefetch and cache current artwork in the background (do not probe "next track" to avoid accidental skips)
                try:
                    # Current album — ensure cached
                    if album:
                        app.logger.debug(f"prefetch: current album='{album}'")
                        def _do_prefetch_and_hash():
                            try:
                                b = _album_art_bytes(album)
                                if b:
                                    try:
                                        _last_snapshot['art_hash'] = hashlib.sha1(b).hexdigest()
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                        threading.Thread(target=_do_prefetch_and_hash, daemon=True).start()
                except Exception:
                    pass
            else:
                # Keep last_pos updated for restart detection next iteration
                if pos is not None:
                    last_pos = pos
        except Exception as e:
            app.logger.debug(f"watch now error: {e}")
        time.sleep(itv)


def _watch_airplay_loop():
    last = None
    while True:
        s = load_settings()
        itv = max(1.0, (s.get('poll_devices_ms', 3000) / 1000.0))
        try:
            items = _read_airplay_full()
            volumes = _get_airplay_volumes()
            for item in items:
                name = item['name']
                item['volume'] = volumes.get(name, None)
            if items != last:
                last = items
                _last_snapshot['airplay'] = items
                _sse_publish('airplay_full', items)
        except Exception as e:
            app.logger.debug(f"watch airplay error: {e}")
        time.sleep(itv)


def _watch_master_loop():
    last_v = None
    last_shuffle = None
    while True:
        s = load_settings()
        pm = int(s.get('poll_master_ms', 1500))
        if pm <= 0:
            time.sleep(2.0)
            continue
        itv = max(0.8, pm / 1000.0)
        try:
            v = _get_master_volume_percent()
            if v >= 0 and v != last_v:
                last_v = v
                _last_snapshot['master'] = v
                _sse_publish('master_volume', v)
            sh = bool(get_shuffle_enabled())
            if sh != last_shuffle:
                last_shuffle = sh
                _last_snapshot['shuffle'] = sh
                _sse_publish('shuffle', {"enabled": sh})
        except Exception as e:
            app.logger.debug(f"watch master/shuffle error: {e}")
        time.sleep(itv)

def run_applescript(script):
    """Execute AppleScript and return the output."""
    process = subprocess.Popen(['osascript', '-e', script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output, error = process.communicate()
    code = process.returncode
    if code != 0:
        return {'error': (error or b'').decode('utf-8').strip() or f'osascript exited {code}'}
    return (output or b'').decode('utf-8').strip()

def applescript_escape(s: str) -> str:
    """Escape a string for safe use inside AppleScript quotes."""
    return s.replace('"', '\\"') if isinstance(s, str) else s


# --- Simple persisted settings (port, auto-apply, open_browser) ---
CONFIG_DIR = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Music App Server")
ARTWORK_DIR = os.path.join(CONFIG_DIR, "Artwork")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

_DEF_SETTINGS = {
    "port": 7766,
    "auto_apply": False,
    "open_browser": True,
    "confirm_quit": True,
    "poll_now_ms": 1500,       # now-playing poll interval (ms)
    "poll_devices_ms": 3000,   # devices poll interval (ms)
    "poll_master_ms": 1500,   # master volume poll interval (ms); 0 disables
}

def load_settings():
    try:
        with open(CONFIG_PATH, 'r') as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return dict(_DEF_SETTINGS)
            out = dict(_DEF_SETTINGS)
            out.update({k: data.get(k, v) for k, v in _DEF_SETTINGS.items()})
            return out
    except Exception:
        return dict(_DEF_SETTINGS)

def save_settings(data: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    # ensure artwork cache dir exists proactively
    try:
        os.makedirs(ARTWORK_DIR, exist_ok=True)
    except Exception:
        pass
    base = load_settings()
    base.update(data or {})
    with open(CONFIG_PATH, 'w') as f:
        json.dump(base, f, indent=2)
    return base

def schedule_restart(delay=1.0):
    def _restart():
        import time
        time.sleep(delay)
        try:
            # If running as a PyInstaller macOS app bundle, relaunch via `open`
            if getattr(sys, 'frozen', False) or os.environ.get('PYINSTALLER_BUNDLED') == '1':
                exe = os.path.realpath(sys.executable)
                try:
                    contents_dir = os.path.dirname(os.path.dirname(exe))  # .../Contents/MacOS -> .../Contents
                    app_bundle = os.path.dirname(contents_dir)            # .../Contents -> .../*.app
                except Exception:
                    contents_dir = None
                    app_bundle = None
                if app_bundle and app_bundle.endswith('.app') and os.path.exists(app_bundle):
                    try:
                        subprocess.Popen(['open', '-n', app_bundle])
                        os._exit(0)
                    except Exception:
                        pass
            # Fallback: re-exec current interpreter + args
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            app.logger.error(f"Restart failed: {e}")
            os._exit(3)
    threading.Thread(target=_restart, daemon=True).start()

def schedule_quit(delay=0.5):
    def _quit():
        import time
        time.sleep(delay)
        try:
            os._exit(0)
        except Exception:
            pass
    threading.Thread(target=_quit, daemon=True).start()

# --- Minimal Web UI and status routes ---

@app.route("/")
def root_redirect():
    return redirect("/ui", code=302)

@app.route("/status")
def status():
    _start_watchers_once()
    """Basic health + current shuffle state."""
    sh = get_shuffle_enabled()
    return jsonify({
        "status": "Music App Server is running",
        "shuffle": bool(sh),
        "webp_enabled": bool(WEBP_ENABLED),
        "artwork_cache_dir": ARTWORK_DIR,
        "endpoints": ["/ui", "/playlists", "/albums", "/artists",
                      "/devices", "/now_playing", "/shuffle", "/queue_artist_shuffled",
                      "/restart", "/quit"]
    })

def get_shuffle_enabled():
    script = '''
    tell application "Music"
        try
            return (shuffle enabled)
        on error
            return false
        end try
    end tell
    '''
    r = run_applescript(script)
    if isinstance(r, dict):
        return False
    s = str(r).strip().lower()
    return s in ("true", "yes", "1")


def set_shuffle_enabled(enabled: bool):
    flag = "true" if enabled else "false"
    script = f'''
    tell application "Music"
        try
            set shuffle enabled to {flag}
            return (shuffle enabled)
        on error
            return false
        end try
    end tell
    '''
    r = run_applescript(script)
    if isinstance(r, dict):
        return False
    return str(r).strip().lower() in ("true", "yes", "1")


def get_repeat_enabled():
    script = '''
    tell application "Music"
        try
            return (song repeat)
        on error
            return "off"
        end try
    end tell
    '''
    r = run_applescript(script)
    if isinstance(r, dict):
        return "off"
    s = str(r).strip().lower()
    return s if s in ("off", "one", "all") else "off"


def set_repeat_enabled(mode: str):
    if mode not in ("off", "one", "all"):
        mode = "off"
    script = f'''
    tell application "Music"
        try
            set song repeat to {mode}
            return (song repeat)
        on error
            return "off"
        end try
    end tell
    '''
    r = run_applescript(script)
    if isinstance(r, dict):
        return "off"
    return str(r).strip().lower()

@app.route('/shuffle', methods=['GET', 'POST'])
def shuffle_toggle():
    """GET {enabled}; POST {enabled:true|false} — toggle global Music shuffle."""
    if request.method == 'GET':
        return jsonify({"enabled": bool(get_shuffle_enabled())})
    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get('enabled'))
    ok = set_shuffle_enabled(enabled)
    _sse_publish('shuffle', {"enabled": bool(get_shuffle_enabled())})
    return jsonify({"ok": bool(ok), "enabled": bool(get_shuffle_enabled())})


@app.route('/repeat', methods=['GET', 'POST'])
def repeat_toggle():
    """GET {mode}; POST {mode:"off"|"one"|"all"} — set global Music repeat mode."""
    if request.method == 'GET':
        return jsonify({"mode": get_repeat_enabled()})
    payload = request.get_json(silent=True) or {}
    mode = payload.get('mode', 'off')
    if mode not in ("off", "one", "all"):
        mode = "off"
    ok = set_repeat_enabled(mode)
    _sse_publish('repeat', {"mode": get_repeat_enabled()})
    return jsonify({"ok": bool(ok), "mode": get_repeat_enabled()})

# --- UI route ---

@app.route("/ui")
def web_ui():
    """Serve a minimal, nice-looking control page for Apple Music & AirPlay."""
    try:
        _start_watchers_once()
    except Exception:
        pass
    return render_template_string(
        r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Music App Server</title>
<style>
  :root { --bg:#0b1116; --panel:#111821; --muted:#7b8a9a; --text:#e6edf3; --accent:#4c8bf5; --good:#3fb950; --warn:#f0883e; --border:#1f2937; --border2:#263244; }
  *{box-sizing:border-box}
  body{margin:0;padding:24px;background:var(--bg);color:var(--text);font:16px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
  h1{font-size:22px;margin:0 0 16px}
  .grid{display:grid;gap:16px}
  .cols{grid-template-columns: 1.2fr 1fr}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px}
  .row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
  .muted{color:var(--muted)}
  .title{font-weight:600}
  .now{display:flex;gap:16px}
  .art{width:128px;height:128px;border-radius:10px;object-fit:cover;background:#1d2633}
  .kv{margin:2px 0}
  .kv .label{display:inline-block;width:70px;color:var(--muted)}
  .slider{width:100%}
  .transport{display:flex;gap:10px;margin-top:12px}
  .btn{appearance:none;border:1px solid var(--border2);border-radius:10px;background:#0f1520;color:var(--text);padding:10px 14px;cursor:pointer}
  .btn:hover{border-color:#2f3d52}
  .btn-primary{background:var(--accent);border-color:var(--accent);color:#fff}
  .btn-warn{background:var(--warn);border-color:var(--warn);color:#fff}
  .btn-circle{width:44px;height:44px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;padding:0}
  .btn-apply{display:inline-flex;align-items:center;gap:8px}
  /* Button feedback */
  .btn{position:relative;transition:transform .08s ease}
  .btn.clicked{transform:translateY(1px)}
  .btn.busy{opacity:.7;pointer-events:none}
  .btn.busy::after{content:"";position:absolute;right:10px;top:50%;width:16px;height:16px;margin-top:-8px;border:2px solid rgba(255,255,255,.35);border-top-color:#fff;border-radius:50%;animation:spin .8s linear infinite}
  .btn-circle.busy::after{right:50%;top:50%;margin:-10px 0 0 -10px;width:20px;height:20px}
  @keyframes spin{to{transform:rotate(360deg)}}
  .btn svg{width:22px;height:22px;display:block}
  .footer{display:flex;justify-content:space-between;align-items:center;margin-top:10px}
  .chip{font-size:12px;padding:2px 8px;border:1px solid #2a3a54;border-radius:999px;color:var(--muted)}
  .devs{display:flex;flex-direction:column;gap:10px;max-height:340px;overflow:auto;padding-right:4px}
  .dev{display:flex;align-items:center;gap:12px;justify-content:space-between;border:1px solid #213047;border-radius:10px;padding:10px}
  .dev .left{display:flex;align-items:center;gap:10px}
  .dev input[type=checkbox]{width:18px;height:18px}
  .dev .name{min-width:160px}
  .status-dot{width:10px;height:10px;border-radius:50%;border:1px solid var(--border2);display:inline-block}
  .status-on{background:var(--good)}
  .status-off{background:#2a3a54}
  .settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .field{display:flex;flex-direction:column;gap:6px}
  .field label{font-size:12px;color:var(--muted)}
  input[type=number]{background:#0f1520;border:1px solid var(--border2);border-radius:8px;color:var(--text);padding:8px}
  input[type=checkbox].toggle{width:18px;height:18px}
  .toast{position:fixed;right:16px;bottom:16px;background:var(--panel);border:1px solid var(--border2);border-radius:10px;padding:10px 14px;box-shadow:0 6px 20px rgba(0,0,0,.35);opacity:0;transform:translateY(10px);transition:opacity .18s, transform .18s;pointer-events:none;z-index:9999}
  .toast.show{opacity:1;transform:translateY(0)}
  .toast.good{border-color:#2b7a3b}
  .toast.warn{border-color:#8a4f20}
  code{background:#0f1520;border:1px solid var(--border2);padding:2px 6px;border-radius:6px}
</style>
</head>
<body>
  <h1>Music App Server</h1>
  <div class="grid cols">
    <div class="card">
      <div class="now">
        <img id="art" class="art" src="/artwork" alt="artwork" onerror="this.src='data:image/svg+xml;utf8,<svg xmlns=\'http://www.w3.org/2000/svg\' viewBox=\'0 0 128 128\'><rect width=\'128\' height=\'128\' fill=\'%231d2633\'/><text x=\'50%\' y=\'55%\' dominant-baseline=\'middle\' text-anchor=\'middle\' font-size=\'14\' fill=\'%237b8a9a\'>No Art</text></svg>'">
        <div>
          <div class="kv"><span class="label">Track:</span> <span id="trk" class="title">—</span></div>
          <div class="kv"><span class="label">Artist:</span> <span id="artst">—</span></div>
          <div class="kv"><span class="label">Album:</span> <span id="albm">—</span></div>
          <div class="kv muted" id="state">—</div>
          <div class="transport">
            <button class="btn btn-circle" title="Previous" onclick="action(this, ()=>call('/previous','POST'))" aria-label="Previous" id="btn_prev">${ICON_PREV}</button>
            <button id="pp" class="btn btn-circle btn-primary" title="Play/Pause" aria-label="Play/Pause" onclick="action(this, playPause)">${ICON_PLAY}</button>
            <button class="btn btn-circle" title="Next" onclick="action(this, ()=>call('/next','POST'))" aria-label="Next" id="btn_next">${ICON_NEXT}</button>
            <button class="btn btn-circle" title="Shuffle" onclick="action(this, toggleShuffle)" aria-label="Shuffle" id="btn_shuffle"><svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path fill="currentColor" d="M14.83 13.41L13.42 14.82L16.55 17.95L14.5 20H20V14.5L17.96 16.54L14.83 13.41M14.5 4L16.54 6.04L13.41 9.17L14.82 10.58L17.95 7.45L20 9.5V4M10.59 9.17L9.17 10.58L12.3 13.71L10.26 15.75H16.5V9.5L14.46 11.54L10.59 9.17M3 2V8.5L5.04 6.46L8.17 9.59L9.59 8.17L6.45 5.04L8.5 3H3M9.17 13.41L10.59 14.82L7.45 17.96L5.5 20H11.5V14.25L9.46 16.29L9.17 13.41Z"/></svg></button>
            <button class="btn btn-circle" title="Repeat" onclick="action(this, toggleRepeat)" aria-label="Repeat" id="btn_repeat"><svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path fill="currentColor" d="M17 17H7V14L3 18L7 22V19H19V13H17M7 7H17V10L21 6L17 2V5H5V11H7V7Z"/></svg></button>
          </div>
        </div>
      </div>
      <div class="footer">
        <div style="flex:1">
          <div class="kv"><span class="label">Master:</span></div>
          <input id="master" class="slider" type="range" min="0" max="100" step="1" value="0" oninput="debouncedMaster()">
        </div>
        <span id="mv" class="chip">0%</span>
      </div>
    </div>

    <div class="card">
      <div class="row" style="justify-content:space-between;margin-bottom:8px">
        <div class="title">AirPlay Devices</div>
        <div class="muted">Select devices, then Apply</div>
      </div>
      <div class="row" style="margin-bottom:8px">
        <label class="row" style="gap:8px"><input id="showDisabled" type="checkbox" class="toggle" onchange="loadDevices()"> Show Disabled Devices</label>
      </div>
      <div id="devs" class="devs"></div>
      <div class="row" style="justify-content:flex-end;margin-top:8px">
        <button class="btn btn-primary btn-apply" onclick="action(this, applyDevicesImmediate)">Apply Selection</button>
      </div>
    </div>

    <div class="card">
      <div class="title" style="margin-bottom:12px">Browse Music Library</div>
      <div class="row" style="margin-bottom:12px">
        <input type="text" id="searchInput" placeholder="Search albums, artists, playlists..." style="flex:1;padding:8px;border:1px solid var(--border2);border-radius:6px;background:#0f1520;color:var(--text)">
        <button class="btn" onclick="action(this, performSearch)" style="margin-left:8px">Search</button>
      </div>
      <div class="row" style="margin-bottom:12px">
        <button class="btn" onclick="action(this, () => loadBrowseTab(this.dataset.tab))" data-tab="albums">Albums</button>
        <button class="btn" onclick="action(this, () => loadBrowseTab(this.dataset.tab))" data-tab="artists">Artists</button>
        <button class="btn" onclick="action(this, () => loadBrowseTab(this.dataset.tab))" data-tab="playlists">Playlists</button>
      </div>
      <div id="letterNav" class="row" style="margin-bottom:12px;gap:4px;flex-wrap:wrap"></div>
      <div id="browseResults" class="devs" style="max-height:300px"></div>
      <div id="pagination" class="row" style="justify-content:center;margin-top:12px;gap:8px"></div>
    </div>
  </div>

  <div class="card" style="margin-top:16px">
    <div class="row" style="justify-content:space-between;margin-bottom:8px">
      <div class="title">Settings</div>
      <button class="btn" onclick="toggleSettings()" id="settingsToggle">Hide</button>
    </div>
    <div id="settingsContent">
    <div class="settings-grid">
      <div class="field">
        <label for="inPort">HTTP Port</label>
        <input id="inPort" type="number" min="1" max="65535" value="7766">
      </div>
      <div class="field">
        <label for="pollNow">Now-playing poll (ms)</label>
        <input id="pollNow" type="number" min="250" max="60000" value="1500">
      </div>
      <div class="field">
        <label for="pollDevices">Devices poll (ms)</label>
        <input id="pollDevices" type="number" min="500" max="60000" value="3000">
      </div>
      <div class="field">
        <label for="pollMaster">Master volume poll (ms)</label>
        <input id="pollMaster" type="number" min="0" max="60000" value="7766">
      </div>
      <div class="field">
        <label class="row" style="gap:8px;margin-top:22px"><input id="openBrowser" type="checkbox" class="toggle"> Open browser at startup</label>
      </div>
      <div class="field">
        <label class="row" style="gap:8px;margin-top:22px"><input id="confirmQuit" type="checkbox" class="toggle"> Require quit confirmation</label>
      </div>
    </div>
    <div class="muted" style="margin-top:6px">Settings file: <code id="cfgPath"></code></div>
    <div class="row" style="justify-content:space-between;margin-top:10px">
      <div>
        <button class="btn btn-warn" onclick="action(this, quitApp)">Quit</button>
      </div>
      <div class="row" style="gap:8px">
        <button class="btn" onclick="action(this, purgeArtworkCache)">Purge Artwork Cache</button>
        <button class="btn" onclick="action(this, loadSettings)">Reload</button>
        <button class="btn btn-primary" onclick="action(this, saveSettings)">Save & Restart</button>
      </div>
    </div>
  </div>

  <div id="toast" class="toast" role="status" aria-live="polite"></div>
<script>
const $ = sel => document.querySelector(sel);
const devBox = $('#devs');
function escHtml(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function attrQuote(s){ return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }
function canonName(s){ try { return String(s).normalize('NFKC').trim(); } catch(e){ return String(s||'').trim(); } }
function unescHtml(s){ return String(s).replace(/&quot;/g,'"').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&'); }
function showToast(msg, ok=true){
  const t = document.getElementById('toast');
  if(!t) return;
  t.textContent = String(msg);
  t.classList.remove('good','warn','show');
  t.classList.add(ok ? 'good' : 'warn');
  // Force reflow to restart transition
  void t.offsetWidth;
  t.classList.add('show');
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(()=>{ t.classList.remove('show'); }, 2500);
}

// Wrap actions to show consistent button feedback (click + busy spinner)
function action(btn, fn){
  try{
    if(btn && btn.classList){
      btn.classList.add('clicked','busy');
      setTimeout(()=>btn.classList.remove('clicked'), 160);
    }
    const p = Promise.resolve().then(fn);
    return p.finally(()=>{ if(btn && btn.classList) btn.classList.remove('busy'); });
  }catch(e){
    if(btn && btn.classList) btn.classList.remove('busy');
    throw e;
  }
}

// Quit handler — ask confirmation based on setting, then fire-and-forget
let CONFIRM_QUIT = true;
async function quitApp(){
  try{
    if (CONFIRM_QUIT){
      const ok = window.confirm('Quit Music App Server?');
      if (!ok){
        showToast('Quit canceled', false);
        await new Promise(r=>setTimeout(r, 120));
        return;
      }
    }
    fetch('/quit', {method:'POST'}).catch(()=>{});
    showToast('Quitting…', true);
    // Resolve quickly so button spinner clears even if server stops before response
    await new Promise(r=>setTimeout(r, 200));
  }catch(e){
    // Best-effort UI feedback
    showToast('Failed to send quit', false);
    await new Promise(r=>setTimeout(r, 150));
  }
}
let deviceVolumes = {}; // name -> 0..100
let selected = new Set(); // UI selection
const dragging = new Set(); // devices being dragged (skip live overwrites)
let devicesApplyTimer = null;

// Poll intervals (ms) — defaults, overridden by settings
let POLL_NOW_MS = 1500;
let POLL_DEVICES_MS = 3000;
let POLL_MASTER_MS = 1500;  // 0 disables
let _timerNow = null;
let _timerDev = null;
let _timerMaster = null;
function applyPollingIntervals(nowMs, devMs, masterMs){
  if (typeof nowMs === 'number') POLL_NOW_MS = nowMs;
  if (typeof devMs === 'number') POLL_DEVICES_MS = devMs;
  if (typeof masterMs === 'number') POLL_MASTER_MS = masterMs;

  if (_timerNow)    clearInterval(_timerNow);
  if (_timerDev)    clearInterval(_timerDev);
  if (_timerMaster) clearInterval(_timerMaster);

  _timerNow = setInterval(loadNow, POLL_NOW_MS);
  _timerDev = setInterval(loadDevicesLive, POLL_DEVICES_MS);

  if (POLL_MASTER_MS > 0) {
    _timerMaster = setInterval(loadMaster, POLL_MASTER_MS);
  }
}

let pendingApplyUntil = 0; // ms timestamp; while in the future, suppress live checkbox overwrites

function syncCheckboxesToSelected(){
  const rows = Array.from(devBox.querySelectorAll('.dev'));
  rows.forEach(row=>{
    const cb = row.querySelector('input[type=checkbox]');
    const rawName = unescHtml(cb.getAttribute('data-name'));
    cb.checked = selected.has(rawName);
  });
}

// --- Icons ---
const ICON_PREV = `<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path fill="currentColor" d="M6 6h2v12H6V6m3.5 6l8.5 6V6l-8.5 6Z"/></svg>`;
const ICON_NEXT = `<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path fill="currentColor" d="M16 6h2v12h-2V6M6 6v12l8.5-6L6 6Z"/></svg>`;
const ICON_PLAY = `<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path fill="currentColor" d="M8 5v14l11-7L8 5Z"/></svg>`;
const ICON_PAUSE = `<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path fill="currentColor" d="M6 5h4v14H6V5m8 0h4v14h-4V5Z"/></svg>`;
const ICON_SHUFFLE = `<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path fill="currentColor" d="M14.83 13.41L13.42 14.82L16.55 17.95L14.5 20H20V14.5L17.96 16.54L14.83 13.41M14.5 4L16.54 6.04L13.41 9.17L14.82 10.58L17.95 7.45L20 9.5V4M10.59 9.17L9.17 10.58L12.3 13.71L10.26 15.75H16.5V9.5L14.46 11.54L10.59 9.17M3 2V8.5L5.04 6.46L8.17 9.59L9.59 8.17L6.45 5.04L8.5 3H3M9.17 13.41L10.59 14.82L7.45 17.96L5.5 20H11.5V14.25L9.46 16.29L9.17 13.41Z"/></svg>`;
const ICON_REPEAT = `<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path fill="currentColor" d="M17 17H7V14L3 18L7 22V19H19V13H17M7 7H17V10L21 6L17 2V5H5V11H7V7Z"/></svg>`;

$('#btn_prev').innerHTML = ICON_PREV;
$('#btn_next').innerHTML = ICON_NEXT;
$('#pp').innerHTML = ICON_PLAY;

function updatePP(isPlaying){
  const pp = $('#pp');
  pp.innerHTML = isPlaying ? ICON_PAUSE : ICON_PLAY;
  pp.classList.toggle('btn-primary', !isPlaying);
}

async function call(url, method='GET', body=null){
  const opt = {method, headers:{'Content-Type':'application/json'}};
  if(body) opt.body = JSON.stringify(body);
  const r = await fetch(url, opt); return r.ok ? r.json().catch(()=>({ok:true})) : Promise.reject(await r.text());
}

function fmt(x){ return (x==null||isNaN(x))? '—' : x }

async function loadNow(){
  try{
    const data = await (await fetch('/now_playing')).json();
    $('#trk').textContent = data.title || '—';
    $('#artst').textContent = data.artist || '—';
    $('#albm').textContent = data.album || '—';
    const st = (data.is_playing===true || data.state==='playing')? 'Playing' : (data.state||'Paused');
    $('#state').textContent = st + (data.position? ` — ${Math.round(data.position)}s` : '');
    updatePP(st==='Playing');
    $('#btn_shuffle').classList.toggle('btn-primary', data.shuffle === true);
    $('#btn_repeat').classList.toggle('btn-primary', data.repeat !== 'off');
    $('#art').src = '/artwork?ts=' + Date.now();
  }catch(e){ console.warn('now_playing', e); }
}

async function loadMaster(){
  try{
    const r = await fetch('/master_volume');
    const v = r.ok ? parseInt(await r.text()) : 0;
    $('#master').value = isFinite(v)? v:0; $('#mv').textContent = (isFinite(v)? v:0) + '%';
  }catch(e){ console.warn('master', e); }
}

let masterTimer=null;
async function setMaster(){
  const v = parseInt($('#master').value);
  $('#mv').textContent = v+'%';
  try{ await call('/master_volume','POST',{level:v}); }catch(e){ console.warn('set master', e); }
}
function debouncedMaster(){ clearTimeout(masterTimer); masterTimer=setTimeout(setMaster, 150); }

function cssId(s){ return s.replace(/[^a-z0-9]+/gi,'-'); }

async function purgeArtworkCache(){
  try{
    const r = await fetch('/purge_album_cache', { method:'POST' });
    if (!r.ok){ throw new Error('HTTP '+r.status); }
    showToast('Artwork cache purged', true);
    try { document.getElementById('art').src = '/artwork?refresh=1&ts=' + Date.now(); } catch(_){}
  }catch(e){
    console.warn('purge cache', e);
    showToast('Failed to purge artwork cache', false);
  }
}

async function loadDevices(){
  try{
    const full = await (await fetch('/airplay_full')).json(); // [{name, volume, active}]
    const showDisabled = $('#showDisabled').checked;
    // Filter devices based on showDisabled setting
    const filtered = showDisabled ? full : full.filter(d => d.active);
    // Sort active first, then by name (case-insensitive)
    filtered.sort((a, b) => {
      const ax = a && a.active ? 0 : 1;
      const bx = b && b.active ? 0 : 1;
      if (ax !== bx) return ax - bx;
      return String(a.name).localeCompare(String(b.name), undefined, { sensitivity: 'base' });
    });
    devBox.innerHTML = '';

    // Seed selection on first render from active devices
    if (selected.size === 0) {
      full.filter(d => d.active).forEach(d => selected.add(String(d.name)));
    }

    filtered.forEach(d => {
      const name = String(d.name);
      const cn = canonName(name);
      const vol = isFinite(parseInt(d.volume)) ? Math.max(0, Math.min(100, parseInt(d.volume))) : 0;
      const checked = selected.has(name) ? 'checked' : '';
      const onClass = d.active ? 'status-on' : 'status-off';
      const onTitle = d.active ? 'On' : 'Off';
      const nameAttr = attrQuote(name);
      const nameText = escHtml(name);
      const row = document.createElement('div'); row.className='dev';
      row.innerHTML = `
        <div class='left'>
          <span class='status-dot ${onClass}' id='st-${cssId(name)}' title='${onTitle}'></span>
          <input type='checkbox' ${checked} data-name="${nameAttr}">
          <div class='name'>${nameText}</div>
        </div>
        <div style='flex:1;display:flex;align-items:center;gap:10px'>
          <input type='range' min='0' max='100' step='1' value='${vol}' data-vol='${nameAttr}' style='width:100%'>
          <span class='chip' id='v-${cssId(name)}'>${vol}%</span>
        </div>`;
      devBox.appendChild(row);
      const cb = row.querySelector('input[type=checkbox]');
      const sl = row.querySelector('input[type=range]');
      cb.addEventListener('change', (e)=>{
        const n = unescHtml(e.target.getAttribute('data-name')); // RAW name
        if(e.target.checked) selected.add(n); else selected.delete(n);
      });
      sl.addEventListener('input', (e)=>{
        const n = unescHtml(e.target.getAttribute('data-vol')); // RAW name
        const v = parseInt(e.target.value)||0;
        document.getElementById('v-'+cssId(n)).textContent = v+'%';
        debounceDevice(n, v);
      });
      sl.addEventListener('pointerdown', ()=>{ dragging.add(name); });
      sl.addEventListener('pointerup',   ()=>{ dragging.delete(name); });
      sl.addEventListener('pointercancel',()=>{ dragging.delete(name); });
    });
    syncCheckboxesToSelected();
  }catch(e){ console.warn('devices', e); }
}

async function loadDevicesLive(){
  try{
    const full = await (await fetch('/airplay_full')).json(); // [{name, volume, active}]
    // Sort active first, then by name (case-insensitive) to match initial render
    full.sort((a, b) => {
      const ax = a && a.active ? 0 : 1;
      const bx = b && b.active ? 0 : 1;
      if (ax !== bx) return ax - bx;
      return String(a.name).localeCompare(String(b.name), undefined, { sensitivity: 'base' });
    });
    const namesCanon = full.map(d => canonName(String(d.name)));

    const rows = Array.from(devBox.querySelectorAll('.dev'));
    const rendered = rows.map(r=> r.querySelector('input[type=checkbox]').getAttribute('data-name'));
    const renderedCanon = rendered.map(n => canonName(unescHtml(n)));
    const same = (renderedCanon.length === namesCanon.length) && renderedCanon.every(n => namesCanon.includes(n));
    if(!same){ return loadDevices(); }

    rows.forEach(row => {
      const cb = row.querySelector('input[type=checkbox]');
      const sl = row.querySelector('input[type=range]');
      const nameAttr = cb.getAttribute('data-name');
      const rawName = unescHtml(nameAttr);
      const cn = canonName(rawName);
      const info = full.find(d => canonName(String(d.name)) === cn);
      if(!info) return;

      // Checkbox reflects pending selection, not live active set
      const shouldBeChecked = selected.has(rawName);
      if(cb.checked !== shouldBeChecked){
        cb.checked = shouldBeChecked;
      }

      // Status dot reflects live active state
      const dot = row.querySelector('.status-dot');
      if (dot){
        const on = !!info.active;
        dot.classList.toggle('status-on', on);
        dot.classList.toggle('status-off', !on);
        dot.title = on ? 'On' : 'Off';
      }

      // Volume live update unless user is dragging
      if(!dragging.has(rawName)){
        const vol = isFinite(parseInt(info.volume)) ? Math.max(0, Math.min(100, parseInt(info.volume))) : 0;
        sl.value = vol;
        const chip = document.getElementById('v-'+cssId(rawName));
        if(chip) chip.textContent = vol + '%';
      }
    });
  }catch(e){ console.warn('devicesLive', e); }
}

const devTimers = new Map();
function debounceDevice(name, v){
  if(devTimers.has(name)) clearTimeout(devTimers.get(name));
  devTimers.set(name, setTimeout(()=>setDeviceVolume(name,v), 150));
}
async function setDeviceVolume(name, level){
  try{ await call('/set_device_volume','POST',{device:name, level:level}); }
  catch(e){ console.warn('dev vol', name, e); }
}

function applyDevicesImmediate(){
  const payload = {devices: Array.from(selected).join(',')};
  return call('/set_devices','POST',payload)
    .then(async (res)=>{
      const attempted = Array.from(selected);
      // Use server-reported applied list when present
      if(res && Array.isArray(res.applied)){
        selected = new Set(res.applied.map(String));
      }
      // Then verify against Music's current devices as ground truth
      try {
        const cur = await (await fetch('/current_devices')).json();
        if (Array.isArray(cur)) {
          selected = new Set(cur.map(String));
        }
      } catch(e) { /* ignore */ }

      syncCheckboxesToSelected();

      const appliedList = Array.from(selected);
      if (appliedList.length === 0 && attempted.length > 0){
        showToast('No AirPlay devices accepted by Music', false);
      } else {
        showToast('Applied: ' + (appliedList.join(', ') || 'None'), true);
      }

      // Suppress live overwrites a bit longer; Music may take a couple seconds
      pendingApplyUntil = Date.now() + 4000;
      setTimeout(()=>{ loadDevicesLive(); }, 2500);
      setTimeout(()=>{ loadDevicesLive(); }, 4500);
    })
    .catch(e=>{
      console.warn('apply devices', e);
      const msg = typeof e === 'string' ? e : (e && e.message) ? e.message : 'Failed to apply devices';
      showToast(msg, false);
    });
}
function debouncedApplyDevices(){
  if(devicesApplyTimer) clearTimeout(devicesApplyTimer);
  devicesApplyTimer = setTimeout(applyDevicesImmediate, 150);
}

async function playPause(){
  try{ await call('/playpause','POST'); await loadNow(); }
  catch(e){ console.warn('playpause', e); }
}

async function toggleShuffle(){
  try{
    const r = await call('/shuffle','POST',{enabled: !$('#btn_shuffle').classList.contains('btn-primary')});
    if(r.ok){
      $('#btn_shuffle').classList.toggle('btn-primary', r.enabled);
      showToast(`Shuffle ${r.enabled ? 'on' : 'off'}`, true);
    }
  }catch(e){ console.warn('toggle shuffle', e); showToast('Failed to toggle shuffle', false); }
}

async function toggleRepeat(){
  try{
    const r = await call('/repeat','POST',{mode: $('#btn_repeat').classList.contains('btn-primary') ? 'off' : 'all'});
    if(r.ok){
      const isOn = r.mode !== 'off';
      $('#btn_repeat').classList.toggle('btn-primary', isOn);
      showToast(`Repeat ${r.mode}`, true);
    }
  }catch(e){ console.warn('toggle repeat', e); showToast('Failed to toggle repeat', false); }
}

async function performSearch(){
  const query = $('#searchInput').value.trim();
  if(!query) return;
  try{
    const r = await (await fetch(`/search?q=${encodeURIComponent(query)}`)).json();
    const results = [];
    r.albums.forEach(name => results.push({name, type: 'album'}));
    r.artists.forEach(name => results.push({name, type: 'artist'}));
    r.playlists.forEach(name => results.push({name, type: 'playlist'}));
    r.songs.forEach(name => results.push({name, type: 'song'}));
    displayBrowseResults(results, 'search');
    showToast(`Found ${results.length} results`, true);
  }catch(e){ console.warn('search', e); showToast('Search failed', false); }
}

let currentBrowseTab = 'albums';
let currentPage = 1;
let itemsPerPage = 5;
let currentLetter = '';
let allItems = [];

async function loadBrowseTab(tab){
  try{
    currentBrowseTab = tab;
    currentPage = 1;
    currentLetter = '';
    let data = [];
    if(tab === 'albums'){
      const response = await fetch('/albums');
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }
      data = await response.json();
      allItems = data.map(name => ({name, type: 'album'}));
    }else if(tab === 'artists'){
      const response = await fetch('/artists');
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }
      data = await response.json();
      allItems = data.map(name => ({name, type: 'artist'}));
    }else if(tab === 'playlists'){
      const response = await fetch('/playlists');
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }
      data = await response.json();
      allItems = data.map(name => ({name, type: 'playlist'}));
    }
    updateLetterNavigation();
    displayBrowseResults(allItems, tab);
    const count = Array.isArray(data) ? data.length : 0;
    showToast(`Loaded ${count} ${tab}`, true);
  }catch(e){ console.warn('load browse tab', e); showToast(`Failed to load ${tab}: ${e.message}`, false); }
}

function updateLetterNavigation(){
  const letterNav = $('#letterNav');
  const letters = new Set();
  allItems.forEach(item => {
    const firstLetter = item.name.charAt(0).toUpperCase();
    if(firstLetter.match(/[A-Z]/)) letters.add(firstLetter);
  });
  const sortedLetters = Array.from(letters).sort();
  letterNav.innerHTML = '<button class="btn" onclick="filterByLetter(\'\')">All</button>' +
    sortedLetters.map(letter => `<button class="btn" onclick="filterByLetter('${letter}')">${letter}</button>`).join('');
}

function filterByLetter(letter){
  currentLetter = letter;
  currentPage = 1;
  const filtered = letter ? allItems.filter(item => item.name.charAt(0).toUpperCase() === letter) : allItems;
  displayBrowseResults(filtered, currentBrowseTab);
}

function displayBrowseResults(items, type){
  const container = $('#browseResults');
  const pagination = $('#pagination');
  const totalPages = Math.ceil(items.length / itemsPerPage);
  const startIndex = (currentPage - 1) * itemsPerPage;
  const endIndex = startIndex + itemsPerPage;
  const pageItems = items.slice(startIndex, endIndex);

  container.innerHTML = '';
  pageItems.forEach(item => {
    const div = document.createElement('div');
    div.className = 'dev';
    div.innerHTML = `
      <div class='left'>
        <div class='name'>${escHtml(item.name)}</div>
      </div>
      <div style='flex:1;display:flex;align-items:center;gap:10px'>
        <span class='chip'>${item.type}</span>
        <button class="btn" onclick="action(this, () => playItem('${item.type}', '${attrQuote(item.name)}'))">Play</button>
        ${item.type === 'album' || item.type === 'artist' || item.type === 'playlist' ?
          `<button class="btn" onclick="action(this, () => shuffleItem('${item.type}', '${attrQuote(item.name)}'))">Shuffle</button>` : ''}
        ${item.type === 'album' || item.type === 'artist' || item.type === 'playlist' ?
          `<button class="btn" onclick="action(this, () => browseItem('${item.type}', '${attrQuote(item.name)}'))">Browse</button>` : ''}
      </div>`;
    container.appendChild(div);
  });

  // Update pagination
  pagination.innerHTML = '';
  if(totalPages > 1){
    const prevBtn = document.createElement('button');
    prevBtn.className = 'btn';
    prevBtn.textContent = 'Prev';
    prevBtn.disabled = currentPage === 1;
    prevBtn.onclick = () => changePage(currentPage - 1);
    pagination.appendChild(prevBtn);

    for(let i = 1; i <= totalPages; i++){
      const pageBtn = document.createElement('button');
      pageBtn.className = 'btn' + (i === currentPage ? ' btn-primary' : '');
      pageBtn.textContent = i.toString();
      pageBtn.onclick = () => changePage(i);
      pagination.appendChild(pageBtn);
    }

    const nextBtn = document.createElement('button');
    nextBtn.className = 'btn';
    nextBtn.textContent = 'Next';
    nextBtn.disabled = currentPage === totalPages;
    nextBtn.onclick = () => changePage(currentPage + 1);
    pagination.appendChild(nextBtn);
  }
}

async function browseItem(type, name){
  try{
    if(type === 'artist'){
      await browseArtist(name);
    }else if(type === 'album'){
      await browseAlbum(name);
    }else if(type === 'playlist'){
      await browsePlaylist(name);
    }
  }catch(e){ console.warn('browse item', e); showToast('Failed to browse item', false); }
}

function changePage(page){
  currentPage = page;
  const filtered = currentLetter ? allItems.filter(item => item.name.charAt(0).toUpperCase() === currentLetter) : allItems;
  displayBrowseResults(filtered, currentBrowseTab);
}

async function shuffleItem(type, name){
  try{
    const r = await call('/play', 'POST', {type, name, shuffle: true});
    if(r.status === 'playing'){
      showToast(`Shuffling ${type}: ${name}`, true);
      await loadNow();
    }else{
      showToast('Failed to shuffle', false);
    }
  }catch(e){ console.warn('shuffle item', e); showToast('Failed to shuffle item', false); }
}



async function playItem(type, name){
  try{
    const r = await call('/play', 'POST', {type, name});
    if(r.status === 'playing'){
      showToast(`Playing ${type}: ${name}`, true);
      await loadNow();
    }else{
      showToast('Failed to play', false);
    }
  }catch(e){ console.warn('play item', e); showToast('Failed to play item', false); }
}

async function browseArtist(artistName){
  try{
    const albums = await (await fetch(`/albums_by_artist/${encodeURIComponent(artistName)}`)).json();
    const songs = await (await fetch(`/songs_by_artist/${encodeURIComponent(artistName)}`)).json();
    const container = $('#browseResults');
    container.innerHTML = `
      <div style="margin-bottom:16px;padding:12px;border:1px solid var(--border);border-radius:8px;background:#0f1520">
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
          <button class="btn" onclick="action(this, () => loadBrowseTab(currentBrowseTab))">← Back</button>
          <h3 style="margin:0;color:var(--text)">${escHtml(artistName)}</h3>
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn" onclick="action(this, () => playItem('artist', '${attrQuote(artistName)}'))">Play All</button>
          <button class="btn" onclick="action(this, () => shuffleItem('artist', '${attrQuote(artistName)}'))">Shuffle All</button>
        </div>
      </div>
      <div style="margin-bottom:16px">
        <h4 style="margin:0 0 8px;color:var(--text)">Albums (${albums.length})</h4>
        <div class="devs" style="max-height:200px">
          ${albums.map(album => `
            <div class="dev">
              <div class='left'>
                <div class='name'>${escHtml(album)}</div>
              </div>
              <div style='flex:1;display:flex;align-items:center;gap:10px'>
                <span class='chip'>album</span>
                <button class="btn" onclick="action(this, () => playItem('album', '${attrQuote(album)}'))">Play</button>
                <button class="btn" onclick="action(this, () => shuffleItem('album', '${attrQuote(album)}'))">Shuffle</button>
                <button class="btn" onclick="action(this, () => browseItem('album', '${attrQuote(album)}'))">Browse</button>
              </div>
            </div>
          `).join('')}
        </div>
      </div>
      <div>
        <h4 style="margin:0 0 8px;color:var(--text)">Songs (${songs.length})</h4>
        <div class="devs" style="max-height:200px">
          ${songs.slice(0, 10).map(song => `
            <div class="dev">
              <div class='left'>
                <div class='name'>${escHtml(song)}</div>
              </div>
              <div style='flex:1;display:flex;align-items:center;gap:10px'>
                <span class='chip'>song</span>
                <button class="btn" onclick="action(this, () => playItem('song', '${attrQuote(song)}'))">Play</button>
              </div>
            </div>
          `).join('')}
          ${songs.length > 10 ? `<div style="text-align:center;padding:8px;color:var(--muted)">... and ${songs.length - 10} more songs</div>` : ''}
        </div>
      </div>
    `;
    showToast(`Loaded ${albums.length} albums, ${songs.length} songs`, true);
  }catch(e){ console.warn('browse artist', e); showToast('Failed to load artist details', false); }
}

async function browseAlbum(albumName){
  try{
    const songs = await (await fetch(`/songs_by_album/${encodeURIComponent(albumName)}`)).json();
    const container = $('#browseResults');
    container.innerHTML = `
      <div style="margin-bottom:16px;padding:12px;border:1px solid var(--border);border-radius:8px;background:#0f1520">
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
          <button class="btn" onclick="action(this, () => loadBrowseTab(currentBrowseTab))">← Back</button>
          <h3 style="margin:0;color:var(--text)">${escHtml(albumName)}</h3>
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn" onclick="action(this, () => playItem('album', '${attrQuote(albumName)}'))">Play Album</button>
          <button class="btn" onclick="action(this, () => shuffleItem('album', '${attrQuote(albumName)}'))">Shuffle Album</button>
        </div>
      </div>
      <div>
        <h4 style="margin:0 0 8px;color:var(--text)">Tracks (${songs.length})</h4>
        <div class="devs" style="max-height:300px">
          ${songs.map(song => `
            <div class="dev">
              <div class='left'>
                <div class='name'>${escHtml(song)}</div>
              </div>
              <div style='flex:1;display:flex;align-items:center;gap:10px'>
                <span class='chip'>song</span>
                <button class="btn" onclick="action(this, () => playItem('song', '${attrQuote(song)}'))">Play</button>
              </div>
            </div>
          `).join('')}
        </div>
      </div>
    `;
    showToast(`Loaded ${songs.length} tracks`, true);
  }catch(e){ console.warn('browse album', e); showToast('Failed to load album details', false); }
}

async function browsePlaylist(playlistName){
  try{
    const songs = await (await fetch(`/songs/${encodeURIComponent(playlistName)}`)).json();
    const container = $('#browseResults');
    container.innerHTML = `
      <div style="margin-bottom:16px;padding:12px;border:1px solid var(--border);border-radius:8px;background:#0f1520">
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
          <button class="btn" onclick="action(this, () => loadBrowseTab(currentBrowseTab))">← Back</button>
          <h3 style="margin:0;color:var(--text)">${escHtml(playlistName)}</h3>
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn" onclick="action(this, () => playItem('playlist', '${attrQuote(playlistName)}'))">Play Playlist</button>
          <button class="btn" onclick="action(this, () => shuffleItem('playlist', '${attrQuote(playlistName)}'))">Shuffle Playlist</button>
        </div>
      </div>
      <div>
        <h4 style="margin:0 0 8px;color:var(--text)">Tracks (${songs.length})</h4>
        <div class="devs" style="max-height:300px">
          ${songs.slice(0, 20).map(song => `
            <div class="dev">
              <div class='left'>
                <div class='name'>${escHtml(song)}</div>
              </div>
              <div style='flex:1;display:flex;align-items:center;gap:10px'>
                <span class='chip'>song</span>
                <button class="btn" onclick="action(this, () => playItem('song', '${attrQuote(song)}'))">Play</button>
              </div>
            </div>
          `).join('')}
          ${songs.length > 20 ? `<div style="text-align:center;padding:8px;color:var(--muted)">... and ${songs.length - 20} more songs</div>` : ''}
        </div>
      </div>
    `;
    showToast(`Loaded ${songs.length} tracks`, true);
  }catch(e){ console.warn('browse playlist', e); showToast('Failed to load playlist details', false); }
}

function toggleSettings(){
  const content = $('#settingsContent');
  const toggle = $('#settingsToggle');
  if(content.style.display === 'none'){
    content.style.display = 'block';
    toggle.textContent = 'Hide';
  }else{
    content.style.display = 'none';
    toggle.textContent = 'Show';
  }
}

async function loadSettings(){
  try{
    const s = await (await fetch('/settings')).json();
    $('#inPort').value = s.port || 7766;
    $('#openBrowser').checked = !!s.open_browser;
    // Quit confirmation toggle
    CONFIRM_QUIT = (s.confirm_quit !== undefined) ? !!s.confirm_quit : true;
    $('#confirmQuit').checked = CONFIRM_QUIT;
    // New: polling intervals + path
    $('#pollNow').value = s.poll_now_ms || POLL_NOW_MS;
    $('#pollDevices').value = s.poll_devices_ms || POLL_DEVICES_MS;
    $('#pollMaster').value = (typeof s.poll_master_ms === 'number') ? s.poll_master_ms : POLL_MASTER_MS;
    if (s.config_path){ $('#cfgPath').textContent = s.config_path; }
    // Apply timers to whatever is saved
    applyPollingIntervals(
      s.poll_now_ms || POLL_NOW_MS,
      s.poll_devices_ms || POLL_DEVICES_MS,
      (typeof s.poll_master_ms === 'number') ? s.poll_master_ms : POLL_MASTER_MS
    );
  }catch(e){ console.warn('settings', e); }
}

async function saveSettings(){
  const p = parseInt($('#inPort').value)||7766;
  const ob = $('#openBrowser').checked;
  const cq = $('#confirmQuit').checked;
  const pn = parseInt($('#pollNow').value)||POLL_NOW_MS;
  const pd = parseInt($('#pollDevices').value)||POLL_DEVICES_MS;
  const pm = parseInt($('#pollMaster').value);
  const s = await call('/settings','POST',{
    port:p,
    open_browser: ob,
    confirm_quit: cq,
    poll_now_ms: pn,
    poll_devices_ms: pd,
    poll_master_ms: (isFinite(pm) ? pm : POLL_MASTER_MS)
  });
  // Determine target URL after restart
  const newPort = (s && s.settings && s.settings.port) ? parseInt(s.settings.port) : p;
  const curPort = parseInt(location.port || '80');
  const targetUrl = (newPort && newPort !== curPort)
    ? `${location.protocol}//${location.hostname}:${newPort}/ui`
    : '/ui';

  // Apply polling changes locally for immediate effect
  if (s && s.settings){
    applyPollingIntervals(
      s.settings.poll_now_ms || pn,
      s.settings.poll_devices_ms || pd,
      (typeof s.settings.poll_master_ms === 'number') ? s.settings.poll_master_ms : pm
    );
  }

  // Trigger server restart explicitly
  try { await fetch('/restart', {method:'POST'}); } catch(e) { /* ignore */ }

  // Give the server a moment, then navigate
  setTimeout(()=>{ location.href = targetUrl; }, 1200);
}

// initial load + polling
loadSettings();
loadNow(); loadMaster(); loadDevices();
// intervals are created inside loadSettings()
</script>
</body></html>
        '''
    )
# --- Settings endpoint ---

@app.route('/airplay_full', methods=['GET'])
def airplay_full():
    statuses = _read_airplay_full()
    volumes = _get_airplay_volumes()
    for item in statuses:
        name = item['name']
        item['volume'] = volumes.get(name, None)
    return jsonify(statuses)


# ---- SSE endpoint ----
@app.route('/events')
def sse_events():
    """Server-Sent Events stream for live updates."""
    _start_watchers_once()

    def _stream():
        q = _sse_subscribe()
        # Initial snapshot
        snap = _current_snapshot()
        init = json.dumps({"event": "snapshot", "data": snap, "ts": int(time.time() * 1000)}, ensure_ascii=False)
        yield f"data: {init}\n\n"
        try:
            while True:
                msg = q.get()
                yield f"data: {msg}\n\n"
        finally:
            _sse_unsubscribe(q)

    return Response(_stream(), mimetype='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

# --- AirPlay debug endpoint ---
@app.route('/airplay_debug', methods=['GET'])
def airplay_debug():
    """Return raw AppleScript text for AirPlay device state (primary and fallback)."""
    script_primary = '''
    tell application "Music"
        set outLines to {}
        try
            repeat with d in AirPlay devices
                set nm to ""
                set volTxt to "-1"
                set isSel to false
                try
                    set nm to (name of d as text)
                end try
                try
                    set isSel to (selected of d)
                end try
                try
                    set volTxt to (sound volume of d as text)
                end try
                set end of outLines to nm & tab & (isSel as text) & tab & volTxt
            end repeat
        on error errm number errn
            return "ERROR:" & errn & ":" & errm
        end try
        set AppleScript's text item delimiters to linefeed
        return outLines as text
    end tell
    '''
    r1 = run_applescript(script_primary)
    used = 'primary'
    raw_primary = r1 if isinstance(r1, str) else (r1.get('error') if isinstance(r1, dict) else '')
    raw_fallback = None
    if isinstance(r1, dict) or (isinstance(r1, str) and (r1.startswith('ERROR:') or r1.strip()=='')):
        script_fallback = '''
        tell application "Music"
            set outLines to {}
            set nameList to {}
            try
                set nameList to name of AirPlay devices
            on error
                set nameList to {}
            end try
            repeat with nm in nameList
                set isSel to false
                set volTxt to "-1"
                try
                    set d to (first AirPlay device whose name is (nm as text))
                    try
                        set isSel to (selected of d)
                    end try
                    try
                        set volTxt to (sound volume of d as text)
                    end try
                end try
                set end of outLines to (nm as text) & tab & (isSel as text) & tab & volTxt
            end repeat
            set AppleScript's text item delimiters to linefeed
            return outLines as text
        end tell
        '''
        r2 = run_applescript(script_fallback)
        raw_fallback = r2 if isinstance(r2, str) else (r2.get('error') if isinstance(r2, dict) else '')
        used = 'fallback'
        return jsonify({'used': used, 'primary': raw_primary, 'fallback': raw_fallback})
    return jsonify({'used': used, 'primary': raw_primary})
@app.route('/settings', methods=['GET', 'POST'])
def settings_endpoint():
    """Get or update controller settings. POST may trigger a restart if port changes."""
    if request.method == 'GET':
        s = load_settings()
        # Include config path so UI can show it
        s_out = dict(s)
        s_out["config_path"] = CONFIG_PATH
        return jsonify(s_out)

    payload = request.get_json(silent=True) or {}
    current = load_settings()
    updated = dict(current)

    if 'port' in payload:
        try:
            p = int(payload['port'])
            if 1 <= p <= 65535:
                updated['port'] = p
        except Exception:
            pass
    if 'auto_apply' in payload:
        updated['auto_apply'] = bool(payload['auto_apply'])
    if 'open_browser' in payload:
        updated['open_browser'] = bool(payload['open_browser'])
    if 'confirm_quit' in payload:
        updated['confirm_quit'] = bool(payload['confirm_quit'])

    # Optional polling intervals
    if 'poll_now_ms' in payload:
        try:
            pn = int(payload['poll_now_ms'])
            # clamp to sane range
            if 250 <= pn <= 60000:
                updated['poll_now_ms'] = pn
        except Exception:
            pass
    if 'poll_devices_ms' in payload:
        try:
            pd = int(payload['poll_devices_ms'])
            if 500 <= pd <= 60000:
                updated['poll_devices_ms'] = pd
        except Exception:
            pass
    if 'poll_master_ms' in payload:
        try:
            pm = int(payload['poll_master_ms'])
            # allow 0 to disable; clamp upper bound
            if 0 <= pm <= 60000:
                updated['poll_master_ms'] = pm
        except Exception:
            pass

    save_settings(updated)
    need_restart = (updated.get('port') != current.get('port')) or (updated.get('open_browser') != current.get('open_browser'))
    out_settings = dict(updated)
    out_settings["config_path"] = CONFIG_PATH
    return jsonify({"ok": True, "restart": need_restart, "settings": out_settings})


@app.route('/playlists', methods=['GET'])
def get_playlists():
    script = '''
    tell application "Music"
        set playlist_names to name of every playlist
        return playlist_names
    end tell
    '''
    result = run_applescript(script)
    app.logger.debug(f"/playlists result: {result}")
    if isinstance(result, dict):
        return jsonify({'error': result.get('error', 'Unknown error in AppleScript')}), 500
    playlists = result.split(', ') if result else []
    return jsonify(playlists)

@app.route('/albums', methods=['GET'])
def get_albums():
    script = '''
    tell application "Music"
        set album_names to album of every track of library playlist 1
        set AppleScript's text item delimiters to linefeed
        return album_names as text
    end tell
    '''
    result = run_applescript(script)
    if LIB_LIST_DEBUG:
        app.logger.debug(f"/albums result: {result}")
    if isinstance(result, dict):
        app.logger.error(f"/albums AppleScript error: {result.get('error')}")
        # Stay AppleScript-only but keep HA UI happy
        return jsonify([])
    albums = [name for name in (result.splitlines() if isinstance(result, str) and result else []) if name]
    # De-duplicate while preserving first occurrence, then sort case-insensitively
    albums = list(dict.fromkeys(albums))
    albums.sort(key=str.casefold)
    return jsonify(albums)

@app.route('/songs/<playlist>', methods=['GET'])
def get_songs(playlist):
    script = f'''
    tell application "Music"
        set song_names to name of every track of playlist "{playlist}"
        return song_names
    end tell
    '''
    result = run_applescript(script)
    app.logger.debug(f"/songs result for {playlist}: {result}")
    if isinstance(result, dict):
        return jsonify({'error': result.get('error', 'Unknown error in AppleScript')}), 500
    songs = result.split(', ') if result else []
    return jsonify(songs)

@app.route('/artists', methods=['GET'])
def get_artists():
    script = '''
    tell application "Music"
        set artist_names to artist of every track of library playlist 1
        set AppleScript's text item delimiters to linefeed
        return artist_names as text
    end tell
    '''
    result = run_applescript(script)
    if LIB_LIST_DEBUG:
        app.logger.debug(f"/artists result: {result}")
    if isinstance(result, dict):
        return jsonify({'error': result.get('error', 'Unknown error in AppleScript')}), 500
    # Deduplicate and sort alphabetically (case-insensitive)
    artists = sorted({name for name in (result.splitlines() if isinstance(result, str) and result else []) if name}, key=str.casefold)
    return jsonify(artists)


@app.route('/search', methods=['GET'])
def search_endpoint():
    """Search Apple Music library for albums/artists/playlists/songs.

    Query params:
      - q: search term (required)
      - types: comma-separated in {album, artist, playlist, song} (optional; default all)
        (also accepts singular 'type' for compatibility)
      - limit: maximum results per type (default 25, max 100)

    Returns JSON like {"albums": [...], "artists": [...], "playlists": [...], "songs": [...]}.
    """
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({"albums": [], "artists": [], "playlists": [], "songs": []})

    # Parse requested types (accept both 'types' and legacy 'type')
    types_raw = (request.args.get('types') or request.args.get('type') or '').strip()
    allowed = {t.strip().lower() for t in types_raw.split(',') if t.strip()}
    if not allowed:
        allowed = {"album", "artist", "playlist", "song"}

    # Clamp per-type limit
    try:
        limit = int(request.args.get('limit', 25))
        limit = max(1, min(100, limit))
    except Exception:
        limit = 25

    safe = applescript_escape(q)

    def _dedupe_limit(items):
        seen = set()
        out = []
        for s in items:
            if s and s not in seen:
                seen.add(s)
                out.append(s)
            if len(out) >= limit:
                break
        return out

    def _run_list_script(script):
        r = run_applescript(script)
        if isinstance(r, dict):
            app.logger.error(f"/search AppleScript error: {r.get('error')}")
            return []
        lines = [ln for ln in (r.splitlines() if isinstance(r, str) and r else []) if ln]
        return _dedupe_limit(lines)

    result = {"albums": [], "artists": [], "playlists": [], "songs": []}

    # Albums
    if 'album' in allowed:
        script = f'''
        tell application "Music"
            ignoring case
                set xs to album of (every track of library playlist 1 whose album contains "{safe}")
            end ignoring
            set AppleScript's text item delimiters to linefeed
            return xs as text
        end tell
        '''
        try:
            result["albums"] = _run_list_script(script)
        except Exception as e:
            app.logger.debug(f"/search albums fallback: {e}")

    # Artists
    if 'artist' in allowed:
        script = f'''
        tell application "Music"
            ignoring case
                set xs to artist of (every track of library playlist 1 whose artist contains "{safe}")
            end ignoring
            set AppleScript's text item delimiters to linefeed
            return xs as text
        end tell
        '''
        try:
            # De-duplicate and limit; artists are often highly duplicated
            result["artists"] = _run_list_script(script)
        except Exception as e:
            app.logger.debug(f"/search artists fallback: {e}")

    # Playlists
    if 'playlist' in allowed:
        script = f'''
        tell application "Music"
            ignoring case
                set xs to name of (every playlist whose name contains "{safe}")
            end ignoring
            set AppleScript's text item delimiters to linefeed
            return xs as text
        end tell
        '''
        try:
            result["playlists"] = _run_list_script(script)
        except Exception as e:
            app.logger.debug(f"/search playlists fallback: {e}")

    # Songs (tracks)
    if 'song' in allowed:
        script = f'''
        tell application "Music"
            ignoring case
                set xs to name of (every track of library playlist 1 whose name contains "{safe}")
            end ignoring
            set AppleScript's text item delimiters to linefeed
            return xs as text
        end tell
        '''
        try:
            result["songs"] = _run_list_script(script)
        except Exception as e:
            app.logger.debug(f"/search songs fallback: {e}")

    # Optionally return a 'tracks' alias for compatibility
    result["tracks"] = list(result["songs"]) if result.get("songs") else []

    app.logger.debug(f"/search q='{q}' types={sorted(list(allowed))} => sizes: "
                     f"albums={len(result['albums'])}, artists={len(result['artists'])}, "
                     f"playlists={len(result['playlists'])}, songs={len(result['songs'])}")

    return jsonify(result)

@app.route('/songs_by_album/<album>', methods=['GET'])
def get_songs_by_album(album):
    safe = applescript_escape(album)
    script = f'''
    tell application "Music"
        try
            set track_list to every track of library playlist 1 whose album is "{safe}"
        on error
            set track_list to {{}}
        end try
        set song_names to {{}}
        repeat with t in track_list
            try
                set n to name of t
                if n is not missing value and n is not "" then set end of song_names to n
            end try
        end repeat
        set AppleScript's text item delimiters to linefeed
        return song_names as text
    end tell
    '''
    result = run_applescript(script)
    app.logger.debug(f"/songs_by_album result for {album}: {result}, type: {type(result)}")
    if isinstance(result, dict) and 'error' in result:
        app.logger.error(f"Error fetching songs for album {album}: {result['error']}")
        return jsonify([])
    # Preserve the album's track order; do not sort
    songs = [name for name in (result.splitlines() if isinstance(result, str) and result else []) if name]
    return jsonify(songs)

@app.route('/songs_by_artist/<artist>', methods=['GET'])
def get_songs_by_artist(artist):
    safe = applescript_escape(artist)
    script = f'''
    tell application "Music"
        try
            set track_list to every track of library playlist 1 whose artist is "{safe}"
        on error
            set track_list to {{}}
        end try
        set song_names to {{}}
        repeat with t in track_list
            try
                set n to name of t
                if n is not missing value and n is not "" then set end of song_names to n
            end try
        end repeat
        set AppleScript's text item delimiters to linefeed
        return song_names as text
    end tell
    '''
    result = run_applescript(script)
    app.logger.debug(f"/songs_by_artist result for {artist}: {result}")
    if isinstance(result, dict) and 'error' in result:
        app.logger.error(f"/songs_by_artist AppleScript error for '{artist}': {result['error']}")
        return jsonify([])
    songs = [name for name in (result.splitlines() if isinstance(result, str) and result else []) if name]
    return jsonify(songs)

@app.route('/albums_by_artist/<artist>', methods=['GET'])
def get_albums_by_artist(artist):
    safe = applescript_escape(artist)
    script = f'''
    tell application "Music"
        try
            set track_list to every track of library playlist 1 whose artist is "{safe}"
        on error
            set track_list to {{}}
        end try
        set album_names to {{}}
        repeat with t in track_list
            try
                set a to album of t
                if a is not missing value and a is not "" and album_names does not contain a then set end of album_names to a
            end try
        end repeat
        set AppleScript's text item delimiters to linefeed
        set unsorted to album_names as text
        set sortedText to do shell script "/usr/bin/printf '%s\\n' " & quoted form of unsorted & " | /usr/bin/sort -f"
        return sortedText
    end tell
    '''
    result = run_applescript(script)
    app.logger.debug(f"/albums_by_artist result for {artist}: {result}")
    if isinstance(result, dict) and 'error' in result:
        app.logger.error(f"/albums_by_artist AppleScript error for '{artist}': {result['error']}")
        return jsonify([])
    albums = [name for name in (result.splitlines() if isinstance(result, str) and result else []) if name]
    return jsonify(albums)

@app.route('/playlist_tracks/<path:playlist>', methods=['GET'])
def get_playlist_tracks(playlist):
    return get_songs(playlist)

@app.route('/album_tracks/<path:album>', methods=['GET'])
def get_album_tracks(album):
    return get_songs_by_album(album)

@app.route('/play', methods=['POST'])
def play_music():
    data = request.json
    music_type = data.get('type')
    name = data.get('name')
    devices = data.get('devices')  # Accepts string or list
    shuffle = bool(data.get('shuffle'))

    # If no devices provided, do not fail — keep current AirPlay devices as-is.
    # Only apply device selection if caller provided one.
    if not name or not music_type:
        return jsonify({'error': 'Music type and name required'}), 400

    # Handle devices: ensure it's a list
    if devices is None:
        devices = []
    elif isinstance(devices, str):
        devices = [dev.strip() for dev in devices.split(',') if dev.strip()]
    elif not isinstance(devices, list):
        devices = []

    # Escape device names for AppleScript safety
    devices = [applescript_escape(dev) for dev in devices]

    # --- Disambiguated song play: honor album/artist or playlist+index if provided ---
    if music_type == 'song':
        album = data.get('album')
        artist = data.get('artist')
        playlist = data.get('playlist')
        index = data.get('index') or data.get('idx')
        try:
            index = int(index) if index is not None else None
        except Exception:
            index = None
        safe_name = applescript_escape(name)
        devices_script = ', '.join(f'AirPlay device "{dev}"' for dev in devices)

        # Case 1: playlist + index (play the Nth track of the named playlist)
        if playlist and index:
            safe_pl = applescript_escape(playlist)
            script = f'''
            tell application "Music"
                try
                    if { '{' + devices_script + '}' if devices else '{}' } is not {{}} then set current AirPlay devices to {{ {devices_script} }}
                    set pl to (first playlist whose name is "{safe_pl}")
                    set cnt to (count of tracks of pl)
                    if {index} ≥ 1 and {index} ≤ cnt then
                        play (track {index} of pl)
                        return "OK"
                    else
                        return "ERROR: index out of range"
                    end if
                on error errm number errn
                    return "ERROR:" & errn & ":" & errm
                end try
            end tell
            '''
            r = run_applescript(script)
            if isinstance(r, dict) or (isinstance(r, str) and r.startswith('ERROR:')):
                return jsonify({"error": str(r.get('error') if isinstance(r, dict) else r)}), 500
            return jsonify({"status": "playing", "result": r})

        # Case 2: explicit album/artist qualifiers
        if album or artist:
            safe_album = applescript_escape(album) if album else None
            safe_artist = applescript_escape(artist) if artist else None
            where_parts = [f'name is "{safe_name}"']
            if safe_album:
                where_parts.append(f'album is "{safe_album}"')
            if safe_artist:
                where_parts.append(f'artist is "{safe_artist}"')
            where_txt = ' and '.join(where_parts)
            script = f'''
            tell application "Music"
                try
                    if { '{' + devices_script + '}' if devices else '{}' } is not {{}} then set current AirPlay devices to {{ {devices_script} }}
                    set xs to (every track of library playlist 1 whose {where_txt})
                    if (count of xs) ≥ 1 then
                        play (item 1 of xs)
                        return "OK"
                    else
                        return "ERROR: no matching track"
                    end if
                on error errm number errn
                    return "ERROR:" & errn & ":" & errm
                end try
            end tell
            '''
            r = run_applescript(script)
            if isinstance(r, dict) or (isinstance(r, str) and r.startswith('ERROR:')):
                return jsonify({"error": str(r.get('error') if isinstance(r, dict) else r)}), 500
            return jsonify({"status": "playing", "result": r})

    # Construct play command based on type (escaped)
    safe_name = applescript_escape(name)
    selection_block = ""

    if music_type == 'playlist':
        selection_block = f'''
try
    set thePlaylist to playlist "{safe_name}"
    play
on error
    play thePlaylist "{safe_name}"
end try
'''
        play_command = ''
    elif music_type == 'album':
        selection_block = (
            f'set theTracks to (every track of library playlist 1 whose album is "{safe_name}")\n'
            f'set theName to "Home Assistant"\n'
            f'if (exists user playlist theName) then delete user playlist theName\n'
            f'set q to make new user playlist with properties {{name:theName}}\n'
            f'repeat with t in theTracks\n'
            f'  try\n'
            f'    duplicate t to q\n'
            f'  end try\n'
            f'end repeat'
        )
        play_command = 'play user playlist theName'
    elif music_type == 'song':
        play_command = f'play (first track of library playlist 1 whose name is "{safe_name}")'
    elif music_type == 'artist':
        selection_block = (
            f'set theTracks to (every track of library playlist 1 whose artist is "{safe_name}")\n'
            f'set theName to "Home Assistant"\n'
            f'if (exists user playlist theName) then delete user playlist theName\n'
            f'set q to make new user playlist with properties {{name:theName}}\n'
            f'repeat with t in theTracks\n'
            f'  try\n'
            f'    duplicate t to q\n'
            f'  end try\n'
            f'end repeat'
        )
        play_command = 'play user playlist theName'
    else:
        return jsonify({'error': 'Invalid type'}), 400

    devices_script = ', '.join(f'AirPlay device "{dev}"' for dev in devices)
    script = f'''
    tell application "Music"
        try
            -- Only set devices when provided; otherwise keep current selection
            if { '{' + devices_script + '}' if devices else '{}' } is not {{}} then set current AirPlay devices to {{ {devices_script} }}
        end try
        {selection_block}
        set shuffle enabled to {str(shuffle).lower()}
        {play_command}
    end tell
    '''
    result = run_applescript(script)
    app.logger.debug(f"/play result for {music_type} '{name}' on {devices}: {result}")
    if isinstance(result, dict) and 'error' in result:
        return jsonify({'error': result['error']}), 500
    return jsonify({'status': 'playing', 'result': result})

@app.route('/volume', methods=['POST'])
def set_volume():
    data = request.json
    device = data.get('device')
    level = data.get('level')
    if not device or level is None:
        return jsonify({'error': 'Device and level required'}), 400
    script = f'''
    tell application "Music"
        set sound volume of AirPlay device "{device}" to {level}
    end tell
    '''
    result = run_applescript(script)
    app.logger.debug(f"/volume result: {result}")
    if isinstance(result, dict):
        return jsonify({'error': result.get('error', 'Unknown error in AppleScript')}), 500
    return jsonify({'status': 'volume set', 'result': result})


# Set Apple Music master volume (0-100)
@app.route('/set_volume', methods=['POST'])
def set_master_volume():
    """Set Apple Music master sound volume (0-100)."""
    data = request.get_json(silent=True) or {}
    try:
        vol = float(data.get('volume', 0))
        vol_int = max(0, min(100, int(round(vol))))
    except Exception:
        return jsonify({'error': 'invalid volume'}), 400
    script = f'tell application "Music" to set sound volume to {vol_int}'
    result = run_applescript(script)
    app.logger.debug(f"/set_volume result: {result}")
    if isinstance(result, dict):
        return jsonify({'error': result.get('error', 'AppleScript error')}), 500
    return jsonify({'status': 'ok', 'volume': vol_int})

@app.route('/pause', methods=['POST'])
def pause_music():
    script = 'tell application "Music" to pause'
    result = run_applescript(script)
    app.logger.debug(f"/pause result: {result}")
    if isinstance(result, dict):
        return jsonify({'error': result.get('error', 'Unknown error in AppleScript')}), 500
    return jsonify({'status': 'paused', 'result': result})

@app.route('/stop', methods=['POST'])
def stop_music():
    script = 'tell application "Music" to stop'
    result = run_applescript(script)
    app.logger.debug(f"/stop result: {result}")
    if isinstance(result, dict):
        return jsonify({'error': result.get('error', 'Unknown error in AppleScript')}), 500
    return jsonify({'status': 'stopped', 'result': result})

@app.route('/next', methods=['POST'])
def next_track():
    script = 'tell application "Music" to next track'
    result = run_applescript(script)
    app.logger.debug(f"/next result: {result}")
    if isinstance(result, dict):
        return jsonify({'error': result.get('error', 'Unknown error in AppleScript')}), 500
    try:
        app.logger.debug("/next: prefetch trigger")
        _prefetch_now_and_next(delay=0.4)
    except Exception as e:
        app.logger.debug(f"/next: prefetch trigger error: {e}")
    return jsonify({'status': 'skipped', 'result': result})


@app.route('/previous', methods=['POST'])
def previous_track():
    script = 'tell application "Music" to previous track'
    result = run_applescript(script)
    app.logger.debug(f"/previous result: {result}")
    if isinstance(result, dict):
        return jsonify({'error': result.get('error', 'Unknown error in AppleScript')}), 500
    try:
        app.logger.debug("/previous: prefetch trigger")
        _prefetch_now_and_next(delay=0.4)
    except Exception as e:
        app.logger.debug(f"/previous: prefetch trigger error: {e}")
    return jsonify({'status': 'restarted_or_previous', 'result': result})

# /resume endpoint for resuming playback and optionally setting AirPlay devices
@app.route('/resume', methods=['POST'])
def resume():
    """Resume playback of the current track in Music, optionally applying AirPlay devices first."""
    data = request.get_json(silent=True) or {}
    devices_csv = (data.get('devices') or '').strip()
    devices = [d.strip() for d in devices_csv.split(',') if d.strip()]

    # If device names provided, try to set current AirPlay devices before playing
    if devices:
        name_list = ", ".join([f'"{applescript_escape(n)}"' for n in devices])
        set_devices_script = f'''
        tell application "Music"
            try
                set tlist to {{{name_list}}}
                set outDevs to {{}}
                repeat with nm in tlist
                    try
                        set d to (first AirPlay device whose name is (nm as text))
                        set end of outDevs to d
                    end try
                end repeat
                if (count of outDevs) > 0 then set current AirPlay devices to outDevs
            end try
        end tell
        '''
        res = run_applescript(set_devices_script)
        if isinstance(res, dict) and 'error' in res:
            app.logger.warning(f"/resume: failed to set AirPlay devices: {res['error']}")

    # Now ask Music to play/resume whatever is queued
    play_script = 'tell application "Music" to play'
    res2 = run_applescript(play_script)
    app.logger.debug(f"/resume result: {res2}")
    if isinstance(res2, dict) and 'error' in res2:
        return jsonify({'error': res2.get('error', 'AppleScript error')}), 500
    return jsonify({'status': 'ok'})

@app.route('/now_playing', methods=['GET'])
def now_playing():
    """Return current playback info from Music as JSON."""
    try:
        _start_watchers_once()
    except Exception:
        pass
    script = '''
    tell application "Music"
        set pstate to player state as text
        set shuf to shuffle enabled
        set rep to song repeat
        set vol to sound volume
        if pstate is "stopped" then
            set AppleScript's text item delimiters to linefeed
            return pstate & "\n" & "" & "\n" & "" & "\n" & "" & "\n" & "0" & "\n" & (shuf as text) & "\n" & (rep as text) & "\n" & (vol as text) & "\n" & "0"
        end if
        set t to current track
        set nm to ""
        set ar to ""
        set al to ""
        try
            set nm to name of t
        end try
        try
            set ar to artist of t
        end try
        try
            set al to album of t
        end try
        set pos to player position
        set dur to 0
        try
            set dur to duration of t
        end try
        set AppleScript's text item delimiters to linefeed
        return pstate & "\n" & nm & "\n" & ar & "\n" & al & "\n" & (pos as text) & "\n" & (shuf as text) & "\n" & (rep as text) & "\n" & (vol as text) & "\n" & (dur as text)
    end tell
    '''
    result = run_applescript(script)
    # app.logger.debug(f"/now_playing result: {result}")
    if isinstance(result, dict):
        # Keep UI happy: return a minimal payload with state unknown
        return jsonify({
            'state': 'unknown',
            'title': None,
            'artist': None,
            'album': None,
            'position': 0,
            'duration': 0,
            'shuffle': None,
            'volume': None,
            'error': result.get('error', 'AppleScript error')
        })

    lines = result.splitlines() if isinstance(result, str) else []
    # Ensure we have 9 fields
    while len(lines) < 9:
        lines.append("")
    state, title, artist, album, position, shuffle_txt, repeat_txt, volume_txt, duration = lines[:9]

    def _to_float(s):
        try:
            return float(s)
        except Exception:
            return 0.0

    def _to_int(s):
        try:
            return int(float(s))
        except Exception:
            return None

    def _to_bool(s):
        s = (s or "").strip().lower()
        return s in ("true", "yes", "1")

    payload = {
        'state': state or 'stopped',
        'title': title or None,
        'artist': artist or None,
        'album': album or None,
        'position': _to_float(position),
        'duration': _to_float(duration),
        'shuffle': _to_bool(shuffle_txt) if shuffle_txt != '' else None,
        'repeat': repeat_txt.strip() if repeat_txt.strip() else 'off',
        'volume': _to_int(volume_txt),
    }
    try:
        # Include current artwork token to help clients align cache keys when polling
        payload['artwork_token'] = _last_snapshot.get('art_tok')
    except Exception:
        pass
    return jsonify(payload)

@app.route('/icon/<name>', methods=['GET'])
def icon(name: str):
    """Return a small SVG icon for browse categories (playlist/album/artist)."""
    name = (name or "").lower()
    # Use explicit fills so they render regardless of HA theme colors
    if name == "playlist":
        svg = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path fill='#9da0a2' d='M3 6h12v2H3V6m0 4h12v2H3v-2m0 4h8v2H3v-2m13-3a3 3 0 1 1 2 5.236V21h-2v-4.764A3 3 0 0 1 16 11Z'/></svg>"
    elif name == "album":
        svg = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path fill='#9da0a2' d='M12 2a10 10 0 1 0 0 20a10 10 0 0 0 0-20m0 5a5 5 0 1 1 0 10a5 5 0 0 1 0-10m0 3a2 2 0 1 0 0 4a2 2 0 0 0 0-4'/></svg>"
    elif name == "artist":
        svg = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path fill='#9da0a2' d='M12 12a4 4 0 1 0-4-4a4 4 0 0 0 4 4m0 2c-4 0-8 2-8 5v1h16v-1c0-3-4-5-8-5Z'/></svg>"
    else:
        svg = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><circle cx='12' cy='12' r='10' fill='#9da0a2'/></svg>"
    return Response(svg, mimetype='image/svg+xml')

@app.route('/devices', methods=['GET'])
def get_devices():
    script = '''
    tell application "Music"
        try
            set device_names to name of AirPlay devices
        on error
            try
                set device_names to name of current AirPlay devices
            on error
                set device_names to {}
            end try
        end try
        set AppleScript's text item delimiters to linefeed
        return device_names as text
    end tell
    '''
    result = run_applescript(script)
    app.logger.debug(f"/devices result: {result}")
    if isinstance(result, dict):
        err = result.get('error', '')
        if '-1731' in err or 'Unknown object type' in err:
            app.logger.warning("/devices: AppleScript AirPlay classes not available; returning empty list")
            return jsonify([])
        return jsonify({'error': err or 'Unknown error in AppleScript'}), 500
    raw = [ (name.strip() if isinstance(name, str) else name) for name in (result.splitlines() if isinstance(result, str) and result else []) ]
    devices = []
    seen = set()
    for n in raw:
        if not n or n in seen:
            continue
        seen.add(n)
        devices.append(n)
    return jsonify(devices)

@app.route('/purge_album_cache', methods=['POST','GET'])
def purge_album_cache():
    """Delete all cached album artwork files under the Artwork directory."""
    try:
        os.makedirs(ARTWORK_DIR, exist_ok=True)
        cnt = 0
        for name in os.listdir(ARTWORK_DIR):
            p = os.path.join(ARTWORK_DIR, name)
            try:
                if os.path.isfile(p):
                    os.remove(p)
                    cnt += 1
            except Exception:
                pass
        return jsonify({"status": "ok", "deleted": cnt, "path": ARTWORK_DIR})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route('/purge_thumb_cache', methods=['POST','GET'])
def purge_thumb_cache():
    """Alias for compatibility; thumbnails are generated on the fly from album cache."""
    return purge_album_cache()

# --- Debug helpers ---
@app.route('/debug/cache_index', methods=['GET'])
def debug_cache_index():
    try:
        os.makedirs(ARTWORK_DIR, exist_ok=True)
        items = []
        for name in sorted(os.listdir(ARTWORK_DIR)):
            p = os.path.join(ARTWORK_DIR, name)
            try:
                st = os.stat(p)
                items.append({
                    "name": name,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                })
            except Exception:
                continue
        return jsonify({"dir": ARTWORK_DIR, "count": len(items), "files": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/debug/state', methods=['GET'])
def debug_state():
    try:
        now = _last_snapshot.get("now") or {}
        return jsonify({
            "watchers_started": bool(_watchers_started),
            "last_snapshot": {
                "has_now": _last_snapshot.get("now") is not None,
                "has_airplay": _last_snapshot.get("airplay") is not None,
                "master": _last_snapshot.get("master"),
                "shuffle": _last_snapshot.get("shuffle"),
                "art_tok": _last_snapshot.get("art_tok"),
                "now": {
                    "title": now.get("title"),
                    "artist": now.get("artist"),
                    "album": now.get("album"),
                    "pid": now.get("pid"),
                    "state": now.get("state"),
                },
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/debug/now_dump', methods=['GET'])
def debug_now_dump():
    try:
        return jsonify({
            "_get_now_playing_dict": _get_now_playing_dict(),
            "snapshot_now": _last_snapshot.get("now") or {}
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/set_devices', methods=['POST'])
def set_devices():
    """Immediately set current AirPlay devices in Music to the provided list of names.
    Body: { "devices": "Name 1,Name 2,..." }
    """
    data = request.get_json(silent=True) or {}
    devices_csv = (data.get('devices') or '').strip()
    names = [d.strip() for d in devices_csv.split(',') if d.strip()]
    if not names:
        # No-op if empty; don't clear devices implicitly
        return jsonify({"status": "ok", "applied": []})

    # Build AppleScript list of names safely
    name_list = ", ".join([f'"{applescript_escape(n)}"' for n in names])
    script = f'''
    tell application "Music"
        try
            set tlist to {{{name_list}}}
            set outDevs to {{}}
            repeat with nm in tlist
                try
                    set d to (first AirPlay device whose name is (nm as text))
                    set end of outDevs to d
                end try
            end repeat
            if (count of outDevs) > 0 then set current AirPlay devices to outDevs
            -- Reflect what Music actually accepted:
            set appliedNames to {{}}
            try
                repeat with d in current AirPlay devices
                    set end of appliedNames to (name of d as text)
                end repeat
            on error
                -- Fallback if class not scriptable on this version
                repeat with d in outDevs
                    set end of appliedNames to (name of d as text)
                end repeat
            end try
            set AppleScript's text item delimiters to ","
            return appliedNames as text
        on error errm number errn
            return "ERROR:" & errn & ":" & errm
        end try
    end tell
    '''
    result = run_applescript(script)
    app.logger.debug(f"/set_devices result: {result}")
    if isinstance(result, dict):
        return jsonify({"error": result.get('error', 'AppleScript error')}), 500
    if isinstance(result, str) and result.startswith("ERROR:"):
        return jsonify({"error": result}), 500

    applied = []
    if isinstance(result, str) and result:
        applied = [s.strip() for s in result.split(',') if s and s.strip()]
    # Push an immediate AirPlay devices update so UIs refresh without waiting for poll
    try:
        statuses = _read_airplay_full()
        volumes = _get_airplay_volumes()
        for item in statuses:
            name = item['name']
            item['volume'] = volumes.get(name, None)
        _last_snapshot['airplay'] = statuses
        _sse_publish('airplay_full', statuses)
    except Exception:
        pass
    return jsonify({"status": "ok", "applied": applied})

@app.route('/device_volumes', methods=['GET'])
def device_volumes():
    """Return a JSON mapping of AirPlay device name -> current volume (0-100)."""
    script = '''
    tell application "Music"
        try
            set devNames to name of AirPlay devices
        on error
            try
                set devNames to name of current AirPlay devices
            on error
                set devNames to {}
            end try
        end try
        set outLines to {}
        repeat with nm in devNames
            set volTxt to "-1"
            try
                set v to sound volume of (first AirPlay device whose name is (nm as text))
                set volTxt to (v as text)
            end try
            set end of outLines to ((nm as text) & tab & volTxt)
        end repeat
        set AppleScript's text item delimiters to linefeed
        return outLines as text
    end tell
    '''
    result = run_applescript(script)
    app.logger.debug(f"/device_volumes result: {result}")
    if isinstance(result, dict):
        err = result.get('error', '')
        if '-1731' in err or 'Unknown object type' in err:
            return jsonify({})
        return jsonify({'error': err or 'AppleScript error'}), 500
    volumes = {}
    if isinstance(result, str) and result:
        for line in result.splitlines():
            if "\t" in line:
                name, vol = line.split("\t", 1)
                name = name.strip()
                try:
                    v = int(float(vol.strip()))
                except Exception:
                    v = -1
                if name:
                    volumes[name] = max(0, min(100, v)) if v >= 0 else None
    return jsonify(volumes)


@app.route('/artwork', methods=['GET'])
def artwork():
    """Return current track artwork, preferring cached album artwork.

    Query params:
      - refresh=1: ignore cache and re-read from Music, updating the cache.
    """
    # Read current album/artist via our helper to form a cache key
    # Ensure watchers are running so _last_snapshot stays warm
    try:
        _start_watchers_once()
    except Exception:
        pass
    now = _get_now_playing_dict() or {}
    title = (now.get('title') or '').strip()
    album = (now.get('album') or '').strip()
    artist = (now.get('artist') or '').strip()
    pid = (now.get('pid') or '').strip()
    # If current poll returned blanks (common during transitions or for some sources),
    # fall back to the watcher snapshot for best-known values.
    if not album or not artist or not pid:
        try:
            snap = _last_snapshot.get('now') or {}
            if not title:
                title = (snap.get('title') or '').strip()
            if not album:
                album = (snap.get('album') or '').strip()
            if not artist:
                artist = (snap.get('artist') or '').strip()
            if not pid:
                pid = (snap.get('pid') or '').strip()
        except Exception:
            pass
    force_refresh = str(request.args.get('refresh') or '').strip() in ('1', 'true', 'yes')
    try:
        app.logger.info(f"/artwork: req title='{title}' album='{album}' artist='{artist}' pid='{pid}' refresh={force_refresh}")
    except Exception:
        pass

    if album and not force_refresh:
        data, mime_cached = _try_read_album_cache(album, artist)
        if data:
            app.logger.info(f"/artwork: serve from cache for album='{album}' artist='{artist}'")
            # If cache is not WEBP, convert on the fly for consistency
            out = _convert_to_webp(data)
            if out is not None:
                try:
                    etag = hashlib.sha1(out[0]).hexdigest()
                except Exception:
                    etag = None
                headers = {'ETag': etag} if etag else {}
                return Response(out[0], mimetype=out[1], headers=headers)
            mime_r = (mime_cached or _guess_image_mime(data))
            try:
                etag = hashlib.sha1(data).hexdigest()
            except Exception:
                etag = None
            headers = {'ETag': etag} if etag else {}
            return Response(data, mimetype=mime_r, headers=headers)

    # Fallback to reading directly from Music for the current track
    script = '''
    tell application "Music"
        try
            set t to current track
            if t is missing value then return "NOART"
            if (count of artworks of t) is 0 then return "NOART"
            set fmtText to ""
            try
                set fmtText to (format of artwork 1 of t) as text
            end try
            set ext to "jpg"
            if fmtText contains "PNG" then set ext to "png"
            set raw_data to data of artwork 1 of t
            set tmp to (POSIX path of (path to temporary items)) & "ha_music_art." & ext
            set outFile to open for access (POSIX file tmp) with write permission
            set eof outFile to 0
            write raw_data to outFile
            close access outFile
            return tmp
        on error
            return "NOART"
        end try
    end tell
    '''
    result = run_applescript(script)
    if not isinstance(result, str) or not result.strip() or result.strip() == "NOART":
        # Fallback: try album-wide search (often another track has embedded art)
        if album:
            try:
                data = _album_art_bytes(album)
            except Exception:
                data = None
            if data:
                key_album = album if album else (f"pid_{pid}" if pid else None)
                if key_album:
                    _write_album_cache(key_album, artist, data)
                    app.logger.info(f"/artwork: fallback(album_scan) cached key='{key_album}' artist='{artist}' -> {ARTWORK_DIR}")
                try:
                    etag = hashlib.sha1(data).hexdigest()
                except Exception:
                    etag = None
                headers = {'ETag': etag} if etag else {}
                return Response(data, mimetype=_guess_image_mime(data), headers=headers)
        # Final fallback: return a tiny PNG (not SVG) so HA color extraction doesn't error
        app.logger.info("/artwork: NOART after current+fallback; serving tiny PNG")
        return Response(_BLANK_PNG, mimetype='image/png')

    path = result.strip()
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except Exception as e:
        app.logger.error(f"/artwork file read error: {e}")
        return Response(status=404)
    finally:
        try:
            os.remove(path)
        except Exception:
            pass

    # Save into album cache for future fast reads
    # Cache under album when possible; otherwise fall back to a composite or pid-based key
    key_album = None
    if album:
        key_album = album
    elif artist and title:
        key_album = f"{artist} - {title}"
    elif pid:
        key_album = f"pid_{pid}"
    if key_album:
        _write_album_cache(key_album, artist, data)
        app.logger.info(f"/artwork: cached key='{key_album}' artist='{artist}' -> {ARTWORK_DIR}")

    # Serve WEBP if possible
    out = _convert_to_webp(data)
    if out is not None:
        try:
            etag = hashlib.sha1(out[0]).hexdigest()
        except Exception:
            etag = None
        headers = {'ETag': etag} if etag else {}
        return Response(out[0], mimetype=out[1], headers=headers)
    mime = _guess_image_mime(data)
    try:
        etag = hashlib.sha1(data).hexdigest()
    except Exception:
        etag = None
    headers = {'ETag': etag} if etag else {}
    return Response(data, mimetype=mime, headers=headers)


@app.route('/artwork_thumb/<int:size>', methods=['GET'])
def artwork_thumb(size: int):
    """Return current track artwork resized to <= size px, with album-scan fallback.

    This mirrors /artwork but applies a resize step using macOS 'sips' for speed.
    """
    try:
        _start_watchers_once()
    except Exception:
        pass
    now = _get_now_playing_dict() or {}
    album = (now.get('album') or '').strip()
    artist = (now.get('artist') or '').strip()
    # Try direct current-track artwork first
    script = '''
    tell application "Music"
        try
            set t to current track
            if t is missing value then return "NOART"
            if (count of artworks of t) is 0 then return "NOART"
            set fmtText to ""
            try
                set fmtText to (format of artwork 1 of t) as text
            end try
            set ext to "jpg"
            if fmtText contains "PNG" then set ext to "png"
            set raw_data to data of artwork 1 of t
            set tmp to (POSIX path of (path to temporary items)) & "ha_music_art." & ext
            set outFile to open for access (POSIX file tmp) with write permission
            set eof outFile to 0
            write raw_data to outFile
            close access outFile
            return tmp
        on error
            return "NOART"
        end try
    end tell
    '''
    result = run_applescript(script)
    data = None
    if isinstance(result, str) and result and result.strip() != 'NOART':
        p = result.strip()
        try:
            with open(p, 'rb') as f:
                data = f.read()
        except Exception:
            data = None
        finally:
            try:
                os.remove(p)
            except Exception:
                pass
    # Fallback: scan album
    if not data and album:
        try:
            data = _album_art_bytes(album)
        except Exception:
            data = None
    if not data:
        return Response(_BLANK_PNG, mimetype='image/png')
    # Resize via sips
    try:
        out, mime = _resize_bytes_with_sips(data, max(32, min(2048, int(size))))
    except Exception:
        out = data
        mime = _guess_image_mime(data)
    try:
        etag = hashlib.sha1(out).hexdigest()
    except Exception:
        etag = None
    headers = {'ETag': etag} if etag else {}
    return Response(out, mimetype=mime, headers=headers)

# ---- Helpers for artwork bytes (unified logic for full & thumbnails) ----

def _read_and_cleanup(path: str) -> bytes | None:
    if not path:
        return None
    try:
        with open(path, 'rb') as f:
            return f.read()
    except Exception:
        return None
    finally:
        try:
            os.remove(path)
        except Exception:
            pass

def _safe_slug(s: str) -> str:
    try:
        s = s.strip()
    except Exception:
        return ""
    # Replace path separators and illegal characters, collapse whitespace
    out = []
    for ch in s:
        if ch.isalnum() or ch in (" ", "-", "_", "."):
            out.append(ch)
        else:
            out.append("_")
    slug = "".join(out).strip().strip("._")
    # Collapse spaces and underscores
    while "  " in slug:
        slug = slug.replace("  ", " ")
    slug = slug.replace(" ", "_")
    if not slug:
        slug = "unknown"
    # Limit filename length
    return slug[:120]

def _album_cache_path(album: str, artist: str | None, ext: str = "jpg") -> str:
    base = _safe_slug(album or "unknown")
    # Add a short hash suffix to ensure uniqueness across artists
    key = f"{(artist or '').lower()}|{(album or '').lower()}".encode("utf-8", "ignore")
    suf = hashlib.sha1(key).hexdigest()[:8]
    fn = f"{base}__{suf}.{ext.lower()}"
    try:
        os.makedirs(ARTWORK_DIR, exist_ok=True)
    except Exception as e:
        app.logger.debug(f"artwork cache: failed to ensure dir {ARTWORK_DIR}: {e}")
    return os.path.join(ARTWORK_DIR, fn)

def _try_read_album_cache(album: str, artist: str | None) -> tuple[bytes | None, str | None]:
    """Attempt reading cached artwork for multiple key variants.

    We first try the exact (album, artist) key; on miss, fall back to (album, "").
    This prevents noisy initial misses when the artist was unknown during prefetch
    but becomes available a moment later for the request.
    """
    tried = []
    variants = [artist or ""]
    if (artist or ""):
        variants.append("")
    for who in variants:
        for ext in ("webp", "jpg", "png", "jpeg"):
            p = _album_cache_path(album, who, ext)
            tried.append(p)
            try:
                with open(p, "rb") as f:
                    data = f.read()
                    app.logger.debug(f"artwork cache: HIT {p} ({len(data)} bytes)")
                    return data, _guess_image_mime(data)
            except Exception:
                continue
    # Only log a single MISS per (album, artist) request to reduce noise
    app.logger.debug(
        f"artwork cache: MISS for album='{album}' artist='{artist}' (tried {len(tried)} paths)"
    )
    return None, None

def _write_album_cache(album: str, artist: str | None, data: bytes) -> None:
    if not data:
        return
    # Prefer WEBP on disk for consistency and size
    out = _convert_to_webp(data)
    if out is not None:
        data_to_write, _ = out
        path = _album_cache_path(album, artist, "webp")
    else:
        mime = _guess_image_mime(data)
        ext = "png" if (isinstance(mime, str) and "png" in mime) else "jpg"
        path = _album_cache_path(album, artist, ext)
        data_to_write = data
        try:
            app.logger.debug("artwork cache: WEBP conversion unavailable; wrote %s", path)
        except Exception:
            pass
    try:
        with open(path, "wb") as f:
            f.write(data_to_write)
        app.logger.debug(f"artwork cache: WROTE {path} ({len(data_to_write)} bytes)")
    except Exception as e:
        app.logger.debug(f"artwork cache write failed: {e}")

def _album_art_bytes(album: str) -> bytes | None:
    """Return raw artwork bytes for an album, with cache and artist fallback."""
    # Attempt cached read first using album + first-track artist
    try:
        # Find primary artist name for this album for cache keying
        safe = applescript_escape(album)
        script_artist = f'''
        tell application "Music"
            try
                set t to (first track of library playlist 1 whose album is "{safe}")
                return (artist of t as text)
            on error
                return ""
            end try
        end tell
        '''
        artist_name = run_applescript(script_artist)
        if isinstance(artist_name, dict):
            artist_name = ""
    except Exception:
        artist_name = ""
    data, _ = _try_read_album_cache(album, artist_name if isinstance(artist_name, str) else "")
    if data:
        return data
    safe = applescript_escape(album)
    script = f'''
    tell application "Music"
        try
            set tlist to every track of library playlist 1 whose album is "{safe}"
        on error
            set tlist to {{}}
        end try
        set found to false
        set tmp to ""
        set fmtText to ""
        repeat with t in tlist
            try
                if (count of artworks of t) > 0 then
                    try
                        set fmtText to (format of artwork 1 of t) as text
                    end try
                    set ext to "jpg"
                    if fmtText contains "PNG" then set ext to "png"
                    set raw_data to data of artwork 1 of t
                    set tmp to (POSIX path of (path to temporary items)) & "ha_music_art." & ext
                    set outFile to open for access (POSIX file tmp) with write permission
                    set eof outFile to 0
                    write raw_data to outFile
                    close access outFile
                    set found to true
                    exit repeat
                end if
            end try
        end repeat
        if not found then
            -- Fallback: try artist artwork for the first track of this album
            set artistName to ""
            try
                set artistName to artist of (first track of library playlist 1 whose album is "{safe}")
            end try
            if artistName is not missing value and artistName is not "" then
                try
                    set alist to every track of library playlist 1 whose artist is artistName
                on error
                    set alist to {{}}
                end try
                repeat with t in alist
                    try
                        if (count of artworks of t) > 0 then
                            try
                                set fmtText to (format of artwork 1 of t) as text
                            end try
                            set ext to "jpg"
                            if fmtText contains "PNG" then set ext to "png"
                            set raw_data to data of artwork 1 of t
                            set tmp to (POSIX path of (path to temporary items)) & "ha_music_art." & ext
                            set outFile to open for access (POSIX file tmp) with write permission
                            set eof outFile to 0
                            write raw_data to outFile
                            close access outFile
                            set found to true
                            exit repeat
                        end if
                    end try
                end repeat
            end if
        end if
        if found then return tmp
        return "NOART"
    end tell
    '''
    result = run_applescript(script)
    if not isinstance(result, str) or not result.strip() or result.strip() == "NOART":
        return None
    bytes_out = _read_and_cleanup(result.strip())
    if bytes_out:
        _write_album_cache(album, artist_name if isinstance(artist_name, str) else "", bytes_out)
    return bytes_out

def _resize_bytes_with_sips(data: bytes, size: int) -> tuple[bytes, str]:
    """Resize image bytes to <=size; prefer WEBP via Pillow, else fall back to sips/JPEG."""
    # Try Pillow → WEBP first
    out = _convert_to_webp(data, size)
    if out is not None:
        return out
    # Fallback: use sips to resize JPEG, return JPEG
    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as inf:
        inf.write(data)
        in_path = inf.name
    try:
        try:
            subprocess.run(
                ["/usr/bin/sips", "-Z", str(int(size)), in_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
            )
        except Exception:
            pass
        with open(in_path, 'rb') as f:
            out_data = f.read()
    finally:
        try:
            os.remove(in_path)
        except Exception:
            pass
    return out_data, _guess_image_mime(out_data)

# --- Prefetch helper (on-demand) ---
def _prefetch_now_and_next(delay: float = 0.3) -> None:
    """Spawn a background task to cache current and next-track album artwork after a short delay."""
    def _run():
        try:
            app.logger.debug(f"prefetch(on-demand): begin (delay={delay})")
            if delay and delay > 0:
                time.sleep(delay)
            # Retry a few times while Music updates metadata
            attempts = 6
            for i in range(attempts):
                now = _get_now_playing_dict() or {}
                alb = (now.get('album') or '').strip()
                art = (now.get('artist') or '').strip()
                if not alb:
                    # Fallback to last snapshot if watcher populated it
                    try:
                        snap = _last_snapshot.get('now') or {}
                        alb = (snap.get('album') or '').strip() or alb
                        art = (snap.get('artist') or '').strip() or art
                    except Exception:
                        pass
                if alb:
                    app.logger.debug(f"prefetch(on-demand): current album='{alb}' (attempt {i+1})")
                    try:
                        _album_art_bytes(alb)
                    except Exception as e:
                        app.logger.debug(f"prefetch current error: {e}")
                    break
                time.sleep(0.35)

            # NOTE: Do not attempt to read "next track" via AppleScript — using that term can invoke the skip command.
            # If you want next-track prefetch in the future, compute it via playlist + index safely, not "next track".
            app.logger.debug("prefetch(on-demand): done")
        except Exception as e:
            app.logger.debug(f"prefetch task error: {e}")
    threading.Thread(target=_run, daemon=True).start()

# --- ARTWORK ENDPOINTS FOR ALBUM, PLAYLIST, ARTIST (Browse Media thumbnails) ---

@app.route('/artwork_album/<path:album>', methods=['GET'])
def artwork_album(album):
    data = _album_art_bytes(album)
    app.logger.debug(f"/artwork_album album='{album}' bytes={len(data) if data else 0}")
    if not data:
        placeholder = (
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'>"
            "<rect width='24' height='24' fill='#e0e3e7'/><text x='12' y='14' font-size='8' text-anchor='middle' fill='#9aa0a6'>ALBUM</text></svg>"
        )
        return Response(placeholder, mimetype='image/svg+xml')
    # Prefer WEBP serve
    out = _convert_to_webp(data)
    if out is not None:
        try:
            etag = hashlib.sha1(out[0]).hexdigest()
        except Exception:
            etag = None
        headers = {'ETag': etag} if etag else {}
        return Response(out[0], mimetype=out[1], headers=headers)
    mime = _guess_image_mime(data)
    try:
        etag = hashlib.sha1(data).hexdigest()
    except Exception:
        etag = None
    headers = {'ETag': etag} if etag else {}
    return Response(data, mimetype=mime, headers=headers)


@app.route('/artwork_playlist/<path:plist>', methods=['GET'])
def artwork_playlist(plist):
    safe = applescript_escape(plist)
    script = f'''
    tell application "Music"
        try
            set tlist to every track of playlist "{safe}"
        on error
            set tlist to {{}}
        end try
        set found to false
        set tmp to ""
        set fmtText to ""
        repeat with t in tlist
            try
                if (count of artworks of t) > 0 then
                    try
                        set fmtText to (format of artwork 1 of t) as text
                    end try
                    set ext to "jpg"
                    if fmtText contains "PNG" then set ext to "png"
                    set raw_data to data of artwork 1 of t
                    set tmp to (POSIX path of (path to temporary items)) & "ha_music_art." & ext
                    set outFile to open for access (POSIX file tmp) with write permission
                    set eof outFile to 0
                    write raw_data to outFile
                    close access outFile
                    set found to true
                    exit repeat
                end if
            end try
        end repeat
        if found then return tmp
        return "NOART"
    end tell
    '''
    result = run_applescript(script)
    if not isinstance(result, str) or not result.strip() or result.strip() == "NOART":
        placeholder = (
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'>"
            "<rect width='24' height='24' fill='#e0e3e7'/><text x='12' y='14' font-size='8' text-anchor='middle' fill='#9aa0a6'>LIST</text></svg>"
        )
        return Response(placeholder, mimetype='image/svg+xml')
    path = result.strip()
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except Exception:
        return Response(status=404)
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
    out = _convert_to_webp(data)
    if out is not None:
        try:
            etag = hashlib.sha1(out[0]).hexdigest()
        except Exception:
            etag = None
        headers = {'ETag': etag} if etag else {}
        return Response(out[0], mimetype=out[1], headers=headers)
    mime = _guess_image_mime(data)
    try:
        etag = hashlib.sha1(data).hexdigest()
    except Exception:
        etag = None
    headers = {'ETag': etag} if etag else {}
    return Response(data, mimetype=mime, headers=headers)


@app.route('/artwork_artist/<path:artist>', methods=['GET'])
def artwork_artist(artist):
    safe = applescript_escape(artist)
    script = f'''
    tell application "Music"
        try
            set tlist to every track of library playlist 1 whose artist is "{safe}"
        on error
            set tlist to {{}}
        end try
        set found to false
        set tmp to ""
        set fmtText to ""
        repeat with t in tlist
            try
                if (count of artworks of t) > 0 then
                    try
                        set fmtText to (format of artwork 1 of t) as text
                    end try
                    set ext to "jpg"
                    if fmtText contains "PNG" then set ext to "png"
                    set raw_data to data of artwork 1 of t
                    set tmp to (POSIX path of (path to temporary items)) & "ha_music_art." & ext
                    set outFile to open for access (POSIX file tmp) with write permission
                    set eof outFile to 0
                    write raw_data to outFile
                    close access outFile
                    set found to true
                    exit repeat
                end if
            end try
        end repeat
        if found then return tmp
        return "NOART"
    end tell
    '''
    result = run_applescript(script)
    if not isinstance(result, str) or not result.strip() or result.strip() == "NOART":
        placeholder = (
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'>"
            "<rect width='24' height='24' fill='#e0e3e7'/><text x='12' y='14' font-size='8' text-anchor='middle' fill='#9aa0a6'>ART</text></svg>"
        )
        return Response(placeholder, mimetype='image/svg+xml')
    path = result.strip()
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except Exception:
        return Response(status=404)
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
    out = _convert_to_webp(data)
    if out is not None:
        return Response(out[0], mimetype=out[1])
    mime = _guess_image_mime(data)
    return Response(data, mimetype=mime)


# --- THUMBNAIL ARTWORK ENDPOINTS (resized with sips) ---

@app.route('/artwork_album_thumb/<int:size>/<path:album>', methods=['GET'])
def artwork_album_thumb(size, album):
    data = _album_art_bytes(album)
    app.logger.debug(f"/artwork_album_thumb/{size} album='{album}' base_bytes={len(data) if data else 0}")
    if not data:
        placeholder = (
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'>"
            "<rect width='24' height='24' fill='#e0e3e7'/><text x='12' y='14' font-size='8' text-anchor='middle' fill='#9aa0a6'>ALBUM</text></svg>"
        )
        return Response(placeholder, mimetype='image/svg+xml')
    thumb_bytes, mime = _resize_bytes_with_sips(data, size)
    # Ensure WEBP serve
    out = _convert_to_webp(thumb_bytes, None)
    if out is not None:
        try:
            etag = hashlib.sha1(out[0]).hexdigest()
        except Exception:
            etag = None
        headers = {'ETag': etag} if etag else {}
        return Response(out[0], mimetype=out[1], headers=headers)
    try:
        etag = hashlib.sha1(thumb_bytes).hexdigest()
    except Exception:
        etag = None
    headers = {'ETag': etag} if etag else {}
    return Response(thumb_bytes, mimetype=mime, headers=headers)


@app.route('/artwork_playlist_thumb/<int:size>/<path:plist>', methods=['GET'])
def artwork_playlist_thumb(size, plist):
    safe = applescript_escape(plist)
    script = f'''
    tell application "Music"
        try
            set tlist to every track of playlist "{safe}"
        on error
            set tlist to {{}}
        end try
        set found to false
        set tmp to ""
        set fmtText to ""
        repeat with t in tlist
            try
                if (count of artworks of t) > 0 then
                    try
                        set fmtText to (format of artwork 1 of t) as text
                    end try
                    set ext to "jpg"
                    if fmtText contains "PNG" then set ext to "png"
                    set raw_data to data of artwork 1 of t
                    set tmp to (POSIX path of (path to temporary items)) & "ha_music_art." & ext
                    set outFile to open for access (POSIX file tmp) with write permission
                    set eof outFile to 0
                    write raw_data to outFile
                    close access outFile
                    set found to true
                    exit repeat
                end if
            end try
        end repeat
        if found then return tmp
        return "NOART"
    end tell
    '''
    result = run_applescript(script)
    if not isinstance(result, str) or not result.strip() or result.strip() == "NOART":
        placeholder = (
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'>"
            "<rect width='24' height='24' fill='#e0e3e7'/><text x='12' y='14' font-size='8' text-anchor='middle' fill='#9aa0a6'>LIST</text></svg>"
        )
        return Response(placeholder, mimetype='image/svg+xml')
    path = result.strip()
    try:
        try:
            subprocess.run(["/usr/bin/sips", "-Z", str(int(size)), path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except Exception:
            pass
        with open(path, 'rb') as f:
            data = f.read()
    except Exception:
        return Response(status=404)
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
    # Ensure WEBP serve (and size-resized by sips already)
    out = _convert_to_webp(data, None)
    if out is not None:
        try:
            etag = hashlib.sha1(out[0]).hexdigest()
        except Exception:
            etag = None
        headers = {'ETag': etag} if etag else {}
        return Response(out[0], mimetype=out[1], headers=headers)
    mime = _guess_image_mime(data)
    try:
        etag = hashlib.sha1(data).hexdigest()
    except Exception:
        etag = None
    headers = {'ETag': etag} if etag else {}
    return Response(data, mimetype=mime, headers=headers)


@app.route('/artwork_artist_thumb/<int:size>/<path:artist>', methods=['GET'])
def artwork_artist_thumb(size, artist):
    safe = applescript_escape(artist)
    script = f'''
    tell application "Music"
        try
            set tlist to every track of library playlist 1 whose artist is "{safe}"
        on error
            set tlist to {{}}
        end try
        set found to false
        set tmp to ""
        set fmtText to ""
        repeat with t in tlist
            try
                if (count of artworks of t) > 0 then
                    try
                        set fmtText to (format of artwork 1 of t) as text
                    end try
                    set ext to "jpg"
                    if fmtText contains "PNG" then set ext to "png"
                    set raw_data to data of artwork 1 of t
                    set tmp to (POSIX path of (path to temporary items)) & "ha_music_art." & ext
                    set outFile to open for access (POSIX file tmp) with write permission
                    set eof outFile to 0
                    write raw_data to outFile
                    close access outFile
                    set found to true
                    exit repeat
                end if
            end try
        end repeat
        if found then return tmp
        return "NOART"
    end tell
    '''
    result = run_applescript(script)
    if not isinstance(result, str) or not result.strip() or result.strip() == "NOART":
        placeholder = (
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'>"
            "<rect width='24' height='24' fill='#e0e3e7'/><text x='12' y='14' font-size='8' text-anchor='middle' fill='#9aa0a6'>ART</text></svg>"
        )
        return Response(placeholder, mimetype='image/svg+xml')
    path = result.strip()
    try:
        try:
            subprocess.run(["/usr/bin/sips", "-Z", str(int(size)), path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except Exception:
            pass
        with open(path, 'rb') as f:
            data = f.read()
    except Exception:
        return Response(status=404)
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
    out = _convert_to_webp(data, None)
    if out is not None:
        return Response(out[0], mimetype=out[1])
    mime = _guess_image_mime(data)
    return Response(data, mimetype=mime)

@app.route('/artwork_album_meta/<path:album>', methods=['GET'])
def artwork_album_meta(album):
    """Return metadata (etag, ctype) for an album's artwork without sending the bytes."""
    safe = applescript_escape(album)
    script = f'''
    tell application "Music"
        try
            set tlist to every track of library playlist 1 whose album is "{safe}"
        on error
            set tlist to {{}}
        end try
        set found to false
        set tmp to ""
        set fmtText to ""
        repeat with t in tlist
            try
                if (count of artworks of t) > 0 then
                    try
                        set fmtText to (format of artwork 1 of t) as text
                    end try
                    set ext to "jpg"
                    if fmtText contains "PNG" then set ext to "png"
                    set raw_data to data of artwork 1 of t
                    set tmp to (POSIX path of (path to temporary items)) & "ha_music_art." & ext
                    set outFile to open for access (POSIX file tmp) with write permission
                    set eof outFile to 0
                    write raw_data to outFile
                    close access outFile
                    set found to true
                    exit repeat
                end if
            end try
        end repeat
        if found then return tmp
        return "NOART"
    end tell
    '''
    result = run_applescript(script)
    if not isinstance(result, str) or not result.strip() or result.strip() == "NOART":
        return jsonify({"etag": "noart", "ctype": "image/svg+xml"})
    path = result.strip()
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except Exception:
        return jsonify({"etag": "noart", "ctype": "image/svg+xml"})
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
    # Report WEBP meta/etag if conversion available
    out = _convert_to_webp(data)
    if out is not None:
        wbytes, wmime = out
        etag = hashlib.sha1(wbytes).hexdigest()
        return jsonify({"etag": etag, "ctype": wmime})
    mime = _guess_image_mime(data)
    etag = hashlib.sha1(data).hexdigest()
    return jsonify({"etag": etag, "ctype": mime})


# --- META THUMBNAIL ENDPOINTS ---

@app.route('/artwork_album_thumb_meta/<int:size>/<path:album>', methods=['GET'])
def artwork_album_thumb_meta(size, album):
    data = _album_art_bytes(album)
    if not data:
        return jsonify({"etag": "noart", "ctype": "image/svg+xml"})
    thumb_bytes, mime = _resize_bytes_with_sips(data, size)
    out = _convert_to_webp(thumb_bytes)
    if out is not None:
        b, m = out
        etag = hashlib.sha1(b).hexdigest()
        return jsonify({"etag": etag, "ctype": m})
    etag = hashlib.sha1(thumb_bytes).hexdigest()
    return jsonify({"etag": etag, "ctype": mime})



@app.route('/artwork_playlist_thumb_meta/<int:size>/<path:plist>', methods=['GET'])
def artwork_playlist_thumb_meta(size, plist):
    safe = applescript_escape(plist)
    script = f'''
    tell application "Music"
        try
            set tlist to every track of playlist "{safe}"
        on error
            set tlist to {{}}
        end try
        set found to false
        set tmp to ""
        set fmtText to ""
        repeat with t in tlist
            try
                if (count of artworks of t) > 0 then
                    try
                        set fmtText to (format of artwork 1 of t) as text
                    end try
                    set ext to "jpg"
                    if fmtText contains "PNG" then set ext to "png"
                    set raw_data to data of artwork 1 of t
                    set tmp to (POSIX path of (path to temporary items)) & "ha_music_art." & ext
                    set outFile to open for access (POSIX file tmp) with write permission
                    set eof outFile to 0
                    write raw_data to outFile
                    close access outFile
                    set found to true
                    exit repeat
                end if
            end try
        end repeat
        if found then return tmp
        return "NOART"
    end tell
    '''
    result = run_applescript(script)
    if not isinstance(result, str) or not result.strip() or result.strip() == "NOART":
        return jsonify({"etag": "noart", "ctype": "image/svg+xml"})
    path = result.strip()
    try:
        try:
            subprocess.run(["/usr/bin/sips", "-Z", str(int(size)), path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except Exception:
            pass
        with open(path, 'rb') as f:
            data = f.read()
    except Exception:
        return jsonify({"etag": "noart", "ctype": "image/svg+xml"})
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
    out = _convert_to_webp(data)
    if out is not None:
        b, m = out
        etag = hashlib.sha1(b).hexdigest()
        return jsonify({"etag": etag, "ctype": m})
    mime = _guess_image_mime(data)
    etag = hashlib.sha1(data).hexdigest()
    return jsonify({"etag": etag, "ctype": mime})


@app.route('/artwork_artist_thumb_meta/<int:size>/<path:artist>', methods=['GET'])
def artwork_artist_thumb_meta(size, artist):
    safe = applescript_escape(artist)
    script = f'''
    tell application "Music"
        try
            set tlist to every track of library playlist 1 whose artist is "{safe}"
        on error
            set tlist to {{}}
        end try
        set found to false
        set tmp to ""
        set fmtText to ""
        repeat with t in tlist
            try
                if (count of artworks of t) > 0 then
                    try
                        set fmtText to (format of artwork 1 of t) as text
                    end try
                    set ext to "jpg"
                    if fmtText contains "PNG" then set ext to "png"
                    set raw_data to data of artwork 1 of t
                    set tmp to (POSIX path of (path to temporary items)) & "ha_music_art." & ext
                    set outFile to open for access (POSIX file tmp) with write permission
                    set eof outFile to 0
                    write raw_data to outFile
                    close access outFile
                    set found to true
                    exit repeat
                end if
            end try
        end repeat
        if found then return tmp
        return "NOART"
    end tell
    '''
    result = run_applescript(script)
    if not isinstance(result, str) or not result.strip() or result.strip() == "NOART":
        return jsonify({"etag": "noart", "ctype": "image/svg+xml"})
    path = result.strip()
    try:
        try:
            subprocess.run(["/usr/bin/sips", "-Z", str(int(size)), path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except Exception:
            pass
        with open(path, 'rb') as f:
            data = f.read()
    except Exception:
        return jsonify({"etag": "noart", "ctype": "image/svg+xml"})
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
    out = _convert_to_webp(data)
    if out is not None:
        b, m = out
        etag = hashlib.sha1(b).hexdigest()
        return jsonify({"etag": etag, "ctype": m})
    mime = _guess_image_mime(data)
    etag = hashlib.sha1(data).hexdigest()
    return jsonify({"etag": etag, "ctype": mime})


@app.route('/artwork_playlist_meta/<path:plist>', methods=['GET'])
def artwork_playlist_meta(plist):
    """Return metadata (etag, ctype) for a playlist's artwork."""
    safe = applescript_escape(plist)
    script = f'''
    tell application "Music"
        try
            set tlist to every track of playlist "{safe}"
        on error
            set tlist to {{}}
        end try
        set found to false
        set tmp to ""
        set fmtText to ""
        repeat with t in tlist
            try
                if (count of artworks of t) > 0 then
                    try
                        set fmtText to (format of artwork 1 of t) as text
                    end try
                    set ext to "jpg"
                    if fmtText contains "PNG" then set ext to "png"
                    set raw_data to data of artwork 1 of t
                    set tmp to (POSIX path of (path to temporary items)) & "ha_music_art." & ext
                    set outFile to open for access (POSIX file tmp) with write permission
                    set eof outFile to 0
                    write raw_data to outFile
                    close access outFile
                    set found to true
                    exit repeat
                end if
            end try
        end repeat
        if found then return tmp
        return "NOART"
    end tell
    '''
    result = run_applescript(script)
    if not isinstance(result, str) or not result.strip() or result.strip() == "NOART":
        return jsonify({"etag": "noart", "ctype": "image/svg+xml"})
    path = result.strip()
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except Exception:
        return jsonify({"etag": "noart", "ctype": "image/svg+xml"})
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
    mime = _guess_image_mime(data)
    etag = hashlib.sha1(data).hexdigest()
    return jsonify({"etag": etag, "ctype": mime})


@app.route('/artwork_artist_meta/<path:artist>', methods=['GET'])
def artwork_artist_meta(artist):
    """Return metadata (etag, ctype) for an artist's artwork (first track with art)."""
    safe = applescript_escape(artist)
    script = f'''
    tell application "Music"
        try
            set tlist to every track of library playlist 1 whose artist is "{safe}"
        on error
            set tlist to {{}}
        end try
        set found to false
        set tmp to ""
        set fmtText to ""
        repeat with t in tlist
            try
                if (count of artworks of t) > 0 then
                    try
                        set fmtText to (format of artwork 1 of t) as text
                    end try
                    set ext to "jpg"
                    if fmtText contains "PNG" then set ext to "png"
                    set raw_data to data of artwork 1 of t
                    set tmp to (POSIX path of (path to temporary items)) & "ha_music_art." & ext
                    set outFile to open for access (POSIX file tmp) with write permission
                    set eof outFile to 0
                    write raw_data to outFile
                    close access outFile
                    set found to true
                    exit repeat
                end if
            end try
        end repeat
        if found then return tmp
        return "NOART"
    end tell
    '''
    result = run_applescript(script)
    if not isinstance(result, str) or not result.strip() or result.strip() == "NOART":
        return jsonify({"etag": "noart", "ctype": "image/svg+xml"})
    path = result.strip()
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except Exception:
        return jsonify({"etag": "noart", "ctype": "image/svg+xml"})
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
    mime = _guess_image_mime(data)
    etag = hashlib.sha1(data).hexdigest()
    return jsonify({"etag": etag, "ctype": mime})

### --- PLAYBACK CONTROLS AND VOLUME for Web UI / API --- ###
@app.route('/playpause', methods=['POST'])
def playpause():
    script = '''
    tell application "Music"
        try
            playpause
            return "ok"
        on error errm number errn
            return "ERROR:" & errn & ":" & errm
        end try
    end tell
    '''
    result = run_applescript(script)
    if isinstance(result, str) and result.startswith("ERROR:"):
        return jsonify({'error': result}), 500
    if isinstance(result, dict) and 'error' in result:
        return jsonify({'error': result['error']}), 500
    return jsonify({'ok': True})

@app.route('/master_volume', methods=['GET', 'POST'])
def master_volume():
    if request.method == 'GET':
        script = 'tell application "Music" to get sound volume'
        result = run_applescript(script)
        if isinstance(result, dict):
            return Response("0", mimetype="text/plain")
        # return plain text number to keep it simple
        return Response(str(int(float(result or 0))), mimetype="text/plain")

    # POST
    data = request.get_json(silent=True) or {}
    try:
        level = int(float(data.get('level', 0)))
    except Exception:
        return jsonify({'error': 'invalid level'}), 400
    level = max(0, min(100, level))
    script = f'tell application "Music" to set sound volume to {level}'
    result = run_applescript(script)
    if isinstance(result, dict):
        return jsonify({'error': result.get('error', 'AppleScript error')}), 500
    # Publish instant master volume SSE so UIs reflect change without waiting for poll
    try:
        _last_snapshot['master'] = level
        _sse_publish('master_volume', level)
    except Exception:
        pass
    return jsonify({'ok': True, 'level': level})

@app.route('/set_device_volume', methods=['POST'])
def set_device_volume():
    data = request.get_json(silent=True) or {}
    device = data.get('device')
    level = data.get('level')
    if not device or level is None:
        return jsonify({'error': 'Device and level required'}), 400
    try:
        level = int(level)
    except Exception:
        return jsonify({'error': 'invalid level'}), 400
    level = max(0, min(100, level))
    device_safe = applescript_escape(device)
    script = f'''
    tell application "Music"
        try
            set sound volume of (first AirPlay device whose name is "{device_safe}") to {level}
            return "ok"
        on error errm number errn
            return "ERROR:" & errn & ":" & errm
        end try
    end tell
    '''
    result = run_applescript(script)
    if isinstance(result, str) and result.startswith('ERROR:'):
        return jsonify({'error': result}), 500
    if isinstance(result, dict) and 'error' in result:
        return jsonify({'error': result['error']}), 500
    # Push an immediate AirPlay snapshot so per-device volume and selection update quickly
    try:
        statuses = _read_airplay_full()
        volumes = _get_airplay_volumes()
        for item in statuses:
            name = item['name']
            item['volume'] = volumes.get(name, None)
        _last_snapshot['airplay'] = statuses
        _sse_publish('airplay_full', statuses)
    except Exception:
        pass
    return jsonify({'ok': True, 'device': device, 'level': level})

@app.route('/current_devices', methods=['GET'])
def current_devices():
    script = '''
    tell application "Music"
        set out to {}
        try
            repeat with d in AirPlay devices
                try
                    if (selected of d) is true then set end of out to (name of d as text)
                end try
            end repeat
        end try
        set AppleScript's text item delimiters to linefeed
        return out as text
    end tell
    '''
    result = run_applescript(script)
    if isinstance(result, dict):
        return jsonify([])
    raw = [ (n.strip() if isinstance(n, str) else n) for n in (result.splitlines() if isinstance(result, str) and result else []) ]
    devs = []
    seen = set()
    for n in raw:
        if not n or n in seen:
            continue
        seen.add(n)
        devs.append(n)
    return jsonify(devs)

@app.route('/queue_artist_shuffled', methods=['POST'])
def queue_artist_shuffled():
    """Build playlist of all tracks by artist, enable shuffle, and play.
       Body: {"artist": "Name"}  Returns: {ok, count, playlist}
    """
    payload = request.get_json(silent=True) or {}
    artist = (payload.get('artist') or '').strip()
    if not artist:
        return jsonify({"ok": False, "error": "artist required"}), 400

    safe_artist = applescript_escape(artist)
    playlist_name = "Home Assistant"

    script = f'''
    tell application "Music"
        set tgtName to "{applescript_escape(playlist_name)}"
        if not (exists (playlist tgtName)) then
            make new user playlist with properties {{name:tgtName}}
        end if
        set tgt to playlist tgtName

        -- Clear current items
        try
            delete (every track of tgt)
        end try

        -- Collect tracks by artist and duplicate into tgt
        set libTracks to (every track of library playlist 1 whose artist is "{safe_artist}")
        set addedCount to 0
        repeat with t in libTracks
            try
                duplicate t to tgt
                set addedCount to addedCount + 1
            end try
        end repeat

        -- Enable shuffle and play
        try
            set shuffle enabled to true
        end try
        play tgt
        return addedCount as text
    end tell
    '''
    r = run_applescript(script)
    if isinstance(r, dict):
        app.logger.error(f"/queue_artist_shuffled AppleScript error: {r.get('error')}")
        return jsonify({"ok": False, "error": r.get('error', 'AppleScript error')})
    try:
        cnt = int(str(r).strip())
    except Exception:
        cnt = 0
    return jsonify({"ok": True, "count": cnt, "playlist": playlist_name})

### End Web API endpoints ###

# --- Process control endpoints ---

@app.route('/restart', methods=['POST', 'GET'])
def restart_endpoint():
    """Schedule a short-delay restart of the server process."""
    try:
        schedule_restart(0.5)
        return jsonify({"ok": True, "restarting": True})
    except Exception as e:
        app.logger.error(f"/restart failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/quit', methods=['POST', 'GET'])
def quit_endpoint():
    """Schedule a clean exit of the server process."""
    try:
        schedule_quit(0.5)
        return jsonify({"ok": True, "quitting": True})
    except Exception as e:
        app.logger.error(f"/quit failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

def open_browser():
    """Open the default web browser to the Flask app's URL (can be disabled).
    Set AM_OPEN_BROWSER=0 to disable.
    """
    settings = load_settings()
    if settings.get("open_browser", True) is not True:
        return
    if os.getenv("AM_OPEN_BROWSER", "1").lower() not in ("1", "true", "yes", "on"):  # opt-out
        return
    try:
        import webbrowser as _wb
        port = int(settings.get("port", 7766))
        threading.Timer(1.5, lambda: _wb.open(f'http://127.0.0.1:{port}')).start()
    except Exception as e:
        app.logger.warning(f"open_browser failed: {e}")

def launch_apple_music():
    """Launch Apple Music if not already running."""
    script = '''
    tell application "Music"
        if it is not running then
            launch
            delay 5  # Increased for stability
        end if
    end tell
    '''
    result = run_applescript(script)
    if isinstance(result, dict):
        app.logger.error(f"Failed to launch Apple Music: {result.get('error', 'Unknown error')}")
    else:
        app.logger.debug("Apple Music launched successfully")


if __name__ == '__main__':
    # Hide console output when bundled
    if os.environ.get('PYINSTALLER_BUNDLED') == '1':
        import sys
        sys.stdout = open(os.devnull, 'w')
        sys.stderr = open(os.devnull, 'w')
    try:
        launch_apple_music()
        # Start background watchers immediately so state and tokens update
        try:
            _start_watchers_once()
        except Exception:
            pass
        open_browser()
        _settings = load_settings()
        app.run(host='0.0.0.0', port=int(_settings.get('port', 7766)), debug=False)
    except Exception as e:
        app.logger.error(f"Server failed to start: {e}")
        raise
