#!/usr/bin/env python3
"""Holographic memory hook for Devin.

Handles SessionStart, UserPromptSubmit, Stop, PostToolUse events.
Queries the SQLite memory DB directly and outputs JSON with add_context
to inject relevant facts, detect contradictions, and remind the agent
to update/delete outdated facts.

Replaces Hermes' prefetch(), system_prompt_block(), on_session_end(),
and on_memory_write() hooks.
"""
import sys, os, json, sqlite3, re
from pathlib import Path

DB_PATH = os.environ.get(
    "HOLOGRAPHIC_DB_PATH",
    str(Path.home() / ".local" / "share" / "devin" / "holographic" / "memory.db"),
)

MIN_TRUST = float(os.environ.get("HOLOGRAPHIC_MIN_TRUST", "0.3"))
MAX_FACTS_INJECT = int(os.environ.get("HOLOGRAPHIC_MAX_INJECT", "5"))

# Phrases that signal the user is correcting/updating/changing something
# When these appear alongside a matching fact, the agent should UPDATE or DELETE
CORRECTION_SIGNALS = [
    "actually", "no longer", "not anymore", "switched", "changed",
    "updated", "migrated", "moved", "replaced", "deprecated",
    "wrong", "incorrect", "outdated", "forget that", "disregard",
    "that's not right", "that's old", "we don't use", "we stopped",
    "we removed", "we dropped", "I don't prefer", "I no longer",
    "I switched", "I changed", "I updated", "I replaced",
    "instead of", "rather than", "not that", "correct that",
    "fix that", "delete that", "remove that",
]


def get_db():
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def count_facts(conn):
    try:
        return conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    except Exception:
        return 0


def top_facts(conn, limit=5):
    try:
        rows = conn.execute(
            "SELECT fact_id, content, category, trust_score FROM facts ORDER BY trust_score DESC, updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def search_facts(conn, query, limit=5):
    try:
        stop_words = {
            "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
            "have", "has", "had", "do", "does", "did", "will", "would", "could",
            "should", "may", "might", "must", "can", "what", "who", "when", "where",
            "why", "how", "about", "for", "with", "from", "into", "onto", "and",
            "or", "but", "not", "no", "yes", "this", "that", "these", "those",
            "it", "its", "they", "them", "their", "we", "us", "our", "you", "your",
            "he", "she", "him", "her", "his", "hers", "i", "me", "my", "of", "to",
            "in", "on", "at", "by", "as", "so", "if", "than", "then", "too",
            "very", "just", "also", "only", "some", "any", "all", "each", "every",
            "know", "tell", "show", "give", "get", "make", "use", "using", "like",
            "actually", "no", "longer", "anymore", "switched", "changed",
            "updated", "wrong", "incorrect", "outdated", "forget",
        }
        tokens = [
            t.strip(".,!?;:\"'()[]{}")
            for t in re.split(r"\s+", query.strip())
            if t and len(t) > 2 and t.lower() not in stop_words
        ]
        if not tokens:
            return []
        fts_query = " OR ".join(f'"{t}"' for t in tokens)
        rows = conn.execute(
            """SELECT f.fact_id, f.content, f.category, f.trust_score
               FROM facts f
               JOIN facts_fts fts ON fts.rowid = f.fact_id
               WHERE facts_fts MATCH ? AND f.trust_score >= ?
               ORDER BY fts.rank, f.trust_score DESC LIMIT ?""",
            (fts_query, MIN_TRUST, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def find_contradictions(conn, limit=5):
    """Run the contradict algorithm: find fact pairs with high token overlap."""
    try:
        rows = conn.execute(
            "SELECT fact_id, content, category, trust_score FROM facts ORDER BY trust_score DESC LIMIT ?",
            (limit * 3,),
        ).fetchall()
        if len(rows) < 2:
            return []
        facts = [dict(r) for r in rows]
        pairs = []
        for i in range(len(facts)):
            for j in range(i + 1, len(facts)):
                tokens_a = set(facts[i]["content"].lower().split())
                tokens_b = set(facts[j]["content"].lower().split())
                if not tokens_a or not tokens_b:
                    continue
                jaccard = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
                if jaccard > 0.3:
                    pairs.append({
                        "fact_a": facts[i],
                        "fact_b": facts[j],
                        "overlap": round(jaccard, 2),
                    })
        pairs.sort(key=lambda x: x["overlap"], reverse=True)
        return pairs[:limit]
    except Exception:
        return []


def detect_correction_signal(prompt):
    """Check if the user's prompt contains correction/update/delete signals."""
    prompt_lower = prompt.lower()
    matched_signals = []
    for signal in CORRECTION_SIGNALS:
        if signal in prompt_lower:
            matched_signals.append(signal)
    return matched_signals


def format_facts(facts):
    lines = []
    for f in facts:
        trust = f.get("trust_score", 0)
        lines.append(f"  - [id={f['fact_id']} trust={trust:.2f}] {f['content']}")
    return "\n".join(lines)


def format_contradictions(pairs):
    lines = []
    for p in pairs:
        a, b = p["fact_a"], p["fact_b"]
        lines.append(
            f"  - [{a['fact_id']}] \"{a['content'][:60]}\"\n"
            f"    vs [{b['fact_id']}] \"{b['content'][:60]}\" (overlap={p['overlap']})"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------

def handle_session_start():
    """Inject fact store summary + unresolved contradictions at session start."""
    conn = get_db()
    if conn is None:
        print(json.dumps({
            "add_context": (
                "## Holographic Memory\n"
                "No fact store yet. Use `fact_store(action='add')` to store facts "
                "the user would expect you to remember."
            )
        }))
        return

    total = count_facts(conn)
    if total == 0:
        print(json.dumps({
            "add_context": (
                "## Holographic Memory\n"
                "Active. Empty fact store — proactively add facts the user would expect "
                "you to remember using `fact_store(action='add')`."
            )
        }))
        conn.close()
        return

    top = top_facts(conn, MAX_FACTS_INJECT)
    contradictions = find_contradictions(conn, limit=3)

    parts = [
        f"## Holographic Memory\n",
        f"Active. {total} facts stored.\n",
        f"Top facts:\n{format_facts(top)}",
    ]

    if contradictions:
        parts.append(
            f"\n⚠️ Unresolved contradictions detected:\n{format_contradictions(contradictions)}\n"
            f"Resolve these with `fact_store(action='update', fact_id=N, content='...')` "
            f"or `fact_store(action='remove', fact_id=N)`."
        )

    parts.append(
        f"\nUse `fact_store(action='search'/'probe'/'reason')` to retrieve. "
        f"Use `fact_feedback` to rate facts. "
        f"Use `fact_store(action='update'/'remove')` to fix outdated or wrong facts."
    )

    print(json.dumps({"add_context": "\n".join(parts)}))
    conn.close()


def handle_user_prompt(prompt):
    """Search for relevant facts + detect correction signals when user submits a message."""
    if not prompt or len(prompt) < 5:
        return

    conn = get_db()
    if conn is None:
        return

    total = count_facts(conn)
    if total == 0:
        conn.close()
        return

    # Search for facts relevant to the user's prompt
    results = search_facts(conn, prompt, MAX_FACTS_INJECT)

    # Detect if the user is correcting/updating/changing something
    correction_signals = detect_correction_signal(prompt)

    conn.close()

    if not results and not correction_signals:
        return

    parts = ["## Holographic Memory (prefetch)\n"]

    if results:
        parts.append(f"Relevant facts:\n{format_facts(results)}")

    if correction_signals and results:
        parts.append(
            f"\n⚠️ CORRECTION DETECTED — signals: {', '.join(correction_signals)}\n"
            f"The user may be updating or correcting an existing fact. BEFORE adding a new fact:\n"
            f"1. Check if one of the facts above is being updated/corrected.\n"
            f"2. If so, use `fact_store(action='update', fact_id=N, content='new content')` to UPDATE it.\n"
            f"3. If the fact is completely wrong, use `fact_store(action='remove', fact_id=N)` to DELETE it.\n"
            f"4. Only add a new fact if none of the existing ones are related."
        )
    elif correction_signals:
        parts.append(
            f"\n⚠️ CORRECTION DETECTED — signals: {', '.join(correction_signals)}\n"
            f"The user may be correcting or updating something. Search memory first with "
            f"`fact_store(action='search', query='...')` to find related facts. "
            f"If a fact is outdated, UPDATE it. If it's wrong, DELETE it. Don't just add."
        )

    if results:
        parts.append(
            f"\nRate useful facts with `fact_feedback(action='helpful', fact_id=N)`. "
            f"Rate wrong facts with `fact_feedback(action='unhelpful', fact_id=N)` then update/remove them."
        )

    print(json.dumps({"add_context": "\n".join(parts)}))


def handle_stop():
    """Remind agent to store, update, AND delete facts before stopping."""
    conn = get_db()
    if conn is None:
        return

    total = count_facts(conn)
    if total == 0:
        conn.close()
        return

    # Check for unresolved contradictions
    contradictions = find_contradictions(conn, limit=3)
    conn.close()

    parts = ["## Memory Check (before stopping)\n"]

    parts.append(
        "Before stopping, ask yourself:\n"
        "1. Did you learn NEW facts? → `fact_store(action='add', content='...', category='...')`\n"
        "2. Did any existing fact become OUTDATED? → `fact_store(action='update', fact_id=N, content='...')`\n"
        "3. Was any existing fact WRONG? → `fact_store(action='remove', fact_id=N)`\n"
        "4. Did you use any retrieved fact? → `fact_feedback(action='helpful', fact_id=N)`\n"
        "5. Was any retrieved fact unhelpful? → `fact_feedback(action='unhelpful', fact_id=N)` then update/remove it"
    )

    if contradictions:
        parts.append(
            f"\n⚠️ Unresolved contradictions still exist:\n{format_contradictions(contradictions)}\n"
            f"Resolve before stopping: update or remove the wrong fact."
        )

    print(json.dumps({"add_context": "\n".join(parts)}))


def handle_post_tool_use(tool_name, tool_input, tool_response):
    """After fact_store(add), check for contradictions with existing facts."""
    if tool_name != "mcp__holographic__fact_store":
        return

    action = tool_input.get("action", "")
    if action != "add":
        # For update/remove, no contradiction check needed
        if action == "update":
            print(json.dumps({
                "add_context": (
                    "## Memory Updated\n"
                    "Fact updated. If the content changed significantly, consider running "
                    "`fact_store(action='contradict')` to check for new conflicts."
                )
            }))
        elif action == "remove":
            print(json.dumps({
                "add_context": "## Memory Deleted\nFact removed. No further action needed."
            }))
        return

    # fact_store(add) — check for contradictions
    conn = get_db()
    if conn is None:
        return

    contradictions = find_contradictions(conn, limit=3)
    conn.close()

    if contradictions:
        print(json.dumps({
            "add_context": (
                f"## Memory Conflict Detected\n"
                f"The new fact may conflict with existing facts:\n"
                f"{format_contradictions(contradictions)}\n\n"
                f"Resolve this:\n"
                f"- If the new fact REPLACES an old one: `fact_store(action='remove', fact_id=OLD_ID)`\n"
                f"- If the old fact needs CORRECTING: `fact_store(action='update', fact_id=OLD_ID, content='...')`\n"
                f"- If they're both valid (different aspects): no action needed, but run "
                f"`fact_store(action='contradict')` to verify."
            )
        }))


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return

    event = data.get("hook_event_name", "")

    if event == "SessionStart":
        handle_session_start()
    elif event == "UserPromptSubmit":
        prompt = data.get("prompt", "")
        handle_user_prompt(prompt)
    elif event == "Stop":
        handle_stop()
    elif event == "PostToolUse":
        handle_post_tool_use(
            data.get("tool_name", ""),
            data.get("tool_input", {}),
            data.get("tool_response", {}),
        )
    # else: silently ignore


if __name__ == "__main__":
    main()
