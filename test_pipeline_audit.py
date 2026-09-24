#!/usr/bin/env python3
"""
Clio Pipeline Diagnostic Test
Audits: Record Start → Event Feed → Stop/Dissect → Workflow Listed → Execute
"""
import json, time, sys, requests

BASE = "http://127.0.0.1:8765"
PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "


def r(method, path, **kwargs):
    try:
        fn = getattr(requests, method)
        resp = fn(f"{BASE}{path}", timeout=10, **kwargs)
        return resp.status_code, resp.json()
    except Exception as e:
        return -1, {"error": str(e)}


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def check(label, condition, detail=""):
    icon = PASS if condition else FAIL
    print(f"  {icon} {label}", end="")
    if detail:
        print(f"  → {detail}", end="")
    print()
    return condition


# ─── STAGE A: Server Alive ────────────────────────────────────────────────────
section("STAGE A: Server Health")
code, body = r("get", "/api/status")
check("Server responds on :8765", code == 200, f"HTTP {code}")
if code != 200:
    print(f"  {FAIL} Server is DOWN. Exiting.")
    sys.exit(1)

check("Status is idle", body.get("status") == "idle", body.get("status"))
check("Recording not active", not body["recording"]["is_recording"])
check("Virtual cursor idle", body["virtual_cursor"]["state"] == "idle")

# ─── STAGE B: Recording Start ────────────────────────────────────────────────
section("STAGE B: Recording Start")
code, body = r("post", "/api/record/start")
check("POST /api/record/start returns 200", code == 200, f"HTTP {code}")
check("success=True", body.get("success") is True, str(body))

code, body = r("get", "/api/record/status")
check("GET /api/record/status: is_recording=True", body.get("is_recording") is True, str(body))

# ─── STAGE C: Feed Events ────────────────────────────────────────────────────
section("STAGE C: Event Feed")
test_events = [
    {"event_type": "mouse_down", "x": 500.0, "y": 400.0, "button": "left", "bundle_id": "com.apple.finder"},
    {"event_type": "mouse_up",   "x": 500.0, "y": 400.0, "button": "left", "bundle_id": "com.apple.finder"},
    {"event_type": "key_down",   "x": 0.0,   "y": 0.0,   "key": "cmd+n",  "bundle_id": "com.apple.finder"},
]
for i, ev in enumerate(test_events):
    code, body = r("post", "/api/record/feed", json=ev)
    check(f"Feed event {i+1} ({ev['event_type']})", code == 200, str(body))

time.sleep(0.5)
code, body = r("get", "/api/record/status")
event_count = body.get("event_count", 0)
check(f"event_count={event_count} (expected ≥ 3)", event_count >= 3, str(body))

# ─── STAGE D: Stop / Dissect / Save ─────────────────────────────────────────
section("STAGE D: Stop, Dissect, Save")
code, body = r("post", "/api/record/stop", json={
    "name": "Open Finder Window",
    "trigger": "open finder window",
    "description": "Click finder and open new window"
})
check("POST /api/record/stop returns 200", code == 200, f"HTTP {code}")
check("success=True", body.get("success") is True, str(body))

wf_id = body.get("workflow_id", "")
total_steps = body.get("total_steps", 0)
check("workflow_id returned", bool(wf_id), wf_id or "MISSING")
check(f"total_steps={total_steps} (expected ≥ 1)", total_steps >= 1, str(body.get("steps", [])[:2]))

print(f"\n  Returned steps:")
for s in body.get("steps", []):
    action = s.get("action") or s.get("action_type", "?")
    print(f"    → order={s.get('order',0)} action={action} desc={s.get('description','')[:60]}")

# ─── STAGE E: Workflow In List ───────────────────────────────────────────────
section("STAGE E: Workflow Appears in /api/workflows")
time.sleep(0.3)
code, body = r("get", "/api/workflows")
check("GET /api/workflows returns 200", code == 200, f"HTTP {code}")
ids = [w["id"] for w in body]
check(f"Saved workflow {wf_id[:8]}... appears in list", wf_id in ids, f"List has {len(body)} items")
if wf_id not in ids:
    print(f"  {WARN} IDs in list: {ids}")

# ─── STAGE F: Search / Retrieval ─────────────────────────────────────────────
section("STAGE F: Search / NL Retrieval")
code, body = r("get", "/api/search", params={"q": "open finder window"})
check("GET /api/search returns 200", code == 200)
check(f"Got ≥1 results for 'open finder window'", len(body) >= 1, f"Got {len(body)}")
for res in body[:3]:
    print(f"    → id={res['workflow_id'][:8]} conf={res.get('confidence','?')} name={res['name']}")

# ─── STAGE G: Execute Workflow ───────────────────────────────────────────────
section("STAGE G: Execute Workflow by ID")
if wf_id:
    code, body = r("post", "/api/execute", json={"workflow_id": wf_id, "background": True})
    check("POST /api/execute returns 200", code == 200, f"HTTP {code}")
    check("success=True", body.get("success") is True, str(body))
    print(f"  → Execution started: {body.get('message','')}")

    # Poll status for up to 8s to see it executing or completing
    started_executing = False
    for _ in range(16):
        time.sleep(0.5)
        sc, sb = r("get", "/api/status")
        if sb.get("status") == "executing":
            started_executing = True
            print(f"  {PASS} Status flipped to 'executing' ✓")
            break

    if not started_executing:
        print(f"  {WARN} Never saw 'executing' status (may have completed instantly or failed)")

    # Wait for finish
    for _ in range(20):
        time.sleep(0.5)
        sc, sb = r("get", "/api/status")
        if sb.get("status") == "idle":
            print(f"  {PASS} Execution finished, status back to 'idle'")
            break
    else:
        print(f"  {WARN} Workflow still running after 10s — cancelling")
        r("post", "/api/cancel")
else:
    print(f"  {FAIL} No workflow_id to test execution with (Stage D failed)")

# ─── STAGE H: Execute by Query ───────────────────────────────────────────────
section("STAGE H: Execute by NL Query")
code, body = r("post", "/api/execute", json={"query": "open finder window"})
check("POST /api/execute with query returns 200", code == 200, f"HTTP {code}")
check("success=True", body.get("success") is True, str(body))
if body.get("success"):
    print(f"  → {body.get('message','')}")
    time.sleep(3)
    r("post", "/api/cancel")

# ─── STAGE I: Dynamic Intent ─────────────────────────────────────────────────
section("STAGE I: Dynamic Intent Synthesis")
code, body = r("get", "/api/search", params={"q": "open safari"})
check("Search 'open safari' returns results", len(body) >= 1)
if body:
    res = body[0]
    check("First result is dynamic_intent type", res.get("match_type") == "dynamic_intent",
          f"match_type={res.get('match_type')}")
    print(f"  → {res['name']} steps={res['step_count']}")

    # Test actual execution
    dyn_id = res["workflow_id"]
    code, body = r("post", "/api/execute", json={"workflow_id": dyn_id})
    check("Execute 'open safari' succeeds", body.get("success") is True, str(body))
    time.sleep(3)
    r("post", "/api/cancel")

# ─── SUMMARY ─────────────────────────────────────────────────────────────────
section("SUMMARY")
print("  Diagnostic complete. Check FAIL lines above for pipeline gaps.")
print("  Server log: /tmp/clio_server.log")
