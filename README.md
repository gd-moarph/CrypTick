# CrypTick

Lightweight always-on-top crypto price ticker for Windows.

## Features
- Profiles per monitor, hotkey profile cycling
- Multi-network GeckoTerminal support
- Logos, custom names, separators, bold styling
- Click-through top bar, real-time scrolling
- Auto refresh every 30s
- Per-user state: `%APPDATA%\CrypTick`

## Install
Download the latest **CrypTick-Setup.exe** from Releases and run it.

## Build (dev)
```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python .\app.py
