# -*- coding: utf-8 -*-
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

"""
HeatSync — Hive + TP-Link Kasa Heating Coordinator
Flask backend that bridges Kasa radiator valves with a Hive boiler.
"""

from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
import asyncio
import threading
import time
import logging
import json
from datetime import datetime

# --- Optional imports (graceful fallback for development) ---
try:
    from kasa import Discover
    KASA_AVAILABLE = True
except ImportError:
    KASA_AVAILABLE = False
    print("WARNING: python-kasa not installed. Run: pip install python-kasa")

try:
    import pyhiveapi as Hive
    HIVE_AVAILABLE = True
except ImportError:
    HIVE_AVAILABLE = False
    print("WARNING: pyhiveapi not installed. Run: pip install pyhiveapi")

# ──────────────────────────────────────────────
# App Setup
# ──────────────────────────────────────────────

import os as _os
_BASE = _os.path.dirname(_os.path.abspath(__file__))
app = Flask(__name__, template_folder=_os.path.join(_BASE, "templates"))
CORS(app)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

CONFIG_FILE = _os.path.join(_BASE, "config.json")

def _load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        logging.error(f"Failed to save config: {e}")

# ──────────────────────────────────────────────
# Global State
# ──────────────────────────────────────────────

state = {
    "hive": {
        "connected": False,
        "current_temp": None,
        "target_temp": None,
        "mode": None,
        "heating": False,
    },
    "kasa_devices": {},      # alias -> device dict
    "boiler_active": False,
    "last_poll": None,
    "logs": [],
}

settings = {
    "poll_interval": 60,         # seconds between polls
    "hysteresis": 0.5,           # °C below target before calling for heat
    "boost_temp": 20.0,          # temperature Hive boosts to when valves call for heat
    "boost_duration": 60,        # boost duration in minutes (refreshed each poll cycle)
    "auto_mode": True,           # enable/disable coordination
}

# Internal Hive session and Kasa device cache
hive_session = None
kasa_cache = {}       # alias -> kasa device object
kasa_credentials = {"username": None, "password": None, "hub_ip": "192.168.0.178"}  # TP-Link account credentials

# ──────────────────────────────────────────────
# Logging Helper
# ──────────────────────────────────────────────

def log(message, level="info"):
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "message": message,
        "level": level,
    }
    state["logs"].insert(0, entry)
    state["logs"] = state["logs"][:100]
    getattr(logging, level)(message)

# ──────────────────────────────────────────────
# Kasa Integration — Local + Cloud fallback
# Tries local python-kasa first (faster, no cloud issues)
# Falls back to TP-Link cloud API if local fails
# ──────────────────────────────────────────────

import requests
import uuid

try:
    from kasa import Discover, Credentials
    try:
        from kasa import SmartHub
    except ImportError:
        try:
            from kasa.smart import SmartHub
        except ImportError:
            SmartHub = None
    KASA_LOCAL_AVAILABLE = True
except ImportError:
    KASA_LOCAL_AVAILABLE = False
    SmartHub = None

TPLINK_CLOUD_URL = "https://eu-wap.tplinkcloud.com"
_cloud_token = None        # logged-in cloud token
_cloud_devices = {}        # device_id -> cloud device info
_ke100_map = {}            # alias -> {device_id (hub), child_id, alias}
_local_hub = None          # python-kasa hub object for local connection

def _cloud_request(method, params, token=None):
    """Make a TP-Link cloud API request."""
    payload = {
        "method": method,
        "params": params,
    }
    headers = {"Content-Type": "application/json"}
    url = TPLINK_CLOUD_URL
    if token:
        url += f"?token={token}"
    resp = requests.post(url, json=payload, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("error_code", 0) != 0:
        raise RuntimeError(f"Cloud API error {data.get('error_code')}: {data.get('msg', 'unknown')}")
    return data.get("result", {})


def _passthrough(device_id, app_server_url, request_data, token):
    """Send a passthrough command to a device via the cloud."""
    import json
    payload = {
        "method": "passthrough",
        "params": {
            "deviceId": device_id,
            "requestData": json.dumps(request_data),
        }
    }
    headers = {"Content-Type": "application/json"}
    resp = requests.post(
        f"{app_server_url}?token={token}",
        json=payload, headers=headers, timeout=10
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("error_code", 0) != 0:
        raise RuntimeError(f"Passthrough error: {data.get('msg')}")
    import json as _json
    return _json.loads(data["result"]["responseData"])


async def _connect_kasa_local(hub_ip, username, password):
    """Try to connect to KH100 hub locally using python-kasa."""
    global _local_hub, _ke100_map

    log(f"Trying local connection to KH100 at {hub_ip}...")

    creds = Credentials(username, password) if username else None
    hub = None

    # python-kasa 0.10 uses DeviceConfig
    try:
        from kasa import DeviceConfig
        config = DeviceConfig(host=hub_ip, credentials=creds) if creds else DeviceConfig(host=hub_ip)
        from kasa import Device
        hub = await asyncio.wait_for(Device.connect(config=config), timeout=15)
        await hub.update()
        log(f"Local connection succeeded via DeviceConfig+Device.connect!")
    except Exception as e:
        log(f"DeviceConfig method failed: {e}", "error")
        hub = None

    # Fallback: discover_single
    if not hub:
        try:
            kwargs = {"host": hub_ip, "timeout": 10}
            if creds:
                kwargs["credentials"] = creds
            hub = await asyncio.wait_for(Discover.discover_single(**kwargs), timeout=12)
            if hub:
                await hub.update()
                log(f"Local connection succeeded via discover_single!")
        except Exception as e:
            log(f"discover_single failed: {e}", "error")
            hub = None

    if not hub:
        raise RuntimeError(f"Could not connect to {hub_ip} locally")

    _local_hub = hub
    _ke100_map.clear()
    state["kasa_devices"].clear()

    children = getattr(hub, "children", []) or []
    log(f"Hub has {len(children)} child device(s) | model={getattr(hub, 'model', '?')} | type={type(hub).__name__}")

    for child in children:
        alias = getattr(child, "alias", None) or getattr(child, "name", None) or str(child)
        model = getattr(child, "model", "KE100")

        _ke100_map[alias] = {"local": True, "child": child, "alias": alias, "model": model}

        current_temp = None
        target_temp  = None
        modules = getattr(child, "modules", {})

        tc = modules.get("TemperatureControl")
        ts = modules.get("TemperatureSensor")
        if tc:
            target_temp  = getattr(tc, "target_temperature", None)
        if ts:
            current_temp = getattr(ts, "temperature", None)
        if current_temp is None:
            current_temp = getattr(child, "temperature", None)
        if target_temp is None:
            target_temp  = getattr(child, "target_temperature", None)

        state["kasa_devices"][alias] = {
            "alias": alias, "ip": hub_ip, "model": model,
            "current_temp": current_temp, "target_temp": target_temp,
            "heating": False, "online": True,
        }
        log(f"Registered valve: {alias} (current={current_temp}, target={target_temp})")

    if not _ke100_map:
        raise RuntimeError("Hub connected but no KE100 valves found — make sure they are paired in the Kasa app")

    return list(_ke100_map.keys())


def connect_kasa_cloud(username, password):
    """Login to TP-Link cloud and load device list."""
    global _cloud_token, _cloud_devices, _ke100_map

    log(f"Logging into TP-Link cloud (Kasa) as {username}...")
    resp = requests.post(
        "https://eu-wap.tplinkcloud.com",
        json={
            "method": "login",
            "params": {
                "appType": "Kasa_Android",
                "cloudPassword": password,
                "cloudUserName": username,
                "terminalUUID": str(uuid.uuid4()),
            }
        },
        headers={"Content-Type": "application/json"},
        timeout=10
    )
    data = resp.json()
    if data.get("error_code", 0) != 0:
        raise RuntimeError(f"Kasa login failed: {data.get('msg')} (code {data.get('error_code')})")
    _cloud_token = data["result"]["token"]
    log("Kasa cloud login successful")

    # Get device list
    device_list = _cloud_request("getDeviceList", {}, token=_cloud_token)
    devices = device_list.get("deviceList", [])
    _cloud_devices = {d["deviceId"]: d for d in devices}
    log(f"Found {len(devices)} cloud device(s)")

    _ke100_map.clear()
    state["kasa_devices"].clear()

    for device in devices:
        model = device.get("deviceModel", "")
        dtype = device.get("deviceType", "")
        alias = device.get("alias", device["deviceId"])
        app_url = device.get("appServerUrl", "")
        log(f"Device: alias={alias} | model={model} | type={dtype} | url={app_url} | id={device['deviceId'][:8]}...")

        if "KH100" in model.upper() or "HUB" in dtype.upper() or "KASAHUB" in dtype.upper():
            log(f"Found hub: {alias} — loading child valves...")
            _load_hub_children(device)

    if not _ke100_map:
        log("No KE100 valves found. Make sure they are added to the KH100 hub in the Kasa app.", "error")

    return list(_ke100_map.keys())


def _load_hub_children(hub_device):
    """Load KE100 child devices from a KH100 hub via cloud passthrough."""
    import json as _json
    hub_id = hub_device["deviceId"]
    app_url = hub_device.get("appServerUrl") or TPLINK_CLOUD_URL

    log(f"Hub appServerUrl: {app_url}")

    # Try all sensible server URLs and method names with the Kasa token
    all_urls = list(dict.fromkeys(filter(None, [
        app_url,
        "https://eu-wap.tplinkcloud.com",
        "https://wap.tplinkcloud.com",
        "https://use1-wap.tplinkcloud.com",
    ])))

    result = None
    for server_url in all_urls:
        for method_name in ["get_child_device_list", "getChildDeviceList"]:
            try:
                payload = {
                    "method": "passthrough",
                    "params": {
                        "deviceId": hub_id,
                        "requestData": _json.dumps({"method": method_name, "params": {}}),
                    }
                }
                resp = requests.post(
                    f"{server_url}?token={_cloud_token}",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=10
                )
                data = resp.json()
                log(f"Passthrough {method_name} @ {server_url}: error_code={data.get('error_code')} msg={data.get('msg')}")
                if data.get("error_code", 0) == 0:
                    result = _json.loads(data["result"]["responseData"])
                    log(f"Passthrough succeeded!")
                    break
            except Exception as exc:
                log(f"Passthrough error {server_url}/{method_name}: {exc}", "error")
        if result:
            break

    if result is None:
        log("All passthrough attempts failed.", "error")
        log("Hub is showing 'Device is offline' — try unplugging/replugging the KH100, then reconnect.", "error")
        return

    children = result.get("result", {}).get("child_device_list", [])
    log(f"Hub has {len(children)} child device(s)")

    for child in children:
        model = child.get("model", "")
        child_id = child.get("device_id") or child.get("nodeDeviceId", "")
        alias = child.get("nickname") or child.get("alias") or child_id

        try:
            import base64
            decoded = base64.b64decode(alias).decode("utf-8")
            if decoded.isprintable() and len(decoded) > 1:
                alias = decoded
        except Exception:
            pass

        if not alias:
            alias = f"KE100-{child_id[:6]}"

        server_url = hub_device.get("appServerUrl") or TPLINK_CLOUD_URL
        _ke100_map[alias] = {
            "hub_id": hub_id,
            "hub_server_url": server_url,
            "child_id": child_id,
            "alias": alias,
            "model": model,
        }

        current_temp, target_temp = _get_ke100_temps(hub_id, server_url, child_id)

        state["kasa_devices"][alias] = {
            "alias": alias,
            "ip": "cloud",
            "model": model or "KE100",
            "current_temp": current_temp,
            "target_temp": target_temp,
            "heating": False,
            "online": True,
        }
        log(f"Registered valve: {alias} (current={current_temp}, target={target_temp})")


def _get_ke100_temps(hub_id, server_url, child_id):
    """Get current and target temperature for a KE100 valve."""
    try:
        result = _passthrough(hub_id, server_url, {
            "method": "get_device_info",
            "params": {"device_id": child_id}
        }, _cloud_token)
        info = result.get("result", {})
        current = info.get("current_temp")
        target = info.get("target_temp")
        # Temps may be in tenths of a degree on some firmware
        if current and current > 100:
            current = current / 10
        if target and target > 100:
            target = target / 10
        return current, target
    except Exception as exc:
        log(f"Failed to get temps for {child_id}: {exc}", "error")
        return None, None


def update_kasa_devices():
    """Refresh all KE100 valve readings — local or cloud depending on connection type."""
    if not _ke100_map:
        return

    # Check if we're using local connection
    first = next(iter(_ke100_map.values()), {})
    if first.get("local"):
        _run_async(_update_kasa_local())
    elif _cloud_token:
        _update_kasa_cloud()


async def _update_kasa_local():
    """Refresh valve readings via local python-kasa."""
    if not _local_hub:
        return
    try:
        await _local_hub.update()

        children = getattr(_local_hub, "children", []) or []
        child_by_alias = {}
        for child in children:
            alias = getattr(child, "alias", None) or getattr(child, "name", None)
            if alias:
                child_by_alias[alias] = child
                if alias in _ke100_map:
                    _ke100_map[alias]["child"] = child

        for alias, info in _ke100_map.items():
            child = child_by_alias.get(alias) or info.get("child")
            if not child:
                continue
            try:
                current_temp = None
                target_temp  = None
                modules = getattr(child, "modules", {})

                # Read from named modules directly — TemperatureControl has target, TemperatureSensor has current
                tc = modules.get("TemperatureControl")
                ts = modules.get("TemperatureSensor")

                if tc:
                    target_temp = getattr(tc, "target_temperature", None)
                if ts:
                    current_temp = getattr(ts, "temperature", None)

                # Fallbacks
                if current_temp is None:
                    current_temp = getattr(child, "temperature", None)
                if target_temp is None:
                    target_temp = getattr(child, "target_temperature", None)

                dev = state["kasa_devices"].setdefault(alias, {"alias": alias})
                dev["current_temp"] = current_temp
                dev["target_temp"]  = target_temp
                is_heating = (
                    (target_temp - current_temp) > settings["hysteresis"]
                    if current_temp is not None and target_temp is not None else False
                )
                dev["heating"] = is_heating
                dev["online"] = True
                log(f"  {alias}: current={current_temp} target={target_temp} heating={is_heating}")

            except Exception as exc:
                state["kasa_devices"].get(alias, {})["online"] = False
                log(f"Error reading {alias}: {exc}", "error")

    except Exception as exc:
        log(f"Local hub update failed: {exc}", "error")


def _update_kasa_cloud():
    """Refresh valve readings via TP-Link cloud."""
    for alias, info in _ke100_map.items():
        try:
            current, target = _get_ke100_temps(
                info["hub_id"], info["hub_server_url"], info["child_id"]
            )
            dev = state["kasa_devices"].setdefault(alias, {"alias": alias})
            dev["current_temp"] = current
            dev["target_temp"]  = target
            dev["heating"] = (
                (target - current) > settings["hysteresis"]
                if current is not None and target is not None else False
            )
            dev["online"] = True
        except Exception as exc:
            state["kasa_devices"].get(alias, {})["online"] = False
            log(f"Error updating {alias}: {exc}", "error")


def set_ke100_temp(alias, temp):
    """Set target temperature on a KE100 valve — local or cloud."""
    info = _ke100_map.get(alias)
    if not info:
        raise ValueError(f"Valve '{alias}' not found")

    if info.get("local"):
        async def _set_local():
            child = info["child"]
            modules = getattr(child, "modules", {})

            # Use TemperatureControl module directly (confirmed from logs)
            tc = modules.get("TemperatureControl")
            if tc and hasattr(tc, "set_target_temperature"):
                await tc.set_target_temperature(int(temp))
                await _local_hub.update()
                return

            # Fallback to any module with set_target_temperature
            for mod_name, mod in modules.items():
                fn = getattr(mod, "set_target_temperature", None)
                if fn and callable(fn):
                    await fn(int(temp))
                    await _local_hub.update()
                    return

            raise RuntimeError(f"No set_target_temperature method found on {alias}. Modules: {list(modules.keys())}")

        _run_async(_set_local())
    else:
        if not _cloud_token:
            raise RuntimeError("Not connected to TP-Link cloud")
        _passthrough(info["hub_id"], info["hub_server_url"], {
            "method": "set_device_info",
            "params": {"device_id": info["child_id"], "target_temp": int(temp)},
        }, _cloud_token)

    log(f"Set {alias} to {temp}°C")







# ──────────────────────────────────────────────
# Hive Integration
# Uses pyhiveapi.Auth (synchronous, boto3/Cognito)
# then calls beekeeper.hivehome.com with the token
# ──────────────────────────────────────────────

_hive_token = None
_hive_product_id = None
_hive_refresh_token = None
_hive_credentials = {"username": None, "password": None}  # stored for auto-reconnect

HIVE_NODES_URL   = "https://beekeeper.hivehome.com/1.0/nodes/all"
HIVE_HEATING_URL = "https://beekeeper.hivehome.com/1.0/nodes/heating/{id}"

def _hive_headers():
    return {"Authorization": _hive_token, "Content-Type": "application/json"}


def connect_hive(username, password):
    global _hive_token, _hive_product_id, _hive_refresh_token

    log(f"Attempting Hive login for {username}...")
    _hive_credentials["username"] = username
    _hive_credentials["password"] = password
    try:
        # pyhiveapi.Auth uses boto3 Cognito — synchronous, no async needed
        hive_auth = Hive.Auth(username, password)
        auth_data = hive_auth.login()
        log(f"Auth response keys: {list(auth_data.keys())}")

        # Handle 2FA challenge if needed
        if auth_data.get("ChallengeName") == "SMS_MFA":
            raise RuntimeError("Hive 2FA is required. Disable SMS 2FA in your Hive account settings to use HeatSync, then try again.")

        if "AuthenticationResult" not in auth_data:
            raise RuntimeError(f"Unexpected auth response: {list(auth_data.keys())}")

        session = auth_data["AuthenticationResult"]
        # Use IdToken for API calls (not AccessToken)
        _hive_token = session["IdToken"]
        _hive_refresh_token = session.get("RefreshToken")
        log("Hive Cognito login successful, loading devices...")

        # Now load devices from beekeeper API
        resp = requests.get(
            f"{HIVE_NODES_URL}?products=true&devices=true&actions=true",
            headers=_hive_headers(),
            timeout=15
        )
        resp.raise_for_status()
        nodes = resp.json()

        products = nodes.get("products", [])
        heating = next((p for p in products if p.get("type") == "heating"), None)
        if not heating:
            raise RuntimeError(f"No heating product. Products: {[p.get('type') for p in products]}")

        _hive_product_id = heating["id"]
        log(f"Found heating zone: {heating.get('state', {}).get('name', 'Thermostat')} (id={_hive_product_id[:8]}...)")

        props  = heating.get("props", {})
        hstate = heating.get("state", {})
        state["hive"]["connected"]    = True
        state["hive"]["current_temp"] = props.get("temperature")
        state["hive"]["target_temp"]  = hstate.get("target")
        state["hive"]["mode"]         = hstate.get("mode", "SCHEDULE")
        state["hive"]["heating"]      = props.get("working", False)
        log("Hive connected successfully")
        return True

    except Exception as exc:
        import traceback
        state["hive"]["connected"] = False
        _hive_token = None
        log(f"Hive login failed: {exc}", "error")
        log(traceback.format_exc(), "error")
        raise RuntimeError(f"Hive login failed: {exc}")


def _update_hive():
    """Refresh Hive thermostat readings. Auto-reconnects on token expiry."""
    if not _hive_token or not state["hive"]["connected"]:
        return

    try:
        resp = requests.get(
            f"{HIVE_NODES_URL}?products=true&devices=true",
            headers=_hive_headers(),
            timeout=15
        )
        if resp.status_code == 401:
            log("Hive token expired — attempting auto-reconnect...", "error")
            state["hive"]["connected"] = False
            if _hive_credentials["username"] and _hive_credentials["password"]:
                try:
                    connect_hive(_hive_credentials["username"], _hive_credentials["password"])
                    log("Hive auto-reconnected successfully")
                except Exception as e:
                    log(f"Hive auto-reconnect failed: {e}", "error")
            return
        resp.raise_for_status()
        nodes = resp.json()

        products = nodes.get("products", [])
        heating = next((p for p in products if p.get("type") == "heating"), None)
        if not heating:
            return

        props  = heating.get("props", {})
        hstate = heating.get("state", {})
        state["hive"]["current_temp"] = props.get("temperature")
        state["hive"]["target_temp"]  = hstate.get("target")
        state["hive"]["mode"]         = hstate.get("mode", "SCHEDULE")
        state["hive"]["heating"]      = props.get("working", False)
        state["hive"]["boost"]        = hstate.get("boost")

    except Exception as exc:
        log(f"Hive update error: {exc}", "error")


def _activate_hive_boost():
    """Boost Hive without touching the schedule."""
    if not _hive_token or not _hive_product_id:
        raise RuntimeError("Hive is not connected")

    # Correct format confirmed from Hive API data:
    # flat structure, duration in seconds, temperature as "target"
    body = {
        "mode": "BOOST",
        "target": int(settings["boost_temp"]),
        "duration": settings["boost_duration"] * 60,  # minutes -> seconds
    }
    log(f"Activating Hive boost: {body}")
    resp = requests.post(
        HIVE_HEATING_URL.format(id=_hive_product_id),
        headers=_hive_headers(),
        json=body,
        timeout=15
    )
    log(f"Boost response: {resp.status_code} {resp.text[:200]}")
    resp.raise_for_status()
    state["hive"]["boost_active"] = True


def _cancel_hive_boost():
    """Cancel boost, returning Hive to its schedule."""
    if not _hive_token or not _hive_product_id:
        raise RuntimeError("Hive is not connected")

    resp = requests.post(
        HIVE_HEATING_URL.format(id=_hive_product_id),
        headers=_hive_headers(),
        json={"mode": "SCHEDULE"},
        timeout=15
    )
    resp.raise_for_status()
    state["hive"]["boost_active"] = False


# ──────────────────────────────────────────────
# Coordination Thread
# ──────────────────────────────────────────────


# ── Persistent event loop for all Kasa async operations ──
# Kasa device objects are bound to the loop they were created in.
# Using a fresh loop each call breaks existing connections.
_kasa_loop = None
_kasa_loop_thread = None

def _ensure_kasa_loop():
    """Start the persistent Kasa event loop in a background thread if not running."""
    global _kasa_loop, _kasa_loop_thread
    if _kasa_loop and _kasa_loop.is_running():
        return
    import concurrent.futures
    _kasa_loop = asyncio.new_event_loop()
    _kasa_loop_thread = threading.Thread(
        target=_kasa_loop.run_forever, daemon=True, name="kasa-loop"
    )
    _kasa_loop_thread.start()

def _run_async(coro):
    """Submit a coroutine to the persistent Kasa event loop and wait for the result."""
    _ensure_kasa_loop()
    future = asyncio.run_coroutine_threadsafe(coro, _kasa_loop)
    return future.result(timeout=30)


def coordination_loop():
    """
    Background thread that:
      1. Polls all Kasa valves (local or cloud)
      2. Refreshes Hive state
      3. Activates / idles the boiler based on valve demand
    """
    log("Coordination loop started")

    while True:
        try:
            # Refresh Kasa valve readings (local or cloud)
            if _ke100_map:
                update_kasa_devices()

            # Refresh Hive
            _update_hive()

            state["last_poll"] = datetime.now().strftime("%H:%M:%S")

            # ── Coordination logic ──
            if settings["auto_mode"] and state["hive"]["connected"]:
                any_calling = any(
                    d.get("heating") and d.get("online")
                    for d in state["kasa_devices"].values()
                )

                if any_calling:
                    calling = [
                        a for a, d in state["kasa_devices"].items()
                        if d.get("heating") and d.get("online")
                    ]
                    try:
                        _activate_hive_boost()
                        if not state["boiler_active"]:
                            log(f"[HEAT] Boost activated — {', '.join(calling)} calling for heat")
                        state["boiler_active"] = True
                    except Exception as exc:
                        log(f"Failed to activate boost: {exc}", "error")

                elif not any_calling and state["boiler_active"]:
                    try:
                        _cancel_hive_boost()
                        state["boiler_active"] = False
                        log("[IDLE] Boost cancelled — all valves satisfied, Hive back on schedule")
                    except Exception as exc:
                        log(f"Failed to cancel boost: {exc}", "error")

        except Exception as exc:
            log(f"Coordination loop error: {exc}", "error")

        time.sleep(settings["poll_interval"])


_coord_thread = threading.Thread(target=coordination_loop, daemon=True)
_coord_thread.start()


# ──────────────────────────────────────────────
# API Routes
# ──────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def get_status():
    return jsonify({
        "hive": state["hive"],
        "kasa_devices": state["kasa_devices"],
        "boiler_active": state["boiler_active"],
        "last_poll": state["last_poll"],
        "logs": state["logs"][:30],
        "settings": settings,
    })


@app.route("/api/hive/connect", methods=["POST"])
def hive_connect():
    data = request.get_json()
    if not data or "username" not in data or "password" not in data:
        return jsonify({"success": False, "error": "username and password required"}), 400
    try:
        connect_hive(data["username"], data["password"])
        cfg = _load_config()
        cfg["hive"] = {"username": data["username"], "password": data["password"]}
        _save_config(cfg)
        return jsonify({"success": True})
    except Exception as exc:
        import traceback
        full = traceback.format_exc()
        logging.error(full)
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/hive/boost", methods=["POST"])
def hive_boost():
    data = request.get_json()
    action = data.get("action", "on")
    try:
        if action == "on":
            _activate_hive_boost()
            log(f"Manual boost activated")
        else:
            _cancel_hive_boost()
            log(f"Manual boost cancelled")
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 400


@app.route("/api/kasa/credentials", methods=["POST"])
def kasa_set_credentials():
    """Save TP-Link credentials. Tries local connection first, cloud as fallback."""
    data = request.get_json()
    if not data or "username" not in data or "password" not in data:
        return jsonify({"success": False, "error": "username and password required"}), 400

    username = data["username"]
    password = data["password"]
    hub_ip = data.get("hub_ip") or "192.168.0.178"

    kasa_credentials["username"] = username
    kasa_credentials["password"] = password
    kasa_credentials["hub_ip"] = hub_ip

    # Try local first
    log(f"KASA_LOCAL_AVAILABLE={KASA_LOCAL_AVAILABLE}, SmartHub={'available' if SmartHub else 'not available'}, hub_ip={hub_ip}")
    if KASA_LOCAL_AVAILABLE and hub_ip:
        try:
            aliases = _run_async(_connect_kasa_local(hub_ip, username, password))
            log(f"Kasa local connected — {len(aliases)} valve(s) found")
            cfg = _load_config()
            cfg["kasa"] = {"username": username, "password": password, "hub_ip": hub_ip}
            _save_config(cfg)
            return jsonify({"success": True, "devices": aliases, "method": "local"})
        except Exception as exc:
            log(f"Local connection failed: {exc} — trying cloud...", "error")

    # Fall back to cloud
    try:
        aliases = connect_kasa_cloud(username, password)
        log(f"Kasa cloud connected — {len(aliases)} valve(s) found")
        cfg = _load_config()
        cfg["kasa"] = {"username": username, "password": password, "hub_ip": hub_ip}
        _save_config(cfg)
        return jsonify({"success": True, "devices": aliases, "method": "cloud"})
    except Exception as exc:
        import traceback
        log(f"Kasa cloud connect also failed: {exc}", "error")
        log(traceback.format_exc(), "error")
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/kasa/discover", methods=["POST"])
def kasa_discover():
    """Re-scan the hub for valves (uses existing cloud credentials)."""
    if not _cloud_token:
        return jsonify({"success": False, "error": "Not connected — enter TP-Link credentials first"}), 400
    try:
        update_kasa_devices()
        aliases = list(_ke100_map.keys())
        log(f"Refreshed {len(aliases)} Kasa valve(s)")
        return jsonify({"success": True, "devices": aliases})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/kasa/<path:alias>/temperature", methods=["POST"])
def kasa_set_temp(alias):
    data = request.get_json()
    try:
        set_ke100_temp(alias, float(data["temperature"]))
        log(f"Set {alias} to {data['temperature']}°C")
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 400



@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify(settings)


@app.route("/api/settings", methods=["POST"])
def update_settings():
    data = request.get_json()
    allowed = {"poll_interval", "hysteresis", "auto_mode", "boost_temp", "boost_duration"}
    for key in allowed:
        if key in data:
            settings[key] = data[key]
    log("Settings updated")
    return jsonify({"success": True, "settings": settings})


@app.route("/api/saved-credentials", methods=["GET"])
def get_saved_credentials():
    cfg = _load_config()
    hive_cfg = cfg.get("hive", {})
    kasa_cfg = cfg.get("kasa", {})
    return jsonify({
        "hive": {
            "username": hive_cfg.get("username"),
            "has_password": bool(hive_cfg.get("password")),
        },
        "kasa": {
            "username": kasa_cfg.get("username"),
            "hub_ip": kasa_cfg.get("hub_ip", "192.168.0.178"),
            "has_password": bool(kasa_cfg.get("password")),
        },
    })


@app.route("/api/credentials/forget", methods=["POST"])
def forget_credentials():
    data = request.get_json() or {}
    target = data.get("target", "all")
    cfg = _load_config()
    if target in ("hive", "all"):
        cfg.pop("hive", None)
    if target in ("kasa", "all"):
        cfg.pop("kasa", None)
    _save_config(cfg)
    log(f"Saved credentials cleared ({target})")
    return jsonify({"success": True})


# ──────────────────────────────────────────────
# Entry Point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    # ── Advertise heatsync.local via mDNS so any device on the network can find it ──
    try:
        from zeroconf import Zeroconf, ServiceInfo
        import socket as _socket
        _zc = Zeroconf()
        _local_ip = _socket.gethostbyname(_socket.gethostname())
        _info = ServiceInfo(
            "_http._tcp.local.",
            "HeatSync._http._tcp.local.",
            addresses=[_socket.inet_aton(_local_ip)],
            port=5000,
            properties={"path": "/"},
            server="heatsync.local.",
        )
        _zc.register_service(_info)
        print(f"\nHeatSync available at http://heatsync.local:5000")
        print(f"Also available at http://{_local_ip}:5000\n")
    except ImportError:
        print("\nHeatSync starting on http://localhost:5000")
        print("(Install zeroconf for heatsync.local support: pip install zeroconf)\n")
    except Exception as e:
        print(f"\nHeatSync starting (mDNS unavailable: {e})")
        print("Access via http://localhost:5000\n")

    # ── Auto-connect from saved credentials ──
    _startup_cfg = _load_config()

    _hive_saved = _startup_cfg.get("hive", {})
    if _hive_saved.get("username") and _hive_saved.get("password"):
        print(f"Auto-connecting Hive as {_hive_saved['username']}...")
        try:
            connect_hive(_hive_saved["username"], _hive_saved["password"])
            print("Hive auto-connected successfully")
        except Exception as _e:
            print(f"Hive auto-connect failed: {_e}")

    _kasa_saved = _startup_cfg.get("kasa", {})
    if _kasa_saved.get("username") and _kasa_saved.get("password"):
        _ku = _kasa_saved["username"]
        _kp = _kasa_saved["password"]
        _ki = _kasa_saved.get("hub_ip", "192.168.0.178")
        kasa_credentials["username"] = _ku
        kasa_credentials["password"] = _kp
        kasa_credentials["hub_ip"] = _ki
        print(f"Auto-connecting Kasa as {_ku}...")
        _kasa_connected = False
        if KASA_LOCAL_AVAILABLE and _ki:
            try:
                _aliases = _run_async(_connect_kasa_local(_ki, _ku, _kp))
                print(f"Kasa auto-connected (local) — {len(_aliases)} valve(s)")
                _kasa_connected = True
            except Exception as _e:
                print(f"Kasa local auto-connect failed: {_e} — trying cloud...")
        if not _kasa_connected:
            try:
                _aliases = connect_kasa_cloud(_ku, _kp)
                print(f"Kasa auto-connected (cloud) — {len(_aliases)} valve(s)")
            except Exception as _e:
                print(f"Kasa auto-connect failed: {_e}")

    app.run(host="0.0.0.0", port=5000, debug=False)
