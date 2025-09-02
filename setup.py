from flask import Flask, request, jsonify, Response, render_template_string, redirect
# -------------------------------
# Minimal Web UI
# -------------------------------

@app.route("/")
def root_redirect():
    return redirect("/ui", code=302)

@app.route("/ui")
def web_ui():
    """Serve a minimal, nice-looking control page for Apple Music & AirPlay."""
    return render_template_string(
        r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Apple Music Controller</title>
<style>
  :root { --bg:#0b1116; --panel:#111821; --muted:#7b8a9a; --text:#e6edf3; --accent:#4c8bf5; --good:#3fb950; --warn:#f0883e; }
  *{box-sizing:border-box}
  body{margin:0;padding:24px;background:var(--bg);color:var(--text);font:16px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
  h1{font-size:22px;margin:0 0 16px}
  .grid{display:grid;gap:16px}
  .cols{grid-template-columns: 1.2fr 1fr}
  .card{background:var(--panel);border:1px solid #1f2937;border-radius:12px;padding:16px}
  .row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
  .btn{appearance:none;border:1px solid #263244;border-radius:10px;background:#0f1520;color:var(--text);padding:10px 14px;cursor:pointer}
  .btn:hover{border-color:#2f3d52}
  .btn-primary{background:var(--accent);border-color:var(--accent);color:#fff}
  .btn-icon{width:40px;height:40px;display:inline-flex;align-items:center;justify-content:center}
  .muted{color:var(--muted)}
  .title{font-weight:600}
  .now{display:flex;gap:16px}
  .art{width:128px;height:128px;border-radius:10px;object-fit:cover;background:#1d2633}
  .kv{margin:2px 0}
  .kv .label{display:inline-block;width:70px;color:var(--muted)}
  .slider{width:100%}
  .devs{display:flex;flex-direction:column;gap:10px;max-height:380px;overflow:auto;padding-right:4px}
  .dev{display:flex;align-items:center;gap:12px;justify-content:space-between;border:1px solid #213047;border-radius:10px;padding:10px}
  .dev .left{display:flex;align-items:center;gap:10px}
  .dev input[type=checkbox]{width:18px;height:18px}
  .dev .name{min-width:160px}
  .footer{display:flex;justify-content:space-between;align-items:center;margin-top:10px}
  .chip{font-size:12px;padding:2px 8px;border:1px solid #2a3a54;border-radius:999px;color:var(--muted)}
</style>
</head>
<body>
  <h1>Apple Music Controller</h1>
  <div class="grid cols">
    <div class="card">
      <div class="now">
        <img id="art" class="art" src="/artwork" alt="artwork" onerror="this.src='data:image/svg+xml;utf8,<svg xmlns=\'http://www.w3.org/2000/svg\' viewBox=\'0 0 128 128\'><rect width=\'128\' height=\'128\' fill=\'%231d2633\'/><text x=\'50%\' y=\'55%\' dominant-baseline=\'middle\' text-anchor=\'middle\' font-size=\'14\' fill=\'%237b8a9a\'>No Art</text></svg>'">
        <div>
          <div class="kv"><span class="label">Track:</span> <span id="trk" class="title">—</span></div>
          <div class="kv"><span class="label">Artist:</span> <span id="artst">—</span></div>
          <div class="kv"><span class="label">Album:</span> <span id="albm">—</span></div>
          <div class="kv muted" id="state">—</div>
          <div class="row" style="margin-top:12px">
            <button class="btn btn-icon" title="Previous" onclick="call('/previous','POST')">⏮️</button>
            <button id="pp" class="btn btn-primary" onclick="playPause()">Play</button>
            <button class="btn btn-icon" title="Next" onclick="call('/next','POST')">⏭️</button>
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
        <button class="btn" onclick="applyDevices()">Apply Selection</button>
      </div>
      <div id="devs" class="devs"></div>
    </div>
  </div>

<script>
const $ = sel => document.querySelector(sel);
const devBox = $('#devs');
let deviceVolumes = {}; // name -> 0..100
let selected = new Set(); // UI selection only; backend selection is applied on demand

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
    $('#pp').textContent = (st==='Playing')? 'Pause' : 'Play';
    // bump artwork (cache-bust)
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

async function loadDevices(){
  try{
    const names = await (await fetch('/devices')).json();
    deviceVolumes = await (await fetch('/device_volumes')).json();
    devBox.innerHTML = '';
    (names||[]).forEach(name => {
      const vol = deviceVolumes?.[name] ?? 0;
      const row = document.createElement('div'); row.className='dev';
      row.innerHTML = `
        <div class='left'>
          <input type='checkbox' data-name="${name}">
          <div class='name'>${name}</div>
        </div>
        <div style='flex:1;display:flex;align-items:center;gap:10px'>
          <input type='range' min='0' max='100' step='1' value='${vol}' data-vol='${name}' style='width:100%'>
          <span class='chip' id='v-${cssId(name)}'>${vol}%</span>
        </div>`;
      devBox.appendChild(row);
      row.querySelector('input[type=checkbox]').addEventListener('change', (e)=>{
        const n=e.target.getAttribute('data-name');
        if(e.target.checked) selected.add(n); else selected.delete(n);
      });
      row.querySelector('input[type=range]').addEventListener('input', (e)=>{
        const n=e.target.getAttribute('data-vol'); const v=parseInt(e.target.value)||0;
        document.getElementById('v-'+cssId(n)).textContent = v+'%';
        debounceDevice(n, v);
      });
    });
  }catch(e){ console.warn('devices', e); }
}

function cssId(s){ return s.replace(/[^a-z0-9]+/gi,'-'); }

const devTimers = new Map();
function debounceDevice(name, v){
  if(devTimers.has(name)) clearTimeout(devTimers.get(name));
  devTimers.set(name, setTimeout(()=>setDeviceVolume(name,v), 150));
}
async function setDeviceVolume(name, level){
  try{ await call('/set_device_volume','POST',{device:name, level:level}); }
  catch(e){ console.warn('dev vol', name, e); }
}

async function applyDevices(){
  try{ await call('/set_devices','POST',{devices: Array.from(selected).join(',')}); }
  catch(e){ alert('Failed to apply devices: '+e); }
}

async function playPause(){
  try{ await call('/playpause','POST'); await loadNow(); }
  catch(e){ console.warn('playpause', e); }
}

// initial load + polling
loadNow(); loadMaster(); loadDevices();
setInterval(loadNow, 2000);
</script>
</body></html>
        '''
    )

# --- Minimal API for UI (master volume + play/pause) ---

@app.route('/master_volume', methods=['GET', 'POST'])
def master_volume():
    """GET: return 0..100; POST: set master volume."""
    if request.method == 'GET':
        script = '''
        tell application "Music"
            try
                return sound volume as integer
            on error
                return 0
            end try
        end tell
        '''
        result = run_applescript(script)
        try:
            return str(int(result)) if isinstance(result, (int, str)) else '0'
        except Exception:
            return '0'
    # POST
    data = request.get_json(force=True, silent=True) or {}
    level = int(data.get('level', 0))
    level = max(0, min(100, level))
    script = f'''
    tell application "Music"
        try
            set sound volume to {level}
            return sound volume as integer
        on error
            return 0
        end try
    end tell
    '''
    result = run_applescript(script)
    try:
        return jsonify({"ok": True, "level": int(result)})
    except Exception:
        return jsonify({"ok": False}), 500

@app.route('/playpause', methods=['POST'])
def playpause():
    """Toggle playback in Apple Music."""
    script = '''
    tell application "Music"
        try
            playpause
            return "ok"
        on error
            return "err"
        end try
    end tell
    '''
    result = run_applescript(script)
    if isinstance(result, str) and result.strip() == 'ok':
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 500