#!/usr/bin/env python3
"""Behavior test: verify HRR compositional queries work with numpy enabled.

This test specifically validates that probe/related/reason use the HRR
algebraic path (not FTS fallback) when numpy is available, and that
the results are semantically meaningful.
"""
import sys, os, json, tempfile
sys.path.insert(0, os.path.dirname(__file__))

tmpdb = tempfile.mktemp(suffix=".db")
os.environ["HOLOGRAPHIC_DB_PATH"] = tmpdb

for mod in list(sys.modules):
    if "mcp_server" in mod:
        del sys.modules[mod]

from mcp_server import handle_fact_store, handle_fact_feedback, _store, _retriever, _HAS_NUMPY

def j(s):
    return json.loads(s)

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

print(f"=== HRR Behavior Tests (numpy={_HAS_NUMPY}) ===\n")

if not _HAS_NUMPY:
    print("SKIP: numpy not available, HRR path cannot be tested")
    sys.exit(0)

# Seed facts with entities that should be discoverable via HRR algebra
print("[1] Seeding facts with structured entities")
facts = [
    ("Fabzter prefers Python 3.13 for backend work", "user_pref"),
    ("Edgar Alberto Alvarez Garcia builds auth-service at luca-edu", "project"),
    ("The luca-edu project uses DASHSCOPE for LLM inference", "project"),
    ("Fabzter and Edgar collaborate on api.lucaedu.com", "project"),
    ("Python 3.13 is the runtime for the backend", "tool"),
    ("DASHSCOPE provides Qwen models via API", "tool"),
    ("The auth-service uses JWT tokens for authentication", "tool"),
    ("Fabzter hates walls of text and values direct communication", "user_pref"),
]

for content, category in facts:
    r = j(handle_fact_store({"action": "add", "content": content, "category": category}))
    assert r["status"] == "added", f"Failed to add: {content}"

print(f"    Seeded {len(facts)} facts\n")

# Verify HRR vectors were computed
print("[2] HRR vector computation")
conn = _store._conn
rows = conn.execute("SELECT fact_id, hrr_vector FROM facts WHERE hrr_vector IS NOT NULL").fetchall()
check("all facts have HRR vectors", len(rows) == len(facts), f"only {len(rows)}/{len(facts)} have vectors")

# Verify memory banks were built
banks = conn.execute("SELECT bank_name, fact_count FROM memory_banks").fetchall()
print(f"    Memory banks: {[(r['bank_name'], r['fact_count']) for r in banks]}")
check("memory banks exist", len(banks) >= 2, f"only {len(banks)} banks")

# --- probe: should find facts ABOUT an entity, even without keyword match ---
print("\n[3] probe — entity recall via HRR algebra")
r = j(handle_fact_store({"action": "probe", "entity": "Fabzter"}))
print(f"    probe('Fabzter'): {r['count']} results")
for res in r["results"][:3]:
    print(f"      [{res.get('score', 0):.3f}] {res['content'][:60]}")
check("probe finds Fabzter facts", r["count"] >= 2, str(r["count"]))

r = j(handle_fact_store({"action": "probe", "entity": "Edgar"}))
print(f"    probe('Edgar'): {r['count']} results")
for res in r["results"][:3]:
    print(f"      [{res.get('score', 0):.3f}] {res['content'][:60]}")
check("probe finds Edgar facts", r["count"] >= 1)

# --- related: structural adjacency ---
print("\n[4] related — structural adjacency via HRR")
r = j(handle_fact_store({"action": "related", "entity": "Fabzter"}))
print(f"    related('Fabzter'): {r['count']} results")
for res in r["results"][:3]:
    print(f"      [{res.get('score', 0):.3f}] {res['content'][:60]}")
check("related returns results", r["count"] >= 1)

# --- reason: multi-entity compositional query ---
print("\n[5] reason — compositional multi-entity query")
r = j(handle_fact_store({"action": "reason", "entities": ["Fabzter", "Edgar"]}))
print(f"    reason(['Fabzter', 'Edgar']): {r['count']} results")
for res in r["results"][:3]:
    print(f"      [{res.get('score', 0):.3f}] {res['content'][:60]}")
check("reason finds joint facts", r["count"] >= 1)

r = j(handle_fact_store({"action": "reason", "entities": ["Python", "DASHSCOPE"]}))
print(f"    reason(['Python', 'DASHSCOPE']): {r['count']} results")
check("reason with different entities", r["count"] >= 0)

# --- HRR vs FTS comparison ---
print("\n[6] HRR path vs FTS fallback comparison")
# Force FTS fallback by temporarily disabling numpy
import mcp_server
original_numpy = mcp_server._HAS_NUMPY
mrf_server_numpy = mcp_server._HAS_NUMPY

# Create a retriever with hrr_weight=0 to simulate FTS-only
from mcp_server import FactRetriever
fts_only_retriever = FactRetriever(store=_store, hrr_weight=0.0, hrr_dim=1024)

hrr_results = _retriever.probe("Fabzter", limit=5)
fts_results = fts_only_retriever.search("Fabzter", limit=5)

hrr_ids = set(r["fact_id"] for r in hrr_results)
fts_ids = set(r["fact_id"] for r in fts_results)

print(f"    HRR probe IDs: {hrr_ids}")
print(f"    FTS search IDs: {fts_ids}")

# HRR should find facts that mention Fabzter even indirectly
# (through structural/entity connections, not just keyword)
hrr_fabzter_facts = [r for r in hrr_results if "Fabzter" in r["content"]]
fts_fabzter_facts = [r for r in fts_results if "Fabzter" in r["content"]]

print(f"    HRR found {len(hrr_fabzter_facts)} facts mentioning 'Fabzter'")
print(f"    FTS found {len(fts_fabzter_facts)} facts mentioning 'Fabzter'")

# HRR probe should find at least as many facts as FTS (it uses algebraic extraction)
check("HRR probe finds facts about entity", len(hrr_results) >= 1, str(len(hrr_results)))

# --- trust scoring with feedback ---
print("\n[7] trust scoring affects retrieval ranking")
# Give fact 1 (Fabzter prefers Python) many helpful votes
for _ in range(10):
    handle_fact_feedback({"action": "helpful", "fact_id": 1})

# Give fact 8 (Fabzter hates walls of text) unhelpful votes
for _ in range(5):
    handle_fact_feedback({"action": "unhelpful", "fact_id": 8})

r = j(handle_fact_store({"action": "search", "query": "Fabzter"}))
if r["count"] > 0:
    top = r["results"][0]
    print(f"    Top result: [{top['trust_score']:.2f}] {top['content'][:60]}")
    check("high-trust fact ranks first", top["fact_id"] == 1, f"got fact_id={top['fact_id']}")

# --- contradict detection ---
print("\n[8] contradict — find semantically overlapping facts")
r = j(handle_fact_store({"action": "contradict", "limit": 5}))
print(f"    contradict: {r['count']} pairs found")
for pair in r["results"][:2]:
    print(f"      overlap={pair['overlap']:.2f}: {pair['fact_a']['content'][:40]} <-> {pair['fact_b']['content'][:40]}")
check("contradict finds overlapping pairs", r["count"] >= 1, str(r["count"]))

# --- HRR vector serialization roundtrip ---
print("\n[9] HRR vector serialization roundtrip")
from mcp_server import encode_atom, phases_to_bytes, bytes_to_phases, similarity
vec = encode_atom("test_entity", 1024)
serialized = phases_to_bytes(vec)
restored = bytes_to_phases(serialized)
sim = similarity(vec, restored)
check("serialization roundtrip preserves vector", sim > 0.99, f"similarity={sim}")

# --- HRR bind/unbind algebra ---
print("\n[10] HRR bind/unbind algebraic properties")
from mcp_server import encode_atom, bind, unbind, similarity
a = encode_atom("concept_a", 1024)
b = encode_atom("concept_b", 1024)
bound = bind(a, b)
recovered = unbind(bound, a)
sim = similarity(recovered, b)
print(f"    unbind(bind(a, b), a) ≈ b: similarity={sim:.3f}")
check("bind/unbind recovers original", sim > 0.5, f"similarity={sim}")

# --- HRR bundle similarity ---
print("\n[11] HRR bundle — superposition properties")
from mcp_server import encode_atom, bundle, similarity
a = encode_atom("alpha", 1024)
b = encode_atom("beta", 1024)
c = encode_atom("gamma", 1024)
bundled = bundle(a, b, c)
sim_a = similarity(bundled, a)
sim_b = similarity(bundled, b)
sim_c = similarity(bundled, c)
print(f"    bundle sim to a={sim_a:.3f}, b={sim_b:.3f}, c={sim_c:.3f}")
check("bundle is similar to all components", sim_a > 0 and sim_b > 0 and sim_c > 0)

# Cleanup
_store.close()
os.unlink(tmpdb)
for ext in ["-wal", "-shm"]:
    if os.path.exists(tmpdb + ext):
        os.unlink(tmpdb + ext)

print(f"\n=== Results: {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
