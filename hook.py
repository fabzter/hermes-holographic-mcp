#!/usr/bin/env python3
"""Holographic memory hook for Devin — handles SessionStart, UserPromptSubmit, Stop.

Reads hook event data from stdin, queries the SQLite memory DB directly,
and outputs JSON with add_context to inject relevant facts into the conversation.

This replaces Hermes' prefetch() and system_prompt_block() hooks that we
can't replicate via MCP alone.
"""
import sys, os, json, sqlite3, re
from pathlib import Path

DB_PATH = os.environ.get(
    "HOLOGRAPHIC_DB_PATH",
    str(Path.home() / ".local" / "share" / "devin" / "holographic" / "memory.db"),
)

MIN_TRUST = float(os.environ.get("HOLOGRAPHIC_MIN_TRUST", "0.3"))
MAX_FACTS_INJECT = int(os.environ.get("HOLOGRAPHIC_MAX_INJECT", "5"))


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
        # Extract meaningful tokens (skip stop words and very short tokens)
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
        }
        tokens = [
            t.strip(".,!?;:\"'()[]{}")
            for t in re.split(r"\s+", query.strip())
            if t and len(t) > 2 and t.lower() not in stop_words
        ]
        if not tokens:
            return []
        # Use OR in FTS5 so any token match returns results
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


def format_facts(facts):
    lines = []
    for f in facts:
        trust = f.get("trust_score", 0)
        lines.append(f"  - [trust={trust:.2f}] {f['content']}")
    return "\n".join(lines)


def handle_session_start():
    """Inject fact store summary at session start."""
    conn = get_db()
    if conn is None:
        # DB doesn't exist yet — no facts
        output = {
            "add_context": (
                "## Holographic Memory\n"
                "No fact store yet. Use `fact_store(action='add')` to store facts "
                "the user would expect you to remember."
            )
        }
        print(json.dumps(output))
        return

    total = count_facts(conn)
    if total == 0:
        output = {
            "add_context": (
                "## Holographic Memory\n"
                "Active. Empty fact store — proactively add facts the user would expect "
                "you to remember using `fact_store(action='add')`."
            )
        }
    else:
        top = top_facts(conn, MAX_FACTS_INJECT)
        context = (
            f"## Holographic Memory\n"
            f"Active. {total} facts stored.\n"
            f"Top facts:\n{format_facts(top)}\n\n"
            f"Use `fact_store(action='search')` or `fact_store(action='probe')` "
            f"to retrieve more. Use `fact_feedback` to rate facts after using them."
        )
        output = {"add_context": context}

    conn.close()
    print(json.dumps(output))


def handle_user_prompt(prompt):
    """Search for relevant facts when user submits a message."""
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
    conn.close()

    if not results:
        return

    context = (
        f"## Holographic Memory (prefetch)\n"
        f"Relevant facts for this query:\n{format_facts(results)}\n\n"
        f"Use `fact_feedback(action='helpful'/'unhelpful', fact_id=N)` to rate these. "
        f"If you learn new facts this session, store them with `fact_store(action='add')`."
    )
    print(json.dumps({"add_context": context}))


def handle_stop():
    """Remind agent to store new facts before stopping."""
    conn = get_db()
    if conn is None:
        return

    total = count_facts(conn)
    conn.close()

    if total == 0:
        return

    # Only remind — don't block (blocking causes loops)
    print(json.dumps({
        "add_context": (
            "## Memory Check\n"
            "Before stopping, consider: did you learn any new facts this session "
            "that the user would expect you to remember? If so, store them with "
            "`fact_store(action='add', content='...', category='...')`. "
            "If any retrieved fact was useful, rate it with `fact_feedback(action='helpful')`."
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
    # else: silently ignore


if __name__ == "__main__":
    main()
