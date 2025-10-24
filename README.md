# CrypTick

Lightweight always-on-top crypto price ticker for Windows.

## Features
- Profiles per monitor, hotkey profile cycling
- Multi-network GeckoTerminal support
- Logos, custom names, separators, bold styling
- Click-through top bar, real-time scrolling
- Auto refresh every 30s
- Per-user state: `%APPDATA%\CrypTick`

<img width="1226" height="877" alt="image" src="https://github.com/user-attachments/assets/ef912c80-71dd-4ff8-989e-548c59184820" />

Top Bar:
<img width="2551" height="52" alt="image" src="https://github.com/user-attachments/assets/0baadc7a-b165-4c85-8277-bdd87dec4a86" />

Always-On-Top:
<img width="2559" height="1430" alt="image" src="https://github.com/user-attachments/assets/fd134270-f0cd-48f1-b818-5541b3225e26" />

## Install
Download the latest **CrypTick-Setup.exe** from Releases and run it.

## Build (dev)
```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python .\app.py

