# Skydimo LED Controller
Cross-platform LED backlight controller for Skydimo devices (CH340 serial), with:
- Python web UI server (FastAPI)
- Optional web variant (`skydimo_web.py`)

## Features
- Auto-detect CH340 serial port
- Static color and animated modes: `solid`, `rainbow`, `breathe`, `wave`, `chase`, `twinkle`
- Brightness and speed control
- Mobile-friendly web interface

## Repository Layout
- `skydimo_server.py` - FastAPI web server (main entrypoint)
- `skydimo_web.py` - equivalent web server variant
- `README.md` - setup and usage guide

## Requirements
- Python 3.10+
- USB CH340 driver installed
- Skydimo connected via USB

## Setup (Step by Step)

### Step 1: Install Python
If Python is not installed yet:  
https://www.python.org/downloads/

During setup, enable **"Add Python to PATH"**.

### Step 2: Install CH340 Driver
Skydimo uses the CH340 USB serial chip.

1. Open **Device Manager** (`Win+X` -> Device Manager).
2. Connect the LED strip over USB.
3. Check **Ports (COM & LPT)** for an entry like `USB-SERIAL CH340 (COM3)`.
4. If it appears as an unknown device, install the driver:  
   https://www.wch-ic.com/downloads/CH341SER_EXE.html
5. Remember the detected port, for example `COM3`.

### Step 3: Install Dependencies
```bash
pip install pyserial fastapi uvicorn
```

## Run Web UI
Start server:
```bash
python skydimo_server.py --inches 34
```

`--inches` is required and selects LED count for a 3-side layout (left, top, right).

On startup, the script asks:
`Run in background and close this console? [y/N]:`

- `y`/`yes`/`да`: starts detached in background and closes current console.
- `N`/Enter: keeps running in the current console (as before).

Stop background server:
```bash
python skydimo_server.py --stop
```

Open in browser:
- Local: `http://localhost:8000`
- LAN (phone/tablet): `http://<your-pc-ip>:8000`

Notes:
- Server listens on `0.0.0.0` for LAN access.
- If you use a hostname instead of IP, ensure local DNS/mDNS resolves it.

## API Endpoints
- `GET /` - web UI
- `GET /api/ports` - list serial ports
- `POST /api/apply` - apply mode/color/brightness/speed
- `POST /api/off` - switch LEDs off
- `GET /api/state` - current controller state

## Modes
- `solid`
- `rainbow`
- `breathe`
- `wave`
- `chase`
- `twinkle`
- `off`

## Troubleshooting
- **Port busy / access denied**: close other apps that may lock the same COM port.
- **Device not detected**: verify CH340 driver and USB cable.
- **No LAN access**: allow Python in firewall and confirm same Wi-Fi subnet.

## Protocol Notes
Skydimo packet format used by this project:
```text
b'Ada' + 0x00 + 0x00 + N_LEDS + [R,G,B] * N_LEDS
```
- Model 34": `N_LEDS = 71`
- Serial speed: `115200`