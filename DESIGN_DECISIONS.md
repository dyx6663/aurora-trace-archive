# AURORA TRACE Design Decisions

## ADR-001 · Evidence is a first-class domain object

**Decision:** Store causal event metadata together with human-readable details, rather than treating logging as an optional print statement.

**Reason:** A reviewer should be able to answer which decision caused a tool call, which files were affected, and which verification result supports completion.

**Trade-off:** Event records are larger and require schema discipline. This is accepted because auditability is the project’s defining research direction.

## ADR-002 · Completion is a local policy decision

**Decision:** A model `finish` request is only a proposal. `complete_run()` evaluates the Acceptance Contract and either emits a completion event or blocks it.

**Reason:** Language output is not a substitute for test execution.

**Trade-off:** Some legitimate tasks without a failing baseline need a richer contract. The current prototype intentionally uses a bug-repair contract and should extend the contract by task type rather than weakening the global rule.

## ADR-003 · Each Run receives a copied workspace

**Decision:** Never mutate the selected source project directly. Copy it into `.runs/<run_id>/workspace/`; keep `run.json` and `evidence.ndjson` in the run root outside the Agent-visible workspace.

**Reason:** This makes repeated demonstrations independent and allows the original fixture to remain a trustworthy baseline.

**Trade-off:** Copying consumes storage and is not a complete OS-level sandbox. The current safety boundary is explicit filesystem and command policy, not a claim of hostile-code isolation. The nested layout also keeps the Agent from modifying its own audit metadata.

## ADR-004 · Exact replacement is the default patch primitive

**Decision:** `replace_text` requires exactly one match and returns a unified Diff.

**Reason:** A small, deterministic change is easier to review, attribute and replay than unconstrained text generation.

**Trade-off:** Complex refactors require a richer patch format in a future version. The restriction is intentional for the evaluation fixture.

## ADR-005 · Mock and Live share the factual executor

**Decision:** Mock mode changes the decision source, not the executor, event format or completion policy.

**Reason:** The no-key demo must demonstrate the same engineering claims as Live mode wherever possible.

**Trade-off:** Mock steps are deterministic rather than model-generated. This is disclosed in the UI and documentation; it is used for repeatability, not presented as autonomous planning.

## ADR-006 · Framework depth follows evaluation value

**Decision:** Prioritize Registry, Run persistence, Evidence Graph, verification gates and tests before Subagents, Plugins and MCP.

**Reason:** The former directly supports the project’s research question and can be demonstrated with local evidence. The latter would increase feature count without necessarily improving the central claim.

## ADR-007 · No historical fabrication

**Decision:** The repository records real commits and real verification results. It does not manufacture a staged development history, alter timestamps or invent a public repository address.

**Reason:** Reproducibility and academic integrity are part of the project evaluation, not post-processing decoration.

## ADR-008 · Acceptance is task-aware

**Decision:** Keep one four-gate completion protocol, but parameterize the baseline gate by task type. Bug repair requires an observed failure; feature, refactor and general change tasks require a green baseline.

**Reason:** A fixed failing-baseline rule is faithful to the seeded bug demo but rejects valid software-engineering tasks whose tests already pass. The contract should express the verification obligation, not the accidental shape of one fixture.

**Trade-off:** Automatic task classification is heuristic. The UI exposes an explicit contract profile, and the resolved profile is persisted in the Run so a reviewer can see which policy was used.

## ADR-009 · Evidence-aware context budget

**Decision:** When Live context grows beyond a fixed character budget, compact old message content while retaining assistant/tool message roles and machine-readable verification facts.

**Reason:** Long file reads and command outputs can make an Agent lose the useful recent context or exceed provider limits. Preserving the message skeleton keeps tool-call chronology interpretable.

**Trade-off:** This deterministic compaction is less semantically rich than an LLM-generated summary. It is deliberately offline-testable and does not add another hidden model call to the core demonstration.

## ADR-010 · Mock fixture scope is explicit

**Decision:** The no-key Mock path accepts only the built-in repair fixture. If a user selects a feature, refactor or general-change contract in Mock mode, the request is rejected before the isolated workspace is copied.

**Reason:** The deterministic fixture contains a deliberately failing Todo test and a known one-line repair. Pretending that the same sequence represents a green-baseline feature or refactor task would make the demonstration look more general than its evidence supports.

**Trade-off:** The no-key path covers one canonical scenario rather than every contract profile. The limitation is visible in the UI/API, while Live mode and imported projects remain available for broader task types.
