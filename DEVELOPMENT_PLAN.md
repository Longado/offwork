# Offwork Prototype Development Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use test-driven development for every implementation slice. Execute this plan inline unless the user explicitly requests subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a five-minute, zero-install prototype that proves a history-free Agent can inspect a structured handoff Receipt, detect whether the local Git workspace changed after capture, and safely begin the correct next step.

**Architecture:** Offwork is a local-first Python CLI. Each project owns a small SQLite state database and immutable Capsule directories under `.offwork/`. A Receipt is a derived view over immutable capture evidence, persisted check results, current project freshness, and explicit human acceptance; it is not a second workflow engine.

**Tech Stack:** Python 3.9+ standard library, `argparse`, `sqlite3`, `subprocess` with argv and `shell=False`, `hashlib`, `json`, `pathlib`, `unittest`, Git.

---

## 1. Product premise

Offwork exists for one moment:

> An Agent stops with unfinished local work. A new Agent, possibly from another provider and without the old transcript, must decide whether it can continue, must verify first, or should stop.

The prototype does not try to remember every interaction. It binds five facts into one handoff:

1. What Task is being handed over.
2. What the previous Agent claimed.
3. What Offwork observed in the project.
4. What Offwork actually executed and checked.
5. What remains unknown and what the next safe step is.

The prototype succeeds when a fresh Agent can make the correct first decision without reopening the old Session.

## 2. Demo storyline

The canonical demo uses a small Git project and one Task: “修复登录失败”.

1. Create a Task with one real check command.
2. Give an Agent context that claims the fix and tests are complete, while explicitly retaining one unknown.
3. Run `offwork capture`.
4. Show a Receipt that separates the claim from the observed files and the check Offwork actually ran.
5. Modify one project file after capture.
6. Show the same Capsule with integrity still passing and workspace freshness now `changed`.
7. Explicitly accept or reject the handoff with a note.
8. Show the final Receipt in human and JSON form with the same facts.

The demo must visibly prove that:

- an Agent claim cannot create an Offwork check result;
- a changed workspace does not corrupt an immutable Capsule;
- an automatic check cannot create human acceptance;
- only an explicit user command can accept or reject the handoff.

## 3. Prototype scope

### Included

- explicit project boundary;
- minimal Task identity and goal;
- immutable Capsule;
- Handoff Receipt;
- `agent_claimed`, `offwork_observed`, `auto_checked`, and `handoff_verified` separation;
- Evidence, Unknowns, open loops, and next step;
- Git workspace freshness limited to the explicit project;
- explicit human `pending | accepted | rejected` state;
- one JSON envelope on stdout;
- matching human and JSON facts;
- V0.1 prototype Capsule reader compatibility only if a legacy fixture is supplied before implementation begins.

### Excluded

- Agent launching, stopping, steering, or scheduling;
- daemon, TUI, Web UI, cloud sync, vector database, or remote control plane;
- Shell history collection, Ctrl-R replacement, aliases, or `.zshrc` edits;
- automatic workflow execution;
- Session transcript storage or full Agent observability;
- task dependencies, task board, global project registry, searchable history, or persistent memory;
- Automation Opportunity analysis;
- customer acceptance, deployment, production state, legal approval, or compliance certification.

## 4. State boundaries

The implementation must never collapse these states into one `verified` flag.

| Field | Meaning | Allowed status |
| --- | --- | --- |
| `agent_claimed` | Text supplied by the Agent or capture context | structured text, never pass/fail |
| `offwork_observed` | Project and Git facts collected by Offwork | structured snapshot |
| `auto_checked` | Commands Offwork actually attempted | `not_run`, `passed`, `failed`, `unavailable` |
| `handoff_verified.integrity` | Capsule files match their manifest | `passed`, `failed` |
| `handoff_verified.restore` | Required handoff fields are complete | `passed`, `failed` |
| `workspace_freshness` | Current Git workspace compared with capture | `fresh`, `changed`, `unavailable` |
| `human_acceptance` | Explicit user response to this Capsule | `pending`, `accepted`, `rejected` |

Terminology boundaries:

- Capsule integrity means local self-consistency, not independent identity or cryptographic attestation.
- Workspace freshness covers only the Git project snapshot described below. External services, databases, environment variables, ignored files, and production state remain `unavailable` unless a future explicit evidence adapter exists.
- Human acceptance means the local user received this handoff. It is not PR approval, an electronic signature, customer acceptance, or release authorization.

## 5. Command surface

Only these commands belong in the prototype:

```text
offwork init --project PATH [--json]

offwork task add TITLE \
  --goal GOAL \
  [--check COMMAND]... \
  --project PATH [--json]

offwork capture \
  --task TASK_ID \
  --context CONTEXT.json \
  --project PATH [--json]

offwork task show TASK_ID --project PATH [--json]
offwork resume --task TASK_ID --project PATH [--json]

offwork task accept TASK_ID [--note TEXT] --project PATH [--json]
offwork task reject TASK_ID [--note TEXT] --project PATH [--json]
```

Do not add a top-level `receipt` command. `capture`, `task show`, and `resume` reuse the same Receipt builder.

## 6. Structured contracts

### Capture context

```json
{
  "summary": "已实现 Token 刷新修复并补充测试",
  "agent_claims": [
    "登录失败已经修复",
    "测试全部通过"
  ],
  "unknowns": [
    "旧 Token 迁移行为尚未确认"
  ],
  "open_loops": [
    {
      "title": "确认旧 Token 的迁移行为",
      "disposition": "resolve",
      "note": "先运行迁移测试"
    }
  ],
  "next_step": "运行旧 Token 迁移测试"
}
```

`agent_claims`, `unknowns`, and `open_loops` may be empty arrays. `next_step` is required. Offwork does not infer missing business outcomes.

### Receipt

```json
{
  "schema_version": "offwork.receipt/v1",
  "task": {
    "task_id": "task-...",
    "title": "修复登录失败",
    "goal": "恢复 Token 刷新行为",
    "revision": 2
  },
  "capsule": {
    "capsule_id": "capsule-...",
    "captured_at": "2026-08-29T00:00:00+00:00",
    "completeness": {
      "status": "complete",
      "missing_information": []
    }
  },
  "agent_claimed": {
    "source": "capture_context",
    "summary": "已实现 Token 刷新修复并补充测试",
    "items": ["登录失败已经修复", "测试全部通过"]
  },
  "offwork_observed": {
    "project_path": "/absolute/project",
    "branch": "feature/login-fix",
    "head": "0123456789abcdef",
    "dirty_files": ["auth/token.py"]
  },
  "auto_checked": {
    "status": "passed",
    "checks": [
      {
        "command": "python3 -m unittest",
        "argv": ["python3", "-m", "unittest"],
        "cwd": "/absolute/project",
        "returncode": 0,
        "checked_at": "2026-08-29T00:00:01+00:00"
      }
    ]
  },
  "handoff_verified": {
    "integrity": {"status": "passed"},
    "restore": {"status": "passed"}
  },
  "unknowns": ["旧 Token 迁移行为尚未确认"],
  "open_loops": [],
  "workspace_freshness": {
    "status": "fresh",
    "checked_at": "2026-08-29T00:00:02+00:00",
    "changes": []
  },
  "human_acceptance": {
    "status": "pending",
    "acted_at": null,
    "note": null
  }
}
```

Human output must be rendered from this object. It must not maintain a second set of facts.

## 7. Storage model

Project-private storage lives under `<project>/.offwork/`:

```text
.offwork/
├── project.json
├── state.sqlite3
└── capsules/
    └── <capsule-id>/
        ├── capsule.json
        ├── checks.json
        ├── restore-test.json
        └── manifest.json
```

Permissions:

- `.offwork/` and Capsule directories: `0700`;
- state, JSON, manifest, and lock files: `0600`;
- fixed paths and Capsule files reject symlinks;
- Capsule publication uses a private staging directory, `fsync`, and atomic rename.

SQLite contains only mutable truth:

```sql
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    check_commands_json TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE capsules (
    capsule_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    archive_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE human_acceptance_events (
    event_id TEXT PRIMARY KEY,
    capsule_id TEXT NOT NULL REFERENCES capsules(capsule_id),
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    status TEXT NOT NULL CHECK(status IN ('accepted', 'rejected')),
    note TEXT NOT NULL,
    task_revision INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
```

All mutations pass through one StateService transaction boundary. Accept and reject append an event and increment the Task revision in one transaction. No row means `pending`.

## 8. Git workspace snapshot

Freshness is limited to the canonical `--project` boundary.

At capture, record:

- canonical project path;
- containing Git root only as metadata;
- branch and full HEAD;
- project-relative porcelain status;
- tracked, untracked, deleted, renamed, and symlink paths inside the project;
- SHA-256 content fingerprint for each dirty or untracked regular file;
- symlink target fingerprint for dirty symlinks.

Exclude `.offwork/` and every path outside the explicit project, even when the project is nested inside a parent Git repository.

Freshness comparison:

- `fresh`: the current bounded snapshot exactly matches the captured snapshot;
- `changed`: both snapshots are reliable but differ;
- `unavailable`: Git is unavailable, the project identity/path cannot be verified, a required dirty path cannot be read, or the Capsule predates the required snapshot fields.

`changed` never changes Capsule integrity.

## 9. Future file map

The repository currently contains only this plan. Implementation creates files only when their plan task begins.

```text
DEVELOPMENT_PLAN.md
README.md
pyproject.toml
bin/offwork
offwork/
├── __init__.py
├── __main__.py
├── cli.py
├── errors.py
├── output.py
├── project.py
├── state.py
├── capsule.py
└── receipt.py
tests/
├── __init__.py
├── helpers.py
├── test_cli.py
├── test_project.py
├── test_capsule.py
├── test_receipt.py
└── test_demo.py
```

Do not copy source or tests from `offwork-capsule`. It may be read as historical reference only.

## 10. Implementation plan

### Task 1: Bootstrap the zero-install CLI and project boundary

**Files:** create `pyproject.toml`, `bin/offwork`, `offwork/__init__.py`, `offwork/__main__.py`, `offwork/cli.py`, `offwork/errors.py`, `offwork/project.py`, `offwork/state.py`, `tests/__init__.py`, `tests/helpers.py`, `tests/test_cli.py`, and `tests/test_project.py`.

- [ ] Write startup tests proving `--help` and `--version` do not create `.offwork`.
- [ ] Run `python3 -m unittest tests.test_cli tests.test_project -v`; verify RED because the package does not exist.
- [ ] Implement `offwork init`, canonical project validation, private paths, SQLite connection settings, and one JSON envelope.
- [ ] Run the same command; verify GREEN.
- [ ] Verify a nested project keeps its explicit path rather than adopting the parent Git root.
- [ ] Commit only Task 1 files with `feat: initialize standalone offwork projects`.

### Task 2: Add minimal Tasks and immutable Capsule capture

**Files:** modify `offwork/cli.py` and `offwork/state.py`; create `offwork/capsule.py`, `tests/test_capsule.py`, and Capsule fixtures inside the test temporary directories.

- [ ] Write failing tests for Task creation, unknown Task ID, context validation, claims, unknowns, open loops, and immutable Capsule publication.
- [ ] Write failing tests proving a claim that tests passed does not create an `auto_checked=passed` result.
- [ ] Write failing tests proving configured check commands run as argv with `shell=False` and the canonical project as cwd.
- [ ] Run `python3 -m unittest tests.test_capsule -v`; verify RED for missing Task and Capsule behavior.
- [ ] Implement the smallest Task schema, `task add`, context parser, bounded command runner, Capsule staging, manifest, and DB registration.
- [ ] Run `python3 -m unittest tests.test_cli tests.test_project tests.test_capsule -v`; verify GREEN.
- [ ] Commit with `feat: capture immutable handoff capsules`.

### Task 3: Build one structured Receipt and two renderers

**Files:** create `offwork/receipt.py`, `offwork/output.py`, and `tests/test_receipt.py`; modify `offwork/cli.py`.

- [ ] Write failing tests for every Receipt section and every allowed status.
- [ ] Write a failing test showing configured but unexecuted checks are `not_run`, never `passed`.
- [ ] Write a failing test showing Unknowns survive capture and appear in Receipt.
- [ ] Write a failing parity test that parses JSON output and checks that every human Receipt section is rendered from the same values.
- [ ] Write a failing test proving JSON stdout contains exactly one valid envelope and diagnostics stay off stdout.
- [ ] Run `python3 -m unittest tests.test_receipt -v`; verify RED because no Receipt builder exists.
- [ ] Implement one `build_receipt()` function and render human output exclusively from its return value.
- [ ] Reuse it from `capture`, `task show`, and `resume`.
- [ ] Run the Receipt tests and all earlier tests; verify GREEN.
- [ ] Commit with `feat: render structured handoff receipts`.

### Task 4: Add bounded workspace freshness and integrity failure behavior

**Files:** modify `offwork/project.py`, `offwork/capsule.py`, and `offwork/receipt.py`; extend `tests/test_project.py`, `tests/test_capsule.py`, and `tests/test_receipt.py`.

- [ ] Write a failing test for `fresh` immediately after capture.
- [ ] Write a failing test where a project file changes after capture; expect integrity `passed` and freshness `changed`.
- [ ] Write a failing manifest-tamper test; expect `CAPSULE_INTEGRITY_FAILED` and never `freshness=changed` as a substitute.
- [ ] Write a failing non-Git/insufficient-snapshot test; expect freshness `unavailable`.
- [ ] Write a failing nested-parent test proving unrelated parent changes do not affect freshness.
- [ ] Run targeted tests and verify RED for missing comparison behavior.
- [ ] Implement bounded dirty-file fingerprints and the three-state comparison.
- [ ] Run all Task 4 targets and verify GREEN.
- [ ] Commit with `feat: detect bounded workspace freshness`.

### Task 5: Add explicit human acceptance events

**Files:** modify `offwork/state.py`, `offwork/cli.py`, `offwork/receipt.py`, and `offwork/output.py`; extend `tests/test_receipt.py`.

- [ ] Write a failing test proving default acceptance is `pending`.
- [ ] Write a failing test proving successful automatic checks leave acceptance `pending`.
- [ ] Write failing CLI tests for explicit accept and reject, timestamp, optional note, and Task revision compare-and-swap.
- [ ] Write a failing test proving no other command changes human acceptance.
- [ ] Run the acceptance tests and verify RED.
- [ ] Implement append-only acceptance events and explicit nested Task commands.
- [ ] Run targeted and full tests; verify GREEN.
- [ ] Commit with `feat: record explicit handoff acceptance`.

### Task 6: Package and verify the five-minute prototype

**Files:** create `README.md` and `tests/test_demo.py`; modify only files required by failures discovered in the demo test.

- [ ] Write one end-to-end test that creates a temporary Git project, creates a Task, captures claims and Unknowns, shows a fresh Receipt, changes a file, shows `changed`, and explicitly accepts or rejects.
- [ ] Verify the same scenario through human and `--json` commands.
- [ ] Run the demo test and verify RED before adding documentation or fixes.
- [ ] Add README commands that exactly match the passing scenario.
- [ ] Run `python3 -m unittest discover -v` and require zero failures.
- [ ] Run `python3 -m compileall -q offwork tests` and require exit code 0.
- [ ] Run the README demo manually in a new temporary project and preserve its concise command/result transcript.
- [ ] Confirm the working tree contains no unrelated refactors or dependencies.
- [ ] Commit with `docs: publish offwork prototype demo`.

## 11. Prototype acceptance checklist

- [ ] Agent claims and Offwork checks are separate fields.
- [ ] An unexecuted check never appears as passed.
- [ ] Unknowns survive capture and appear in Receipt.
- [ ] Capsule integrity can pass while workspace freshness is `changed`.
- [ ] Manifest tampering returns an integrity failure.
- [ ] Human acceptance defaults to `pending`.
- [ ] Automatic checks never create human acceptance.
- [ ] Accept and reject occur only through explicit commands.
- [ ] Nested Git projects exclude unrelated parent changes.
- [ ] Human and JSON outputs express the same facts.
- [ ] JSON stdout contains exactly one envelope.
- [ ] No command uses `shell=True`.
- [ ] No command restores, overwrites, stashes, commits, or executes the suggested next step.
- [ ] The full standard-library test suite passes.

## 12. Stop conditions during implementation

Stop and request authorization before:

- adding a production dependency;
- adding a daemon, UI, cloud service, Agent controller, or Shell integration;
- weakening project-boundary or symlink checks;
- rewriting an existing Capsule;
- treating a test result as human, customer, deployment, or production acceptance;
- expanding the prototype into Automation Opportunity analysis.

The first release is successful when the five-minute storyline works honestly. It does not need to prove market demand, establish a fourteen-day validation gate, or become a general Agent platform.
