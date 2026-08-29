# Offwork Prototype Development Plan

> **Execution rule:** implement every behavior test-first. Each task starts with a focused failing test, proves the expected RED, adds the smallest production code, and proves GREEN before continuing.

**Product contract:** [`PRD_V1.0.md`](./PRD_V1.0.md) is authoritative for product behavior and terminology. This plan defines implementation order only; it must not silently change the PRD.

**Goal:** Build a five-minute, zero-install prototype that proves a history-free Agent can inspect a structured Handoff Receipt, detect whether the explicit Git project changed after capture, and safely choose the correct first step.

**Architecture:** Offwork is a local-first Python CLI. Each project owns a private SQLite database and immutable Capsule directories under `.offwork/`. A Receipt is a derived view over immutable capture evidence, persisted check results, current project freshness, and explicit Capsule-level human acceptance. It is not a workflow engine.

**Tech stack:** Python 3.9+ standard library, `argparse`, `sqlite3`, `subprocess` with argv and `shell=False`, `hashlib`, `json`, `pathlib`, `unittest`, and system Git.

## 1. Current baseline

At the start of implementation the repository contains only product and development documents. There is no historical source tree or test suite to preserve.

Before each implementation task:

1. inspect `git status --short --branch`;
2. preserve unrelated user changes;
3. run the currently available full test suite;
4. record the real result rather than assuming an earlier task is complete.

Do not copy source or tests from `offwork-capsule`. It may be read only as historical reference.

## 2. Prototype proof

The first prototype must demonstrate this full path:

```text
init
  → task add
  → capture
  → Receipt fresh
  → modify project file
  → same Capsule reports changed
  → explicit accept or reject
  → fresh Agent selects the correct first decision
```

The demo must visibly prove:

- an Agent claim cannot create an Offwork check result;
- Unknowns, open loops, and next step survive capture and resume;
- Capsule integrity and workspace freshness are independent;
- an automatic check cannot create human acceptance;
- acceptance targets one explicit Capsule and one observed Task revision;
- `resume` never executes the suggested next step or modifies the project;
- a new Agent can decide `continue`, `verify`, or `stop` without the old Session.

## 3. Fixed state boundaries

Never collapse these into one `verified` flag.

| Field | Meaning | Allowed status |
| --- | --- | --- |
| `agent_claimed` | Text supplied by capture context | structured text, never pass/fail |
| `offwork_observed` | Facts collected inside the explicit project | structured snapshot |
| `auto_checked` | Commands Offwork actually attempted during capture | `not_run`, `passed`, `failed`, `unavailable` |
| `handoff_verified.integrity` | Capsule payload bytes match the trusted manifest hash chain | `passed`, `failed` |
| `handoff_verified.completeness` | Required handoff fields exist and parse | `complete`, `incomplete` |
| `handoff_verified.restore` | Published Capsule can be reloaded and reproduce the canonical Receipt projection | `passed`, `failed` |
| `workspace_freshness` | Current bounded Git snapshot compared with capture | `fresh`, `changed`, `unavailable` |
| `human_acceptance` | Explicit local response to one Capsule | `pending`, `accepted`, `rejected` |

Terms retain the limitations in PRD 1.0: integrity is local self-consistency, freshness excludes ignored/external state, and human acceptance is not approval, release, customer acceptance, or legal signature.

## 4. Fixed command surface

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

offwork task show TASK_ID \
  [--capsule CAPSULE_ID] \
  --project PATH [--json]

offwork resume \
  --task TASK_ID \
  [--capsule CAPSULE_ID] \
  --project PATH [--json]

offwork task accept TASK_ID \
  --capsule CAPSULE_ID \
  --if-revision N \
  [--note TEXT] \
  --project PATH [--json]

offwork task reject TASK_ID \
  --capsule CAPSULE_ID \
  --if-revision N \
  [--note TEXT] \
  --project PATH [--json]
```

Rules:

- do not add a top-level `receipt` command;
- `capture`, `task show`, and `resume` call the same `build_receipt()` function;
- read-only commands may default to the Task's persisted latest Capsule but must return the resolved Capsule ID;
- acceptance commands never infer “latest”; they require Capsule ID and expected revision;
- `resume` renders facts only and never executes `next_step`.

## 5. Fixed structured contracts

### 5.1 Capture context

The parser implements the exact PRD 1.0 contract:

```json
{
  "summary": "已实现 Token 刷新修复并补充测试",
  "agent_claims": ["登录失败已经修复", "测试全部通过"],
  "unknowns": ["旧 Token 迁移行为尚未确认"],
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

`summary` and `next_step` are required. Array fields may be empty. Offwork never infers missing outcomes and never executes `next_step`.

### 5.2 Receipt

The authoritative shape is PRD 1.0 section 9. The implementation must preserve at least:

- Task ID, title, goal, captured revision, and current revision;
- Capsule ID and capture time;
- Agent summary and claims;
- explicit project identity, path, Git metadata, and observed paths;
- every configured check with argv, cwd, status, return code, and timestamps;
- integrity, completeness, and restore round-trip status;
- Unknowns, open loops, and next step without transformation or silent removal;
- freshness status, changes, scope, limitations, and check time;
- human acceptance status, time, and note.

Human output is rendered exclusively from this object. Tests compare a canonical user-facing fact projection, not merely whether both renderers called the same function.

### 5.3 JSON envelope

Every `--json` command writes exactly one object to stdout:

```json
{
  "schema_version": "offwork.cli/v1",
  "ok": true,
  "command": "task.show",
  "data": {}
}
```

Errors use `ok=false` and stable `error.code`, `error.message`, and `error.details`. Diagnostics and child-process output never enter stdout. Argument errors, missing project/Task/Capsule, revision conflict, and integrity failure must follow this contract and return nonzero.

## 6. Storage and publication contracts

Project-private state lives under `<project>/.offwork/`:

```text
.offwork/
├── project.json
├── state.sqlite3
├── state.lock
└── capsules/
    └── <capsule-id>/
        ├── capsule.json
        ├── checks.json
        ├── restore-test.json
        └── manifest.json
```

Permissions:

- `.offwork/`, staging, and Capsule directories: `0700`;
- state, JSON, manifest, SQLite auxiliary files, and lock files: `0600`;
- existing paths are checked for owner, type, mode, and symlinks before reuse;
- fixed paths and Capsule members reject symlinks;
- archive paths are normalized `.offwork`-relative paths and reject absolute paths and `..`.

SQLite contains mutable truth:

- Tasks and current revision;
- explicit current/latest Capsule relation;
- Capsule registration and manifest raw-byte hash;
- append-only human acceptance events.

All connections enable foreign keys and schema version checks. All mutations use one `StateService` transaction boundary. Receipt reads use one consistent SQLite snapshot.

Capsule publication order is fixed:

```text
write private staging
  → fsync payloads, manifest, and staging directory
  → atomic rename to capsules/<capsule-id>
  → fsync capsules directory
  → register Capsule and current relation in SQLite
```

Startup/read reconciliation may register a fully published, valid, unregistered Capsule idempotently. It never exposes incomplete staging or registers a missing Capsule directory.

## 7. Check runner contract

- checks run only during `capture`;
- configured strings are parsed to argv and executed with `shell=False`;
- cwd is the canonical project path;
- zero configured checks produce `not_run`;
- a non-empty set is `passed` only when every check starts, finishes before its limit, and returns zero;
- any nonzero return produces `failed`;
- spawn error, timeout, interruption, or incomplete execution produces `unavailable`;
- `failed` or `unavailable` is never hidden by another successful check;
- per-command and total capture timeouts are fixed and tested;
- stdout/stderr contents are not persisted in V1.0;
- the runner is explicitly trusted local execution, not an OS sandbox.

Run checks before collecting the final capture snapshot. A check-generated project change therefore belongs to the captured state.

## 8. Bounded Git freshness contract

Freshness is limited to the canonical `--project` boundary.

The snapshot records project identity, canonical path, Git root metadata, branch, full HEAD metadata, and fingerprints for project-local tracked/untracked regular files and symlinks. It records deletion, rename, type, and mode. `.offwork/` is excluded.

For a project nested inside a parent repository:

- every Git worktree query uses a project pathspec and NUL-delimited parsing;
- project-external worktree paths are never part of the result;
- parent HEAD and branch are observation metadata only;
- an outside-only dirty change or commit cannot produce `changed`;
- project-local content identity determines freshness.

Return `unavailable` for unreadable required paths, unstable concurrent scans, unsupported gitlinks/submodules/nested `.git`, unverifiable project identity, or older snapshots without required fields.

`changed` never alters Capsule integrity. Integrity failure stops normal Receipt loading; freshness is then not evaluated.

## 9. Human acceptance contract

- no event means `pending`;
- only explicit accept/reject commands append events;
- every event binds a valid Capsule to its owning Task;
- mutation uses `--if-revision` compare-and-swap inside one transaction;
- revision conflict makes no change and returns a stable error;
- later explicit decisions may append a new event;
- the valid event with the highest Task revision is current;
- automated checks and every read-only command leave acceptance untouched.

## 10. Implementation tasks

### Task 1: Bootstrap the zero-install CLI and private project state

**Create:** `pyproject.toml`, `bin/offwork`, package entry points, CLI/output/error/project/state modules, and initial test helpers.

- [ ] Write startup tests for repository-external execution of `bin/offwork --help`, `--version`, and `init`.
- [ ] Assert help/version do not create `.offwork`.
- [ ] Assert `init` creates private project state and one valid JSON envelope.
- [ ] Assert a nested project keeps its explicit path rather than adopting the parent Git root.
- [ ] Assert unsafe existing `.offwork` types, modes, ownership, and symlinks fail closed.
- [ ] Run targeted tests and observe expected RED because the package does not exist.
- [ ] Implement only enough CLI, project validation, permissions, SQLite schema/version, and envelope handling to pass.
- [ ] Run targeted and full tests; require GREEN.
- [ ] Commit `feat: initialize standalone offwork projects`.

### Task 2: Build the first vertical Task → Capsule → Receipt path

**Create/modify:** Task state, context parser, Capsule writer, Receipt builder, human renderer, and focused tests.

- [ ] Write failing tests for Task creation and revision, unknown Task, context validation, and immutable Capsule ID.
- [ ] Write a failing test proving claims, Unknowns, open loops, and next step survive capture and `resume` unchanged.
- [ ] Write a failing test proving a claim that tests passed cannot create `auto_checked=passed` when no check is configured.
- [ ] Write failing tests for explicit resolved Capsule ID and captured/current Task revisions.
- [ ] Write failing human/JSON canonical fact parity and single-envelope tests.
- [ ] Observe RED for missing Task/Capsule/Receipt behavior.
- [ ] Implement the smallest happy path with `auto_checked=not_run` and initial freshness `unavailable`.
- [ ] Reload the published Capsule rather than rendering from the staging object.
- [ ] Run targeted and full tests; require GREEN.
- [ ] Commit `feat: capture structured handoff receipts`.

### Task 3: Add real checks and bounded workspace freshness

**Modify:** command runner, project snapshot, Capsule/Receipt fields, and tests.

- [ ] Write failing tests for argv + `shell=False`, canonical cwd, zero/nonzero/spawn-error/timeout, and multi-check aggregation.
- [ ] Write a failing test proving child output cannot corrupt JSON stdout.
- [ ] Write failing `fresh`, `changed`, and `unavailable` tests.
- [ ] Write a failing test where checks modify a tracked file and the final post-check snapshot is immediately fresh.
- [ ] Write failing nested-parent tests for outside dirty changes and outside-only commits.
- [ ] Write a failing test for unsupported/unreliable Git states returning `unavailable`.
- [ ] Observe RED for missing runner and snapshot behavior.
- [ ] Implement the bounded runner, post-check snapshot, and three-state comparison.
- [ ] Run targeted and full tests; require GREEN.
- [ ] Commit `feat: verify checks and workspace freshness`.

### Task 4: Harden Capsule integrity, publication, and restore

**Modify:** Capsule storage, StateService reconciliation, Receipt error path, and tests.

- [ ] Write failing fixed-member, schema-version, raw-byte hash, path escape, and symlink tests.
- [ ] Write a failing manifest-tamper test expecting `CAPSULE_INTEGRITY_FAILED`, nonzero exit, and freshness not evaluated.
- [ ] Write failing publication-boundary tests proving DB never exposes a missing Capsule.
- [ ] Write a failing orphan reconciliation test for a durable valid Capsule.
- [ ] Write a failing restore round-trip test that reloads from the published directory.
- [ ] Observe RED for missing integrity and recovery behavior.
- [ ] Implement the fixed verification order and durable publication protocol.
- [ ] Run targeted and full tests; require GREEN.
- [ ] Commit `feat: verify immutable capsule recovery`.

### Task 5: Add explicit Capsule-level human acceptance

**Modify:** StateService, CLI, Receipt renderer, and tests.

- [ ] Write a failing test proving default acceptance is `pending`.
- [ ] Write a failing test proving successful automatic checks leave it `pending`.
- [ ] Write failing accept/reject tests requiring Capsule ID and expected revision.
- [ ] Write a failing stale-revision test proving no event is appended.
- [ ] Write a failing cross-Task Capsule binding test.
- [ ] Write a failing explicit later-decision test using the highest valid revision.
- [ ] Write a failing test proving no other command changes acceptance.
- [ ] Observe RED for missing acceptance behavior.
- [ ] Implement append-only events and revision CAS in one transaction.
- [ ] Run targeted and full tests; require GREEN.
- [ ] Commit `feat: record explicit capsule acceptance`.

### Task 6: Verify the five-minute prototype

**Create/modify:** `README.md`, end-to-end tests, and only fixes required by those tests.

- [ ] Add an integration test for init → task add → capture → fresh → file change → changed → explicit accept/reject.
- [ ] Verify claims/checks, Unknowns/open loops/next step, integrity/freshness, and acceptance remain distinct.
- [ ] Verify the exact scenario in both human and JSON modes.
- [ ] Verify `resume` does not execute a sentinel next step or change HEAD/index/project files.
- [ ] Run the integration test as a regression; if prior tasks are complete it should be GREEN, not artificially forced RED.
- [ ] Add README commands matching the passing scenario exactly.
- [ ] Run `python3 -m unittest discover -v` and require zero failures.
- [ ] Run `python3 -m compileall -q offwork tests` and require exit code 0.
- [ ] Run the README demo manually in a new temporary project and record concise commands, key results, and elapsed time.
- [ ] Start a new history-free Agent Session with only the project path and selected `resume --json` Receipt.
- [ ] Verify preset `continue`, `verify`, and `stop` cases; changed + Unknown must choose `verify` and cite `next_step`.
- [ ] Confirm no unrelated dependencies, refactors, or platform features entered the tree.
- [ ] Commit `docs: publish offwork prototype demo`.

## 11. Acceptance checklist

- [ ] Agent claims and Offwork checks are separate facts.
- [ ] An unexecuted or incomplete check never appears as passed.
- [ ] Unknowns, open loops, and next step survive capture/resume.
- [ ] Multiple Capsules for one Task remain explicitly addressable.
- [ ] Human writes bind Capsule ID and expected Task revision.
- [ ] Integrity and restore can pass while freshness is `changed`.
- [ ] Manifest tampering produces integrity failure and skips freshness.
- [ ] Nested Git projects exclude outside dirty changes and commits.
- [ ] Human and JSON render the same canonical user-facing facts.
- [ ] JSON stdout contains exactly one envelope.
- [ ] No command uses `shell=True`.
- [ ] No read-only command executes `next_step` or modifies project files.
- [ ] Automatic checks never create human acceptance.
- [ ] Full standard-library tests and compileall pass.
- [ ] The temporary-project demo completes within five minutes.
- [ ] A history-free Agent makes the expected first decision from Receipt alone.

## 12. Stop conditions

Stop and request authorization before:

- adding a production dependency;
- adding a daemon, UI, cloud service, Agent controller, or Shell integration;
- weakening project-boundary, path, permission, or symlink checks;
- automatically executing `next_step` or modifying the user's workspace;
- rewriting an existing Capsule;
- treating an automatic result as human, customer, deployment, or production acceptance;
- adding Automation Opportunity, Shell history analysis, aliases, or workflow execution.

The prototype succeeds when the five-minute storyline works honestly. It does not need to prove market demand, pass a fourteen-day validation gate, or become a general Agent platform.
