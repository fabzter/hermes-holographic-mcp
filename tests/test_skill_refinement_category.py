"""Tests for the 'skill_refinement' fact category.

The skill refinement loop (in the sister repo `devin-skill-auto-create`)
writes deviation candidates to the holographic store with
`category="skill_refinement"`. Schema-strict MCP clients (Bean) reject any
category value not in the public enum, so this is an additive enum
extension. The SQLite `category` column is already free-text; no schema,
HRR, or FTS5 changes are required.

These tests mirror `test_procedural_category.py` to verify:
  1. 'skill_refinement' is recognized in the public tool schema enum.
  2. add(action='add', category='skill_refinement') persists and returns a
     real fact_id, and the round-trip lookup returns the same fact.
  3. search/list filtered by category='skill_refinement' returns the
     stored fact (real behavior, not mock).
"""
import json
import os
import sys
import tempfile

import pytest

# Each test module gets its own DB so it doesn't collide with the live one.
_TMPDB = tempfile.mktemp(suffix=".db")
os.environ["HOLOGRAPHIC_DB_PATH"] = _TMPDB

# Project root is the parent of this tests/ directory.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Force a clean import bound to the tmp DB above.
for _mod in list(sys.modules):
    if _mod == "mcp_server" or _mod.startswith("mcp_server."):
        del sys.modules[_mod]

from mcp_server import FACT_STORE_SCHEMA, handle_fact_store  # noqa: E402


def _j(s):
    return json.loads(s)


@pytest.fixture(autouse=True)
def _clear_db():
    """Wipe facts between tests so each test starts from a known state."""
    from mcp_server import _store
    with _store._lock:
        _store._conn.execute("DELETE FROM facts")
        _store._conn.execute("DELETE FROM entities")
        _store._conn.execute("DELETE FROM fact_entities")
        _store._conn.commit()
    yield


# --- 1. Schema advertises 'skill_refinement' as a valid category --------------

def test_skill_refinement_in_category_enum():
    enum = FACT_STORE_SCHEMA["inputSchema"]["properties"]["category"]["enum"]
    assert "skill_refinement" in enum, (
        f"'skill_refinement' should be listed in the fact_store category enum; "
        f"got {enum}"
    )


# --- 2. add with category='skill_refinement' actually persists ----------------

def test_add_skill_refinement_fact_returns_id_and_roundtrips():
    r = _j(handle_fact_store({
        "action": "add",
        "content": "Skill deploy hermes observed deviation exec then write but documented write then exec",
        "category": "skill_refinement",
        "tags": "skill_refinement,deploy_hermes",
    }))
    assert r.get("status") == "added", f"expected status=added, got {r}"
    assert isinstance(r.get("fact_id"), int) and r["fact_id"] > 0, r
    fact_id = r["fact_id"]

    # Round-trip: list filtered by category must return our fact.
    listing = _j(handle_fact_store({
        "action": "list", "category": "skill_refinement"
    }))
    assert listing["count"] == 1, listing
    got = listing["facts"][0]
    assert got["fact_id"] == fact_id
    assert got["category"] == "skill_refinement"
    assert "deploy hermes" in got["content"]


# --- 3. search filtered by category='skill_refinement' returns the fact -------

def test_search_by_skill_refinement_category():
    handle_fact_store({
        "action": "add",
        "content": "Skill rotate keys deviation observed exec only documented write then exec",
        "category": "skill_refinement",
        "tags": "skill_refinement,rotate_keys",
    })
    # Add a non-skill_refinement fact that shares the keyword "rotate"
    # to prove the category filter actually bites.
    handle_fact_store({
        "action": "add",
        "content": "User prefers manual key rotation over automation",
        "category": "user_pref",
    })

    r = _j(handle_fact_store({
        "action": "search",
        "query": "rotate",
        "category": "skill_refinement",
    }))
    assert r["count"] == 1, r
    assert r["results"][0]["category"] == "skill_refinement"
    assert "rotate keys" in r["results"][0]["content"]
