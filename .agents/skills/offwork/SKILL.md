---
name: offwork
description: Close out a work session by settling open loops, archiving a local Work Capsule, and testing whether a context-free session can resume it. Use when the user explicitly asks to run offwork or finish the day.
---

# Offwork capsule

Create a resumable checkpoint, not a generic session summary.

## Safety boundaries

- Inspect Git and the workspace read-only.
- Do not commit, stash, push, discard changes, delete user files, stop processes, or contact anyone.
- Do not mark an item `drop` unless the user explicitly chooses to cancel it.
- Before invoking an external verifier, tell the user that capsule content will be sent through that Agent provider.

## Workflow

1. Extract the current goal, stopping point, key decisions, failed attempts, next step, optional next command, and all meaningful open loops.
2. Give each open loop one explicit disposition: `resolve`, `park`, `drop`, or `delegate`. Ask only when the correct disposition is ambiguous.
3. Write a temporary JSON file using the schema documented in `README.md`.
4. From this repository, run `./bin/offwork capture --project <project-root> --context <context-file>`. Add `--verifier claude` only after the user accepts an external fresh-session check.
5. If the check is blocked, surface its missing-information questions instead of claiming hibernation.
6. On success, report the capsule path and the exact next step. Remove only the temporary context file created for this run.

To resume, run `./bin/offwork resume --project <project-root>` and continue from the restored goal and next step.
