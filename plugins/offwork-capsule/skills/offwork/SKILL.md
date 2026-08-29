---
name: offwork
description: "Use when the user invokes Offwork, /offwork, $offwork, asks to package current work for another Agent Session, or wants to resume or review a trusted Capsule handoff."
---

# Offwork

Turn the current project state into a concise Offwork Capsule Receipt, or resume an existing handoff. Prefer the installed `offwork` command; fall back to this repository's `bin/offwork` launcher.

## Commands

- `$offwork capture` or `/offwork`: capture the current work. This is the default action.
- `$offwork resume <task-id> [capsule-id]`: render the handoff for a fresh Session.
- `$offwork status <task-id> [capsule-id]`: show the current Receipt.
- `$offwork accept ...` and `$offwork reject ...`: record a human decision only when the user explicitly requests that exact action.

## Capture

1. Resolve the active Git project from the current workspace. Ask for a path only when there is no unambiguous project boundary.
2. Inspect the current work and conversation. Build the smallest valid context with `summary`, `agent_claims`, `unknowns`, `open_loops`, and one concrete `next_step`.
3. Run `offwork init --project <project> --json` when the project has no `.offwork` state.
4. Create one Task from the current goal. Add check commands only when the user explicitly supplied or authorized them. With no authorized check, omit `--check`; `auto_checked` must remain `not_run`.
5. Write the context JSON to a private temporary file outside the Git project, run one `offwork capture ... --json`, then render the same Capsule with `offwork task show`.
6. Report the Task ID, Capsule ID, exact next step, check status, integrity, workspace freshness, and human acceptance.

Treat these as separate evidence lanes:

- `agent_claimed`: what the Agent said.
- `offwork_observed`: what Offwork observed in the explicit project.
- `auto_checked`: only checks Offwork actually attempted.
- `handoff_verified`: Capsule integrity and restore evidence.
- `human_acceptance`: changed only by an explicit accept or reject operation.

Never turn an Agent claim into a passed check. Never turn `auto_checked=passed` into human acceptance. Put unresolved facts in Unknowns. Workspace changes affect freshness, not Capsule integrity.

## Resume and status

Use the supplied Task and optional Capsule IDs with `offwork resume` or `offwork task show`. Render the Receipt and identify the first action. Do not execute the next step as part of resume.

If an ID is missing, recover it from evidence already present in the conversation or ask for it. Do not guess identifiers.

## Human decision

Before accept or reject, render the current Receipt and use its current Task revision. Execute the requested decision once with the user's note. Automatic checks never perform this step.
