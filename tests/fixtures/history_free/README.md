# History-free decision fixtures

These fixtures prepare the Loop 5 black-box exercise without claiming that a fresh Agent has run.

- `cases.json` defines deterministic temporary-project construction and the evidence each later Agent decision must match.
- `agent-prompt.md` limits the Agent to an explicit project path and one CLI JSON envelope.
- `response.schema.json` requires one `continue`, `verify`, or `stop` decision, exact Receipt fact citations, and one proposed first action.
- `run-record.template.json` starts at `not_run`; `run-record.schema.json` defines its command and elapsed-time fields. A real exercise copies the template once per case and records each command as an object with `purpose`, exact `argv`, `elapsed_ms`, and `exit_code`, plus `overall_elapsed_ms` and the schema-valid Agent response.

`tests.test_history_free_demo` uses only the Python standard library and the real Offwork CLI. It proves that each case can be constructed and that `resume`/`task show` do not change HEAD, the Git index, project files, or acceptance state. It does not synthesize or score an LLM response. The later real run must start a genuinely fresh Agent Session, preserve its exact output, and leave human PM acceptance pending.
