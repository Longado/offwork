# Offwork Prototype Evolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` or `executing-plans` to implement one approved loop at a time. Do not start the next loop until its human PM checkpoint is explicit.

**Goal:** Evolve Offwork from a working vertical slice into an honest, five-minute prototype that proves a history-free Agent can safely decide whether to continue, verify, or stop from a trustworthy Handoff Receipt.

**Architecture:** Keep the existing local-first Python CLI, project-private SQLite state, immutable Capsule directories, derived Receipt, bounded Git freshness, and explicit human acceptance. Each loop closes one trust or decision gap without adding orchestration, remote control, Shell history, or a general Agent platform.

**Tech Stack:** Python 3.9+ standard library, `argparse`, `sqlite3`, `subprocess` with argv and `shell=False`, `hashlib`, `json`, `pathlib`, `unittest`, and system Git.

---

## 1. North-star outcome

The prototype is successful when this event can be demonstrated honestly:

> An interrupted coding task is captured by one Agent. A different Agent receives only the explicit project path and one Receipt, detects whether that evidence still matches the current workspace, chooses `continue`, `verify`, or `stop`, cites the evidence behind its choice, and waits for an explicit human decision before the handoff is accepted.

The prototype does not need to prove market demand, run a fourteen-day validation gate, or become a team platform. It must prove the mechanism and its failure behavior.

## 2. Loop operating model

Every loop follows the same sequence:

```text
human PM authorizes one loop
  → add focused failing tests
  → implement the smallest complete change
  → run targeted and full verification
  → commit coherent progress slices
  → push the feature branch
  → prepare evidence and remaining risks
  → human PM accepts, rejects, or requests changes
```

Rules:

- `PM_REVIEW.md` always shows the current checkpoint and defaults to `pending`.
- Agent review, tests, commits, pushes, and demos are evidence, not human acceptance.
- One loop answers one product question and ends in observable behavior.
- Use multiple small commits where changes can be independently verified and reverted.
- Do not mix later-loop polish or optional product ideas into the current loop.
- A failed acceptance condition stays visible; it is not rewritten as success.

## 3. Evolution overview

| Loop | Product question | Expected user-visible effect | Exit evidence |
| --- | --- | --- | --- |
| 0. Vertical slice and audit | Does the end-to-end shape exist, and where can it lie? | Users can run the basic flow; known trust gaps are explicit rather than hidden | Six implementation commits, 37-test baseline, multi-Agent findings, PM status `pending` |
| 1. Trust boundary | Can a failed or read-only command mutate or escape the project? | Failed acceptance leaves no hidden state; viewing a Receipt cannot execute repository hooks or cross project boundaries | Adversarial acceptance, symlink, fsmonitor, path-boundary, and later-orphan tests pass |
| 2. Evidence consistency | Can a Receipt claim completeness while its data or database state is contradictory? | Every displayed complete Receipt is parseable, restored from published bytes, and built from one consistent state | Strict context/schema tests, consistent-read tests, restore/reconciliation tests pass |
| 3. Freshness and check reliability | Does `fresh` mean a reliable bounded comparison, and do checks terminate predictably? | Changed work is detected; unstable work returns `unavailable`; checks cannot hang beyond the capture budget | Pathspec, Git-boundary, concurrent-scan, process-tree, deadline, and output-bound tests pass |
| 4. Decision-grade Receipt | Do a human and an Agent receive the same facts and understand the evidence boundary? | Human and JSON views support the same decision; claims cannot visually masquerade as verification | Canonical parity, safe-text, error-detail, claim/check contradiction, and copy-paste demo tests pass |
| 5. History-free recovery proof | Can a new Agent choose the correct first action without old Session history? | Preset `continue`, `verify`, and `stop` cases produce the expected evidence-citing decision | Three black-box runs, unchanged workspace proof, elapsed-time record, human PM decision |
| 6. Reviewable prototype release | Can another developer clone and reproduce the proof without help? | A clean POSIX environment can run the five-minute story and understand its limits | Clean-clone run, full suite, compileall, exact README demo, PM-approved merge/release decision |

## 4. Loop 0 — Vertical slice and audit

**Status:** Technical evidence collected; human PM review remains `pending`.

**Already produced:**

- standalone zero-install CLI and private project state;
- Task → Capsule → Receipt path;
- checks, basic project freshness, Capsule integrity, restore, and explicit acceptance;
- 37 passing standard-library tests and a temporary-project demo;
- multi-Agent review of product content, security, storage, CLI, and adversarial gaps.

**Expected effect:** The product can be discussed from running evidence instead of a speculative feature list. It is not yet merge-ready because known P1 trust failures remain.

**PM decision:** Approve, reject, or change the Loop 1 remediation order.

## 5. Loop 1 — Make the trust boundary non-negotiable

**Product question:** Can the user trust that Offwork does not secretly change acceptance, execute repository-configured code during read-only inspection, or cross the explicit project boundary?

**Files expected to change:**

- `offwork/cli.py` and `offwork/state.py` for validation-before-mutation and transactional acceptance;
- `offwork/project.py` for fixed-path validation, safe Git invocation, and no-follow project reads;
- `offwork/capsule.py` and `offwork/state.py` for later-Capsule reconciliation;
- focused cases in `tests/test_project.py`, `tests/test_capsule.py`, and `tests/test_receipt.py`.

**Implementation slices and progress commits:**

1. `fix: make human acceptance fail atomically`
   - Add a failing tampered-Capsule accept/reject test.
   - Prove exit failure leaves Task revision and acceptance events unchanged.
   - Validate the target Capsule before the acceptance transaction.
2. `fix: enforce private state path boundaries`
   - Add owner/type/mode/symlink tests for every fixed state path.
   - Reject unsafe existing database, lock, project metadata, and Capsule directories before reuse.
3. `fix: make receipt inspection inert`
   - Add a repository `core.fsmonitor` execution sentinel.
   - Disable fsmonitor for every Git observation and apply a fixed Git timeout.
   - Reject any intermediate symlink encountered while reading a project file.
4. `fix: reconcile later published capsules safely`
   - Add a second-capture crash fixture where publication succeeds and SQLite registration does not.
   - Reconcile exactly one valid next-revision Capsule; fail closed on ambiguity.

**Expected effect:**

- a command that reports failure has made no hidden acceptance change;
- `show` and `resume` are operationally read-only within the stated boundary;
- state files and workspace scanning cannot follow symlinks outside the project;
- the newest fully published valid handoff can be recovered after interruption.

**Exit evidence:** Focused adversarial tests, full `unittest`, `compileall`, clean diff check, pushed commits, and an updated PM checkpoint.

## 6. Loop 2 — Make every Receipt internally truthful

**Product question:** Can malformed input, a partial write, a schema mismatch, or concurrent state change produce a Receipt that calls itself complete or combines facts that never coexisted?

**Files expected to change:**

- `offwork/capsule.py` for strict capture context projection, complete writes, and derived restore evidence;
- `offwork/state.py` for schema-version enforcement and a single Receipt read transaction;
- `offwork/receipt.py` for one canonical state projection;
- context, recovery, schema, and concurrency cases under `tests/`.

**Implementation slices and progress commits:**

1. `fix: validate and minimize capture context`
   - Accept only the five contracted context fields.
   - Validate every claim, Unknown, and open-loop element before checks or publication.
   - Reject unknown transcript-like fields rather than persisting them.
2. `fix: derive completeness and restore from published data`
   - Build completeness from validated required fields.
   - Mark restore passed only after the published Capsule rebuilds the canonical projection.
3. `fix: build receipts from one state snapshot`
   - Read Task, selected Capsule, and current acceptance through one SQLite connection and explicit read transaction.
4. `fix: enforce state schema and complete writes`
   - Reject unknown future schemas and unsupported historical schemas with a stable envelope.
   - Loop on file writes until every byte is written before fsync and publication.

**Expected effect:** A complete Receipt is actually parseable and recoverable; its revision, Capsule, and human status describe one real database state; unsupported storage fails clearly instead of being silently relabeled.

**Exit evidence:** Malformed-context, unknown-field, short-write, schema-version, consistent-read, and restore tests pass in human and JSON modes.

## 7. Loop 3 — Make freshness and checks reliable under pressure

**Product question:** Can Offwork distinguish `fresh`, `changed`, and `unavailable` without hanging, leaking unbounded output, or overlooking an unstable Git project?

**Files expected to change:**

- `offwork/checks.py` for process-group termination, output bounds, per-check and total deadlines;
- `offwork/project.py` for literal pathspecs, Git-root identity, nested-repository handling, and stable double scans;
- `offwork/capsule.py` and Receipt fields only where the verified result contract requires it;
- focused reliability tests under `tests/test_capsule.py` and `tests/test_receipt.py`.

**Implementation slices and progress commits:**

1. `fix: bound check execution and output`
   - Start checks in an isolated process group on supported POSIX systems.
   - Terminate the complete process group on timeout.
   - Enforce a total capture deadline and bounded stdout/stderr collection.
2. `fix: keep check credentials out of receipts`
   - Define and test the V1 rule that secrets must not be embedded in persisted argv.
   - Reject case-insensitive `Authorization`, `--password`, `--token`, `--api-key`, their `--name=value` forms, and URLs containing user-info credentials.
   - Document use of a project-local wrapper when a check must obtain a secret outside argv; the wrapper path remains auditable while the secret does not enter the Receipt.
3. `fix: make git project addressing literal`
   - Use a literal top-level pathspec for nested projects.
   - Preserve valid Unix backslashes and compare the Git-root relationship with capture.
4. `fix: fail closed on unstable workspace scans`
   - Detect nested `.git`, unsupported gitlinks, changing path sets, and changing fingerprints.
   - Return `unavailable` whenever two stable observations cannot be obtained.

**Expected effect:** `fresh` becomes a positive result only for a stable, bounded project snapshot. Races and unsupported layouts become `unavailable`; timeout behavior is predictable and leaves no running check process tree.

**Exit evidence:** Controlled race, unusual path, nested repository, process-tree timeout, total-deadline, output-limit, and credential-argument tests pass.

## 8. Loop 4 — Make the Receipt decision-grade

**Product question:** Will a human reading terminal output and an Agent reading JSON make the same decision from the same facts?

**Files expected to change:**

- `offwork/output.py` for complete canonical rendering and Unicode-safe visible text;
- `offwork/cli.py` for stable nested command names and JSON-aware help/error behavior;
- `offwork/receipt.py` only if a missing canonical field is required;
- `README.md` and parity tests.

**Implementation slices and progress commits:**

1. `fix: render the canonical receipt facts`
   - Include open-loop disposition/note, freshness scope/limitations, acceptance time/note, check cwd/argv/timestamps, Capsule ID, and revisions.
2. `fix: preserve failure facts across renderers`
   - Human integrity errors show the target Capsule, integrity failure, and freshness not evaluated.
   - Nested JSON envelopes use stable command names such as `task.accept`.
3. `fix: escape untrusted terminal text`
   - Escape C0, C1, ANSI, bidirectional, and line-separator controls without altering normal content.
4. `docs: make the five-minute contradiction visible`
   - Provide a copy-paste context file.
   - Demonstrate an Agent claim of success beside a controlled failed or unavailable Offwork check.

**Expected effect:** The Receipt stops being a data dump and becomes a consistent decision surface. A claim cannot visually impersonate a check, and neither renderer hides material limitations.

**Exit evidence:** Unique sentinel facts appear in both outputs, every JSON mode returns one envelope, hostile text cannot forge headings, and the README commands run exactly as written.

## 9. Loop 5 — Prove history-free recovery

**Product question:** Does Offwork help a new Agent begin safely without reopening the old Session?

**Artifacts expected to change:**

- `tests/test_demo.py` for deterministic fixture construction and non-mutation assertions;
- `README.md` for the exact black-box exercise;
- `PM_REVIEW.md` for the human decision and evidence references;
- a concise, versioned demo record if the run output is small and free of sensitive data.

**Three required cases:**

| Case | Receipt facts | Expected first decision |
| --- | --- | --- |
| Continue | integrity/restore passed, workspace fresh, checks passed, no blocking Unknown | `continue`, citing the verified next step |
| Verify | integrity passed, workspace changed or a material Unknown/open loop remains | `verify`, citing the exact uncertainty and safe next check |
| Stop | integrity failed, project identity unavailable, or evidence is contradictory | `stop`, citing the failed trust boundary and avoiding execution |

**Execution steps:**

1. Create each case in a new temporary Git project.
2. Start a new Agent Session with only the project path and selected `resume --json` output.
3. Require one structured decision, cited Receipt facts, and one proposed first action.
4. Verify the Agent did not change HEAD, index, project files, or acceptance state.
5. Record the commands, decision output, result, and elapsed time.
6. Present the three records to the human PM; do not self-accept them.

**Expected effect:** The product's central promise is demonstrated behaviorally. The evidence shows not merely that a Receipt can be printed, but that it changes a new Agent's first decision in the intended safe direction.

**Exit evidence:** All three cases pass, the changed + Unknown case selects `verify`, the stop case performs no action, the exercise completes within five minutes, and the human PM records an explicit decision.

## 10. Loop 6 — Produce a reviewable prototype release

**Product question:** Can a developer outside the implementation Session reproduce the proof from GitHub without assistance?

**Files expected to change:** `README.md`, version metadata only if needed, release notes, and no new runtime subsystem.

**Implementation slices and progress commits:**

1. `docs: declare the supported prototype environment`
   - State the tested POSIX/macOS/Linux boundary and do not imply Windows support without CI evidence.
2. `docs: publish the exact prototype walkthrough`
   - Replace placeholders with commands that create and extract all required IDs using the Python standard library.
3. `test: verify the clean-clone prototype`
   - Run help, version, full tests, compileall, and the five-minute demo from a new clone or archive.
4. `docs: prepare the PM release receipt`
   - List the exact commit, checks, known limitations, unsupported environments, and unresolved risks.

**Expected effect:** A new developer can clone the repository, reproduce the story, and understand precisely what Offwork proves and does not prove.

**Exit evidence:** Clean-clone commands pass, the branch is clean and pushed, and the human PM explicitly decides whether to merge or release. No tag, merge, or release is created from Agent inference.

## 11. Prototype completion definition

The prototype is complete only when all of the following are true:

- Loops 1–5 meet their acceptance evidence;
- P1 trust-boundary findings are closed with regression tests;
- `fresh` is never used when the project comparison is unreliable;
- human and JSON Receipt views expose the same decision-relevant facts;
- three history-free Agent cases produce the expected decision without workspace mutation;
- the five-minute clean-run storyline succeeds;
- the human PM explicitly accepts the prototype milestone.

Passing automated tests alone is insufficient. A human PM acceptance alone is also insufficient if the required technical evidence is absent.

## 12. Explicitly parked after the prototype

The following are not scheduled loops and require a new PRD plus explicit human PM authorization:

- Automation Opportunity or Shell-history analysis;
- shared team Receipt index or cloud synchronization;
- signatures, trusted timestamps, organizational identity, SSO, RBAC, or audit export;
- GitHub/GitLab checks or enterprise integrations;
- daemon, Web UI, TUI, Agent orchestration, or automatic workflow execution.

These may become later product hypotheses, but none is required to prove the current prototype.

## 13. Verification and commit discipline

Every implementation slice runs:

```bash
python3 -m unittest <focused test module> -v
python3 -m unittest discover -v
python3 -m compileall -q offwork tests
git diff --check
```

Each progress report records:

- branch and exact commit;
- focused and full verification results;
- user-visible effect achieved;
- claims not yet verified;
- remaining risks;
- current human PM review status.

The next authorized implementation target is **Loop 1 — Make the trust boundary non-negotiable**.
