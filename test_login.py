# -*- coding: utf-8 -*-
"""
HeatSync Login Tester
Run this in your venv to test Hive and Kasa connections independently.
Usage: python test_login.py
"""

import sys, io, asyncio, uuid, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Credentials ────────────────────────────────
HIVE_USER = input("Enter Hive email: ").strip()
HIVE_PASS = input("Enter Hive password: ").strip()
KASA_USER = input("Enter Kasa/TP-Link email: ").strip()
KASA_PASS = input("Enter Kasa/TP-Link password: ").strip()
print()

# ═══════════════════════════════════════════════
# HIVE TEST
# ═══════════════════════════════════════════════
print("=" * 50)
print("TESTING HIVE")
print("=" * 50)

try:
    import pyhiveapi as Hive
    print(f"pyhiveapi version: {getattr(Hive, '__version__', 'unknown')}")

    hive = Hive.Hive(username=HIVE_USER, password=HIVE_PASS)
    hive.session.username = HIVE_USER
    hive.session.password = HIVE_PASS

    print("Attempting login...")

    async def hive_test():
        await hive.session.login()
        print("  login() OK")
        try:
            await hive.session.startSession()
            print("  startSession() OK")
        except Exception as e:
            print(f"  startSession() failed: {e}")
            try:
                await hive.session.getDevices("No_ID")
                print("  getDevices() OK")
            except Exception as e2:
                print(f"  getDevices() failed: {e2}")

        data = getattr(hive.session, "data", None) or getattr(hive, "data", None)
        if data:
            print(f"  Data keys: {list(data.keys())}")
            products = data.get("products", {})
            print(f"  Products count: {len(products)}")
            for pid, prod in (products.items() if isinstance(products, dict) else enumerate(products)):
                ptype = prod.get("type", "?")
                pname = prod.get("state", {}).get("name", "?")
                temp = prod.get("props", {}).get("temperature", "?")
                print(f"    - {pname} | type={ptype} | temp={temp}")
        else:
            print("  No data found")
            print(f"  session attrs: {[k for k in vars(hive.session) if not k.startswith('_')]}")

    loop = asyncio.new_event_loop()
    loop.run_until_complete(hive_test())
    loop.close()
    print("HIVE: OK")

except Exception as e:
    import traceback
    print(f"HIVE FAILED: {e}")
    traceback.print_exc()

# ═══════════════════════════════════════════════
# KASA TEST
# ═══════════════════════════════════════════════
print()
print("=" * 50)
print("TESTING KASA CLOUD")
print("=" * 50)

try:
    import requests

    # Step 1: Login
    print("Attempting Kasa cloud login...")
    resp = requests.post(
        "https://eu-wap.tplinkcloud.com",
        json={
            "method": "login",
            "params": {
                "appType": "Kasa_Android",
                "cloudPassword": KASA_PASS,
                "cloudUserName": KASA_USER,
                "terminalUUID": str(uuid.uuid4()),
            }
        },
        headers={"Content-Type": "application/json"},
        timeout=10
    )
    data = resp.json()
    print(f"  Login response error_code: {data.get('error_code')} msg: {data.get('msg','')}")

    if data.get("error_code", 0) != 0:
        print(f"KASA LOGIN FAILED: {data.get('msg')}")
    else:
        token = data["result"]["token"]
        print(f"  Token: {token[:20]}...")

        # Step 2: Get device list
        print("Getting device list...")
        resp2 = requests.post(
            f"https://eu-wap.tplinkcloud.com?token={token}",
            json={"method": "getDeviceList", "params": {}},
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        data2 = resp2.json()
        devices = data2.get("result", {}).get("deviceList", [])
        print(f"  Found {len(devices)} device(s)")

        for d in devices:
            print(f"  Device: alias={d.get('alias')} model={d.get('deviceModel')} type={d.get('deviceType')} url={d.get('appServerUrl')}")

        # Step 3: Try passthrough on any hub
        for d in devices:
            dtype = d.get("deviceType", "")
            model = d.get("deviceModel", "")
            if "HUB" in dtype.upper() or "KH100" in model.upper():
                hub_id = d["deviceId"]
                app_url = d.get("appServerUrl") or "https://eu-wap.tplinkcloud.com"
                print(f"\n  Trying passthrough on hub {d.get('alias')} at {app_url}...")

                for method_name in ["get_child_device_list", "getChildDeviceList"]:
                    resp3 = requests.post(
                        f"{app_url}?token={token}",
                        json={
                            "method": "passthrough",
                            "params": {
                                "deviceId": hub_id,
                                "requestData": json.dumps({"method": method_name, "params": {}}),
                            }
                        },
                        headers={"Content-Type": "application/json"},
                        timeout=10
                    )
                    data3 = resp3.json()
                    print(f"    {method_name}: error_code={data3.get('error_code')} msg={data3.get('msg','')}")
                    if data3.get("error_code", 0) == 0:
                        inner = json.loads(data3["result"]["responseData"])
                        children = inner.get("result", {}).get("child_device_list", [])
                        print(f"    SUCCESS! Found {len(children)} child device(s)")
                        for c in children:
                            print(f"      Child: {c.get('nickname') or c.get('alias')} model={c.get('model')} id={str(c.get('device_id',''))[:8]}")
                        break
                    else:
                        print(f"    Full response: {json.dumps(data3)[:200]}")

        print("KASA: OK")

except Exception as e:
    import traceback
    print(f"KASA FAILED: {e}")
    traceback.print_exc()

print()
print("Done. Copy and paste the output above back to Claude.")
