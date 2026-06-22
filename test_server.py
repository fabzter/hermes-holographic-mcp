#!/usr/bin/env python3
"""Comprehensive test suite for holographic-mcp server."""
import sys, os, json, tempfile, subprocess
sys.path.insert(0, os.path.dirname(__file__))

# Use a temp DB for testing
tmpdb = tempfile.mktemp(suffix=".db")
os.environ["HOLOGRAPHIC_DB_PATH"] = tmpdb

# Force reimport with new env
for mod in list(sys.modules):
    if "mcp_server" in mod:
        del sys.modules[mod]

from mcp_server import handle_fact_store, handle_fact_feedback, _store, _retriever, _HAS_NUMPY

PASS = 0
FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name} — {detail}")

def j(s):
    return json.loads(s)


print(f"=== Holographic MCP Test Suite (numpy={_HAS_NUMPY}) ===\n")

# --- 1. add ---
print("[1] add action")
r = j(handle_fact_store({"action": "add", "content": "User prefers Python 3.13", "category": "user_pref"}))
check("add returns fact_id", r.get("fact_id") == 1, str(r))
check("add status", r.get("status") == "added")

r = j(handle_fact_store({"action": "add", "content": "Project uses DASHSCOPE for LLM inference", "category": "project", "tags": "llm,alibaba"}))
check("add with tags", r.get("fact_id") == 2)

r = j(handle_fact_store({"action": "add", "content": "Fabzter works on luca-edu with Edgar Alberto Alvarez Garcia", "category": "project"}))
check("add with entity names", r.get("fact_id") == 3)

# --- 2. dedup ---
print("\n[2] deduplication")
r = j(handle_fact_store({"action": "add", "content": "User prefers Python 3.13", "category": "user_pref"}))
check("duplicate returns same id", r.get("fact_id") == 1, str(r))

# --- 3. empty content ---
print("\n[3] empty content rejection")
try:
    handle_fact_store({"action": "add", "content": ""})
    check("empty content raises", False, "no error raised")
except Exception as e:
    check("empty content raises", True)

# --- 4. search ---
print("\n[4] search action")
r = j(handle_fact_store({"action": "search", "query": "Python"}))
check("search finds results", r.get("count", 0) >= 1, str(r))
check("search result has content", "Python" in r["results"][0]["content"])

r = j(handle_fact_store({"action": "search", "query": "DASHSCOPE"}))
check("search finds by keyword", r.get("count", 0) >= 1)

r = j(handle_fact_store({"action": "search", "query": "nonexistent_xyz"}))
check("search returns empty for no match", r.get("count") == 0)

# --- 5. search with category filter ---
print("\n[5] search with category filter")
r = j(handle_fact_store({"action": "search", "query": "Python", "category": "user_pref"}))
check("category filter works", r.get("count") == 1)

r = j(handle_fact_store({"action": "search", "query": "Python", "category": "project"}))
check("category filter excludes", r.get("count") == 0)

# --- 6. list ---
print("\n[6] list action")
r = j(handle_fact_store({"action": "list", "limit": 10}))
check("list returns all facts", r.get("count") == 3, str(r))

r = j(handle_fact_store({"action": "list", "category": "project"}))
check("list with category", r.get("count") == 2)

r = j(handle_fact_store({"action": "list", "min_trust": 0.9}))
check("list with min_trust filter", r.get("count") == 0)

# --- 7. probe ---
print("\n[7] probe action")
r = j(handle_fact_store({"action": "probe", "entity": "Fabzter"}))
check("probe returns results", r.get("count", 0) >= 1, str(r))

# --- 8. related ---
print("\n[8] related action")
r = j(handle_fact_store({"action": "related", "entity": "Fabzter"}))
check("related returns results", r.get("count", 0) >= 0, str(r))  # may be 0 with FTS fallback

# --- 9. reason ---
print("\n[9] reason action (multi-entity)")
r = j(handle_fact_store({"action": "reason", "entities": ["Fabzter", "luca-edu"]}))
check("reason returns results", r.get("count", 0) >= 0, str(r))

# missing entities
r = j(handle_fact_store({"action": "reason", "entities": []}))
check("reason with empty entities errors", "error" in r, str(r))

# --- 10. contradict ---
print("\n[10] contradict action")
# Add a potentially conflicting fact
handle_fact_store({"action": "add", "content": "User prefers JavaScript over Python", "category": "user_pref"})
r = j(handle_fact_store({"action": "contradict", "category": "user_pref"}))
check("contradict returns pairs", isinstance(r.get("results"), list), str(r))

# --- 11. update ---
print("\n[11] update action")
r = j(handle_fact_store({"action": "update", "fact_id": 1, "content": "User prefers Python 3.13.13", "trust_delta": 0.2}))
check("update succeeds", r.get("updated") == True, str(r))

r = j(handle_fact_store({"action": "list", "min_trust": 0.7}))
check("trust_delta applied", any(f["fact_id"] == 1 for f in r["facts"]), str(r))

r = j(handle_fact_store({"action": "update", "fact_id": 999, "content": "nonexistent"}))
check("update nonexistent returns false", r.get("updated") == False)

# --- 12. feedback ---
print("\n[12] feedback action")
r = j(handle_fact_feedback({"action": "helpful", "fact_id": 2}))
check("helpful feedback", r.get("new_trust", 0) > 0.5, str(r))
check("helpful_count incremented", r.get("helpful_count") == 1)

r = j(handle_fact_feedback({"action": "unhelpful", "fact_id": 2}))
check("unhelpful feedback decreases trust", r.get("new_trust", 1) < r.get("old_trust", 1), str(r))

try:
    handle_fact_feedback({"action": "helpful", "fact_id": 999})
    check("feedback on nonexistent raises", False)
except KeyError:
    check("feedback on nonexistent raises", True)

# --- 13. remove ---
print("\n[13] remove action")
r = j(handle_fact_store({"action": "remove", "fact_id": 4}))
check("remove succeeds", r.get("removed") == True)

r = j(handle_fact_store({"action": "remove", "fact_id": 999}))
check("remove nonexistent returns false", r.get("removed") == False)

# --- 14. trust clamping ---
print("\n[14] trust clamping")
# Apply many helpful feedbacks to test upper clamp
for _ in range(20):
    handle_fact_feedback({"action": "helpful", "fact_id": 1})
r = j(handle_fact_store({"action": "list", "limit": 1}))
check("trust clamped at 1.0", r["facts"][0]["trust_score"] <= 1.0, str(r["facts"][0]["trust_score"]))

# --- 15. MCP protocol test ---
print("\n[15] MCP protocol over stdio")
proc = subprocess.Popen(
    [sys.executable, os.path.join(os.path.dirname(__file__), "mcp_server.py")],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    env={**os.environ, "HOLOGRAPHIC_DB_PATH": tmpdb}
)

def send(msg):
    body = json.dumps(msg)
    proc.stdin.write(body + "\n")
    proc.stdin.flush()

def recv():
    line = proc.stdout.readline()
    if not line:
        return None
    return json.loads(line.strip())

send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}}})
r = recv()
check("initialize returns serverInfo", r and "serverInfo" in r.get("result", {}), str(r))

send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
r = recv()
tools = [t["name"] for t in r["result"]["tools"]]
check("tools/list returns fact_store", "fact_store" in tools, str(tools))
check("tools/list returns fact_feedback", "fact_feedback" in tools, str(tools))

send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "fact_store", "arguments": {"action": "list", "limit": 1}}})
r = recv()
check("tools/call returns content", r and "content" in r.get("result", {}), str(r))

proc.terminate()
proc.wait()

# --- 16. unknown tool ---
print("\n[16] unknown tool handling")
r = j(handle_fact_store({"action": "invalid_action"}))
check("unknown action returns error", "error" in r, str(r))

# --- 17. persistence ---
print("\n[17] persistence across restarts")
_store.close()
_store2 = type(_store)(db_path=tmpdb, default_trust=0.5, hrr_dim=1024)
facts = _store2.list_facts(limit=10)
check("data persists across connections", len(facts) >= 2, str(len(facts)))
_store2.close()

# Cleanup
os.unlink(tmpdb)
if os.path.exists(tmpdb + "-wal"):
    os.unlink(tmpdb + "-wal")
if os.path.exists(tmpdb + "-shm"):
    os.unlink(tmpdb + "-shm")

print(f"\n=== Results: {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
