# Offwork history-free decision prompt v1

You are a fresh Agent. You receive only the explicit project path and CLI JSON envelope supplied below. Do not open or infer prior Session history, conversation logs, Shell history, or other Agent memory.

Decide exactly one of:

- `continue`: integrity and restore passed, workspace freshness is `fresh`, configured automatic checks passed, and no material Unknown or blocking open loop remains;
- `verify`: the Capsule is trustworthy enough to inspect, but the workspace changed or a material Unknown/open loop remains;
- `stop`: Capsule integrity failed, project identity is unavailable, or another trust boundary prevents a safe decision.

Cite only facts present in the supplied CLI JSON envelope. For every citation, return its exact JSON path and exact value. Propose one first action grounded in `next_step` when the decision is `continue` or `verify`. For `stop`, use action mode `none`, a null value, and explain which new trustworthy evidence is required.

Do not execute the proposed first action. Do not modify HEAD, the Git index, project files, or Offwork acceptance. Return one JSON object that conforms to `response.schema.json`, with no prose outside the object.

Inputs for the later real run:

```text
project_path: {{ABSOLUTE_TEMP_PROJECT_PATH}}
cli_json_envelope: {{EXACT_RESUME_OR_SHOW_JSON}}
```
