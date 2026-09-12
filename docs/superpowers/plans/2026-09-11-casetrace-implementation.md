# CaseTrace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a real LLM-discovered payment-investigation capability, deterministic replay, explicit outcomes, safe live human handoff, and the evidence required by the take-home brief.

**Architecture:** A Python runner owns one Playwright browser session and a small operator endpoint. A separate local fixture process serves the legacy banking UI. A typed JSON artifact joins discovery to replay; shared policy, surface, session, and evidence code enforce the same execution rules in both paths.

**Tech Stack:** Python 3.12+, async Playwright/Chromium, Pydantic 2, FastAPI/Jinja, Typer, Google Gen AI Python SDK for discovery only, pytest/pytest-asyncio, Ruff, and uv with a committed lockfile. Use [uv's project/lockfile workflow](https://docs.astral.sh/uv/guides/projects/) for reproducible setup.

**Spec:** `docs/superpowers/specs/2026-09-11-casetrace-design.md`. Read it together with the local source brief, `AComputer-Use Automation System.docx`, before execution.

## Global Constraints

- Python 3.12 or newer; one Python package and one implemented browser adapter.
- One capability: `trace_incoming_payment`; synthetic data only; incoming USD payments only.
- Automation interacts only with rendered application UI; no business API, fixture-data imports, hidden application state, or database reads.
- Replay cannot import or invoke a model provider, including during recovery or handoff.
- Every executable artifact path has declared checks, finite limits, and provenance.
- Only one actor may issue actions to a live session at a time.
- Credentials, tokens, full PII, and raw sensitive observations never enter persisted artifacts or logs.
- A real successful model-driven UI discovery and a replay of its saved artifact are mandatory before completion can be claimed.
- Complete a thin version of every core requirement before adding a stretch goal.

This plan creates the application only when execution begins. The workspace currently is not a Git repository. Initialize Git at implementation time, preserve the source brief locally, and exclude it from public publication unless redistribution is authorized. Commit each accepted task with an explicit file list after its checks pass; do not stage secrets or unrelated files. The planning documents themselves require no fabricated build/test evidence.

## File responsibilities

| Files | Responsibility |
| --- | --- |
| `pyproject.toml`, `uv.lock`, `.python-version`, `.gitignore`, `.env.example` | Package/CLI, locked dependencies, Python 3.12 baseline, exclusions, non-secret configuration names |
| `src/casetrace/contracts.py` | Validated input, value/target/predicate/operation/artifact/result/event contracts |
| `src/casetrace/matching.py` | Exact payment matching, deduplication, and complete-search classification |
| `src/casetrace/surface.py`, `src/casetrace/browser.py` | Surface protocol and real browser implementation |
| `src/casetrace/policy.py`, `config/base.json` | Execution allowlist and vendor/tenant/privacy bindings |
| `src/casetrace/session.py`, `src/casetrace/operator.py` | Single-session ownership, interventions, loopback operator page |
| `src/casetrace/static/recording.js` | Cooperative input ownership gate and redacted manual action metadata |
| `src/casetrace/provider.py`, `src/casetrace/discovery.py`, `src/casetrace/compiler.py` | Model adapter, observe/decide/act, and discovery-derived artifact compilation |
| `src/casetrace/replay.py` | Bounded model-free artifact interpreter |
| `src/casetrace/evidence.py`, `src/casetrace/cli.py` | Redacted evidence, command entrypoints, and evidence checks |
| `src/casetrace/fixture/app.py`, `data.py`, `scenarios.py` | Separate synthetic application server, seeds, deterministic exceptional states |
| `src/casetrace/templates/bank/` | `shell.html`, `login.html`, `members.html`, `activity.html`, `detail.html` |
| `src/casetrace/templates/operator.html` | Minimal operator state and Take control/Resume/Abort actions |
| `tests/conftest.py`, `tests/harness.py` | In-memory surface, controlled fixture process, isolated browser and evidence helpers |
| `tests/unit/`, `tests/integration/`, `tests/e2e/` | Contract/policy logic, adapter/session behavior, and actual browser replay verification |
| `examples/queries/`, `evidence/` | Synthetic invocations and genuine sanitized run evidence |
| `README.md`, `REPORT.md`, `.github/workflows/test.yml` | Required submission documentation and offline CI |

Only introduce a file when its task needs it. Keep implementation small; the table is a responsibility map, not a mandate to build abstractions ahead of use.

## Shared interfaces

Task 1 defines these public names so dependent tasks do not invent incompatible contracts:

- `PaymentQuery`: member ID, exact amount string, USD, inclusive date window, nullable reference; constraints from the design.
- `PaymentRecord`: member/account/reference, direction, exact amount/currency, transaction date, displayed status, source, and observation ID. Raw values are run-local.
- `SearchCoverage`: `accounts_complete: bool`, `sources_complete: bool`, `pages_complete: bool`; nonnegative account/page counts. `complete` is true only when all three flags are true. The interpreter derives flags from actual exhaustion checkpoints, never a model assertion.
- `PaymentDecision`: a discriminated success/business_outcome/failure domain decision containing the matched records or outcome/failure code and coverage, before execution metadata is attached. The runner wraps it in `RunResult` with run/digest/evidence fields.
- `Success`, `BusinessOutcome`, `Failure`: terminal models with `kind` discriminator; all include `run_id`, artifact/binding digest, and evidence references. `RunResult` is their union.
- `FailureCode`: exactly the codes enumerated in the design; exceptions inside the engine use `RunStopped(code, step_id, expected, observed)` and are projected through the redactor.
- `ValueRef`: tagged `literal`, `input`, or `variable` reference. Literal values must be non-sensitive static data. Resolve via `resolve_value(ref, args, variables)`; no interpolation/evaluation language.
- `TargetSpec`: target ID, surface kind, frame/container constraints, ordered strategies, and cardinality exactly one for mutation. `Observation`: sanitized visible controls/state plus opaque target handles and an observation ID; any private parsed values stay in session memory.
- `Predicate`: tagged visible/absent/equals/count/all/any expression over target IDs or typed values. `Step`: tagged navigate/fill/click/read/assert/branch/for_each/paginate/return operation with ID, checks, bounds, and provenance. Nested steps are capped at the account/source/page structure described in the design.
- `Capability`, `TenantBindings`, `PolicyConfig`, `RunEvent`, `Intervention`, and `ValidationReport`: Pydantic models following the design's field table and safety constraints.
- `Surface` protocol: `async observe() -> Observation`, `async act(step: Step, args: PaymentQuery, variables: dict) -> None`, `async read(step: Step, args: PaymentQuery, variables: dict) -> object`, `async check(predicate: Predicate, args: PaymentQuery, variables: dict) -> bool`, and `async capture() -> dict`. Browser objects never escape into artifact data.
- `SessionController`: `async execute(operation)` runs an async zero-argument callable under ownership checks; `async request_handoff(reason, step_id) -> Intervention`; `async take_control(intervention_id) -> None`; `async resume(intervention_id) -> None`; `async abort() -> None`. Owns stable run/browser/context/page IDs and an ownership generation.
- `EvidenceWriter(root, run_id)`: `emit(event: RunEvent) -> None`, `capture(surface: Surface) -> dict`, and `finish(result: RunResult) -> None`. Serialization always applies the redacted projection.

Concrete schema definitions should use Pydantic tagged unions and forbidden extra fields. Export JSON Schema for reviewer inspection. Do not implement a general expression language or arbitrary action plugin system.

## Task 1: Contract and safe local package foundation

**Files:** Create package metadata, `src/casetrace/__init__.py`, `contracts.py`, the initial `cli.py`, `tests/conftest.py`, and `tests/unit/test_contracts.py`.

**Consumes:** Design contract and shared interfaces above.

**Produces:** `PaymentQuery`, the remaining shared models, `Capability.model_json_schema()`, and CLI help/schema export. A working offline test command.

- [ ] Create a setuptools-based Python package with `casetrace = "casetrace.cli:app"`, Python `>=3.12`, and dependencies pydantic `>=2,<3`, playwright, fastapi, uvicorn, jinja2, python-multipart, typer, and google-genai. Dev dependencies: pytest, pytest-asyncio, httpx, Ruff. Resolve exact versions into `uv.lock`; do not guess the newest versions in prose. Add `.venv`, `.env`, caches, temporary run output, browser profiles, and the local source DOCX to `.gitignore`. Keep sanitized `evidence/` trackable.
- [ ] Add a `make_query(**changes)` test fixture returning a validated query from the synthetic values below, then write the contract regression tests before validators.

```python
import pytest
from pydantic import ValidationError
from casetrace.contracts import PaymentQuery

BASE = dict(member_id="12345", amount="250.00", currency="USD",
            date_from="2026-09-01", date_to="2026-09-07", reference=None)

@pytest.mark.parametrize("change", [
    {"amount": "250.001"}, {"amount": "-1.00"}, {"amount": "0.00"},
    {"member_id": "12"}, {"currency": "EUR"},
    {"date_to": "2026-08-31"}, {"date_to": "2026-10-15"},
    {"reference": ""}, {"undeclared": True},
])
def test_rejects_invalid_query(change):
    with pytest.raises(ValidationError):
        PaymentQuery.model_validate(BASE | change)

def test_member_id_preserves_zeroes():
    query = PaymentQuery.model_validate(BASE | {"member_id": "00123"})
    assert query.member_id == "00123"
```

- [ ] Run `uv run pytest tests/unit/test_contracts.py -q`; record the initial failures, then implement exact Decimal/date validation and tagged models. Enforce capability version, bounded operations, legal value references, and forbidden extra fields. Validate return shapes against the declared result contract.
- [ ] Add artifact rejection cases: unknown version, missing checkpoint, nonexistent target/reference, unbounded pagination, arbitrary-code operation, and two matching branch guards. Use hand-authored unit fixtures labelled `test_fixture`; they are not discovery evidence.
- [ ] Run the contract tests and `uv run ruff check src tests`. Export the schema with `uv run casetrace schema --output evidence/schema.json` only once the command exists. Commit the accepted contract/package task.

**Acceptance:** Bad invocations fail before browser startup; JSON schema and runtime types agree. Brief 3.2 is represented in testable code.

## Task 2: Small legacy fixture and controllable scenarios

**Files:** Create `fixture/__init__.py`, `fixture/app.py`, `fixture/data.py`, `fixture/scenarios.py`, five bank templates, `tests/harness.py`, and `tests/integration/test_fixture.py`. Extend `cli.py` with `fixture`.

**Consumes:** Query meanings, three screen families, scenario names, and search bounds from the design.

**Produces:** `create_fixture_app(scenario: str) -> FastAPI`; `casetrace fixture --scenario normal --port 8000`; `FixtureServer(scenario="normal")` test context manager with `.url`, `.start()`, and `.stop()` methods. The harness launches a separate subprocess, waits for an HTTP-ready page, and cleans up that exact process.

- [ ] Seed these synthetic query cases; use additional distractor rows with different direction, currency, amount, or date. Give each relevant member two deposit accounts. Include a matching transaction on a second history page. No member names or account numbers may be real.

| Query file | Member | Amount | Window | Expected final behavior |
| --- | --- | --- | --- | --- |
| `posted.json` | 12345 | 250.00 | 2026-09-01 through 2026-09-07 | One POSTED record |
| `posted-new-member.json` | 54321 | 175.50 | Same window | One POSTED record, different account/reference |
| `pending.json` | 12345 | 125.00 | Same window | One PENDING record |
| `reversed.json` | 12345 | 80.00 | Same window | One visibly REVERSED record |
| `ambiguous.json` | 12345 | 60.00 | Same window | Two distinct matches, including different sources |
| `not-found.json` | 12345 | 999.00 | Same window | Exhaustive NOT_FOUND |
| `member-not-found.json` | 99999 | 250.00 | Same window | MEMBER_NOT_FOUND |
| `no-accounts.json` | 11111 | 250.00 | Same window | NO_ACCOUNTS |
| `invalid.json` | 12345 | -1.00 | Same window | INVALID_INPUT before UI |

- [ ] Write a browser integration check that loads the real iframe and can navigate manually by visible controls. Tests can use fixture-specific paths, but discovery/replay must not import these helpers or dataset contents.

```python
import pytest

@pytest.mark.asyncio
async def test_fixture_has_real_frame_and_search(fixture_url, page):
    await page.goto(fixture_url)
    content = page.frame_locator('iframe[name="content"]')
    await content.get_by_role("textbox").fill("12345")
    await content.get_by_role("button", name="Search", exact=True).click()
    await content.get_by_text("Member 12345", exact=True).wait_for()
    assert await content.get_by_text("Deposit accounts", exact=True).count() == 1
```

- [ ] Implement stable table-based layouts, adjacent labels, duplicate contextual buttons, and working form/navigation behavior. Keep at least one genuinely poor-semantic control so the adapter must use an observed label/structural relationship. Supply fixture state only to the server process.
- [ ] Implement scenarios as deterministic transition-triggered behavior. `expire-once` replaces activity with a mock login form and returns to member search after manual demo reauthentication; it retains the same browser session. `stale-member` displays the wrong identity. `slow-once` delays a safe load once. `timeout` never reaches a normal checkpoint. Other scenarios visibly render their named condition.
- [ ] Create `examples/queries/` files using the table, adding currency USD and reference null. Run `uv run pytest tests/integration/test_fixture.py -q`, inspect the three screen families, and commit this task.

**Acceptance:** A person can perform the complete investigation in the browser. Scenario selection is not exposed as an agent tool. This fixture is a target, not the automation implementation.

## Task 3: Protected browser actions, ownership, and redacted evidence

**Files:** Create `surface.py`, `browser.py`, `policy.py`, `session.py`, `evidence.py`, `config/base.json`, `tests/unit/test_policy.py`, `tests/unit/test_evidence.py`, and `tests/integration/test_browser.py`.

**Consumes:** Shared models, fixture pages, and `FixtureServer`.

**Produces:** `BrowserSurface`, `Policy.authorize(step, resolved_target)`, the AUTOMATION/PAUSING ownership gate, `EvidenceWriter`, and a safe primitive tool set usable by discovery and replay.

- [ ] Write policy and redaction tests before enabling UI actions. A policy rule for `click` must also authorize the target's reviewed inquiry context; unknown controls are denied.

```python
import pytest
from casetrace.contracts import RunStopped

@pytest.mark.asyncio
async def test_denied_control_is_never_activated(browser_harness):
    h = browser_harness
    before = await h.visible_financial_action_count()
    with pytest.raises(RunStopped) as failure:
        await h.try_financial_control()
    assert failure.value.code == "POLICY_DENIED"
    assert await h.visible_financial_action_count() == before

def test_persisted_events_omit_canaries(evidence_harness):
    h = evidence_harness
    h.emit_sensitive_example()
    persisted = h.read_all_text()
    assert all(canary not in persisted for canary in h.canaries)
```

- [ ] Implement the test helpers in `tests/harness.py`: `browser_harness` starts the fixture/browser and wraps reviewed Step objects for the synthetic risky control; its count reader reads a visible fixture audit counter through the UI. `evidence_harness` writes action, provider-error, query-URL, and manual-input examples containing seeded canaries and reads every resulting text file. These helpers never appear in discovery.
- [ ] Implement frame/container-scoped resolution, visible-label adjacency and table relationships, unique matching, per-action context checks, 10-second deadlines, and deterministic condition evaluation. Fixed adapter JavaScript may inspect visible DOM and record events; arbitrary model-supplied evaluation is forbidden.
- [ ] Enforce parsed origin/route policy on initial navigation, form destinations, requests, redirects, frames, and popups. Block service workers. Write a local second-origin trap server test proving no request reaches a denied destination. Add prompt-injection-like visible text to show policy is unchanged.
- [ ] Add structured event sequence numbers, run/step/actor IDs, redacted values, and sanitized visible-state snapshots. Only save masked screenshots when sensitive regions resolve; otherwise save a useful sanitized tree. Suppress raw exception dumps, credentials, model response bodies, browser profiles, traces, and videos.
- [ ] Implement the initial `SessionController.execute` lock/generation check so discovery cannot queue new actions after a pause request. A blocked action produces an intervention object even before the operator page is implemented.
- [ ] Run `uv run pytest tests/unit/test_policy.py tests/unit/test_evidence.py tests/integration/test_browser.py -q`. Inspect one masked screenshot and its sanitized snapshot. Commit the protected execution boundary.

**Acceptance:** Both future execution paths can only act through a guarded, observable boundary. Brief 3.4/3.5 are working early rather than added after unsafe capture.

## Task 4: Genuine model discovery and candidate compilation

**Files:** Create `provider.py`, `discovery.py`, `compiler.py`, `tests/unit/test_discovery.py`, `tests/unit/test_compiler.py`, and `tests/integration/test_live_discovery.py`. Extend `cli.py` and `.env.example`.

**Consumes:** Policy-checked primitive operations, sanitized observations, typed contracts, and evidence writer.

**Produces:** `async discover(goal, args, surface, session, provider, evidence) -> Capability`; `compile_candidate(recording, proposal, bindings) -> Capability`; `doctor --live-model`; `discover` CLI. `ModelProvider.decide(observation, history, tools)` returns a typed tool proposal and brief purpose summary.

- [ ] Set up the user's API account privately following the [official quickstart](https://ai.google.dev/gemini-api/docs/quickstart). The local `.env` contains `GEMINI_API_KEY` and `CASETRACE_MODEL`; neither is printed. The model ID is required configuration, must support image/text input and function calling, and is verified by the live doctor request. The example file documents variable names without a real key. If access is unavailable, continue Tasks 5-7 but leave the mandatory live-evidence acceptance item visibly incomplete.
- [ ] Recheck the official Gemini API and Google Gen AI SDK documentation before coding the adapter. Implement one provider using Gemini function declarations, application-side strict Pydantic validation, and serialized tool execution. Disable SDK automatic function execution; the guarded runner executes each validated proposal. [Official SDK documentation](https://googleapis.github.io/python-genai/) describes manual function calling. Provider schemas must use supported JSON Schema features; application contracts still reject extra properties. Handle refusal, invalid tool proposals, provider failure, and exhausted budgets explicitly. Do not log private model reasoning.
- [ ] Write offline tests using a scripted `FakeProvider` whose `.calls` counts requests and `.decide` returns prescribed tool proposals. Label these as tests, never as live discovery evidence.

```python
import pytest
from casetrace.contracts import RunStopped

@pytest.mark.asyncio
async def test_discovery_cannot_override_policy(discovery_harness):
    h = discovery_harness
    h.provider.propose_disallowed_navigation()
    with pytest.raises(RunStopped) as failure:
        await h.discover()
    assert failure.value.code == "POLICY_DENIED"
    assert h.surface.denied_destination_requests == 0

def test_compilation_never_freezes_query_values(compiler_harness):
    h = compiler_harness
    artifact = h.compile_successful_recording()
    serialized = artifact.model_dump_json()
    assert '"12345"' not in serialized
    assert '"250.00"' not in serialized
    assert h.observed_event_ids <= h.referenced_event_ids(artifact)
```

- [ ] Define these harnesses in `tests/harness.py`: `discovery_harness` combines a policy-enforced fake surface/session with `FakeProvider`; `compiler_harness` constructs a labelled synthetic recording with input-reference actions and compares its observation IDs with step provenance. Compiler failures include unresolved bindings, frozen input literals, unknown operations, missing identity checks, and unsupported generalized loops.
- [ ] Implement observe/decide/act with 40 decisions, 100 actions, and 10-minute limits. The goal supplies business intent and typed parameters but no selectors, route recipe, fixture source, or expected seed answer. Model proposals are validated and run through Task 3. Completion requires executable UI checks.
- [ ] Convert the successful recording into a candidate. Preserve action order and provenance; generalize only with explicit input/variable references. Mark unobserved branches and generalized repetition as requiring validation. A candidate is not yet proof of all outcomes.
- [ ] Run the offline discovery/compiler tests. Then start the normal fixture, run `uv run --env-file .env casetrace doctor --live-model`, and perform the first genuine discovery using the command contract below. Save sanitized evidence and the actual artifact. If the real run fails, inspect its evidence and repair the loop; never substitute a scripted success.
- [ ] Commit code and only privacy-reviewed evidence. Do not wait until the final day to discover API or browser integration failures.

**Acceptance:** A real LLM has completed the UI goal and generated a schema-valid candidate artifact with genuine event provenance. Brief 3.1 and the essential discovery half of 3.2 are demonstrated.

## Task 5: First deterministic replay of that artifact

**Files:** Create `replay.py`, `matching.py`, `tests/unit/test_replay.py`, `tests/unit/test_matching.py`, and `tests/e2e/test_replay.py`; extend `compiler.py` and `cli.py` with draft validation/replay.

**Consumes:** Candidate artifact, Query/Result contracts, guarded Surface/Session/Evidence implementations. Replay must not consume `ModelProvider`.

**Produces:** `async replay(capability, args, surface, session, evidence, bindings, *, validation_mode=False) -> RunResult`; `classify_payment(query, records, coverage) -> PaymentDecision`; `validate_artifact` producing an exact-digest `ValidationReport`.

- [ ] Implement exact matching with Decimal/date values, explicit CREDIT/USD handling, composite-identity deduplication, and contradiction detection. Write incomplete-search and ambiguity tests first.

```python
from casetrace.matching import classify_payment

def test_empty_incomplete_search_is_failure(make_query, matching_harness):
    result = classify_payment(make_query(), [], matching_harness.incomplete())
    assert result.kind == "failure"
    assert result.code == "CHECKPOINT_FAILED"

def test_two_distinct_matches_are_ambiguous(make_query, matching_harness):
    h = matching_harness
    result = classify_payment(make_query(), h.two_matching_records(), h.complete())
    assert result.kind == "business_outcome"
    assert result.code == "AMBIGUOUS"
```

- [ ] Define `matching_harness` in conftest to return fixed complete/incomplete SearchCoverage values and typed PaymentRecords with matching query values but distinct account/reference identities. Add wrong-direction, date-boundary, conflicting-status, and decimal tests.
- [ ] Implement a small interpreter dispatching the declared Step union. Resolve symbolic values; check error/auth states before normal conditions; re-resolve targets; assert postconditions; collect outputs from fresh observations. Enforce loop/page/total bounds and repeated-page detection. No model, generated code, arbitrary expressions, or fuzzy fallback.
- [ ] Validation mode may run a draft under the normal policy. Record exact artifact and binding digests; regular replay requires matching validation. Store generalized/authored path coverage explicitly. Execute the discovery-produced path with changed member and amount inputs before extending exception behavior.
- [ ] Write a subprocess replay test that removes model credentials, denies Python network connections to non-loopback destinations, and fails on any import of `casetrace.provider`, `casetrace.discovery`, or the model SDK. Browser egress remains constrained by Task 3. Record `model_calls: 0`; the network/import checks supply evidence beyond that counter.
- [ ] Run `uv run pytest tests/unit/test_replay.py tests/unit/test_matching.py tests/e2e/test_replay.py -q`. Compare observed output to the fixture through the independent test oracle, not through engine fixture imports. Commit the first end-to-end slice.

**Acceptance:** The saved discovery artifact replays with different values and executable success checks, without a model. Target this milestone by the end of Day 2 or early Day 3.

## Task 6: Complete the declared search and exception behavior

**Files:** Extend `replay.py`, `matching.py`, `compiler.py`, and `contracts.py`; create `tests/e2e/test_outcomes.py`, `tests/e2e/test_runtime_failures.py`, and validation-case data in `tests/cases/`.

**Consumes:** Working discovery-derived replay path, fixture scenarios, exact matching, and finite interpreter.

**Produces:** Every declared payment outcome, strict completeness proofs, known transient handlers, and debuggable terminal failures. The shipped capability contains validated branches rather than implicit engine guesses.

- [ ] First add test cases for POSTED, PENDING, REVERSED, NOT_FOUND, AMBIGUOUS, MEMBER_NOT_FOUND, and NO_ACCOUNTS. Keep seeded expected answers exclusively in the test harness.

```python
import pytest

@pytest.mark.asyncio
@pytest.mark.parametrize("query_file,kind,value", [
    ("posted-new-member.json", "success", "POSTED"),
    ("pending.json", "success", "PENDING"),
    ("reversed.json", "success", "REVERSED"),
    ("not-found.json", "business_outcome", "NOT_FOUND"),
    ("ambiguous.json", "business_outcome", "AMBIGUOUS"),
    ("member-not-found.json", "business_outcome", "MEMBER_NOT_FOUND"),
    ("no-accounts.json", "business_outcome", "NO_ACCOUNTS"),
])
async def test_declared_outcome(query_file, kind, value, replay_harness):
    result = await replay_harness.run_query(query_file)
    assert result.kind == kind
    observed = result.payment.status if kind == "success" else result.code
    assert observed == value
    assert replay_harness.model_calls == 0
```

- [ ] Define `replay_harness.run_query(query_file, scenario="normal")` in `tests/harness.py`: starts the isolated fixture, loads the saved capability and synthetic query, runs with provider egress blocked, collects events, and returns the typed result. It exposes `.model_calls` from instrumented requests, not an assumed constant. If the required saved artifact is absent, make the evidence test fail explicitly; authored unit tests remain independently runnable.
- [ ] Validate account/source/page enumeration with the model-derived targets. If new paths need additional UI discovery, perform a targeted genuine model run and link its events; otherwise add a small reviewed handler with `authored` provenance and targeted browser validation. Do not relabel these additions as part of the original discovery.
- [ ] Verify a second-page match is found; duplicate amount/date across History and Pending yields AMBIGUOUS; the first match cannot terminate coverage early; absent member is distinguished from absent payment. Derive completeness from disabled/absent Next state, verified source/filter identity, and account-list exhaustion. Guard against a row-count cap masking more records.
- [ ] Implement known `slow-once` recovery: a verified read-only operation may retry twice with fixed delays; return normal success if a later checkpoint passes and record the recovery event. Deadline exhaustion is TIMEOUT, permission denial ACCESS_DENIED, stale member CHECKPOINT_FAILED, conflicting duplicate INCONSISTENT_RECORD, and exceeded search bounds SEARCH_LIMIT_EXCEEDED.
- [ ] A session-expired or unsupported-dialog detector creates an intervention via Task 3's controller; Task 7 supplies the live operator path. A wrong/ambiguous target must stop before clicking; no blind `.first()` or coordinate fallback is permitted.
- [ ] Run `uv run pytest tests/e2e/test_outcomes.py tests/e2e/test_runtime_failures.py -q`. Update validation coverage for the exact executable artifact/binding digest only after those runs. Commit the completed search/outcome behavior.

**Acceptance:** Business outcomes, recoverable events, and terminal failures are distinguishable. No partial search becomes a unique finding or NOT_FOUND. Brief 3.3 is demonstrated beyond a happy path.

## Task 7: Real human takeover and verified resume

**Files:** Extend `session.py`, `browser.py`, `evidence.py`, and `replay.py`; create `operator.py`, `static/recording.js`, `templates/operator.html`, `tests/unit/test_session.py`, `tests/integration/test_handoff.py`, and `tests/e2e/test_handoff.py`.

**Consumes:** Session lock/generation checks, intervention model, headed browser, known expiry detector, and a root search checkpoint in the artifact.

**Produces:** `AUTOMATION -> PAUSING -> HUMAN -> VERIFYING -> AUTOMATION`, local operator Take control/Resume/Abort actions, redacted manual input events, and same-session deterministic resumption.

- [ ] Write race tests using an `asyncio.Event`-controlled fake operation: request a pause while the operation is in flight, prove ownership is not HUMAN until it settles, reject new actions, and prove late completion cannot dispatch another action. Define `session_harness` in the test file around the actual controller with a fake surface and event recorder.

```python
import pytest
from casetrace.contracts import RunStopped

@pytest.mark.asyncio
async def test_no_automatic_mutation_during_human_control(session_harness):
    h = session_harness
    intervention = await h.session.request_handoff("SESSION_EXPIRED", "search")
    await h.session.take_control(intervention.id)
    with pytest.raises(RunStopped):
        await h.session.execute(h.mutation)
    assert h.mutations == 0
    assert h.session.owner == "HUMAN"

@pytest.mark.asyncio
async def test_resume_rejects_wrong_member(handoff_harness):
    h = handoff_harness
    await h.reach_human_control()
    await h.manual_open_wrong_member()
    await h.request_resume()
    assert h.session.owner == "HUMAN"
    assert h.automatic_mutations_since_takeover == 0
```

- [ ] Define `handoff_harness` around the actual expire-once fixture and operator endpoints. A simulated human in tests uses browser input through the live page; events are explicitly tagged `test_operator`. `.manual_open_wrong_member()` navigates through the visible member search; `.request_resume()` submits the operator form. The final demo requires a real person, recorded as `human_operator`.
- [ ] Implement the operator loopback server on port 8001 by default. Show capability/goal, step, sanitized stop reason, live ownership, and session identifier. Include a per-process CSRF token and enforce operator Origin/Host. Keep token values out of logs. The automation browser tools cannot navigate to this origin.
- [ ] Install context-level input recording and the cooperative page-input gate in each frame/navigation. Suppress user input during AUTOMATION except a stop shortcut/action; pause on observed unexpected manual mutation. In HUMAN state, record clicks, navigation, and field-change metadata without typed values or password keys. Automated events and test-operator events must not be mislabeled human.
- [ ] Implement Resume verification: same browser/context/page IDs, allowed origin, same demo user, recognized state. From member search, clear candidate/coverage variables and restart at the artifact's declared read-only root. From the wrong member or an unrecognized screen, remain in HUMAN. A closed browser is SESSION_LOST; a 10-minute unattended intervention is HANDOFF_TIMEOUT; Abort becomes CANCELLED.
- [ ] Run unit/integration handoff tests, then an automated same-session e2e check. Finally run the headed expire-once scenario and have the user manually reauthenticate and resume. Save genuine ownership transitions, session IDs, redacted manual events, and completed replay under one run ID. Ask for the necessary live interaction when the concrete paused session is available; it cannot be fabricated by the agent.
- [ ] Verify zero automatic mutations while HUMAN owns the session and zero provider calls throughout replay and handoff. Inspect the auth-event logs and captures for sensitive leakage, then commit.

**Acceptance:** The handoff mechanism works on the actual paused browser and resumes safely. A screenshot plus a Continue button alone cannot pass this task. Brief 3.6 is complete only after the real manual demonstration.

## Task 8: Focused adversarial verification and portable replay

**Files:** Create `tests/e2e/test_safety.py`, `tests/unit/test_boundaries.py`, `tests/integration/test_operator_security.py`, and `.github/workflows/test.yml`; extend privacy and replay tests where failures justify changes.

**Consumes:** Complete core flow, saved artifact, all exception scenarios, human ownership state machine.

**Produces:** Offline CI and evidence that checks the actual failure risks in the design.

- [ ] Add import-boundary checks: replay/core modules cannot import the model adapter or fixture data/scenario modules. The CLI imports a provider only in discovery/doctor commands. Offline CI runs without API secrets; live-discovery tests are an explicitly separate marker/command.
- [ ] Add policy bypass cases for redirect, popup, subframe, form submission, encoded URL, unknown action, instruction-like page content, and a financial-write control. Verify the denied destination/operation was not reached, rather than merely asserting an error string.
- [ ] Add privacy canaries in input fields, table cells, auth pages, URLs, exceptions, and provider errors. Scan artifacts, JSONL, sanitized snapshots, and manifests. Check screenshot masking using a known synthetic sensitive region and visually inspect failure captures. Confirm unknown/auth screens fall back to sanitized snapshots.
- [ ] Verify artifact integrity and compatibility: edited executable content invalidates validation; edited tenant bindings require revalidation; unsupported schema/app/surface versions fail before mutation; extra unrecognized fields are rejected.
- [ ] Verify operator security/coordination: missing or wrong CSRF token, wrong Origin, stale intervention ID, double Resume, Resume while PAUSING, browser loss, and cancel while paused. Run each test against a single actual state transition so races cannot be hidden by sleeps.
- [ ] Run five fresh-session repeats of the normal new-member replay and one each of the exceptional cases. Report exact counts and durations. Treat this as limited fixture stability evidence, not a production reliability claim. Investigate failures rather than increasing timeouts blindly.
- [ ] Run `uv run pytest -m "not live_model and not manual" -q`, `uv run ruff check src tests`, and `uv run ruff format --check src tests`. Configure CI to install Chromium and run the same offline checks. Commit after the checks pass; do not repeatedly broaden the suite without a new concern.

**Acceptance:** Safety boundaries, resume races, privacy, and zero-model replay have concrete regression coverage. Test evidence is clearly distinguished from genuine discovery/manual evidence.

## Task 9: Required evidence, README, report, and submission rehearsal

**Files:** Create final `README.md`, `REPORT.md`, `evidence/README.md`, and `evidence/manifest.json`; save the actual capability and actual sanitized run directories. Extend `evidence.py`/`cli.py` with `evidence-check` if not already present.

**Consumes:** Completed application, validated discovery-derived capability, genuine provider evidence, genuine manual-handoff evidence, and fresh verification results.

**Produces:** Reproducible submission repository with a compact reviewer demo and all exact required paths/headings.

- [ ] Finalize the CLI contract below and run every documented command in a clean environment. Commands in this plan are implementation targets, not claims that they already work.
- [ ] Keep evidence folders `discovery/`, `replay-posted/`, `replay-not-found/`, `replay-handoff/`, and `replay-checkpoint-failure/`. Each contains events JSONL, redacted result, and appropriate sanitized snapshots/masked screenshots. `capability.json` is the actual discovered/generalized/validated artifact, not an authored replacement. Copy reviewed run output to these public paths; temporary raw state never becomes evidence.
- [ ] Implement `check_evidence(root: Path) -> list[str]` in `evidence.py`, returning concrete issues. It verifies referenced files/digests, genuine discovery provider metadata, nonzero discovery calls, zero replay provider calls, different replay inputs, terminal checkpoints, provenance/validation links, manual ownership transitions and stable session IDs. It also runs textual canary scans; it does not pretend to authenticate a model provider independently or prove image privacy.

```python
from casetrace.evidence import check_evidence

def test_manifest_rejects_missing_real_discovery(tmp_path, manifest_harness):
    manifest_harness.write_without_discovery(tmp_path)
    problems = check_evidence(tmp_path)
    assert "missing genuine discovery evidence" in problems
```

- [ ] Define `manifest_harness` as a test-only writer of minimal evidence manifests and event files. Cover missing/mismatched artifact digest, a replay with a model call, and a handoff with changed browser IDs. The real evidence audit uses actual run folders.
- [ ] Write README setup, API configuration, fixture startup, exact discovery/validation/replay/handoff commands, offline mode, troubleshooting, and expected result shapes. State the distinction between live discovery and scripted tests. Explain that the headed operator demo needs a person at the displayed browser. Include a five-minute reviewer sequence.
- [ ] Write REPORT at approximately 1-3 pages with these exact headings, in order: `Architecture`, `Artifact schema`, `Determinism & error handling`, `Heterogeneity & multi-tenant`, `Escalation & handoff`, `Safety`, `Cuts`. Describe implemented behavior and known limitations; do not claim optional features or unrun tests.
- [ ] Run the final offline checks, `uv run casetrace evidence-check evidence`, and a fresh-keyless replay from a clean checkout. Inspect all public images and staged files. The public tree must exclude API keys, the private `.env`, browser state, raw captures, and the source take-home document unless redistribution is authorized.
- [ ] Prepare the final public GitHub repository and publish the reviewed source/evidence when the user proceeds with submission. Verify the public README renders, evidence links resolve, and clone/setup commands work. Record the URL only after publication actually succeeds. Publication is part of the eventual submission, not this planning turn.

**Acceptance:** Every brief requirement maps to working source or an explicitly permitted design seam, and the evaluator can reproduce the core without guessing commands. No submission-complete claim while live discovery, manual handoff, or public-repository delivery remains outstanding.

## Command contract to implement

Setup, after installing uv and Python 3.12+:

```text
uv sync --locked
uv run playwright install chromium
uv run casetrace --help
uv run casetrace schema --output evidence/schema.json
```

Start the synthetic bank in one terminal:

```text
uv run casetrace fixture --scenario normal --port 8000
```

With a privately configured `.env`, run the first discovery in another terminal:

```text
uv run --env-file .env casetrace doctor --live-model
uv run --env-file .env casetrace discover --target http://127.0.0.1:8000 --bindings config/base.json --goal "Find the incoming USD 250.00 payment for member 12345 between September 1 and September 7, 2026" --params examples/queries/posted.json --output runs/discovery --headed
uv run casetrace validate-artifact runs/discovery/capability.json --target http://127.0.0.1:8000 --bindings config/base.json --cases tests/cases/normal.json --output runs/validation
```

`validate-artifact` runs the enumerated query cases against the currently started fixture under normal policy, writes a report linked to the executable digest, and attaches the report reference to the candidate. Fault scenarios are validated under their explicitly restarted fixture process and their additional report references are attached without changing executable content. This avoids giving the replay engine a hidden scenario-control API.

After privacy review and copying the validated artifact to the public evidence folder:

```text
uv run casetrace replay evidence/capability.json --target http://127.0.0.1:8000 --bindings config/base.json --params examples/queries/posted-new-member.json --output runs/replay-posted
uv run casetrace replay evidence/capability.json --target http://127.0.0.1:8000 --bindings config/base.json --params examples/queries/not-found.json --output runs/replay-not-found
```

Replay commands do not load `.env`, require model credentials, or contact a provider. Demonstrate provider egress denial in the e2e subprocess tests even if unrelated credentials exist in the shell environment.

For handoff, stop the normal fixture and start the same fixture executable in expiry mode:

```text
uv run casetrace fixture --scenario expire-once --port 8000
uv run casetrace replay evidence/capability.json --target http://127.0.0.1:8000 --bindings config/base.json --params examples/queries/posted-new-member.json --output runs/replay-handoff --headed --operator-port 8001
```

These two commands run in separate terminals. Open the local operator page, take control, operate the already-open bank browser, and resume. The replay process must stay alive throughout the interaction.

Verification:

```text
uv run pytest -m "not live_model and not manual" -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run casetrace evidence-check evidence
```

CLI exit codes: 0 for success or known business outcome; 2 for invalid invocation; 1 for terminal execution failure. Waiting for a human keeps the process alive and emits an intervention event. stdout is one final redacted JSON result; progress goes to redacted structured events/stderr. Do not echo raw arguments in error messages.

## Sequence, parallel work, and schedule

| Day | Deliverable | Gate |
| --- | --- | --- |
| 1 | Task 1; Task 2; Task 3 minimum; API access preparation | A person and protected primitives can operate the actual fixture |
| 2 | Tasks 4-5 first end-to-end path | Real discovery artifact replays with a new input and no provider |
| 3 | Task 6; complete contract/provenance validation | Every declared payment result and incomplete-search condition behaves correctly |
| 4 | Task 7 | Same-session manual takeover and deterministic resume |
| 5 | Task 8 | Safety, privacy, race, and keyless-replay checks pass |
| 6 | Task 9 evidence and report | Genuine discovery/replay/handoff evidence is complete and readable |
| 7 | Clean-checkout rehearsal, fixes, public delivery | All required paths/commands and final repository verified |

Task boundaries are independently reviewable, but the project is one integrated vertical slice. Do not implement all scaffolding before trying a live run. Use small failing-test/implementation/check cycles inside each task and commit accepted increments.

Useful parallel work after Task 1 freezes interfaces: fixture work can run alongside browser policy/evidence work, with separate file ownership. Later, an independent requirement review or documentation/evidence audit can run alongside fixes. Keep replay/compiler contract changes and session/handoff edits coordinated; do not assign overlapping mutations to independent agents.

The day allocations are estimates. If discovery integration takes longer, remove the second tenant and visual polish first. Keep real discovery, a typed artifact, replay/outcomes, guardrails, richer failure evidence, and live handoff. If API access remains unavailable, complete the offline core but report the submission as incomplete until genuine discovery is produced.

## Optional single stretch: tenant variant

Only after Tasks 1-9 core checks pass, add a second fixture binding that changes visible navigation labels/frame structure and verify the same capability contract with reviewed locator overrides. Key validation to both artifact and binding digests. Overrides cannot modify financial effects or weaken identity checks. Stop at one variant; no tenant administration or automatic drift repair.

## Plan self-review record

- Requirement coverage: Tasks 4/5 cover 3.1/3.2; Tasks 5/6 cover 3.3; Task 3/8 cover 3.4/3.5; Task 7 covers 3.6; the design plus compatibility checks in Task 8 cover 3.7; Task 9 covers required paths/headings/public delivery.
- Provenance: observed actions, generalized structure, authored handlers, scripted test actors, and a real person are separately identified.
- Search semantics: no match, multiple matches, partial traversal, permission denial, and stale identity cannot collapse into one generic result.
- Safety/control: read-only retries only; no automation while human owns input; no unverified resume; no raw sensitive evidence.
- Scope: one Python/browser implementation, one capability, one local target, a minimal operator page, and at most one optional tenant variant.
- Verification status at planning time: these are proposed implementation tests and commands; none is claimed to have run against an application yet.
