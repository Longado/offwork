# Offwork history-free decision prompt v1

You are a fresh Agent. You receive only the explicit project path and CLI JSON envelope supplied below. Do not open or infer prior Session history, conversation logs, Shell history, or other Agent memory.

Decide exactly one of:

- `continue`: integrity and restore passed, workspace freshness is `fresh`, configured automatic checks passed, and no material Unknown or blocking open loop remains;
- `verify`: the Capsule is trustworthy enough to inspect, but the workspace changed or a material Unknown/open loop remains;
- `stop`: Capsule integrity failed, project identity is unavailable, or another trust boundary prevents a safe decision.

Cite only facts present in the supplied CLI JSON envelope. For every citation, return its exact JSON path and exact value. Propose one first action grounded in `next_step` when the decision is `continue` or `verify`. For `stop`, use action mode `none`, a null value, and explain which new trustworthy evidence is required.

Do not execute the proposed first action. Do not modify HEAD, the Git index, project files, or Offwork acceptance. Return one JSON object that conforms to `response.schema.json`, with no prose outside the object.

Use exactly this response shape (replace the placeholder strings with Receipt-grounded values):

```json
{
  "decision": "continue | verify | stop",
  "cited_receipt_facts": [
    {
      "receipt_path": "$.exact.path",
      "value": "the exact value at that path, preserving its JSON type",
      "relevance": "why this fact supports the decision"
    }
  ],
  "proposed_first_action": {
    "mode": "project_action | verification | none",
    "value": "the exact $.data.next_step value, or null for stop",
    "receipt_path": "$.data.next_step, or null for stop",
    "reason": "why this is the safe first action, or which new trustworthy evidence is required"
  }
}
```

For `continue`, action mode must be `project_action`; for `verify`, it must be `verification`; for `stop`, it must be `none` and both action `value` and `receipt_path` must be null. Do not add other object fields.

The citations must include at least these decision facts when their paths are present:

- for `continue`: `$.data.handoff_verified.integrity.status`, `$.data.handoff_verified.restore.status`, `$.data.workspace_freshness.status`, `$.data.auto_checked.status`, `$.data.unknowns`, `$.data.open_loops`, and `$.data.next_step`;
- for `verify`: `$.data.handoff_verified.integrity.status`, `$.data.workspace_freshness.status`, the material `$.data.unknowns` or `$.data.open_loops`, and `$.data.next_step`;
- for `stop`: `$.error.code`, `$.error.details.integrity`, and `$.error.details.freshness`.

Inputs for the later real run:

```text
project_path: {{ABSOLUTE_TEMP_PROJECT_PATH}}
cli_json_envelope: {{EXACT_RESUME_OR_SHOW_JSON}}
```
