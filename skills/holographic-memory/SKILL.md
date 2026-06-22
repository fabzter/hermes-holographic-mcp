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
| `add` | Store a fact | `content` | `category`, `tags` |
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

**Rate facts after using them to train trust scores:**

```
fact_feedback(action="helpful", fact_id=3)
fact_feedback(action="unhelpful", fact_id=7)
```

## Storage

- Database: `~/.local/share/devin/holographic/memory.db` (SQLite)
- All data is local. No cloud, no sync, no limits.
- numpy is optional — enables HRR compositional queries (probe/related/reason). Without numpy, falls back to FTS5 keyword search.
