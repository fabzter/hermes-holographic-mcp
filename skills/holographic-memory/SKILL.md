---
name: holographic-memory
description: Deep structured memory with algebraic reasoning. Use fact_store to add, search, probe, reason, and manage facts with trust scoring. Use fact_feedback to rate facts. Use when asked to "remember this", "store a fact", "what do you know about", "search memory", "recall", or when you need persistent cross-session knowledge about people, projects, preferences, or decisions.
---

# Holographic Memory

This skill provides access to the Holographic memory MCP server — a local SQLite-backed fact store with FTS5 full-text search, trust scoring, entity resolution, and HRR (Holographic Reduced Representations) for compositional algebraic queries.

## Tools

### `fact_store` — 9 actions

| Action | Description | Required args | Optional args |
|--------|-------------|---------------|---------------|
| `add` | Store a fact (auto-dedup) | `content` | `category`, `tags` |
| `search` | Keyword lookup | `query` | `category`, `min_trust`, `limit` |
| `probe` | ALL facts about an entity | `entity` | `category`, `limit` |
| `related` | Structural adjacency to entity | `entity` | `category`, `limit` |
| `reason` | Facts connected to MULTIPLE entities | `entities` (array) | `category`, `limit` |
| `contradict` | Find conflicting facts | — | `category`, `limit` |
| `update` | Modify a fact | `fact_id` | `content`, `trust_delta`, `tags`, `category` |
| `remove` | Delete a fact | `fact_id` | — |
| `list` | Browse facts | — | `category`, `min_trust`, `limit` |

### `fact_feedback` — rate facts

| Action | Description | Required args |
|--------|-------------|---------------|
| `helpful` | Trust +0.05 | `fact_id` |
| `unhelpful` | Trust -0.10 | `fact_id` |

## Categories

- `user_pref` — user preferences
- `project` — project facts
- `tool` — tool/tech facts
- `procedural` — learned tool sequences (trigger → tool calls → success criteria)
- `skill_refinement` — candidates from the skill refinement loop (written by devin-skill-auto-create)
- `general` — everything else

## Usage patterns

**Before answering questions about the user, ALWAYS probe or search first.**

```
fact_store(action="probe", entity="Fabzter")
fact_store(action="search", query="deploy process")
fact_store(action="reason", entities=["Fabzter", "backend"])
```

**Store facts the user would expect you to remember:**

```
fact_store(action="add", content="User prefers Python 3.13", category="user_pref")
fact_store(action="add", content="Project uses DASHSCOPE for LLM inference", category="project", tags="llm,alibaba")
```

## Auto-dedup on add

The `add` action automatically detects near-duplicates **across all categories** before inserting. A fact is the same fact regardless of which category bucket it's filed in. Dedup uses FTS5 to find candidates, then computes Jaccard similarity on token sets. If similarity >= 0.65 (configurable via `HOLOGRAPHIC_DEDUP_THRESHOLD` env var):

- **No new fact is inserted** — the existing fact is kept
- **Tags are merged** (union of existing + new tags)
- **Trust is boosted** by +0.02 (seen twice = slightly more credible)
- **`updated_at` is touched**
- The response includes `was_duplicate: true`, `duplicate_of: <id>`, `jaccard: <score>`, `existing_category`, `category_mismatch`

### When you get a `was_duplicate: true` response

1. **If `category_mismatch: true`** → you tried to file the fact under a different category than the existing one. Decide which category is correct and use `update` to change it if needed:
   ```
   fact_store(action="update", fact_id=<duplicate_of>, category="<correct_category>")
   ```
2. **If the new fact had MORE information** than the existing one → use `update` to extend the content:
   ```
   fact_store(action="update", fact_id=<duplicate_of>, content="<richer version>")
   ```
3. **If it was a true duplicate** → no action needed. Tags were merged, trust boosted.
4. **If it was a different fact that happened to share many tokens** → lower the threshold by setting `HOLOGRAPHIC_DEDUP_THRESHOLD=0.8` or rephrase your new fact to be more distinct.

### When to use `update` vs `add`

- **`add`** = new fact. Let dedup catch accidents.
- **`update`** = you already know the fact_id and want to change content/tags/trust.
- **Don't search-then-add** as a manual dedup dance — just `add` and let the system tell you if it was a dup.

**Rate facts after using them to train trust scores:**

```
fact_feedback(action="helpful", fact_id=3)
fact_feedback(action="unhelpful", fact_id=7)
```

## Storage

- Database: `~/.local/share/devin/holographic/memory.db` (SQLite)
- All data is local. No cloud, no sync, no limits.
- numpy is optional — enables HRR compositional queries (probe/related/reason). Without numpy, falls back to FTS5 keyword search.
