#!/usr/bin/env python3
"""
Skydimo 34" LED Controller — FastAPI Web Server
================================================
pip install fastapi uvicorn pyserial
python skydimo_server.py

PC browser : http://localhost:8000
Phone      : http://<your-pc-ip>:8000  (same Wi-Fi)
"""

import argparse
import atexit
import math
import os
import random
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import serial
import serial.tools.list_ports
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from zeroconf import Zeroconf, ServiceInfo

# ── Constants ─────────────────────────────────────────────────────────────────
N_LEDS    = 71
BAUD_RATE = 115200
FPS_DELAY = 0.04   # 25 fps — device turns off without continuous frames

# 3-side layout (left, top, right) LED counts by monitor size.
LED_COUNT_3SIDE_BY_INCHES = {
    34: 71,
}
PID_FILE = Path(__file__).with_name('skydimo_server.pid')
MDNS_HOSTNAME = 'skydimo.local.'

# ── Shared state ──────────────────────────────────────────────────────────────
_state_lock = threading.Lock()
state: dict = {
    "mode":       "off",
    "color":      [255, 100, 0],
    "brightness": 180,
    "speed":      5.0,
    "port":       None,
    "connected":  False,
}

_ser_lock = threading.Lock()
_ser: serial.Serial | None = None


def write_pid_file() -> None:
    PID_FILE.write_text(str(os.getpid()), encoding='ascii')


def remove_pid_file() -> None:
    try:
        if PID_FILE.exists():
            PID_FILE.unlink()
    except Exception:
        pass


def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        s.close()


def start_mdns(ip: str, port: int) -> Zeroconf | None:
    """Advertise this server as skydimo.local via mDNS (Bonjour/Avahi)."""
    try:
        zc = Zeroconf()
        info = ServiceInfo(
            "_http._tcp.local.",
            "Skydimo LED._http._tcp.local.",
            addresses=[socket.inet_aton(ip)],
            port=port,
            server=MDNS_HOSTNAME,
        )
        zc.register_service(info)
        return zc
    except Exception:
        return None


def stop_from_pid_file() -> bool:
    if not PID_FILE.exists():
        print(f'No running background server found ({PID_FILE.name} is missing).')
        return False

    try:
        pid = int(PID_FILE.read_text(encoding='ascii').strip())
    except Exception:
        print(f'Cannot read PID from {PID_FILE.name}. Remove file and try again.')
        return False

    try:
        subprocess.run(
            ['taskkill', '/PID', str(pid), '/T', '/F'],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        print(f'Failed to stop process PID {pid}. It may already be stopped.')
        return False

    remove_pid_file()
    print(f'Stopped background server (PID {pid}).')
    return True


# ── Skydimo protocol ──────────────────────────────────────────────────────────
# Header: 'A' 'd' 'a' 0x00 0x00 N_LEDS  (differs from standard Adalight)
# Source: hyperion-project/hyperion.ng LedDeviceSkydimo.cpp (PR #1800)
def make_packet(leds: list[tuple[int, int, int]]) -> bytes:
    hdr = bytearray([ord('A'), ord('d'), ord('a'), 0x00, 0x00, N_LEDS & 0xFF])
    body = bytearray()
    for r, g, b in leds:
        body += bytearray([r & 0xFF, g & 0xFF, b & 0xFF])
    return bytes(hdr + body)

def solid_packet(r: int, g: int, b: int, bri: int) -> bytes:
    f = bri / 255.0
    return make_packet([(int(r * f), int(g * f), int(b * f))] * N_LEDS)

def black_packet() -> bytes:
    return make_packet([(0, 0, 0)] * N_LEDS)


# ── Effects ───────────────────────────────────────────────────────────────────
def hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    h %= 360
    c = v * s; x = c * (1 - abs((h / 60) % 2 - 1)); m = v - c
    if   h < 60:  r, g, b = c, x, 0
    elif h < 120: r, g, b = x, c, 0
    elif h < 180: r, g, b = 0, c, x
    elif h < 240: r, g, b = 0, x, c
    elif h < 300: r, g, b = x, 0, c
    else:          r, g, b = c, 0, x
    return int((r + m) * 255), int((g + m) * 255), int((b + m) * 255)

def frame_rainbow(off: float, bri: int) -> bytes:
    v = bri / 255.0
    return make_packet([hsv_to_rgb((off + i * 360 / N_LEDS) % 360, 1.0, v)
                        for i in range(N_LEDS)])

def frame_breathe(r, g, b, t: float, spd: float) -> bytes:
    br = (math.sin(t * spd * 0.4) + 1) / 2
    return make_packet([(int(r * br), int(g * br), int(b * br))] * N_LEDS)

def frame_wave(r, g, b, t: float, spd: float, bri: int) -> bytes:
    f = bri / 255.0
    leds = []
    for i in range(N_LEDS):
        v = (math.sin((i / N_LEDS) * 6.28318 - t * spd * 0.3) + 1) / 2
        leds.append((int(r * v * f), int(g * v * f), int(b * v * f)))
    return make_packet(leds)

def frame_chase(r, g, b, off: float, bri: int) -> bytes:
    f = bri / 255.0
    pos = int(off) % N_LEDS
    leds = []
    for i in range(N_LEDS):
        d = min(abs(i - pos), N_LEDS - abs(i - pos))
        if   d == 0: leds.append((int(r * f),       int(g * f),       int(b * f)))
        elif d == 1: leds.append((int(r * f * 0.3), int(g * f * 0.3), int(b * f * 0.3)))
        else:        leds.append((0, 0, 0))
    return make_packet(leds)

def frame_twinkle(r, g, b, prev: list) -> tuple[bytes, list]:
    leds = []
    for i in range(N_LEDS):
        if random.random() < 0.05:
            v = random.uniform(0.5, 1.0)
            leds.append((int(r * v), int(g * v), int(b * v)))
        elif random.random() < 0.05:
            leds.append((0, 0, 0))
        else:
            leds.append(prev[i])
    return make_packet(leds), leds


# ── Port detection ────────────────────────────────────────────────────────────
def find_port() -> str | None:
    for p in serial.tools.list_ports.comports():
        hwid = (p.hwid or '').upper()
        desc = (p.description or '').upper()
        vid  = getattr(p, 'vid', None)
        if vid == 0x1A86 or '1A86' in hwid or 'CH340' in desc or 'CH341' in desc:
            return p.device
    return None

def list_ports() -> list[dict]:
    return [{'device': p.device, 'description': p.description or ''}
            for p in serial.tools.list_ports.comports()]


# ── Serial helpers ────────────────────────────────────────────────────────────
def _send(pkt: bytes) -> bool:
    global _ser
    with _ser_lock:
        if _ser is None or not _ser.is_open:
            return False
        try:
            _ser.write(pkt)
            return True
        except Exception:
            try: _ser.close()
            except: pass
            _ser = None
            return False


# ── Animation loop (background thread) ───────────────────────────────────────
def animation_loop() -> None:
    global _ser
    offset    = 0.0
    t         = 0.0
    prev      = [(0, 0, 0)] * N_LEDS
    last_mode = None

    while True:
        with _state_lock:
            mode = state['mode']
            r, g, b = state['color']
            bri  = state['brightness']
            spd  = state['speed']
            port = state['port']

        if mode != last_mode:
            offset = 0.0
            t      = 0.0
            last_mode = mode

        # ── off ───────────────────────────────────────────────────────────────
        if mode == 'off':
            with _ser_lock:
                if _ser is not None:
                    try: _ser.write(black_packet())
                    except: pass
                    try: _ser.close()
                    except: pass
                    _ser = None
            with _state_lock:
                state['connected'] = False
            time.sleep(0.2)
            continue

        # ── ensure connection ─────────────────────────────────────────────────
        with _ser_lock:
            already_open = _ser is not None and _ser.is_open
        if not already_open:
            target = port or find_port()
            if not target:
                with _state_lock: state['connected'] = False
                time.sleep(1)
                continue
            try:
                new_ser = serial.Serial(target, BAUD_RATE, timeout=1)
                time.sleep(0.1)
                with _ser_lock: _ser = new_ser
                with _state_lock:
                    state['port']      = target
                    state['connected'] = True
            except Exception:
                with _state_lock: state['connected'] = False
                time.sleep(1)
                continue

        # ── generate frame ────────────────────────────────────────────────────
        pkt: bytes | None = None
        if   mode == 'solid':   pkt = solid_packet(r, g, b, bri)   # always send — keeps device alive
        elif mode == 'rainbow': pkt = frame_rainbow(offset, bri)
        elif mode == 'breathe': pkt = frame_breathe(r, g, b, t, spd)
        elif mode == 'wave':    pkt = frame_wave(r, g, b, t, spd, bri)
        elif mode == 'chase':   pkt = frame_chase(r, g, b, offset, bri)
        elif mode == 'twinkle': pkt, prev = frame_twinkle(r, g, b, prev)

        if pkt:
            ok = _send(pkt)
            with _state_lock:
                state['connected'] = ok

        offset = (offset + spd * 2) % 360
        t += 0.05
        time.sleep(FPS_DELAY)


# ── HTML (mobile-first UI) ────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0D0B14">
<title>Skydimo LED</title>
<style>
:root{
  --bg:#0D0B14; --card:#110F20; --border:#1A1830;
  --accent:#7C6FFF; --accent-dim:#1C1840;
  --text:#E2E2E2; --muted:#5A5870;
  --green:#4ADE80; --red:#F87171;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{
  background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI Variable','Segoe UI',sans-serif;
  font-size:15px;min-height:100vh;
  padding-bottom:calc(16px + env(safe-area-inset-bottom));
}
.page{max-width:500px;margin:0 auto;padding:20px 16px 24px}

/* Header */
.hdr{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:18px}
.hdr h1{font-size:22px;font-weight:700;color:#EEEEFF;line-height:1.2}
.hdr p{font-size:12px;color:var(--muted);margin-top:3px}
.status{display:flex;align-items:center;gap:7px;padding-top:4px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--muted);flex-shrink:0;transition:background .3s,box-shadow .3s}
.dot.on{background:var(--green);box-shadow:0 0 8px rgba(74,222,128,.5)}
.dot.err{background:var(--red)}
.stxt{font-size:12px;color:var(--muted);white-space:nowrap}

/* Port row */
.port-row{display:flex;gap:8px;margin-bottom:12px}
select{
  flex:1;background:var(--card);border:1px solid var(--border);
  border-radius:10px;color:var(--text);padding:11px 12px;
  font-size:13px;-webkit-appearance:none;appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%235A5870' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 12px center;
  padding-right:32px;
}
.rbtn{
  background:var(--card);border:1px solid var(--border);border-radius:10px;
  color:var(--muted);padding:11px 15px;font-size:18px;cursor:pointer;
  transition:color .15s
}
.rbtn:active{color:var(--text)}

/* Preview bar */
.preview{
  height:52px;border-radius:14px;margin-bottom:12px;
  background:#180C00;transition:background .3s,box-shadow .3s;
}

/* Card */
.card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:16px;margin-bottom:10px}
.lbl{font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;
     letter-spacing:.8px;margin-bottom:14px;display:flex;justify-content:space-between;align-items:center}
.lbl span{font-family:monospace;font-size:13px;letter-spacing:0;font-weight:500;color:var(--muted)}

/* Swatches */
.swatches{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;margin-bottom:14px}
.sw{aspect-ratio:1;border-radius:50%;cursor:pointer;border:2.5px solid transparent;
    transition:transform .1s,border-color .1s}
.sw:active{transform:scale(1.18)}
.sw.on{border-color:#fff}

/* Custom color */
.color-row{display:flex;align-items:center;gap:12px}
input[type=color]{width:48px;height:42px;border:none;border-radius:8px;cursor:pointer;
                  background:none;padding:0;flex-shrink:0}
.hex{font-family:monospace;font-size:13px;color:var(--muted)}

/* Range slider */
input[type=range]{
  width:100%;height:6px;border-radius:3px;-webkit-appearance:none;appearance:none;
  background:var(--border);cursor:pointer;outline:none;
}
input[type=range]::-webkit-slider-thumb{
  -webkit-appearance:none;width:24px;height:24px;border-radius:50%;
  background:var(--accent);box-shadow:0 0 10px rgba(124,111,255,.6);cursor:pointer;
}
input[type=range]::-moz-range-thumb{
  width:22px;height:22px;border-radius:50%;border:none;
  background:var(--accent);box-shadow:0 0 10px rgba(124,111,255,.6);cursor:pointer;
}

/* Mode pills */
.modes{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.mb{
  background:#0D0B1A;border:1.5px solid var(--border);border-radius:24px;
  padding:12px 4px;font-size:13px;font-weight:600;color:var(--muted);
  cursor:pointer;text-align:center;transition:all .12s;user-select:none;
}
.mb:active{opacity:.7}
.mb.on{border-color:var(--accent);background:var(--accent-dim);color:#A09AFF}

/* Buttons */
.actions{display:flex;gap:10px;margin-top:10px}
.btn{flex:1;height:54px;border-radius:14px;border:none;font-size:15px;
     font-weight:700;cursor:pointer;transition:opacity .12s;display:flex;
     align-items:center;justify-content:center;gap:8px}
.btn:active{opacity:.72}
.apply{
  background:linear-gradient(135deg,#7C6FFF 0%,#9B6FFF 100%);
  color:#fff;box-shadow:0 4px 24px rgba(124,111,255,.35);
}
.off{background:#130D0D;color:var(--red);border:1.5px solid #2A1515;flex:0 0 120px}

#speedCard{display:none}
</style>
</head>
<body>
<div class="page">

  <div class="hdr">
    <div><h1>Skydimo LED</h1><p>Backlight Controller</p></div>
    <div class="status">
      <div class="dot" id="dot"></div>
      <span class="stxt" id="stxt">searching...</span>
    </div>
  </div>

  <div class="port-row">
    <select id="portSel"></select>
    <button class="rbtn" onclick="loadPorts()" title="Refresh ports">&#8635;</button>
  </div>

  <div class="preview" id="preview"></div>

  <!-- Color -->
  <div class="card">
    <div class="lbl">Color</div>
    <div class="swatches" id="swatches"></div>
    <div class="color-row">
      <input type="color" id="cp" value="#ff6400">
      <span class="hex" id="hx">#ff6400</span>
    </div>
  </div>

  <!-- Brightness -->
  <div class="card">
    <div class="lbl">Brightness <span id="briV">180</span></div>
    <input type="range" min="0" max="255" value="180" id="bri">
  </div>

  <!-- Mode -->
  <div class="card">
    <div class="lbl">Mode</div>
    <div class="modes" id="modes"></div>
  </div>

  <!-- Speed (hidden for static) -->
  <div class="card" id="speedCard">
    <div class="lbl">Speed <span id="spdV">5.0</span></div>
    <input type="range" min="1" max="10" step="0.5" value="5" id="spd">
  </div>

  <!-- Actions -->
  <div class="actions">
    <button class="btn apply" onclick="applyAll()">&#9654; Apply</button>
    <button class="btn off"   onclick="turnOff()">&#9724; Off</button>
  </div>

</div>
<script>
const SW=['#ff0000','#ff6400','#ffff00','#00ff00','#00e5ff','#0055ff',
          '#8800ff','#ff00ff','#ffffff','#ffd0a0','#c0d4ff','#000000'];
const MODES=[
  {id:'solid',   label:'Static',  spd:false},
  {id:'rainbow', label:'Rainbow', spd:true},
  {id:'breathe', label:'Breathe', spd:true},
  {id:'wave',    label:'Wave',    spd:true},
  {id:'chase',   label:'Chase',   spd:true},
  {id:'twinkle', label:'Twinkle', spd:true},
];
let mode='solid';

// Build swatches
const sg=document.getElementById('swatches');
SW.forEach(c=>{
  const d=document.createElement('div');
  d.className='sw';d.style.background=c;
  d.onclick=()=>setC(c);
  sg.appendChild(d);
});

// Build mode buttons
const mg=document.getElementById('modes');
MODES.forEach(m=>{
  const b=document.createElement('div');
  b.className='mb'+(m.id==='solid'?' on':'');
  b.id='m'+m.id;b.textContent=m.label;
  b.onclick=()=>{
    mode=m.id;
    document.querySelectorAll('.mb').forEach(x=>x.classList.remove('on'));
    b.classList.add('on');
    document.getElementById('speedCard').style.display=m.spd?'block':'none';
  };
  mg.appendChild(b);
});

function h2r(h){return[parseInt(h.slice(1,3),16),parseInt(h.slice(3,5),16),parseInt(h.slice(5,7),16)];}

function setC(hex){
  document.getElementById('cp').value=hex;
  document.getElementById('hx').textContent=hex;
  document.querySelectorAll('.sw').forEach((s,i)=>s.classList.toggle('on',SW[i]===hex));
  upPrev();
}

function upPrev(){
  const[r,g,b]=h2r(document.getElementById('cp').value);
  const f=+document.getElementById('bri').value/255;
  const rc=Math.round(r*f),gc=Math.round(g*f),bc=Math.round(b*f);
  const p=document.getElementById('preview');
  p.style.background='rgb('+rc+','+gc+','+bc+')';
  p.style.boxShadow='0 0 28px rgba('+rc+','+gc+','+bc+',.3)';
}

document.getElementById('cp').oninput=e=>{
  document.getElementById('hx').textContent=e.target.value;
  document.querySelectorAll('.sw').forEach(s=>s.classList.remove('on'));
  upPrev();
};
document.getElementById('bri').oninput=e=>{
  document.getElementById('briV').textContent=e.target.value;upPrev();
};
document.getElementById('spd').oninput=e=>
  document.getElementById('spdV').textContent=(+e.target.value).toFixed(1);

async function applyAll(){
  const[r,g,b]=h2r(document.getElementById('cp').value);
  const port=document.getElementById('portSel').value||null;
  await fetch('/api/apply',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
      mode,color:[r,g,b],
      brightness:+document.getElementById('bri').value,
      speed:+document.getElementById('spd').value,
      ...(port?{port}:{})
    })
  });
}

async function turnOff(){await fetch('/api/off',{method:'POST'});}

async function loadPorts(){
  try{
    const d=await(await fetch('/api/ports')).json();
    const sel=document.getElementById('portSel');
    const prev=sel.value;
    sel.innerHTML='<option value="">auto-detect</option>';
    d.ports.forEach(p=>{
      const o=document.createElement('option');
      o.value=p.device;
      o.textContent=p.device+(p.description?' — '+p.description:'');
      sel.appendChild(o);
    });
    if(d.detected)sel.value=d.detected;
    else if(prev)sel.value=prev;
  }catch(e){}
}

async function pollState(){
  try{
    const s=await(await fetch('/api/state')).json();
    const dot=document.getElementById('dot'),lbl=document.getElementById('stxt');
    if(s.mode==='off'){dot.className='dot';lbl.textContent='off';}
    else if(s.connected){dot.className='dot on';lbl.textContent=s.port||'connected';}
    else{dot.className='dot err';lbl.textContent='not connected';}
  }catch(e){}
}

upPrev();setC('#ff6400');
loadPorts();
setInterval(pollState,2000);
</script>
</body>
</html>"""


# ── FastAPI app ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=animation_loop, daemon=True).start()
    yield

app = FastAPI(lifespan=lifespan)


class ApplyRequest(BaseModel):
    mode:       str | None       = None
    color:      list[int] | None = None
    brightness: int | None       = None
    speed:      float | None     = None
    port:       str | None       = None


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML

@app.get("/api/ports")
async def api_ports():
    return {"ports": list_ports(), "detected": find_port()}

@app.post("/api/apply")
async def api_apply(req: ApplyRequest):
    global _ser
    port_changed = False
    with _state_lock:
        if req.mode is not None:
            state['mode'] = req.mode
        if req.color is not None:
            state['color'] = [max(0, min(255, int(v))) for v in req.color[:3]]
        if req.brightness is not None:
            state['brightness'] = max(0, min(255, req.brightness))
        if req.speed is not None:
            state['speed'] = max(0.1, min(10.0, req.speed))
        if req.port:
            port_changed = req.port != state.get('port')
            state['port'] = req.port
    if port_changed:
        with _ser_lock:
            if _ser is not None:
                try: _ser.close()
                except: pass
                _ser = None
    return {"ok": True}

@app.post("/api/off")
async def api_off():
    with _state_lock:
        state['mode'] = 'off'
    return {"ok": True}

@app.get("/api/state")
async def api_state():
    with _state_lock:
        return dict(state)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Skydimo LED Web Server')
    ap.add_argument(
        '--inches',
        type=int,
        required=False,
        choices=sorted(LED_COUNT_3SIDE_BY_INCHES.keys()),
        help='Monitor size in inches for 3-side layout (left, top, right)',
    )
    ap.add_argument('--port',       type=int, default=8000, help='HTTP port (default: 8000)')
    ap.add_argument('--no-browser', action='store_true',    help='Do not open browser on start')
    ap.add_argument('--background', action='store_true',    help='Run server in background (internal)')
    ap.add_argument('--stop',       action='store_true',    help='Stop background server')
    args = ap.parse_args()

    if args.stop:
        raise SystemExit(0 if stop_from_pid_file() else 1)

    if args.inches is None:
        ap.error('--inches is required unless --stop is used')

    N_LEDS = LED_COUNT_3SIDE_BY_INCHES[args.inches]

    # Keep current foreground behavior, but allow launching a detached background process.
    if not args.background and sys.stdin.isatty():
        answer = input('Run in background and close this console? [y/N]: ').strip().lower()
        if answer in {'y', 'yes', 'д', 'да'}:
            cmd = [
                sys.executable,
                os.path.abspath(__file__),
                '--inches', str(args.inches),
                '--port', str(args.port),
                '--background',
            ]
            if args.no_browser:
                cmd.append('--no-browser')

            flags = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NO_WINDOW
            )
            pid = subprocess.Popen(
                cmd, cwd=os.getcwd(), close_fds=True, creationflags=flags,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ).pid
            PID_FILE.write_text(str(pid), encoding='ascii')
            print(f'Server started in background (PID {pid}).')
            raise SystemExit(0)

    write_pid_file()
    atexit.register(remove_pid_file)

    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f'http://localhost:{args.port}')).start()

    ip = get_local_ip()

    zc = start_mdns(ip, args.port)
    if zc is not None:
        atexit.register(zc.close)

    print(f"Skydimo LED Web Server")
    print(f"  Layout : 3-side (left, top, right)")
    print(f"  Size   : {args.inches}\"")
    print(f"  LEDs   : {N_LEDS}")
    print(f"  Local  : http://localhost:{args.port}")
    print(f"  Network: http://{ip}:{args.port}  (open on phone)")
    if zc is not None:
        print(f"  mDNS   : http://{MDNS_HOSTNAME[:-1]}:{args.port}  (open on phone)")
    print(f"  Stop   : Ctrl+C")

    uvicorn.run(app, host='0.0.0.0', port=args.port, log_level='warning')
