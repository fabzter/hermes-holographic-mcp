# hermes-holographic-mcp

Holographic memory MCP server for Devin CLI — SQLite + FTS5 + HRR compositional retrieval, trust scoring, entity resolution. Ported from [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent).

## What it does

Gives Devin persistent, structured memory across sessions:

- **fact_store** — 9 actions: add, search, probe, related, reason, contradict, update, remove, list
- **fact_feedback** — rate facts helpful/unhelpful to train trust scores
- **HRR algebra** — compositional queries (probe entities, reason across multiple entities, detect structural relationships)
- **Trust scoring** — asymmetric feedback (+0.05 helpful / -0.10 unhelpful), clamped to [0, 1]
- **Entity resolution** — extracts entities from fact content, links them, supports alias resolution
- **Contradiction detection** — finds fact pairs with high token overlap

All data is local — SQLite at `~/.local/share/devin/holographic/memory.db`. No cloud, no sync, no limits.

## Requirements

- Python 3.10+
- SQLite (usually built-in)
- numpy (optional — enables HRR compositional queries; without it, falls back to FTS5-only)

Install numpy on Termux: `pkg install python-numpy`
Install numpy elsewhere: `pip install numpy`

## Install

### 1. Install the plugin

```bash
devin plugins install fabzter/hermes-holographic-mcp
```

### 2. Run setup (wires MCP server + hooks + AGENTS.md rules)

```bash
~/hermes-holographic-mcp/setup.sh
```

This is idempotent — safe to run multiple times. It:
- Merges the MCP server config into `~/.config/devin/config.json`
- Merges the hooks config (SessionStart, UserPromptSubmit, Stop, PostToolUse)
- Injects memory rules into `~/.config/devin/AGENTS.md` (between markers)
- Verifies everything is valid

### 3. Restart your Devin session

The MCP server and hooks load at session start.

## How memory works

### Automatic (hooks)

| Hook | When | What it does |
|------|------|-------------|
| SessionStart | Session begins | Injects fact count + top facts + unresolved contradictions |
| UserPromptSubmit | User sends message | FTS5-searches DB with prompt keywords, injects relevant facts. Detects correction signals ("actually", "no longer", "switched") and instructs agent to UPDATE/DELETE instead of ADD |
| PostToolUse | After fact_store(add) | Runs contradiction detection, warns about conflicts |
| Stop | Agent tries to stop | Reminds to store new facts, update outdated ones, delete wrong ones, rate used facts |

### On-demand (MCP tools)

The agent calls `fact_store` and `fact_feedback` based on AGENTS.md rules:

- **RETRIEVE** before answering questions about the user, projects, or tools
- **STORE** when user states preferences, decisions, or facts worth remembering
- **UPDATE** when a fact changes (search first, update existing — don't duplicate)
- **DELETE** when a fact is wrong, outdated, or replaced
- **RATE** after using a retrieved fact (helpful/unhelpful trains trust)
- **CONFLICT** check after adds, at session start, or when something feels off

## Upstream sync

The holographic code is ported from `NousResearch/hermes-agent/plugins/memory/holographic/`. To check for upstream changes:

```bash
./sync-upstream.sh
```

This downloads the latest upstream files to `.upstream-sync/` and reports what changed. Manual porting into `mcp_server.py` is required (strip Hermes imports).

Weekly auto-check via `termux-job-scheduler` (job 1001):
```bash
termux-job-scheduler --pending    # check status
termux-job-scheduler --cancel 1001  # cancel
```

## Testing

```bash
python3 test_server.py          # 36 unit tests
python3 test_hrr_behavior.py    # 13 HRR behavior tests (requires numpy)
```

## Repository structure

```
hermes-holographic-mcp/
├── .devin-plugin/
│   └── plugin.json             # Devin plugin manifest
├── bin/
│   └── holographic-mcp         # Wrapper script (finds latest plugin version)
├── config/
│   ├── devin-config-snippet.json  # MCP server + hooks + permissions config
│   └── agents-memory-rules.md     # AGENTS.md memory rules (between markers)
├── skills/
│   └── holographic-memory/
│       └── SKILL.md            # Devin skill description
├── mcp_server.py               # MCP server (SQLite + FTS5 + HRR)
├── hook.py                     # Hook script (prefetch, correction detect, contradictions)
├── setup.sh                    # Idempotent setup (wires everything into Devin config)
├── sync-upstream.sh            # Sync from NousResearch/hermes-agent
├── sync-cron.sh                # Cron wrapper for termux-job-scheduler
├── test_server.py              # Unit tests (36)
├── test_hrr_behavior.py        # HRR behavior tests (13)
└── .upstream-sync/             # Cached upstream source (committed for diff reference)
```

## License

MIT
