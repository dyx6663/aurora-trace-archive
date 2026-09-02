# AURORA TRACE Architecture

## Positioning

AURORA TRACE is an **evidence-driven Coding Agent experimental platform for software-engineering tasks**. It is intentionally not a general-purpose assistant clone. Its central question is:

> Can every important action of a Coding Agent be explained, verified, traced back, and replayed?

The system treats the execution trace as a first-class product object rather than as an after-the-fact log.

## End-to-end control path

```text
Natural-language task
        │
        ▼
Acceptance Contract ──► required evidence gates
        │
        ▼
Agent Controller ──► model decision / Mock decision
        │
        ▼
Tool Registry ──► policy checks ──► isolated Run Workspace
        │                                  │
        │                                  ├─ file Diff
        │                                  └─ command result
        ▼
Evidence Ledger ──► Evidence Score ──► completion gate
        │
        ├─ live event stream
        ├─ run.json persistence
        ├─ evidence.ndjson persistence
        └─ JSON Trace / read-only Replay
```

## Logical layers

### 1. Model Boundary Layer

`ModelAdapter` is the only model-facing boundary. It supports:

- deterministic Mock decisions for a repeatable demonstration;
- OpenAI-compatible `chat/completions` tool calls for Live mode;
- normalized `tool_call` and `finish` decisions;
- model request error propagation into the Run failure record.

The model chooses the next action. It does not directly access the filesystem and it cannot decide that a Run is complete without passing the local acceptance gate.

### 2. Agent Core Layer

`run_agent()` owns the bounded control loop:

- one tool decision at a time;
- a maximum of 12 iterations in Live mode;
- structured tool results fed back into model context;
- recoverable tool failures returned to the model;
- terminal conditions for completion, failure, and iteration exhaustion.

The runtime policy adds two explicit lifecycle controls:

- `Approval Gate`: mutating tools (`write_file`, `replace_text`, `run_command`) can pause in `WAITING_APPROVAL` until a human approves or rejects the exact arguments;
- `Cancellation`: `POST /api/run/<id>/cancel` sets a cooperative cancellation flag, wakes approval waits, and terminates a running command process when its polling loop observes the flag.
- `Verified Apply`: a completed uploaded-project Run can be applied back through one explicit user action. The runtime compares a pre-run source manifest before copying changed files and keeps an apply backup under the Run directory.

The terminal state is persisted as `CANCELLED`, rather than being misreported as a generic failure.

Mock mode follows the same execution entry point as Live mode after the decision has been selected. This keeps the demonstration deterministic without creating a second executor implementation.

The built-in Mock fixture is intentionally scoped to the seeded Todo repair task. The API rejects non-repair contract profiles before copying a workspace, so the no-key path never presents a failing-baseline fixture as a feature or refactor experiment.

### 3. Tool and Policy Layer

`ToolSpec` and `ToolRegistry` define one inspectable contract for every tool:

- name and description;
- parameter names and model schema;
- whether the tool mutates the workspace;
- whether it is safe to run concurrently;
- structured success or error output.

The local executor applies path boundaries, a command allowlist, `shell=False`, argument checks, command-chain rejection and a 20-second timeout. Non-parallel-safe operations are serialized by the registry.

### 4. Run and Evidence Layer

Every task creates a fresh `.runs/<run_id>/` run directory. The selected project is copied into its nested `workspace/`; the Agent is restricted to that child directory while audit metadata stays at the run root. A Run contains:

- lifecycle state;
- task and project metadata;
- event stream;
- compact ledger;
- file Diffs;
- acceptance contract;
- evidence details and trust score;
- persistent timestamps and replay metadata.

`run.json` is atomically updated through a per-writer temporary file and `os.replace`, with serialized persistence and short Windows retry backoff. `evidence.ndjson` stores the complete event records in append order. Older runs without the nested directory remain readable through a compatibility fallback.

### 5. Verification Layer

The current contract contains four equal-weight gates. The first gate is task-aware:

| Gate | Required observation |
| --- | --- |
| Baseline requirement | repair tasks require a pre-patch failure; feature/refactor/change tasks require a pre-patch green baseline |
| Minimal patch | a non-empty unified Diff is produced by a file mutation |
| Regression pass | a post-patch test command succeeds and is tagged as regression evidence |
| Boundary safe | no path or command operation escapes the isolated workspace |

`infer_task_type()` provides a deterministic default classification and the UI/API may provide an explicit task type. The model receives the resulting policy as context, but cannot weaken it. Only `complete_run()` can emit `Task completed`. If a model returns `finish` early, the system emits `Completion blocked`, reports missing evidence, and keeps the Live loop inside the bounded control budget.

### 6. Product and UI Layer

`web/console.html`, `web/console.css` and `web/console.js` provide the current console:

- project selection and ZIP intake;
- Mock / Live runtime mode selection;
- real-time Evidence Ledger;
- acceptance contract progress;
- trust score and execution state;
- Diff and verification output;
- Run History;
- read-only event Replay;
- JSON Trace export.

The original UI files remain in the repository as historical implementation material. The HTTP root serves the UTF-8 console so the running product has one canonical interface.

## Evidence event schema

Each event records at least:

```json
{
  "id": 12,
  "run_id": "a3f756e2",
  "timestamp": "2026-08-30T15:22:53+00:00",
  "kind": "tool_result",
  "title": "run_command returned",
  "tool": "run_command",
  "action": "execute_run_command",
  "phase": "regression",
  "evidence_type": "regression_test",
  "verification_status": "passed",
  "affected_files": [],
  "input": {"command": "python -m unittest discover -s tests -v"},
  "output": {"ok": true, "returncode": 0},
  "parent_event_id": 11,
  "payload": {"ok": true, "returncode": 0}
}
```

The `parent_event_id` edge turns a chronological log into a causal path:

```text
task
  → baseline hypothesis
  → baseline failure
  → repair decision
  → file Diff
  → regression result
  → acceptance Gate
```

Tool decision/result events additionally expose explicit `input` and `output` fields. Long Live conversations pass through `apply_context_budget()`: old bulky content is replaced by compact evidence summaries while the assistant/tool message structure is retained. This is an evidence-preserving context policy, not a claim of reproducing the reference project's four-stage LLM summarization pipeline.

## Deliberate scope boundaries

The current version does not claim to implement a production-grade container sandbox, a general multi-agent scheduler, MCP, plugin distribution, or a learned planning algorithm. Those are possible research directions, not silently simulated features. The project prioritizes verifiable execution, reproducibility and explainable engineering boundaries.
