#!/usr/bin/env bash
# setup.sh — Install the holographic memory plumbing into Devin config.
#
# This script is idempotent — safe to run multiple times. It:
#   1. Merges the MCP server config into ~/.config/devin/config.json
#   2. Merges the hooks config into ~/.config/devin/config.json
#   3. Merges the permissions into ~/.config/devin/config.json
#   4. Injects memory rules into ~/.config/devin/AGENTS.md (between markers)
#
# Usage: ./setup.sh
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
DEVIN_CONFIG="${HOME}/.config/devin/config.json"
DEVIN_AGENTS="${HOME}/.config/devin/AGENTS.md"
SNIPPET="${REPO_DIR}/config/devin-config-snippet.json"
RULES="${REPO_DIR}/config/agents-memory-rules.md"

MARKER_BEGIN="# >>> HOLOGRAPHIC MEMORY RULES BEGIN >>>"
MARKER_END="# <<< HOLOGRAPHIC MEMORY RULES END <<<"

echo "=== Holographic Memory Setup ==="
echo ""

# ---------------------------------------------------------------------------
# 1. Merge config.json (MCP server + hooks + permissions)
# ---------------------------------------------------------------------------
echo "[1] Merging Devin config.json..."

mkdir -p "$(dirname "$DEVIN_CONFIG")"

python3 - "$DEVIN_CONFIG" "$SNIPPET" <<'PYEOF'
import json, sys, os

config_path, snippet_path = sys.argv[1], sys.argv[2]

# Load existing config (or create empty)
if os.path.exists(config_path):
    with open(config_path) as f:
        config = json.load(f)
else:
    config = {"version": 1}

# Load snippet
with open(snippet_path) as f:
    snippet = json.load(f)

changed = False

# Merge mcpServers
config.setdefault("mcpServers", {})
for name, cfg in snippet.get("mcpServers", {}).items():
    if name not in config["mcpServers"]:
        config["mcpServers"][name] = cfg
        print(f"  + mcpServers.{name} added")
        changed = True
    else:
        print(f"  = mcpServers.{name} already exists")

# Merge permissions
config.setdefault("permissions", {})
config["permissions"].setdefault("allow", [])
for perm in snippet.get("permissions", {}).get("allow", []):
    if perm not in config["permissions"]["allow"]:
        config["permissions"]["allow"].append(perm)
        print(f"  + permissions.allow: {perm}")
        changed = True
    else:
        print(f"  = permissions.allow: {perm} already exists")

# Merge hooks
config.setdefault("hooks", {})
for event, hook_list in snippet.get("hooks", {}).items():
    if event not in config["hooks"]:
        config["hooks"][event] = hook_list
        print(f"  + hooks.{event} added ({len(hook_list)} hook(s))")
        changed = True
    else:
        # Check if our hooks are already there by comparing commands
        existing_cmds = set()
        for h in config["hooks"][event]:
            for hook in h.get("hooks", []):
                existing_cmds.add(hook.get("command", ""))
        new_hooks = []
        for h in hook_list:
            for hook in h.get("hooks", []):
                if hook.get("command", "") not in existing_cmds:
                    new_hooks.append(h)
        if new_hooks:
            config["hooks"][event].extend(new_hooks)
            print(f"  + hooks.{event} added {len(new_hooks)} new hook(s)")
            changed = True
        else:
            print(f"  = hooks.{event} already configured")

if changed:
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    print(f"  → Written to {config_path}")
else:
    print(f"  → No changes needed")
PYEOF

echo ""

# ---------------------------------------------------------------------------
# 2. Inject AGENTS.md memory rules (between markers)
# ---------------------------------------------------------------------------
echo "[2] Injecting memory rules into AGENTS.md..."

mkdir -p "$(dirname "$DEVIN_AGENTS")"

# Read the rules file
RULES_CONTENT=$(cat "$RULES")

# Check if markers already exist
if [ -f "$DEVIN_AGENTS" ] && grep -q "$MARKER_BEGIN" "$DEVIN_AGENTS" 2>/dev/null; then
    # Replace content between markers
    echo "  = Markers found, replacing content..."
    python3 - "$DEVIN_AGENTS" "$MARKER_BEGIN" "$MARKER_END" "$RULES_CONTENT" <<'PYEOF'
import sys

agents_path, marker_begin, marker_end, new_content = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

with open(agents_path) as f:
    content = f.read()

# Find and replace between markers
begin_idx = content.index(marker_begin)
end_idx = content.index(marker_end) + len(marker_end)

new_section = f"{marker_begin}\n\n{new_content}\n\n{marker_end}"
new_full = content[:begin_idx] + new_section + content[end_idx:]

with open(agents_path, "w") as f:
    f.write(new_full)

print(f"  → Updated memory rules in {agents_path}")
PYEOF
else
    # Append with markers
    echo "  + Markers not found, appending..."
    {
        echo ""
        echo "$MARKER_BEGIN"
        echo ""
        echo "$RULES_CONTENT"
        echo ""
        echo "$MARKER_END"
        echo ""
    } >> "$DEVIN_AGENTS"
    echo "  → Appended memory rules to $DEVIN_AGENTS"
fi

echo ""

# ---------------------------------------------------------------------------
# 3. Verify
# ---------------------------------------------------------------------------
echo "[3] Verification..."

python3 -c "import json; json.load(open('$DEVIN_CONFIG')); print('  ✓ config.json valid')" 2>&1

if grep -q "$MARKER_BEGIN" "$DEVIN_AGENTS" 2>/dev/null; then
    echo "  ✓ AGENTS.md has memory rules"
else
    echo "  ✗ AGENTS.md missing memory rules"
fi

if python3 -c "import numpy" 2>/dev/null; then
    echo "  ✓ numpy available (HRR enabled)"
else
    echo "  ⚠ numpy not installed (FTS5-only fallback — no HRR algebra)"
    echo "    Install with: pkg install python-numpy (Termux) or pip install numpy"
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Restart your Devin session for changes to take effect."
echo ""
echo "Verify with:"
echo "  /hooks                    — check hooks are loaded"
echo "  mcp__holographic__*       — check MCP tools are available"
echo "  fact_store(action='list') — check memory is working"
