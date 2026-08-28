---
name: offwork
description: End the current work session by settling open loops, creating a local Work Capsule, and verifying that a fresh session can resume it. Use only when the user explicitly invokes /offwork or asks to close out the day.
disable-model-invocation: true
---

# `/offwork`

Turn the current work session into a resumable local checkpoint. Do not merely summarize the conversation.

## Boundaries

- Never commit, stash, push, discard changes, delete files, stop processes, or send messages.
- Treat Git and workspace inspection as read-only.
- Never decide to drop work on the user's behalf.
- Keep customer-sensitive content local unless the user accepts an external verifier call.

## Close-out workflow

1. Inspect the current conversation, task state, relevant project files, and Git status.
2. List every meaningful open loop. Recommend one disposition for each:
   - `resolve`: completed before the capsule is archived.
   - `park`: intentionally resumed later, with a concrete restart point.
   - `drop`: explicitly cancelled by the user.
   - `delegate`: transferred with a named recipient or destination.
3. Ask the user only about dispositions that cannot be inferred safely. Do not archive while any item remains unresolved.
4. Create a temporary JSON context file with this shape:

```json
{
  "goal": "current outcome",
  "summary": "where the work stopped",
  "decisions": ["decision and reason"],
  "failed_attempts": ["attempt and why it failed"],
  "next_step": "the first concrete action for the next session",
  "next_command": "optional safe command",
  "open_loops": [
    {"title": "item", "disposition": "park", "note": "restart detail"}
  ]
}
```

5. Run `"${CLAUDE_SKILL_DIR}/../../../bin/offwork" capture --project "$CLAUDE_PROJECT_DIR" --context <context-file> --verifier claude` when the user accepts a fresh-agent check. Otherwise omit `--verifier claude` and clearly label the result as a local completeness check.
6. If verification reports missing information, ask only those questions, update the context, and rerun. Stop after one failed external verifier retry and preserve the local draft rather than looping.
7. Report the archive path and the exact next step. Delete only the temporary context file created by this workflow; leave the capsule archive intact.

For a new session, run `"${CLAUDE_SKILL_DIR}/../../../bin/offwork" resume --project "$CLAUDE_PROJECT_DIR"` and use the restored goal and next step as the starting context.
