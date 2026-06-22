#!/usr/bin/env bash
# Cron wrapper for upstream sync — runs sync-upstream.sh and logs output.
# Scheduled via termux-job-scheduler (weekly).

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="${REPO_DIR}/.upstream-sync/sync.log"
SCRIPT="${REPO_DIR}/sync-upstream.sh"

mkdir -p "${REPO_DIR}/.upstream-sync"

echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" >> "$LOG_FILE"
"$SCRIPT" >> "$LOG_FILE" 2>&1
echo "" >> "$LOG_FILE"
