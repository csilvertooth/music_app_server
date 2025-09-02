import subprocess
import json
import os
import hashlib
import sys
from flask import Flask, request, jsonify, send_file, Response, render_template_string, redirect
import logging
import tempfile
import threading
from queue import Queue, Empty
import time

logging.basicConfig(level=logging.DEBUG)  # Enable debug logging for requests
LIB_LIST_DEBUG = os.getenv("AM_LIB_LIST_DEBUG", "0") == "1"

def _guess_image_mime(data: bytes) -> str:
    """Best-effort guess for artwork bytes without external deps."""
    if not data:
        return "application/octet-stream"
    header = data[:16]
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


app = Flask(__name__)

# ---- SSE pub/sub for push updates ----
_subscribers = set()
_sub_lock = threading.Lock()
_last_snapshot = {"now": None, "airplay": None, "master": None, "shuffle": None, "art_tok": int(time.time())}


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
    script = '''
    tell application "Music"
        try
            set st to (player state as text)
            if st is "stopped" then
                return "\t\t\t\t0\t" & st
            end if
            set t to current track
            set nm to (name of t as text)
            set ar to (artist of t as text)
            set al to (album of t as text)
            set pid to (persistent ID of t as text)
            set pos to (player position as text)
            return nm & tab & ar & tab & al & tab & pid & tab & pos & tab & st
        on error
            return "ERROR"
        end try
    end tell
    '''
    r = run_applescript(script)
    if isinstance(r, dict) or not isinstance(r, str) or r == "ERROR":
        return {"state": "unknown"}
    parts = r.split("\t")
    out = {
        "title": parts[0] if len(parts) > 0 else "",
        "artist": parts[1] if len(parts) > 1 else "",
        "album": parts[2] if len(parts) > 2 else "",
        "pid": parts[3] if len(parts) > 3 else "",
        "position": float(parts[4]) if len(parts) > 4 and parts[4] else 0,
        "state": parts[5] if len(parts) > 5 else "",
        "is_playing": (len(parts) > 5 and parts[5].lower()[:4] == "play")
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


def _read_airplay_full():
    """Return list of {name, volume, active} using the same AppleScript logic as /airplay_full."""
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
        result = run_applescript(script_fallback)
        if isinstance(result, dict):
            app.logger.error(f"/airplay_full fallback AppleScript error: {result.get('error')}")
            return []

    items = []
    if isinstance(result, str) and result:
        for line in result.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                name = parts[0].strip()
                sel = parts[1].strip().lower()
                vol = parts[2].strip()
                try:
                    vol_i = int(float(vol)); vol_i = max(0, min(100, vol_i))
                except Exception:
                    vol_i = None
                if name:
                    items.append({"name": name, "volume": vol_i, "active": sel in ("true", "yes", "1")})
    try:
        items.sort(key=lambda d: (not bool(d.get("active")), str(d.get("name", "")).casefold()))
    except Exception:
        pass
    return items


def _current_snapshot():
    now = _get_now_playing_dict()
    shuffle = bool(get_shuffle_enabled())
    master = _get_master_volume_percent()
    air = _read_airplay_full()
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
    while True:
        s = load_settings()
        itv = max(0.8, (s.get('poll_now_ms', 1500) / 1000.0))
        try:
            now = _get_now_playing_dict()
            pid = now.get('pid') or ''
            st = now.get('state')
            if pid != last_pid or st != last_state:
                last_pid = pid
                last_state = st
                # bump artwork token on track change
                if pid != (_last_snapshot.get('now') or {}).get('pid'):
                    _last_snapshot['art_tok'] = int(time.time() * 1000)
                _last_snapshot['now'] = now
                _sse_publish('now', {**now, 'artwork_token': _last_snapshot['art_tok']})
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
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

_DEF_SETTINGS = {
    "port": 7766,
    "auto_apply": False,
    "open_browser": True,
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

# --- UI route ---

@app.route("/ui")
def web_ui():
    """Serve a minimal, nice-looking control page for Apple Music & AirPlay."""
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
  .btn-circle{width:44px;height:44px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;padding:0}
  .btn-apply{display:inline-flex;align-items:center;gap:8px}
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
            <button class="btn btn-circle" title="Previous" onclick="call('/previous','POST')" aria-label="Previous" id="btn_prev"></button>
            <button id="pp" class="btn btn-circle btn-primary" title="Play/Pause" aria-label="Play/Pause" onclick="playPause()"></button>
            <button class="btn btn-circle" title="Next" onclick="call('/next','POST')" aria-label="Next" id="btn_next"></button>
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
      <div id="devs" class="devs"></div>
      <div class="row" style="justify-content:flex-end;margin-top:8px">
        <button class="btn btn-primary btn-apply" onclick="applyDevicesImmediate()">Apply Selection</button>
      </div>
    </div>
  </div>

  <div class="card" style="margin-top:16px">
    <div class="title" style="margin-bottom:8px">Settings</div>
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
    </div>
    <div class="muted" style="margin-top:6px">Settings file: <code id="cfgPath"></code></div>
    <div class="row" style="justify-content:flex-end;margin-top:10px">
      <button class="btn" onclick="loadSettings()">Reload</button>
      <button class="btn btn-primary" onclick="saveSettings()">Save & Restart</button>
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

async function loadDevices(){
  try{
    const full = await (await fetch('/airplay_full')).json(); // [{name, volume, active}]
    // Sort active first, then by name (case-insensitive)
    full.sort((a, b) => {
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

    full.forEach(d => {
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

async function loadSettings(){
  try{
    const s = await (await fetch('/settings')).json();
    $('#inPort').value = s.port || 7766;
    $('#openBrowser').checked = !!s.open_browser;
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
  const pn = parseInt($('#pollNow').value)||POLL_NOW_MS;
  const pd = parseInt($('#pollDevices').value)||POLL_DEVICES_MS;
  const pm = parseInt($('#pollMaster').value);
  const s = await call('/settings','POST',{
    port:p,
    open_browser: ob,
    poll_now_ms: pn,
    poll_devices_ms: pd,
    poll_master_ms: (isFinite(pm) ? pm : POLL_MASTER_MS)
  });
  if(s.restart){
    alert('Settings saved. The service will restart on the new port. If the page does not reload, open it manually.');
  } else {
    // Apply new polling without restart (port unchanged)
    if (s && s.settings){
      applyPollingIntervals(
        s.settings.poll_now_ms || pn,
        s.settings.poll_devices_ms || pd,
        (typeof s.settings.poll_master_ms === 'number') ? s.settings.poll_master_ms : pm
      );
    }
  }
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
    items = _read_airplay_full()
    return jsonify(items)


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
    if need_restart:
        schedule_restart(1.0)
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

@app.route('/play', methods=['POST'])
def play_music():
    data = request.json
    music_type = data.get('type')
    name = data.get('name')
    devices = data.get('devices')  # Accepts string or list
    shuffle = bool(data.get('shuffle'))

    if not devices:
        return jsonify({'error': 'No devices selected'}), 400
    if not name or not music_type:
        return jsonify({'error': 'Music type and name required'}), 400

    # Handle devices as string by splitting
    if isinstance(devices, str):
        devices = [dev.strip() for dev in devices.split(',') if dev.strip()]
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
        play_command = f'play playlist "{safe_name}"'
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
            set current AirPlay devices to {{{devices_script}}}
        end try
        set shuffle enabled to {str(shuffle).lower()}
        if {str(shuffle).lower()} then set shuffle mode to songs
        {selection_block}
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
    return jsonify({'status': 'skipped', 'result': result})


@app.route('/previous', methods=['POST'])
def previous_track():
    script = 'tell application "Music" to previous track'
    result = run_applescript(script)
    app.logger.debug(f"/previous result: {result}")
    if isinstance(result, dict):
        return jsonify({'error': result.get('error', 'Unknown error in AppleScript')}), 500
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
    script = '''
    tell application "Music"
        set pstate to player state as text
        set shuf to shuffle enabled
        set vol to sound volume
        if pstate is "stopped" then
            set AppleScript's text item delimiters to linefeed
            return pstate & "\n" & "" & "\n" & "" & "\n" & "" & "\n" & "0" & "\n" & (shuf as text) & "\n" & (vol as text) & "\n" & "0"
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
        return pstate & "\n" & nm & "\n" & ar & "\n" & al & "\n" & (pos as text) & "\n" & (shuf as text) & "\n" & (vol as text) & "\n" & (dur as text)
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
    # Ensure we have 8 fields
    while len(lines) < 8:
        lines.append("")
    state, title, artist, album, position, shuffle_txt, volume_txt, duration = lines[:8]

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
        'volume': _to_int(volume_txt),
    }
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
    """Return current track artwork as an image (PNG/JPEG)."""
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
    # app.logger.debug(f"/artwork result: {result}")
    if not isinstance(result, str) or not result.strip() or result.strip() == "NOART":
        placeholder = """
        <svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'>
          <circle cx='12' cy='12' r='10' fill='#e0e3e7'/>
          <circle cx='12' cy='12' r='3' fill='#b0b4b9'/>
        </svg>
        """.strip()
        return Response(placeholder, mimetype='image/svg+xml')

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

    mime = _guess_image_mime(data)
    return Response(data, mimetype=mime)


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

def _album_art_bytes(album: str) -> bytes | None:
    """Return raw artwork bytes for an album, with artist fallback."""
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
    return _read_and_cleanup(result.strip())

def _resize_bytes_with_sips(data: bytes, size: int) -> tuple[bytes, str]:
    """Resize image bytes to <=size using macOS sips; return (bytes, mime)."""
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
    mime = _guess_image_mime(data)
    return Response(data, mimetype=mime)


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
    mime = _guess_image_mime(data)
    return Response(data, mimetype=mime)


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
    return Response(thumb_bytes, mimetype=mime)


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
    mime = _guess_image_mime(data)
    return Response(data, mimetype=mime)


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
        open_browser()
        _settings = load_settings()
        app.run(host='0.0.0.0', port=int(_settings.get('port', 7766)), debug=False)
    except Exception as e:
        app.logger.error(f"Server failed to start: {e}")
        raise
