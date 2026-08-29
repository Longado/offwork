# Handoff Receipt Prototype Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a CLI-first Offwork prototype that turns each successful capture into a structured, human-readable Handoff Receipt and demonstrates integrity, project-scoped freshness, explicit unknowns, real auto-check results, and explicit human acceptance.

**Architecture:** Keep Capsule artifacts immutable. Build Receipt as a projection of the validated Capsule, persisted audit facts in SQLite, and a read-only comparison with the current project workspace. Route every write through `StateService`; render human and JSON output from the same Receipt object; add no new top-level command.

**Tech Stack:** Python standard library, SQLite, argparse, subprocess argv with `shell=False`, pytest, existing `.offwork` storage and Capsule manifest.

---

## 1. Prototype boundary

The prototype proves one five-minute story:

```text
capture
  -> inspect Handoff Receipt
  -> change the project workspace
  -> inspect Receipt again: integrity passed, freshness changed
  -> explicitly accept or reject the handoff
```

It does not add a daemon, TUI, Web UI, cloud sync, Shell history, alias installation, workflow execution, Agent launcher, team permission system, or Automation Opportunity analyzer.

The following facts remain independent:

```text
agent_claimed
offwork_observed
auto_checked
handoff_verified
workspace_freshness
human_acceptance
```

`task complete` continues to mean local completion only. It must not imply human acceptance, customer acceptance, deployment, merge, or production success.

## 2. Baseline and files

Baseline before implementation:

```text
branch: main
commit: 245885e
tests: 217 passed, 1 skipped
```

Expected change surface:

- Create `src/offwork_capsule/receipt.py`: Receipt construction, freshness comparison, and human rendering.
- Modify `src/offwork_capsule/capsule.py`: accept optional `agent_claims` and `unknowns`; expose validated restore-test loading.
- Modify `src/offwork_capsule/project.py`: reuse the existing project-scoped Git snapshot for freshness comparison without broadening collection.
- Modify `src/offwork_capsule/storage.py`: migrate project schema from v4 to v5 with Capsule audit and human acceptance event tables.
- Modify `src/offwork_capsule/state.py`: persist auto-check facts and append explicit acceptance/rejection events under revision checks.
- Modify `src/offwork_capsule/cli.py`: add nested `task accept` and `task reject`; attach the same structured Receipt to `capture`, `resume`, and `task show`.
- Modify `tests/test_capsule.py`, `tests/test_project.py`, `tests/test_storage.py`, `tests/test_tasks.py`, and `tests/test_lifecycle.py`: targeted behavior and compatibility coverage.
- Modify `README.md` and `docs/handoff-receipt-design.md`: document the implemented prototype only after verification.

## 3. Receipt contract

The single Receipt builder returns this shape:

```python
{
    "schema_version": "offwork.receipt/v1",
    "task": {
        "task_id": str,
        "title": str,
        "goal": str,
        "revision": int,
    },
    "capsule": {
        "capsule_id": str,
        "captured_at": str,
        "completeness": {
            "status": "complete" | "incomplete",
            "missing_information": list[str],
        },
    },
    "agent_claimed": {
        "source": "capture_context",
        "summary": str,
        "items": list[str],
    },
    "offwork_observed": {
        "project_path": str,
        "is_git_repo": bool,
        "branch": str | None,
        "head": str | None,
        "dirty_files": list[str],
        "status_porcelain": str,
        "diff_stat": str,
    },
    "auto_checked": {
        "status": "not_run" | "passed" | "failed" | "unavailable",
        "reason": str,
        "checked_at": str | None,
        "checks": list[dict],
    },
    "handoff_verified": {
        "integrity": {"status": "passed"},
        "restore": {
            "status": "passed" | "failed",
            "missing_information": list[str],
        },
    },
    "unknowns": list[str],
    "open_loops": list[dict],
    "workspace_freshness": {
        "scope": "git_project",
        "status": "fresh" | "changed" | "unavailable",
        "checked_at": str,
        "changes": list[str],
        "reason": str,
    },
    "human_acceptance": {
        "status": "pending" | "accepted" | "rejected",
        "acted_at": str | None,
        "note": str | None,
    },
}
```

`integrity: passed` means the Capsule files match their local manifest and database content hash. It is self-consistency evidence, not an identity signature or external attestation.

`workspace_freshness` is limited to the Git-visible project state collected inside `--project`. Non-Git workspaces and states that cannot be compared reliably return `unavailable`.

## 4. SQLite v5 additions

Use two small tables instead of storing a mutable Receipt document:

```sql
CREATE TABLE IF NOT EXISTS capsule_audits (
    capsule_id TEXT PRIMARY KEY REFERENCES capsules(capsule_id) ON DELETE CASCADE,
    auto_check_status TEXT NOT NULL CHECK (
        auto_check_status IN ('not_run', 'passed', 'failed', 'unavailable')
    ),
    auto_check_reason TEXT NOT NULL,
    checks_json TEXT NOT NULL CHECK (
        json_valid(checks_json) AND json_type(checks_json) = 'array'
    ),
    checked_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_human_acceptance_events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('accepted', 'rejected')),
    note TEXT NOT NULL DEFAULT '',
    task_revision INTEGER NOT NULL CHECK (task_revision > 0),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS task_human_acceptance_latest
    ON task_human_acceptance_events(task_id, created_at, event_id);
```

Every published Capsule receives a default `not_run` audit row in the same transaction. After `evaluate_auto_complete`, `StateService` updates it with the checks actually attempted. If persistence of the later result fails, the durable state remains `not_run`; it must never become a false pass.

No acceptance row means `pending`. Only `task accept` and `task reject` append events. Each event increments Task revision under the existing compare-and-swap rule.

## 5. Task-by-task implementation

### Task 1: Add claims and unknowns to new Capsules

**Files:**

- Modify: `src/offwork_capsule/capsule.py`
- Test: `tests/test_capsule.py`

- [ ] **Step 1: Write the failing tests**

Add tests that build a Capsule with:

```python
context = {
    "goal": "repair login",
    "summary": "implemented refresh-token fix",
    "agent_claims": ["tests pass"],
    "unknowns": ["production behavior not checked"],
    "next_step": "review token migration",
    "open_loops": [],
}
```

Assert that `agent_claims` and `unknowns` are normalized string lists. Add invalid-input cases for non-list values and non-string members. Add a legacy Capsule assertion showing missing fields read as empty lists without rewriting the file.

- [ ] **Step 2: Verify RED**

Run:

```bash
python3 -m pytest -q tests/test_capsule.py -k 'claim or unknown'
```

Expected: failures because `build_capsule` does not emit the new fields.

- [ ] **Step 3: Implement the minimum behavior**

In `build_capsule`, add:

```python
"agent_claims": _text_list(context.get("agent_claims"), "agent_claims"),
"unknowns": _text_list(context.get("unknowns"), "unknowns"),
```

Keep `schema_version` unchanged because the fields are additive and optional for historical Capsules.

- [ ] **Step 4: Verify GREEN**

Run the targeted test command again and then:

```bash
python3 -m pytest -q tests/test_capsule.py
```

Expected: all Capsule tests pass.

### Task 2: Persist real auto-check facts and explicit human decisions

**Files:**

- Modify: `src/offwork_capsule/storage.py`
- Modify: `src/offwork_capsule/state.py`
- Test: `tests/test_storage.py`
- Test: `tests/test_tasks.py`
- Test: `tests/test_lifecycle.py`

- [ ] **Step 1: Write failing schema migration tests**

Assert a v4 database migrates to v5, retains existing Tasks and Capsules, and contains both new tables with their CHECK constraints. Assert fresh project initialization reports `PRAGMA user_version = 5`.

- [ ] **Step 2: Verify schema RED**

```bash
python3 -m pytest -q tests/test_storage.py -k 'v5 or capsule_audit or human_acceptance'
```

Expected: failures because schema v5 does not exist.

- [ ] **Step 3: Add the v5 schema and migration**

Set `CURRENT_PROJECT_SCHEMA_VERSION = 5`, add `PROJECT_MIGRATION_4_TO_5`, include the new tables in `PROJECT_SCHEMA`, accept version 4 as a migration source, and validate both new tables after initialization.

- [ ] **Step 4: Write failing StateService tests**

Cover these service contracts:

```python
service.record_capsule_audit(capsule_id, auto_complete_result)
service.set_human_acceptance(task_id, "accepted", note="reviewed", expected_revision=revision)
service.set_human_acceptance(task_id, "rejected", note="migration unclear", expected_revision=revision)
service.get_human_acceptance(task_id)
```

Assert default `pending`, timestamps and notes, revision increments, stale revision rejection, and no state change from auto-check pass to human accepted.

- [ ] **Step 5: Verify service RED**

```bash
python3 -m pytest -q tests/test_tasks.py tests/test_lifecycle.py -k 'human_acceptance or capsule_audit'
```

Expected: failures because the service methods do not exist.

- [ ] **Step 6: Implement the minimum StateService methods**

Map auto-complete results as follows:

```python
passed is True                         -> "passed"
reason == "acceptance_command_failed" -> "failed"
reason == "acceptance_command_unavailable" -> "unavailable"
otherwise and no attempted checks      -> "not_run"
otherwise with a failing check         -> "failed"
```

Persist only command text, argv and return code; do not persist stdout, stderr, prompts, environment variables or credentials.

- [ ] **Step 7: Verify GREEN**

Run the targeted service tests, then all storage and task tests.

### Task 3: Build project-scoped freshness and Receipt projection

**Files:**

- Create: `src/offwork_capsule/receipt.py`
- Modify: `src/offwork_capsule/capsule.py`
- Test: `tests/test_project.py`
- Test: `tests/test_lifecycle.py`

- [ ] **Step 1: Write failing freshness tests**

Cover:

1. identical Git-visible state -> `fresh`;
2. a project file changes after capture -> `changed`;
3. historical/non-Git state without a reliable snapshot -> `unavailable`;
4. a nested project ignores unrelated parent repository changes;
5. `changed` leaves Capsule integrity as `passed`;
6. tampered manifest raises `CAPSULE_INTEGRITY_FAILED`.

- [ ] **Step 2: Verify freshness RED**

```bash
python3 -m pytest -q tests/test_project.py tests/test_lifecycle.py -k 'freshness or receipt_integrity'
```

Expected: failures because no freshness comparison or Receipt builder exists.

- [ ] **Step 3: Expose validated restore-test loading**

Add:

```python
def load_restore_test(project_root: Path, capsule_id: str) -> Dict[str, Any]:
    load_capsule(project_root, capsule_id)
    # Read and validate restore-test.json as an object after manifest validation.
```

For a legacy Capsule whose restore-test file is unavailable, use `validate_for_restore(capsule)` and represent external verifier evidence as unavailable rather than inventing a pass.

- [ ] **Step 4: Implement `receipt.py`**

Provide three focused functions:

```python
def compare_workspace_freshness(captured: Mapping[str, Any], current: Mapping[str, Any]) -> Dict[str, Any]: ...

def build_receipt(project_root: Path, task: Mapping[str, Any], capsule_id: str, service: StateService) -> Dict[str, Any]: ...

def render_receipt(receipt: Mapping[str, Any]) -> str: ...
```

Compare only `project_path`, `is_git_repo`, `branch`, `head`, `status_porcelain`, `diff_stat`, and normalized `dirty_files`. Never inspect the parent repository outside the existing `capture_project_state` boundary.

- [ ] **Step 5: Verify GREEN**

Run targeted tests, then:

```bash
python3 -m pytest -q tests/test_project.py tests/test_capsule.py tests/test_lifecycle.py
```

### Task 4: Expose the same Receipt through existing CLI flows

**Files:**

- Modify: `src/offwork_capsule/cli.py`
- Test: `tests/test_tasks.py`
- Test: `tests/test_lifecycle.py`

- [ ] **Step 1: Write failing CLI tests**

Assert:

- successful `capture --json` contains `data.receipt`;
- `task show --json` and `resume --json` return the same Receipt facts;
- human `capture`, `task show`, and `resume` render `HANDOFF RECEIPT` from that object;
- an Agent claim that tests passed remains under `agent_claimed` while `auto_checked.status` is `not_run` unless Offwork ran the command;
- Unknowns appear in both human and JSON output;
- JSON stdout parses as exactly one envelope;
- auto-check pass leaves human acceptance `pending`;
- only explicit nested commands change acceptance.

- [ ] **Step 2: Verify CLI RED**

```bash
python3 -m pytest -q tests/test_tasks.py tests/test_lifecycle.py -k 'receipt or human_acceptance'
```

Expected: failures because Receipt and commands are absent.

- [ ] **Step 3: Add nested commands**

Add:

```text
offwork task accept <task-id> [--note TEXT] [--revision N] [--project PATH] [--json]
offwork task reject <task-id> [--note TEXT] [--revision N] [--project PATH] [--json]
```

Do not add a top-level `receipt` command.

- [ ] **Step 4: Attach Receipt to capture, show and resume**

After auto-complete evaluation, persist its audit result and build the Receipt. For `task show` and `resume`, load the latest validated Capsule for the selected Task, verify it, recalculate freshness, and attach the current human acceptance projection.

When a Task has no Capsule, return the existing Task shape with `receipt: null`; do not invent a Receipt.

- [ ] **Step 5: Render human output from the structured object**

Use `render_receipt(data["receipt"])` for all three flows. Escape untrusted terminal fields with the existing `_terminal_text` boundary before printing.

- [ ] **Step 6: Verify GREEN**

Run targeted CLI tests, then:

```bash
python3 -m pytest -q tests/test_cli_startup.py tests/test_tasks.py tests/test_lifecycle.py
```

### Task 5: Full verification and real temporary-project demo

**Files:**

- Modify after successful verification: `README.md`
- Modify after successful verification: `docs/handoff-receipt-design.md`

- [ ] **Step 1: Run source checks**

```bash
python3 -m compileall -q src tests
git diff --check
```

Expected: both commands exit 0.

- [ ] **Step 2: Run the complete suite**

```bash
python3 -m pytest -q
```

Expected: all existing and new tests pass; the existing skip may remain if its documented prerequisite is unavailable.

- [ ] **Step 3: Run the real demo**

Create a temporary Git project and demonstrate:

```text
task add
capture with agent_claims and unknowns
task show receipt
modify one project file
task show receipt -> integrity passed, freshness changed
task accept or task reject with note
task show receipt -> explicit decision and timestamp
```

Parse every JSON invocation with `python3 -m json.tool`. Do not use `shell=True`; do not modify, stash, restore or commit the user's real workspace during the demo.

- [ ] **Step 4: Update documentation with observed behavior only**

Document the actual commands and outputs. Do not call self-consistency a cryptographic identity proof, and do not describe local human acceptance as merge, deployment or customer approval.

- [ ] **Step 5: Inspect final scope**

Run:

```bash
git status --short
git diff --stat
git diff -- src tests README.md docs
```

Confirm every change supports the prototype and no Automation Opportunity, daemon, TUI, Web UI, dependency upgrade or unrelated refactor was introduced.

## 6. Acceptance checklist

The prototype is complete only when all of these are demonstrated by tests or the real demo:

- [ ] Agent claims and Offwork checks are separate fields.
- [ ] An unexecuted command never appears as passed.
- [ ] Explicit Unknowns survive capture and appear in Receipt.
- [ ] Workspace changes do not invalidate Capsule integrity.
- [ ] Manifest tampering produces an integrity failure.
- [ ] Human acceptance defaults to `pending`.
- [ ] Auto-check pass does not set human acceptance.
- [ ] `accepted` and `rejected` require explicit commands.
- [ ] Nested Git projects exclude unrelated parent changes.
- [ ] Human and JSON output derive from the same Receipt.
- [ ] JSON stdout remains one valid CLI envelope.
- [ ] V0.1/V0.2 Capsules remain readable without rewriting.
- [ ] Existing tests continue to pass.

## 7. Deferred work

The following items remain design-only after this prototype:

- Automation Opportunity discovery and history analysis;
- signed identity or external attestation;
- non-Git filesystem freshness;
- production, deployment, customer or external-system verification;
- shared team Receipt index;
- cloud sync, SSO, RBAC, retention policies and enterprise audit export.
