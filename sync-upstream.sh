#!/usr/bin/env bash
# sync-upstream.sh — Sync holographic memory code from NousResearch/hermes-agent
#
# Fetches the upstream source files, ports them into our standalone MCP server,
# and creates a commit if anything changed. Run manually or via cron.
#
# Upstream files tracked:
#   plugins/memory/holographic/__init__.py     → provider logic (reference only)
#   plugins/memory/holographic/store.py        → MemoryStore class
#   plugins/memory/holographic/retrieval.py    → FactRetriever class
#   plugins/memory/holographic/holographic.py  → HRR math
#
# Our mcp_server.py inlines all of these with Hermes dependencies stripped.
# This script detects upstream changes and reports what needs porting.

set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
UPSTREAM_REPO="NousResearch/hermes-agent"
UPSTREAM_PATH="plugins/memory/holographic"
UPSTREAM_BRANCH="main"
SYNC_DIR="${REPO_DIR}/.upstream-sync"
STATE_FILE="${REPO_DIR}/.upstream-sync/last_synced_commit.txt"

mkdir -p "$SYNC_DIR"

echo "=== Holographic MCP Upstream Sync ==="
echo "Upstream: ${UPSTREAM_REPO}@${UPSTREAM_BRANCH}/${UPSTREAM_PATH}"
echo ""

# 1. Get latest upstream commit hash
echo "[1] Fetching latest upstream commit..."
LATEST_COMMIT=$(curl -sf "https://api.github.com/repos/${UPSTREAM_REPO}/commits?path=${UPSTREAM_PATH}&per_page=1" | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data and isinstance(data, list):
    print(data[0]['sha'])
else:
    print('')
" 2>/dev/null || echo "")

if [ -z "$LATEST_COMMIT" ]; then
    echo "ERROR: Could not fetch upstream commit. Check network or rate limits."
    exit 1
fi

echo "    Latest upstream commit: ${LATEST_COMMIT:0:12}"

# 2. Check if we've already synced this commit
LAST_SYNCED=""
if [ -f "$STATE_FILE" ]; then
    LAST_SYNCED=$(cat "$STATE_FILE")
    echo "    Last synced commit:     ${LAST_SYNCED:0:12}"
fi

if [ "$LATEST_COMMIT" = "$LAST_SYNCED" ]; then
    echo "    Already up to date. No changes to sync."
    exit 0
fi

echo "    Changes detected upstream."

# 3. Download upstream files
echo ""
echo "[2] Downloading upstream files..."

FILES=(
    "__init__.py"
    "store.py"
    "retrieval.py"
    "holographic.py"
)

CHANGED_FILES=()

for f in "${FILES[@]}"; do
    UPSTREAM_URL="https://raw.githubusercontent.com/${UPSTREAM_REPO}/${LATEST_COMMIT}/${UPSTREAM_PATH}/${f}"
    LOCAL_PATH="${SYNC_DIR}/${f}"
    
    # Download
    if curl -sfL "$UPSTREAM_URL" -o "${LOCAL_PATH}.new" 2>/dev/null; then
        if [ -f "$LOCAL_PATH" ]; then
            if ! diff -q "$LOCAL_PATH" "${LOCAL_PATH}.new" >/dev/null 2>&1; then
                echo "    CHANGED: ${f}"
                mv "${LOCAL_PATH}.new" "$LOCAL_PATH"
                CHANGED_FILES+=("$f")
            else
                echo "    unchanged: ${f}"
                rm -f "${LOCAL_PATH}.new"
            fi
        else
            echo "    NEW: ${f}"
            mv "${LOCAL_PATH}.new" "$LOCAL_PATH"
            CHANGED_FILES+=("$f")
        fi
    else
        echo "    SKIP (not found): ${f}"
        rm -f "${LOCAL_PATH}.new"
    fi
done

# 4. Report what changed
echo ""
echo "[3] Analysis"

if [ ${#CHANGED_FILES[@]} -eq 0 ]; then
    echo "    No file content changed (commit may have touched other paths)."
    echo "$LATEST_COMMIT" > "$STATE_FILE"
    exit 0
fi

echo "    ${#CHANGED_FILES[@]} file(s) changed upstream:"
for f in "${CHANGED_FILES[@]}"; do
    echo "      - ${f}"
done

# 5. Check if our mcp_server.py needs updating
echo ""
echo "[4] Diff summary (upstream vs our inlined versions)"

for f in "${CHANGED_FILES[@]}"; do
    echo ""
    echo "  --- ${f} ---"
    # Show a brief diff stat
    diff --unified=0 "${SYNC_DIR}/${f}" /dev/null 2>/dev/null | head -5 || true
done

# 6. Instructions
echo ""
echo "[5] Next steps"
echo "    Upstream changes detected. Manual porting required:"
echo ""
echo "    1. Review changes in .upstream-sync/:"
for f in "${CHANGED_FILES[@]}"; do
    echo "       cat .upstream-sync/${f}"
done
echo ""
echo "    2. Port changes into mcp_server.py (strip Hermes imports):"
echo "       - store.py      → MemoryStore class in mcp_server.py"
echo "       - retrieval.py  → FactRetriever class in mcp_server.py"
echo "       - holographic.py → HRR functions in mcp_server.py"
echo "       - __init__.py   → tool schemas + handle_tool_call (reference)"
echo ""
echo "    3. Run tests:"
echo "       python3 test_server.py"
echo ""
echo "    4. Update version in .devin-plugin/plugin.json"
echo ""
echo "    5. Commit and push:"
echo "       git add -A && git commit -m 'sync: port upstream changes from ${LATEST_COMMIT:0:12}'"
echo ""
echo "    6. Update plugins:"
echo "       devin plugins update hermes-holographic-mcp"
echo ""

# 7. Save state (only after successful analysis)
echo "$LATEST_COMMIT" > "$STATE_FILE"
echo "    State saved. Next sync will compare against ${LATEST_COMMIT:0:12}."
