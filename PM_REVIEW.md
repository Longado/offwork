# Offwork Human PM Review

This file keeps the human product decision visible throughout development. It is not an Agent-generated approval log and it is separate from Capsule-level `human_acceptance`.

## Permanent rule

- Human PM review status is always one of `pending`, `accepted`, `rejected`, or `changes_requested`.
- The default is `pending`.
- Only an explicit decision from the user or a named human PM changes the status.
- Agents may prepare evidence and recommendations, but cannot record their own work as human acceptance.
- Tests, demos, commits, pushes, and multi-Agent reviews are evidence only.
- Product scope expansion, merge readiness, and release decisions must cite the current human PM decision.

## Current checkpoint

- Milestone: Loop 6 — Reviewable technical MVP
- Human PM reviewer: user
- Status: `accepted`
- Opened: 2026-08-29
- Decided: 2026-08-29T16:18:58+08:00
- Decision: technical MVP accepted and PR #2 authorized to merge into `main`; tag and release remain unauthorized

### Evidence prepared for review

- Branch: `codex/prototype-vertical-slice`
- GitHub review surface: PR #2, `codex/prototype-vertical-slice` → `main`
- Technical status: Loops 0–5 have implementation and reproducible evidence; Loop 6 clean-clone evidence is complete. Human PM completion is accepted for merge.
- Loop 0–2 evidence commits: `ac697f3`, `2c65c01`, `ac6d555`, `7c58a6a`, `7743cd7`, `b14622b`, and `397273e`.
- Loop 3 integration and blocking fixes: `86cff9f`, `c474172`, `d0b9b61`, `cb7e7a5`, `2262f2f`, and `77f9b7e`.
- Loop 5 real fresh-Agent records and protected-state evidence: `9468fd7`.
- Focused reliability verification: `python3 -m unittest tests.test_checks tests.test_project -v` passed 47 tests in 11.985 seconds.
- Integrated full verification at `2262f2f`: `python3 -m unittest discover -v` passed 144 tests in 176.866 seconds; `python3 -m compileall -q offwork tests` and `git diff --check` passed.
- History-free verification: `python3 -m unittest tests.test_history_free_demo -v` passed 6 tests. Fresh Agents returned `continue`, `verify`, and `stop`; every required citation matched the supplied CLI envelope. Versioned before/after evidence proves unchanged HEAD, Git index, project-file digests, and acceptance events; all three remained `pending`.
- Real MVP demonstration: 16 JSON-mode commands each produced one valid `offwork.cli/v1` envelope. An Agent claim of passed tests remained separate from an Offwork failed check; an unconfigured check stayed `not_run`; freshness moved from `fresh` to `changed` while integrity stayed `passed`; manifest tampering returned `CAPSULE_INTEGRITY_FAILED` with freshness `not_evaluated`; only explicit `task accept` changed human acceptance and recorded time and note.
- Credential preflight regression at `77f9b7e`: nested URL user-info, `--token`, and Authorization values inside a valid argv string are rejected before persistence; focused checks passed 17 tests, and SQLite, Capsule state, and JSON output contained no sentinel secret.
- Final Loop 6 clean clone at `77f9b7e`: `bin/offwork --help` passed; `bin/offwork --version` returned `offwork 1.0.0`; `python3 -m unittest discover -v` passed 146 tests in 129.868 seconds; compileall and diff check passed; the README five-minute flow passed with claim/check separation, `fresh` → `changed`, integrity still `passed`, acceptance initially `pending`, and explicit acceptance recorded. The clone remained clean.
- Verified environment: macOS 26.4.1 arm64, Python 3.9.6, Apple Git 2.50.1.

### Not implemented

- Agent orchestration or automatic control of external Agents;
- daemon, TUI, Web UI, Shell-history capture, or automatic workflow execution;
- Automation Opportunity analysis;
- cloud synchronization, shared team index, signatures, trusted timestamps, SSO, RBAC, compliance export, deployment status, or customer acceptance;
- verified Windows support or independent Linux CI evidence.

### Current risks and limits

1. Configured checks execute explicitly authorized local programs; timeout and output bounds are not an OS sandbox.
2. Capsule integrity proves local hash-chain consistency, not external identity, signatures, trusted time, or non-repudiation.
3. Workspace freshness intentionally excludes ignored files, databases, environment variables, external services, deployment, and production state.
4. POSIX process cleanup has a narrow process-launch interruption window recorded as a non-blocking P2; removing it would require a broader supervisor design.
5. Rename/copy metadata can shorten the second path reported in `offwork_observed.changed_paths`; workspace fingerprint comparison still detects the content change, but the displayed captured path can be inaccurate.
6. The passing suite emits non-fatal SQLite `ResourceWarning` messages in some tests; no state failure was observed, but connection-lifecycle cleanup remains a maintenance risk.
7. The clean-clone proof covers one macOS environment. Linux is intended but not independently verified; Windows support is not claimed.
8. Human PM explicitly authorized merging PR #2. Tag and release remain pending separate authorization.

### Agent recommendation

Merge PR #2 into `main` as explicitly authorized. Do not create a tag or release without a separate human instruction.

### Human PM decision

- Status: `accepted`
- Reviewer: user
- Decided at: 2026-08-29T16:18:58+08:00
- Decision note: User explicitly instructed “合并” after reviewing the technical MVP report; this accepts the milestone for merge.
- Approved next scope: merge PR #2 into `main` only; no tag or release

## Review history

### 2026-08-29 — Technical MVP accepted for merge

- Reviewer: user
- Status: `accepted`
- Decision: Merge PR #2 from `codex/prototype-vertical-slice` into `main`.
- Boundary: This authorizes the merge only. It does not authorize a tag, GitHub release, deployment, or scope expansion.

### 2026-08-29 — Continuous execution through all seven loops

- Reviewer: user
- Status: `accepted`
- Decision: Execute the complete Loop 0–6 prototype evolution without pausing for a new authorization between loops; use small commits and push progress to GitHub.
- Boundary: This authorizes implementation only. It does not automatically accept any loop's evidence or approve merge, tag, or release.

### 2026-08-29 — Evolution plan and Loop 1 authorization

- Reviewer: user
- Status: `accepted`
- Decision: The loop sequence in `EVOLUTION_PLAN.md` is approved, and Loop 1 trust-boundary implementation is authorized.
- Boundary: This decision does not approve Loop 1 completion, Loop 2, merge, tag, or release. Those require later explicit human PM decisions.
