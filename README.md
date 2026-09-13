# 🔥 HeatSync — Hive + Kasa Heating Coordinator

A local web app that links TP-Link Kasa KE100 radiator valves to a Hive boiler.
When any valve is calling for heat, it automatically activates a Hive boost to fire
the boiler — without touching your Hive schedule.

---

## Requirements

- Windows 10 or 11
- Python 3.9+ — download from https://www.python.org/downloads/
  - ✅ Tick **"Add Python to PATH"** during installation
- [Npcap](https://npcap.com/#download) — required for Kasa local network discovery on Windows
  - Install with default options
- All devices on the same local network/Wi-Fi as the PC

---

## Setup

Open **Command Prompt** or **PowerShell** and run:

```bat
:: 1. Get the code and enter the project folder
::    (clone rather than downloading a ZIP — this enables in-app updates)
git clone https://github.com/lukegardner43/HeatSync.git
cd HeatSync

:: 2. Create a virtual environment
python -m venv venv

:: 3. Activate it
venv\Scripts\activate

:: 4. Install dependencies
pip install -r requirements.txt

:: 5. Run the server
python app.py
```

Open your browser at **http://localhost:5000**

To stop the server press `Ctrl+C` in the terminal.

---

## Usage

### 1. Connect Hive
Enter your Hive account email and password in the **Hive Connection** panel and click Connect.

### 2. Discover Kasa Devices
Click **Discover Devices** to scan your local network for KE100 radiator valves.
Make sure Npcap is installed, otherwise discovery will fail silently on Windows.

### 3. Auto Mode
Once both are connected, enable **Auto Mode** in Settings. The app will:
- Poll all Kasa valves every 60 seconds (configurable)
- If **any valve** is more than 0.5°C below its target → activate a Hive boost
- When **all valves** are satisfied → cancel the boost, Hive returns to its schedule

---

## Coordination Settings

| Setting | Default | Description |
|---|---|---|
| Poll Interval | 60s | How often to check valve temperatures |
| Hysteresis | 0.5°C | How far below target before calling for heat |
| Boost Temp | 22°C | Temperature passed to Hive boost (keeps boiler firing) |
| Boost Duration | 60 min | Boost window — refreshed every poll cycle while valves need heat |
| Season | Winter | Winter coordinates the boiler as normal; Summer pauses it entirely |

### Summer / Winter mode

The **Summer Mode** toggle in Settings pauses boiler coordination without disconnecting anything:

- **Winter** — normal operation, valve demand fires the Hive boost.
- **Summer** — HeatSync never calls for heat. Hive and Kasa stay connected, valve
  temperatures are still readable and settable, and any boost still held when you
  switch is cancelled on the next poll so Hive returns to its schedule.

The current season is shown as a badge in the header, and the setting is saved to
`config.json` — so it survives a restart and the Windows startup task won't quietly
revert you to Winter in mid-July.

---

## Updates

**Settings → Software** shows the running version and checks GitHub for a newer one.
If an update is available, **Install update** fast-forwards this folder to the latest
commit on `main`, then **Restart now** relaunches the app to apply it.

Nothing happens on its own — HeatSync never downloads or installs anything unless you
click it. The update is deliberately restrictive:

- Only ever fast-forwards to a commit already on this repo's `main` — it cannot merge,
  rebase, or pull from anywhere else.
- Refuses if you have local edits to the files, so your changes are never clobbered.
- Refuses if your copy has diverged from GitHub, leaving you to resolve it manually.
- Cancels any active boost before restarting, so the boiler falls back to your Hive
  schedule rather than firing unattended across the restart.

This requires HeatSync to have been installed with `git clone`. If you downloaded a ZIP,
the version check still works but updating must be done by re-downloading the files.

---

## Run automatically on Windows startup

The easiest method is Task Scheduler:

1. Create a file called `start_heatsync.bat` in the heating-app folder:

```bat
@echo off
cd /d C:\path\to\heating-app
call venv\Scripts\activate
python app.py
```

2. Open **Task Scheduler** (search in Start menu)
3. Click **Create Basic Task…**
4. Name it `HeatSync`
5. Trigger: **When the computer starts**
6. Action: **Start a program** → browse to `start_heatsync.bat`
7. ✅ Tick **"Run whether user is logged on or not"**
8. ✅ Tick **"Run with highest privileges"**
9. Click Finish

HeatSync will now start silently in the background on every boot.
Access the dashboard at **http://localhost:5000** from any browser on the network.

---

## Firewall note

If you want to access the dashboard from another device (phone, tablet etc.),
Windows Firewall may block incoming connections on port 5000.
To allow it, run this once in an **Administrator** PowerShell:

```powershell
netsh advfirewall firewall add rule name="HeatSync" dir=in action=allow protocol=TCP localport=5000
```

Then access it via your PC's local IP address, e.g. `http://192.168.1.x:5000`

---

## Notes

- **pyhiveapi** uses Hive's unofficial cloud API — your schedule is never modified.
  Boost expires naturally if the app stops, so Hive always falls back to schedule.
- **python-kasa** communicates directly with Kasa devices over your local network —
  no cloud dependency, very reliable.
- Npcap is needed on Windows because python-kasa uses raw UDP broadcast for discovery.
  Without it you'll see a `PermissionError` or empty discovery results.
