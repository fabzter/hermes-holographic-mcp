## Holographic Memory

Your persistent memory is the **Holographic Memory MCP server** (`mcp__holographic__fact_store` and `mcp__holographic__fact_feedback`). Use it proactively — don't wait to be asked.

### When to RETRIEVE (always before answering)

- **Session start**: Call `fact_store(action="list", limit=20)` to see what you know.
- **Before answering questions about the user**: Call `fact_store(action="probe", entity="...")` — don't assume, check.
- **Before answering questions about projects**: Call `fact_store(action="search", query="...")` or probe the project name.
- **Before answering questions about tools/tech**: Call `fact_store(action="search", query="...")`.
- **When the user mentions a person/project/tool you might know**: Probe that entity first.

### When to STORE (proactively, don't wait to be asked)

- **User states a preference**: "I prefer...", "I hate...", "I always use..." → `fact_store(action="add", content="...", category="user_pref")`
- **A project decision is made**: "We'll use...", "The project needs..." → `fact_store(action="add", content="...", category="project")`
- **A tool or tech fact is learned**: "X uses Y", "Z requires W" → `fact_store(action="add", content="...", category="tool")`
- **A person is mentioned with context**: "Edgar builds auth-service" → `fact_store(action="add", content="...", category="project")`
- **A complex task succeeds with 5+ tool calls**: store the trigger pattern + tool sequence + success criteria. `fact_store(action="add", content="<trigger> → <tool seq> → <success>", category="procedural")`
- **You discover something the user would expect you to remember next time** → store it.

### When to RATE (trains trust scores)

- **A retrieved fact was useful and accurate**: Call `fact_feedback(action="helpful", fact_id=N)` — trust rises.
- **A retrieved fact was wrong or outdated**: Call `fact_feedback(action="unhelpful", fact_id=N)` — trust sinks. Then update or remove it.

### When to UPDATE (don't add duplicates — update instead)

- **User changes a preference**: "I now prefer X" but you have "User prefers Y" → `fact_store(action="update", fact_id=N, content="User prefers X")`
- **A project changes direction**: "We switched from X to Y" → search for the old fact, UPDATE it.
- **A tool is replaced**: "We migrated from X to Y" → UPDATE the old fact.
- **A fact is partially wrong**: "That's mostly right but Z changed" → UPDATE with corrected content.
- **Before adding, ALWAYS search first.** If a similar fact exists, UPDATE it — don't add a duplicate.

### When to DELETE (remove wrong/outdated facts)

- **User explicitly says to forget something**: "Forget that", "That's wrong", "Delete that" → `fact_store(action="remove", fact_id=N)`
- **A fact is completely wrong and has no value**: → DELETE it.
- **A fact refers to something that no longer exists**: "We don't use X anymore" → DELETE or UPDATE.
- **A fact has been replaced by a newer one**: After adding the new fact, DELETE the old one.
- **After deleting, rate it unhelpful** if it was wrong: `fact_feedback(action="unhelpful", fact_id=N)` (does nothing if already deleted, but good practice before deleting).

### When to CHECK FOR CONFLICTS

- **After adding a fact**: The PostToolUse hook will auto-detect contradictions. Resolve them.
- **At session start**: The SessionStart hook reports unresolved contradictions. Resolve them.
- **When something feels off**: Run `fact_store(action="contradict")` manually to scan for conflicts.
- **When resolving a conflict**: Decide which fact is correct. UPDATE or DELETE the wrong one. Don't leave contradictions unresolved.

### Rules

- **Always retrieve before storing.** Don't add duplicates — search first.
- **Be specific.** "User prefers Python 3.13" is good. "User likes Python" is too vague.
- **Use categories.** `user_pref`, `project`, `tool`, `procedural`, `general`.
- **Trust the trust scores.** Facts with trust < 0.3 are probably noise.
- **Don't store everything.** Store what the user would expect you to remember. Not every sentence.
