#!/usr/bin/env python3
"""Holographic Memory MCP Server — standalone, no Hermes dependencies.

Implements the Holographic memory provider from NousResearch/hermes-agent
as a Model Context Protocol server over stdio. Uses raw JSON-RPC 2.0 so
no external MCP SDK dependency is needed.

Features:
  - SQLite + FTS5 full-text search
  - Trust scoring with asymmetric feedback
  - Entity extraction and resolution
  - HRR (Holographic Reduced Representations) for compositional queries
  - numpy optional (falls back to FTS5-only without it)

Tools exposed:
  - fact_store: 9 actions (add, search, probe, related, reason, contradict, update, remove, list)
  - fact_feedback: rate facts helpful/unhelpful to train trust scores

Config via environment variables:
  - HOLOGRAPHIC_DB_PATH: SQLite database path (default: ~/.local/share/devin/holographic/memory.db)
  - HOLOGRAPHIC_DEFAULT_TRUST: default trust score (default: 0.5)
  - HOLOGRAPHIC_MIN_TRUST: minimum trust threshold (default: 0.3)
  - HOLOGRAPHIC_HRR_DIM: HRR vector dimensions (default: 1024)
  - HOLOGRAPHIC_AUTO_EXTRACT: auto-extract facts (default: false)
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import struct
import sys
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("holographic-mcp")

# ---------------------------------------------------------------------------
# HRR (Holographic Reduced Representations) — inlined from holographic.py
# ---------------------------------------------------------------------------

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

_TWO_PI = 2.0 * math.pi


def _require_numpy():
    if not _HAS_NUMPY:
        raise RuntimeError("numpy is required for holographic operations")


def encode_atom(word: str, dim: int = 1024):
    if not _HAS_NUMPY:
        return None
    values_per_block = 16
    blocks_needed = math.ceil(dim / values_per_block)
    uint16_values = []
    for i in range(blocks_needed):
        digest = hashlib.sha256(f"{word}:{i}".encode()).digest()
        uint16_values.extend(struct.unpack("<16H", digest))
    phases = np.array(uint16_values[:dim], dtype=np.float64) * (_TWO_PI / 65536.0)
    return phases


def bind(a, b):
    if not _HAS_NUMPY:
        return None
    return (a + b) % _TWO_PI


def unbind(memory, key):
    if not _HAS_NUMPY:
        return None
    return (memory - key) % _TWO_PI


def bundle(*vectors):
    if not _HAS_NUMPY:
        return None
    vectors = [v for v in vectors if v is not None]
    if not vectors:
        return None
    complex_sum = np.sum([np.exp(1j * v) for v in vectors], axis=0)
    return np.angle(complex_sum) % _TWO_PI


def similarity(a, b) -> float:
    if not _HAS_NUMPY or a is None or b is None:
        return 0.0
    return float(np.mean(np.cos(a - b)))


def encode_text(text: str, dim: int = 1024):
    if not _HAS_NUMPY:
        return None
    tokens = [token.strip(".,!?;:\"'()[]{}") for token in text.lower().split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return encode_atom("__hrr_empty__", dim)
    atom_vectors = [encode_atom(token, dim) for token in tokens]
    return bundle(*atom_vectors)


def encode_fact(content: str, entities: list, dim: int = 1024):
    if not _HAS_NUMPY:
        return None
    role_content = encode_atom("__hrr_role_content__", dim)
    role_entity = encode_atom("__hrr_role_entity__", dim)
    components = [bind(encode_text(content, dim), role_content)]
    for entity in entities:
        components.append(bind(encode_atom(entity.lower(), dim), role_entity))
    return bundle(*components)


def phases_to_bytes(phases) -> bytes:
    if not _HAS_NUMPY or phases is None:
        return b""
    return phases.tobytes()


def bytes_to_phases(data: bytes):
    if not _HAS_NUMPY:
        return None
    return np.frombuffer(data, dtype=np.float64).copy()


# ---------------------------------------------------------------------------
# SQLite Store — inlined from store.py, Hermes deps removed
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    fact_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content         TEXT NOT NULL UNIQUE,
    category        TEXT DEFAULT 'general',
    tags            TEXT DEFAULT '',
    trust_score     REAL DEFAULT 0.5,
    retrieval_count INTEGER DEFAULT 0,
    helpful_count   INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    hrr_vector      BLOB
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    entity_type TEXT DEFAULT 'unknown',
    aliases     TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id   INTEGER REFERENCES facts(fact_id),
    entity_id INTEGER REFERENCES entities(entity_id),
    PRIMARY KEY (fact_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_facts_trust    ON facts(trust_score DESC);
CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);
CREATE INDEX IF NOT EXISTS idx_entities_name  ON entities(name);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
    USING fts5(content, tags, content=facts, content_rowid=fact_id);

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, content, tags)
        VALUES ('delete', old.fact_id, old.content, old.tags);
    INSERT INTO facts_fts(rowid, content, tags)
        VALUES (new.fact_id, new.content, new.tags);
END;

CREATE TABLE IF NOT EXISTS memory_banks (
    bank_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_name  TEXT NOT NULL UNIQUE,
    vector     BLOB NOT NULL,
    dim        INTEGER NOT NULL,
    fact_count INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

_HELPFUL_DELTA = 0.05
_UNHELPFUL_DELTA = -0.10
_TRUST_MIN = 0.0
_TRUST_MAX = 1.0
_DEDUP_TRUST_BOOST = 0.02
_DEDUP_THRESHOLD = float(os.environ.get("HOLOGRAPHIC_DEDUP_THRESHOLD", "0.65"))

_RE_CAPITALIZED = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b')
_RE_DOUBLE_QUOTE = re.compile(r'"([^"]+)"')
_RE_SINGLE_QUOTE = re.compile(r"'([^']+)'")
_RE_AKA = re.compile(r'(\w+(?:\s+\w+)*)\s+(?:aka|also known as)\s+(\w+(?:\s+\w+)*)', re.IGNORECASE)


def _clamp_trust(value: float) -> float:
    return max(_TRUST_MIN, min(_TRUST_MAX, value))


class MemoryStore:
    def __init__(self, db_path: str, default_trust: float = 0.5, hrr_dim: int = 1024):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.default_trust = _clamp_trust(default_trust)
        self.hrr_dim = hrr_dim
        self._hrr_available = _HAS_NUMPY
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10.0)
        self._lock = threading.RLock()
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        self._conn.executescript(_SCHEMA)
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(facts)").fetchall()}
        if "hrr_vector" not in columns:
            self._conn.execute("ALTER TABLE facts ADD COLUMN hrr_vector BLOB")
        self._conn.commit()

    def _tokenize_set(self, text: str) -> set:
        """Tokenize text into a lowercase set, stripping punctuation."""
        return {t.strip(".,!?;:\"'()[]{}").lower() for t in text.split() if t.strip(".,!?;:\"'()[]{}")}

    def _jaccard(self, a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    def _merge_tags(self, existing: str, new: str) -> str:
        """Union of two comma-separated tag strings, deduped, order-preserving."""
        seen = {}
        for tag in (existing + "," + new).split(","):
            tag = tag.strip()
            if tag and tag not in seen:
                seen[tag] = True
        return ",".join(seen)

    def _find_near_duplicate(self, content: str, category: str = None) -> dict | None:
        """Search for a near-duplicate fact across ALL categories via FTS5 + Jaccard.
        A fact is the same fact regardless of which category bucket it's in.
        Returns the matching fact row dict (with 'jaccard' key), or None."""
        query_tokens = self._tokenize_set(content)
        if not query_tokens:
            return None
        # FTS5 search on the new content to get candidates — no category filter
        fts_query = " ".join(f'"{t}"' for t in content.split() if t)
        if not fts_query:
            return None
        sql = """
            SELECT f.fact_id, f.content, f.category, f.tags, f.trust_score
            FROM facts f
            JOIN facts_fts fts ON fts.rowid = f.fact_id
            WHERE facts_fts MATCH ?
            LIMIT 20
        """
        rows = self._conn.execute(sql, [fts_query]).fetchall()
        best = None
        best_score = 0.0
        for row in rows:
            existing_tokens = self._tokenize_set(row["content"])
            score = self._jaccard(query_tokens, existing_tokens)
            if score > best_score:
                best_score = score
                best = dict(row)
        if best is not None and best_score >= _DEDUP_THRESHOLD:
            best["jaccard"] = best_score
            return best
        return None

    def add_fact(self, content: str, category: str = "general", tags: str = "") -> dict:
        """Add a fact, with fuzzy cross-category dedup detection.
        Returns: {"fact_id", "was_duplicate", "duplicate_of", "merged_tags",
                  "jaccard", "existing_category", "category_mismatch"}."""
        with self._lock:
            content = content.strip()
            if not content:
                raise ValueError("content must not be empty")
            # Check for near-duplicate across ALL categories BEFORE attempting insert
            near_dup = self._find_near_duplicate(content, category=category)
            if near_dup is not None:
                existing_id = int(near_dup["fact_id"])
                existing_category = near_dup["category"]
                category_mismatch = existing_category != category
                # Merge tags: union of existing and new
                merged = self._merge_tags(near_dup["tags"], tags)
                tags_changed = merged != near_dup["tags"]
                # Small trust boost — we've seen this claim twice
                new_trust = _clamp_trust(near_dup["trust_score"] + _DEDUP_TRUST_BOOST)
                self._conn.execute(
                    "UPDATE facts SET tags = ?, trust_score = ?, updated_at = CURRENT_TIMESTAMP WHERE fact_id = ?",
                    (merged, new_trust, existing_id),
                )
                self._conn.commit()
                self._rebuild_bank(existing_category)
                return {
                    "fact_id": existing_id,
                    "was_duplicate": True,
                    "duplicate_of": existing_id,
                    "merged_tags": tags_changed,
                    "jaccard": near_dup["jaccard"],
                    "existing_category": existing_category,
                    "category_mismatch": category_mismatch,
                }
            # No near-dup found — proceed with insert
            try:
                cur = self._conn.execute(
                    "INSERT INTO facts (content, category, tags, trust_score) VALUES (?, ?, ?, ?)",
                    (content, category, tags, self.default_trust),
                )
                self._conn.commit()
                fact_id = cur.lastrowid
            except sqlite3.IntegrityError:
                # Exact string match (UNIQUE constraint) — still a dup
                row = self._conn.execute(
                    "SELECT fact_id, category FROM facts WHERE content = ?", (content,)
                ).fetchone()
                existing_id = int(row["fact_id"])
                existing_category = row["category"]
                return {
                    "fact_id": existing_id,
                    "was_duplicate": True,
                    "duplicate_of": existing_id,
                    "merged_tags": False,
                    "jaccard": 1.0,
                    "existing_category": existing_category,
                    "category_mismatch": existing_category != category,
                }
            for name in self._extract_entities(content):
                entity_id = self._resolve_entity(name)
                self._link_fact_entity(fact_id, entity_id)
            self._compute_hrr_vector(fact_id, content)
            self._rebuild_bank(category)
            return {
                "fact_id": fact_id,
                "was_duplicate": False,
                "duplicate_of": None,
                "merged_tags": False,
                "existing_category": None,
                "category_mismatch": False,
            }

    def search_facts(self, query: str, category=None, min_trust=0.3, limit=10):
        with self._lock:
            query = query.strip()
            if not query:
                return []
            # Escape FTS5 query: wrap each token in double quotes to prevent
            # column references and operator interpretation (e.g. "luca-edu")
            fts_query = " ".join(f'"{t}"' for t in query.split() if t)
            params = [fts_query, min_trust]
            category_clause = ""
            if category:
                category_clause = "AND f.category = ?"
                params.append(category)
            params.append(limit)
            sql = f"""
                SELECT f.fact_id, f.content, f.category, f.tags,
                       f.trust_score, f.retrieval_count, f.helpful_count,
                       f.created_at, f.updated_at
                FROM facts f
                JOIN facts_fts fts ON fts.rowid = f.fact_id
                WHERE facts_fts MATCH ? AND f.trust_score >= ? {category_clause}
                ORDER BY fts.rank, f.trust_score DESC LIMIT ?
            """
            rows = self._conn.execute(sql, params).fetchall()
            results = [dict(r) for r in rows]
            if results:
                ids = [r["fact_id"] for r in results]
                placeholders = ",".join("?" * len(ids))
                self._conn.execute(
                    f"UPDATE facts SET retrieval_count = retrieval_count + 1 WHERE fact_id IN ({placeholders})", ids
                )
                self._conn.commit()
            return results

    def update_fact(self, fact_id, content=None, trust_delta=None, tags=None, category=None):
        with self._lock:
            row = self._conn.execute("SELECT fact_id, trust_score FROM facts WHERE fact_id = ?", (fact_id,)).fetchone()
            if row is None:
                return False
            assignments = ["updated_at = CURRENT_TIMESTAMP"]
            params = []
            if content is not None:
                assignments.append("content = ?")
                params.append(content.strip())
            if tags is not None:
                assignments.append("tags = ?")
                params.append(tags)
            if category is not None:
                assignments.append("category = ?")
                params.append(category)
            if trust_delta is not None:
                new_trust = _clamp_trust(row["trust_score"] + trust_delta)
                assignments.append("trust_score = ?")
                params.append(new_trust)
            params.append(fact_id)
            self._conn.execute(f"UPDATE facts SET {', '.join(assignments)} WHERE fact_id = ?", params)
            self._conn.commit()
            if content is not None:
                self._conn.execute("DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,))
                for name in self._extract_entities(content):
                    entity_id = self._resolve_entity(name)
                    self._link_fact_entity(fact_id, entity_id)
                self._conn.commit()
                self._compute_hrr_vector(fact_id, content)
            cat = category or self._conn.execute("SELECT category FROM facts WHERE fact_id = ?", (fact_id,)).fetchone()["category"]
            self._rebuild_bank(cat)
            return True

    def remove_fact(self, fact_id):
        with self._lock:
            row = self._conn.execute("SELECT fact_id, category FROM facts WHERE fact_id = ?", (fact_id,)).fetchone()
            if row is None:
                return False
            self._conn.execute("DELETE FROM fact_entities WHERE fact_id = ?", (fact_id,))
            self._conn.execute("DELETE FROM facts WHERE fact_id = ?", (fact_id,))
            self._conn.commit()
            self._rebuild_bank(row["category"])
            return True

    def list_facts(self, category=None, min_trust=0.0, limit=50):
        with self._lock:
            params = [min_trust]
            category_clause = ""
            if category:
                category_clause = "AND category = ?"
                params.append(category)
            params.append(limit)
            sql = f"""
                SELECT fact_id, content, category, tags, trust_score,
                       retrieval_count, helpful_count, created_at, updated_at
                FROM facts WHERE trust_score >= ? {category_clause}
                ORDER BY trust_score DESC LIMIT ?
            """
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def record_feedback(self, fact_id, helpful):
        with self._lock:
            row = self._conn.execute(
                "SELECT fact_id, trust_score, helpful_count FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"fact_id {fact_id} not found")
            old_trust = row["trust_score"]
            delta = _HELPFUL_DELTA if helpful else _UNHELPFUL_DELTA
            new_trust = _clamp_trust(old_trust + delta)
            helpful_inc = 1 if helpful else 0
            self._conn.execute(
                "UPDATE facts SET trust_score = ?, helpful_count = helpful_count + ?, updated_at = CURRENT_TIMESTAMP WHERE fact_id = ?",
                (new_trust, helpful_inc, fact_id),
            )
            self._conn.commit()
            return {"fact_id": fact_id, "old_trust": old_trust, "new_trust": new_trust, "helpful_count": row["helpful_count"] + helpful_inc}

    def _extract_entities(self, text):
        seen = set()
        candidates = []
        def _add(name):
            stripped = name.strip()
            if stripped and stripped.lower() not in seen:
                seen.add(stripped.lower())
                candidates.append(stripped)
        for m in _RE_CAPITALIZED.finditer(text):
            _add(m.group(1))
        for m in _RE_DOUBLE_QUOTE.finditer(text):
            _add(m.group(1))
        for m in _RE_SINGLE_QUOTE.finditer(text):
            _add(m.group(1))
        for m in _RE_AKA.finditer(text):
            _add(m.group(1))
            _add(m.group(2))
        return candidates

    def _resolve_entity(self, name):
        row = self._conn.execute("SELECT entity_id FROM entities WHERE name LIKE ?", (name,)).fetchone()
        if row is not None:
            return int(row["entity_id"])
        alias_row = self._conn.execute(
            "SELECT entity_id FROM entities WHERE ',' || aliases || ',' LIKE '%,' || ? || ',%'", (name,)
        ).fetchone()
        if alias_row is not None:
            return int(alias_row["entity_id"])
        cur = self._conn.execute("INSERT INTO entities (name) VALUES (?)", (name,))
        self._conn.commit()
        return int(cur.lastrowid)

    def _link_fact_entity(self, fact_id, entity_id):
        self._conn.execute("INSERT OR IGNORE INTO fact_entities (fact_id, entity_id) VALUES (?, ?)", (fact_id, entity_id))
        self._conn.commit()

    def _compute_hrr_vector(self, fact_id, content):
        with self._lock:
            if not self._hrr_available:
                return
            rows = self._conn.execute(
                "SELECT e.name FROM entities e JOIN fact_entities fe ON fe.entity_id = e.entity_id WHERE fe.fact_id = ?",
                (fact_id,),
            ).fetchall()
            entities = [row["name"] for row in rows]
            vector = encode_fact(content, entities, self.hrr_dim)
            self._conn.execute("UPDATE facts SET hrr_vector = ? WHERE fact_id = ?", (phases_to_bytes(vector), fact_id))
            self._conn.commit()

    def _rebuild_bank(self, category):
        with self._lock:
            if not self._hrr_available:
                return
            bank_name = f"cat:{category}"
            rows = self._conn.execute(
                "SELECT hrr_vector FROM facts WHERE category = ? AND hrr_vector IS NOT NULL", (category,)
            ).fetchall()
            if not rows:
                self._conn.execute("DELETE FROM memory_banks WHERE bank_name = ?", (bank_name,))
                self._conn.commit()
                return
            vectors = [bytes_to_phases(row["hrr_vector"]) for row in rows]
            bank_vector = bundle(*vectors)
            self._conn.execute(
                """INSERT INTO memory_banks (bank_name, vector, dim, fact_count, updated_at)
                   VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(bank_name) DO UPDATE SET vector=excluded.vector, dim=excluded.dim,
                   fact_count=excluded.fact_count, updated_at=excluded.updated_at""",
                (bank_name, phases_to_bytes(bank_vector), self.hrr_dim, len(vectors)),
            )
            self._conn.commit()

    def close(self):
        self._conn.close()


# ---------------------------------------------------------------------------
# FactRetriever — inlined from retrieval.py, Hermes deps removed
# ---------------------------------------------------------------------------

class FactRetriever:
    def __init__(self, store, temporal_decay_half_life=0, fts_weight=0.4, jaccard_weight=0.3, hrr_weight=0.3, hrr_dim=1024):
        self.store = store
        self.half_life = temporal_decay_half_life
        self.hrr_dim = hrr_dim
        if hrr_weight > 0 and not _HAS_NUMPY:
            fts_weight = 0.6
            jaccard_weight = 0.4
            hrr_weight = 0.0
        self.fts_weight = fts_weight
        self.jaccard_weight = jaccard_weight
        self.hrr_weight = hrr_weight

    def _tokenize(self, text):
        return set(token.strip(".,!?;:\"'()[]{}").lower() for token in text.split() if token.strip(".,!?;:\"'()[]{}"))

    def _jaccard_similarity(self, a, b):
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    def _temporal_decay(self, timestamp):
        if not timestamp or self.half_life <= 0:
            return 1.0
        try:
            from datetime import datetime
            ts = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            age_days = (datetime.now(ts.tzinfo) - ts).days
            return 0.5 ** (age_days / self.half_life)
        except Exception:
            return 1.0

    def search(self, query, category=None, min_trust=0.3, limit=10):
        candidates = self.store.search_facts(query, category=category, min_trust=min_trust, limit=limit * 3)
        if not candidates:
            return []
        query_tokens = self._tokenize(query)
        scored = []
        for fact in candidates:
            content_tokens = self._tokenize(fact["content"])
            tag_tokens = self._tokenize(fact.get("tags", ""))
            all_tokens = content_tokens | tag_tokens
            jaccard = self._jaccard_similarity(query_tokens, all_tokens)
            fts_score = 1.0 / (1.0 + abs(fact.get("retrieval_count", 0)))  # approx rank
            if self.hrr_weight > 0 and _HAS_NUMPY:
                # Would need hrr_vector from raw query — skip for simplicity, use FTS fallback
                hrr_sim = 0.5
            else:
                hrr_sim = 0.5
            relevance = self.fts_weight * fts_score + self.jaccard_weight * jaccard + self.hrr_weight * hrr_sim
            score = relevance * fact["trust_score"]
            if self.half_life > 0:
                score *= self._temporal_decay(fact.get("updated_at") or fact.get("created_at"))
            fact["score"] = score
            scored.append(fact)
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    def probe(self, entity, category=None, limit=10):
        if not _HAS_NUMPY:
            return self.search(entity, category=category, limit=limit)
        conn = self.store._conn
        role_entity = encode_atom("__hrr_role_entity__", self.hrr_dim)
        entity_vec = encode_atom(entity.lower(), self.hrr_dim)
        probe_key = bind(entity_vec, role_entity)
        where = "WHERE hrr_vector IS NOT NULL"
        params = []
        if category:
            where += " AND category = ?"
            params.append(category)
        rows = conn.execute(
            f"SELECT fact_id, content, category, tags, trust_score, retrieval_count, helpful_count, created_at, updated_at, hrr_vector FROM facts {where}",
            params,
        ).fetchall()
        if not rows:
            return self.search(entity, category=category, limit=limit)
        scored = []
        for row in rows:
            fact = dict(row)
            fact_vec = bytes_to_phases(fact.pop("hrr_vector", None))
            if fact_vec is None:
                continue
            residual = unbind(fact_vec, probe_key)
            role_content = encode_atom("__hrr_role_content__", self.hrr_dim)
            content_vec = bind(encode_text(fact["content"], self.hrr_dim), role_content)
            sim = similarity(residual, content_vec)
            fact["score"] = (sim + 1.0) / 2.0 * fact["trust_score"]
            scored.append(fact)
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    def related(self, entity, category=None, limit=10):
        if not _HAS_NUMPY:
            return self.search(entity, category=category, limit=limit)
        conn = self.store._conn
        entity_vec = encode_atom(entity.lower(), self.hrr_dim)
        where = "WHERE hrr_vector IS NOT NULL"
        params = []
        if category:
            where += " AND category = ?"
            params.append(category)
        rows = conn.execute(
            f"SELECT fact_id, content, category, tags, trust_score, retrieval_count, helpful_count, created_at, updated_at, hrr_vector FROM facts {where}",
            params,
        ).fetchall()
        if not rows:
            return self.search(entity, category=category, limit=limit)
        role_entity = encode_atom("__hrr_role_entity__", self.hrr_dim)
        role_content = encode_atom("__hrr_role_content__", self.hrr_dim)
        scored = []
        for row in rows:
            fact = dict(row)
            fact_vec = bytes_to_phases(fact.pop("hrr_vector", None))
            if fact_vec is None:
                continue
            residual = unbind(fact_vec, entity_vec)
            entity_role_sim = similarity(residual, role_entity)
            content_role_sim = similarity(residual, role_content)
            best_sim = max(entity_role_sim, content_role_sim)
            fact["score"] = (best_sim + 1.0) / 2.0 * fact["trust_score"]
            scored.append(fact)
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    def reason(self, entities, category=None, limit=10):
        if not _HAS_NUMPY:
            results = []
            for e in entities:
                results.extend(self.search(e, category=category, limit=limit))
            seen = set()
            deduped = []
            for r in results:
                if r["fact_id"] not in seen:
                    seen.add(r["fact_id"])
                    deduped.append(r)
            return deduped[:limit]
        conn = self.store._conn
        role_entity = encode_atom("__hrr_role_entity__", self.hrr_dim)
        probe_keys = [bind(encode_atom(e.lower(), self.hrr_dim), role_entity) for e in entities]
        where = "WHERE hrr_vector IS NOT NULL"
        params = []
        if category:
            where += " AND category = ?"
            params.append(category)
        rows = conn.execute(
            f"SELECT fact_id, content, category, tags, trust_score, retrieval_count, helpful_count, created_at, updated_at, hrr_vector FROM facts {where}",
            params,
        ).fetchall()
        if not rows:
            return []
        role_content = encode_atom("__hrr_role_content__", self.hrr_dim)
        scored = []
        for row in rows:
            fact = dict(row)
            fact_vec = bytes_to_phases(fact.pop("hrr_vector", None))
            if fact_vec is None:
                continue
            scores = []
            for pk in probe_keys:
                residual = unbind(fact_vec, pk)
                content_vec = bind(encode_text(fact["content"], self.hrr_dim), role_content)
                scores.append((similarity(residual, content_vec) + 1.0) / 2.0)
            min_score = min(scores) if scores else 0.0
            fact["score"] = min_score * fact["trust_score"]
            scored.append(fact)
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    def contradict(self, category=None, limit=10):
        conn = self.store._conn
        where = ""
        params = []
        if category:
            where = "WHERE category = ?"
            params.append(category)
        rows = conn.execute(
            f"SELECT fact_id, content, category, tags, trust_score, retrieval_count, helpful_count, created_at, updated_at FROM facts {where} ORDER BY trust_score DESC LIMIT ?",
            params + [limit * 2],
        ).fetchall()
        if len(rows) < 2:
            return []
        facts = [dict(r) for r in rows]
        pairs = []
        for i in range(len(facts)):
            for j in range(i + 1, len(facts)):
                tokens_a = self._tokenize(facts[i]["content"])
                tokens_b = self._tokenize(facts[j]["content"])
                jaccard = self._jaccard_similarity(tokens_a, tokens_b)
                if jaccard > 0.3:
                    pairs.append({
                        "fact_a": facts[i],
                        "fact_b": facts[j],
                        "overlap": jaccard,
                    })
        pairs.sort(key=lambda x: x["overlap"], reverse=True)
        return pairs[:limit]


# ---------------------------------------------------------------------------
# MCP Server — raw JSON-RPC 2.0 over stdio
# ---------------------------------------------------------------------------

def _get_config():
    home = Path(os.environ.get("HOME", str(Path.home())))
    default_db = home / ".local" / "share" / "devin" / "holographic" / "memory.db"
    return {
        "db_path": os.environ.get("HOLOGRAPHIC_DB_PATH", str(default_db)),
        "default_trust": float(os.environ.get("HOLOGRAPHIC_DEFAULT_TRUST", "0.5")),
        "min_trust": float(os.environ.get("HOLOGRAPHIC_MIN_TRUST", "0.3")),
        "hrr_dim": int(os.environ.get("HOLOGRAPHIC_HRR_DIM", "1024")),
    }


_config = _get_config()
_store = MemoryStore(
    db_path=_config["db_path"],
    default_trust=_config["default_trust"],
    hrr_dim=_config["hrr_dim"],
)
_retriever = FactRetriever(store=_store, hrr_dim=_config["hrr_dim"])

FACT_STORE_SCHEMA = {
    "name": "fact_store",
    "description": (
        "Deep structured memory with algebraic reasoning. "
        "Use for storing and recalling facts about people, projects, preferences, decisions.\n\n"
        "ACTIONS:\n"
        "• add — Store a fact. Auto-detects near-duplicates (Jaccard >= 0.65) and merges tags instead of inserting. Returns was_duplicate flag.\n"
        "• search — Keyword lookup.\n"
        "• probe — Entity recall: ALL facts about a person/thing.\n"
        "• related — What connects to an entity? Structural adjacency.\n"
        "• reason — Compositional: facts connected to MULTIPLE entities simultaneously.\n"
        "• contradict — Memory hygiene: find facts making conflicting claims.\n"
        "• update/remove/list — CRUD operations."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "search", "probe", "related", "reason", "contradict", "update", "remove", "list"]},
            "content": {"type": "string", "description": "Fact content (required for 'add')."},
            "query": {"type": "string", "description": "Search query (required for 'search')."},
            "entity": {"type": "string", "description": "Entity name for 'probe'/'related'."},
            "entities": {"type": "array", "items": {"type": "string"}, "description": "Entity names for 'reason'."},
            "fact_id": {"type": "integer", "description": "Fact ID for 'update'/'remove'."},
            "category": {"type": "string", "enum": ["user_pref", "project", "tool", "general", "procedural"], "description": "Conventional bucket. 'procedural' = learned tool sequences (trigger → tools → success)."},
            "tags": {"type": "string", "description": "Comma-separated tags."},
            "trust_delta": {"type": "number", "description": "Trust adjustment for 'update'."},
            "min_trust": {"type": "number", "description": "Minimum trust filter (default: 0.3)."},
            "limit": {"type": "integer", "description": "Max results (default: 10)."},
        },
        "required": ["action"],
    },
}

FACT_FEEDBACK_SCHEMA = {
    "name": "fact_feedback",
    "description": "Rate a fact after using it. Mark 'helpful' if accurate, 'unhelpful' if outdated. Trains trust scores.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["helpful", "unhelpful"]},
            "fact_id": {"type": "integer", "description": "The fact ID to rate."},
        },
        "required": ["action", "fact_id"],
    },
}

TOOLS = [FACT_STORE_SCHEMA, FACT_FEEDBACK_SCHEMA]


def handle_fact_store(args: dict) -> str:
    action = args["action"]
    if action == "add":
        result = _store.add_fact(args["content"], category=args.get("category", "general"), tags=args.get("tags", ""))
        if result["was_duplicate"]:
            status = "duplicate"
            msg = f"Near-duplicate of fact #{result['duplicate_of']} (jaccard={result.get('jaccard', 1.0):.2f})"
            if result["merged_tags"]:
                msg += " — tags merged"
            if result.get("category_mismatch"):
                msg += f" — category mismatch: tried {args.get('category', 'general')} but existing is {result['existing_category']}"
                msg += ". Use 'update' to change category if the new one is correct."
            else:
                msg += ". Use 'update' action to extend content if needed."
            return json.dumps({**result, "status": status, "message": msg})
        return json.dumps({**result, "status": "added"})
    elif action == "search":
        results = _retriever.search(args["query"], category=args.get("category"), min_trust=float(args.get("min_trust", _config["min_trust"])), limit=int(args.get("limit", 10)))
        return json.dumps({"results": results, "count": len(results)})
    elif action == "probe":
        results = _retriever.probe(args["entity"], category=args.get("category"), limit=int(args.get("limit", 10)))
        return json.dumps({"results": results, "count": len(results)})
    elif action == "related":
        results = _retriever.related(args["entity"], category=args.get("category"), limit=int(args.get("limit", 10)))
        return json.dumps({"results": results, "count": len(results)})
    elif action == "reason":
        entities = args.get("entities", [])
        if not entities:
            return json.dumps({"error": "reason requires 'entities' list"})
        results = _retriever.reason(entities, category=args.get("category"), limit=int(args.get("limit", 10)))
        return json.dumps({"results": results, "count": len(results)})
    elif action == "contradict":
        results = _retriever.contradict(category=args.get("category"), limit=int(args.get("limit", 10)))
        return json.dumps({"results": results, "count": len(results)})
    elif action == "update":
        updated = _store.update_fact(int(args["fact_id"]), content=args.get("content"), trust_delta=float(args["trust_delta"]) if "trust_delta" in args else None, tags=args.get("tags"), category=args.get("category"))
        return json.dumps({"updated": updated})
    elif action == "remove":
        removed = _store.remove_fact(int(args["fact_id"]))
        return json.dumps({"removed": removed})
    elif action == "list":
        facts = _store.list_facts(category=args.get("category"), min_trust=float(args.get("min_trust", 0.0)), limit=int(args.get("limit", 50)))
        return json.dumps({"facts": facts, "count": len(facts)})
    else:
        return json.dumps({"error": f"Unknown action: {action}"})


def handle_fact_feedback(args: dict) -> str:
    fact_id = int(args["fact_id"])
    helpful = args["action"] == "helpful"
    result = _store.record_feedback(fact_id, helpful=helpful)
    return json.dumps(result)


def handle_tool_call(name: str, args: dict) -> str:
    if name == "fact_store":
        return handle_fact_store(args)
    elif name == "fact_feedback":
        return handle_fact_feedback(args)
    return json.dumps({"error": f"Unknown tool: {name}"})


def read_message() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return None
    return json.loads(line)


def write_message(msg: dict):
    body = json.dumps(msg)
    sys.stdout.write(body + "\n")
    sys.stdout.flush()


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(name)s: %(message)s")
    logger.info("Holographic MCP server starting (numpy=%s)", _HAS_NUMPY)
    while True:
        try:
            msg = read_message()
            if msg is None:
                break
            method = msg.get("method", "")
            msg_id = msg.get("id")
            params = msg.get("params", {})

            if method == "initialize":
                write_message({
                    "jsonrpc": "2.0", "id": msg_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "holographic-mcp", "version": "1.1.1"},
                    },
                })
            elif method == "notifications/initialized":
                pass  # notification, no response
            elif method == "tools/list":
                write_message({
                    "jsonrpc": "2.0", "id": msg_id,
                    "result": {"tools": TOOLS},
                })
            elif method == "tools/call":
                tool_name = params.get("name", "")
                tool_args = params.get("arguments", {})
                try:
                    result_text = handle_tool_call(tool_name, tool_args)
                    write_message({
                        "jsonrpc": "2.0", "id": msg_id,
                        "result": {"content": [{"type": "text", "text": result_text}]},
                    })
                except Exception as e:
                    write_message({
                        "jsonrpc": "2.0", "id": msg_id,
                        "result": {"content": [{"type": "text", "text": json.dumps({"error": str(e)})}], "isError": True},
                    })
            else:
                if msg_id is not None:
                    write_message({
                        "jsonrpc": "2.0", "id": msg_id,
                        "error": {"code": -32601, "message": f"Method not found: {method}"},
                    })
        except Exception as e:
            logger.error("Error handling message: %s", e)
    _store.close()
    logger.info("Holographic MCP server shutting down")


if __name__ == "__main__":
    main()
