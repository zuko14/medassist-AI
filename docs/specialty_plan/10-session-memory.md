# Task 12 — Session memory (do this at the END of every working session)

The owner requires that every session of the specialty expansion leaves a written record, so that any agent (Antigravity, Claude Code, or a human) can continue without re-deriving context.

- [ ] **Step 1: Create the session file** `docs/sessions/SESSION_NN_<SHORT_TOPIC>.md`. `NN` is the next number after the highest existing file in `docs/sessions/`. Use this template verbatim and fill every section. Write "none" rather than deleting a section.

~~~markdown
# Session NN: <topic>

**Date:** YYYY-MM-DD
**Agent:** <Antigravity / Claude Code / name>
**Branch / commits:** <branch> — <first sha>..<last sha>
**Plan tasks covered:** <e.g. Task 1, Task 2 (docs/specialty_plan/)>

## 1. Intent for this session
<one paragraph: what the owner asked for and what this session set out to do>

## 2. What was done
| Task / step | Result | Evidence |
|---|---|---|
| Task 1 Step 4 | PASS | `pytest tests/test_specialty_migration_077.py -q` → 14 passed |

## 3. Files changed
- `path` — one line on what changed

## 4. Tests run (exact commands and results)
```
<command>
<summary line, e.g. "212 passed in 41.2s">
```
Pre-existing failures (also failing on `main`): <list with output, or "none">

## 5. Orphan-process check
<command run, processes found/killed, or "none found">

## 6. Decisions and deviations from the plan
<every place the implementation differed from docs/specialty_plan, and why; "none" if none>

## 7. Production actions
<migrations applied, deploys, SQL verification output, clinic plan counts before and after; "none">

## 8. Open items / next session starts at
- <exact next task and step>
- <any blocker or question for the owner>
~~~

- [ ] **Step 2: Update the master log.** Append one row to the Session Log Index table in `docs/SPECIALTY_EXPANSION_MASTER_MEMORY.md`:
```markdown
| **Session NN** | YYYY-MM-DD | <one-line objective> | `docs/sessions/SESSION_NN_<SHORT_TOPIC>.md` |
```

- [ ] **Step 3: Correct stale memory.** If this session proved any statement in the master memory or an earlier session doc wrong:
  1. Fix the statement in place.
  2. Prefix the corrected paragraph with `> ⚠️ CORRECTED IN SESSION NN:` followed by one line on what was wrong.

  Never silently rewrite history.

- [ ] **Step 4: Tick the plan.** In the `docs/specialty_plan/*.md` files, change completed steps from `- [ ]` to `- [x]`, so the next agent can see progress at a glance.

- [ ] **Step 5: Commit the docs**

```bash
git add docs/sessions/ docs/SPECIALTY_EXPANSION_MASTER_MEMORY.md docs/specialty_plan/
git commit -m "docs(specialty): session NN log

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```
