"""Tests for the 'procedural' fact category.

Procedural memory stores learned tool sequences ("to do X, run Y then Z").
This is an additive change to the conventional category enum; the SQLite
`category` column is already free-text, so no schema migration is required.

These tests verify:
  1. The 'procedural' value is recognized in the public tool schema enum.
  2. Adding a fact with category='procedural' actually persists it and
     returns a fact_id (real behavior, not a mock).
  3. search/list filtered by category='procedural' returns the stored fact.
  4. Regression: the four pre-existing conventional categories
     ('user_pref', 'project', 'tool', 'general') still work end-to-end.
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
    # Lazy import of the live store bound to our tmp DB.
    from mcp_server import _store
    with _store._lock:
        _store._conn.execute("DELETE FROM facts")
        _store._conn.execute("DELETE FROM entities")
        _store._conn.execute("DELETE FROM fact_entities")
        _store._conn.commit()
    yield


# --- 1. Schema advertises 'procedural' as a valid category --------------------

def test_procedural_in_category_enum():
    enum = FACT_STORE_SCHEMA["inputSchema"]["properties"]["category"]["enum"]
    assert "procedural" in enum, (
        f"'procedural' should be listed in the fact_store category enum; got {enum}"
    )


# --- 2. add with category='procedural' actually persists ----------------------

def test_add_procedural_fact_returns_id():
    r = _j(handle_fact_store({
        "action": "add",
        "content": "To deploy hermes: run setup.sh then systemctl restart hermes",
        "category": "procedural",
    }))
    assert r.get("status") == "added", f"expected status=added, got {r}"
    assert isinstance(r.get("fact_id"), int) and r["fact_id"] > 0, r

    # Round-trip: the fact must be readable back with the same category.
    listing = _j(handle_fact_store({"action": "list", "category": "procedural"}))
    assert listing["count"] == 1, listing
    assert listing["facts"][0]["category"] == "procedural"
    assert "deploy hermes" in listing["facts"][0]["content"]


# --- 3. search filtered by category='procedural' returns the fact -------------

def test_search_by_procedural_category():
    handle_fact_store({
        "action": "add",
        "content": "To rotate API keys: call vault.rotate then redeploy pods",
        "category": "procedural",
    })
    # Add a non-procedural fact with the same keyword to prove the filter bites.
    handle_fact_store({
        "action": "add",
        "content": "User prefers manual key rotation over automation",
        "category": "user_pref",
    })

    r = _j(handle_fact_store({
        "action": "search",
        "query": "rotate",
        "category": "procedural",
    }))
    assert r["count"] == 1, r
    assert r["results"][0]["category"] == "procedural"
    assert "vault.rotate" in r["results"][0]["content"]


# --- 4. Regression: all four pre-existing categories still work ---------------

@pytest.mark.parametrize("category,content", [
    ("user_pref", "User prefers tabs over spaces"),
    ("project",   "luca-edu uses DASHSCOPE for inference"),
    ("tool",      "ripgrep is faster than grep for large repos"),
    ("general",   "The sky is, on average, blue"),
])
def test_existing_categories_still_work(category, content):
    r = _j(handle_fact_store({
        "action": "add",
        "content": content,
        "category": category,
    }))
    assert r.get("status") == "added", f"add failed for {category}: {r}"
    fid = r["fact_id"]

    listing = _j(handle_fact_store({"action": "list", "category": category}))
    assert any(f["fact_id"] == fid and f["category"] == category
               for f in listing["facts"]), (category, listing)

    # And the schema still advertises them.
    enum = FACT_STORE_SCHEMA["inputSchema"]["properties"]["category"]["enum"]
    assert category in enum, enum
