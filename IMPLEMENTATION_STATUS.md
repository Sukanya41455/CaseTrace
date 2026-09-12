# CaseTrace implementation checkpoint — 2026-09-12

This is an interrupted implementation checkpoint, not a completion report. The user asked to save all pending work before their usage runs out. Start the next implementation session by reading this file, then the approved plan and the current source. Keep the existing work; do not restart planning or scaffolding.

## Decisions that remain in force

- Use the existing **Gemini** configuration in the private `.env`. The selected model is `gemini-3.5-flash`; its real function-call connectivity probe succeeded. That probe is not UI discovery evidence.
- Use the project-local **`.venv` for all Python execution and dependencies**. It is already created with Python 3.12.14 and isolated from system site packages. Do not recreate it unnecessarily or install project packages globally.
- Implement the approved CaseTrace plan, using only the rendered synthetic bank UI for business data. Replay must not import or contact a model provider, including during recovery.
- Keep credentials and raw sensitive observations out of persisted artifacts, logs, Git, and tool output. Do not print `.env` values or raw provider exceptions.
- Genuine model discovery, deterministic replay of its saved artifact with new inputs, explicit outcomes, and a genuine same-session human handoff are mandatory before claiming completion. Authored tests and simulated operators are separate evidence.

## Where the work lives

- Approved design: `docs/superpowers/specs/2026-09-11-casetrace-design.md`.
- Approved plan: `docs/superpowers/plans/2026-09-11-casetrace-implementation.md`.
- Detailed private ledger, task briefs/reports, review findings, and replay design: `.superpowers/sdd/2026-09-11-casetrace-implementation/` (ignored by Git, retained locally).
- The older `progress.md` and `replay-design.md` in that directory contain useful rationale but stale task statuses. This checkpoint supersedes their status statements.
- `README.md` currently documents the environment and explicitly states that the full system is incomplete.

## Implemented and saved

1. Package foundation, lockfile, strict Pydantic query/artifact/result/event schemas, schema CLI. Seven foundation review findings were fixed and reviewed as resolved. The schema export should be regenerated after the latest contract extension.
2. Separate synthetic bank fixture, templates, deterministic scenarios, example queries, and browser tests. It uses a real iframe and normal forms/links. The harmless transfer control provides a denial oracle.
3. Protected Playwright surface, policy allowlist, exclusive session controller, redacted evidence writer, and integrity digests. Browser work includes semantic reads, fresh opaque handles, fields/table parsers, page fingerprints, input gating, and redirect interception. These are still awaiting final integrated review.
4. `provider.py`, `discovery.py`, `compiler.py`, and their offline tests now exist. They implement Gemini manual function calls, bounded exploration, and candidate validation. They are unfinished and have not produced real UI discovery evidence.
5. Exact payment matcher with 26 passing tests. New `replay.py` implements a bounded interpreter, digest validation, compatibility checks, runtime coverage ledger, and exact matching with detail agreement. Its initial six unit tests pass; traversal and recovery remain unfinished.
6. Minimal guarded operator HTTP app and five passing tests. It calls the session controller with CSRF, exact origin/host, intervention, and generation checks. Browser wiring is unfinished.

Existing accepted local commits before the checkpoint: `3f9a4c6`, `90e63dc`, `654398e`, `9996f35`, and `3da43b7` (operator controls). Additional files may be saved as an explicitly unfinished checkpoint; do not confuse that with an accepted release.

## Latest verification and known failures

- Earlier full suite: **109 passed, 2 upstream warnings** before the newest provider/replay/operator integration work.
- Latest focused contract and payment-decision-binding suite: **51 passed**.
- Operator focused suite: **5 passed**.
- Replay focused suite: **6 passed, 1 failed**. The failing test is `test_declared_handoff_keeps_session_and_restarts_after_verified_resume` in `tests/unit/test_replay.py`. It is an intentional red test: replay currently stops on a declared handoff handler instead of requesting an intervention and waiting for verified resume; the simulated operator times out.
- Latest Ruff check: **26 findings** across the newly interrupted files (mostly formatting/imports; three loop-lambda bindings and a lambda assignment in replay). Format check reports seven files. Do not report lint as passing.
- Fresh full-suite run while saving this checkpoint: **140 passed, 1 failed, 2 upstream warnings in 42.97 seconds**. The only failure is the unfinished handoff test described above. No live model run was started during this save operation.

## Important implementation seams

### Replay and contracts

- `ReturnStep.result_kind="payment_decision"` plus `PaymentDecisionBindings(kind="payment_decision", record_steps=[...], detail_steps=[...])` was added. It explicitly reduces observations accumulated across completed loops, avoiding inaccessible loop variables or magic runtime variable names. Final `RunResult` still has only success/business_outcome/failure.
- Referenced read IDs must exist with the correct `read_role`; nesting now permits four levels for account/page/row/primitive. Existing direct `PaymentFieldBindings` success returns remain supported.
- Coverage derives from actual account collection exhaustion, both sources, and terminal pages. `payment_rows` reads require fresh visible FIELDS context with member/account/source and applied date/amount/currency/direction/reference. Labels alone are not proof.
- Review the new terminal reduction carefully: a subset of `record_steps` must not hide other observed candidates while borrowing global coverage. It currently needs a regression test and likely tighter coverage association.
- Add real browser traversal tests with explicitly authored test artifacts before relying on model-generated artifacts. No such integration test landed before worker interruption.
- Pagination currently requires returning to the same page fingerprint after any detail exploration. Fixture “Back to activity” resets filters/page; an artifact must restore state through observed UI, or the execution design needs a small explicit and verified adjustment. Do not bypass this check to force success.

### Discovery and compiler

- Gemini FillProposal now allows typed input references or bounded constants CREDIT/history/pending. Review tests after this late change.
- Candidate requests currently pass the full recursive Capability schema as Gemini structured output. Verify that the selected model accepts it; use an explicit manual proposal boundary if necessary, without relaxing local Pydantic validation.
- Persist sanitized `DiscoveryRecording.public_summary()` after each action so a failed candidate can be diagnosed without losing the recording. This was requested but did not land (`recording.json` is not currently written).
- Capture actual provider metadata for candidate generation and finish decisions, not just UI action decisions. Preserve response IDs and counts, never private reasoning.
- Initial discovery observation may be blank before navigation; update recording surface identity from the first recognized application observation.
- Review compiler target equivalence/provenance checks carefully. Generalized targets must remain grounded in actual observations; matching only a broad role is insufficient.
- Executable confirmation must prove the UI goal; a model’s finish request or checkpoint role label alone is insufficient. No real successful discovery exists yet.

### Handoff and browser

- `SessionController` now accepts `transition_handler: Callable[[SessionState], Awaitable[None]]`; callbacks are invoked for PAUSING/HUMAN/VERIFYING/AUTOMATION/CANCELLED. Review callback failure and lock semantics.
- Wire HUMAN to `surface.set_human_control(True)` only after in-flight actions drain. Close the gate before resume verification and abort. Emit ownership evidence with stable browser/context/page IDs.
- `BrowserSurface.verify_resume()` was planned but did not land. Require the same live session, allowed origin, authenticated recognized member-search screen, supported vendor/version, and no dialog. Reject wrong member/account/unsupported states.
- Preserve human input permission across navigation within the same browser context. The current injected document script defaults to automation; the promised generation-safe ownership initialization did not land.
- Replay needs bounded handoff waits, cancellation and timeout outcomes, separate active/human time budgets, clearing candidates/coverage on verified resume, and restart from the declared read-only root. Do not create a new browser on resume.
- Retry handlers are not implemented; currently all matching handlers stop. Only reviewed read-only actions may be retried under declared limits.

## Remaining delivery tasks, in order

1. Finish the current red handoff test and browser/session wiring; run focused tests and review the interrupted browser/parser changes.
2. Fix offline discovery/compiler tests and lint, regenerate the exported schema, and add meaningful traversal/coverage tests.
3. Implement CLI `doctor --live-model`, `discover`, `validate-artifact`, `replay`, and `evidence-check`. CLI currently only exposes schema and fixture.
4. Run real Gemini discovery promptly against the normal fixture. Save genuine sanitized recording/candidate evidence; diagnose actual failures. Do not hand-author the flagship navigation sequence and label it discovered.
5. Validate its exact artifact/binding digests across normal queries and exceptional fixture processes. Replay with new member/input and zero model calls, including model-egress denial testing.
6. Verify POSTED/PENDING/REVERSED, NOT_FOUND, AMBIGUOUS, MEMBER_NOT_FOUND, NO_ACCOUNTS, stale identity, conflicting rows, search limits, timeouts, permission denial, unknown dialogs, and policy denial. No partial search may return success or NOT_FOUND.
7. Run the real headed same-session human takeover/resume demonstration when the operator page and paused browser are ready. Request the person’s participation then; simulated tests cannot replace it.
8. Finish offline CI, privacy/evidence validation, required README/REPORT sections, and clean-checkout rehearsal. Prepare concrete public-safe output before any publication approval.

## Resume commands (PowerShell)

```powershell
Set-Location C:\Users\sukan\Documents\bankgpt
$env:UV_CACHE_DIR = Join-Path $PWD '.uv-cache'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PWD '.browser-cache'
.\.venv\Scripts\python.exe -m pytest tests/unit/test_replay.py -q --tb=short
.\.venv\Scripts\python.exe -m pytest -q --tb=short
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m ruff format --check src tests
.\.venv\Scripts\python.exe -m casetrace.cli fixture --scenario normal --port 8000
```

Use `.tools\bin\uv.exe sync --locked --extra dev` only if dependency synchronization is needed. Chromium is already installed in `.browser-cache`. Network/model calls may need narrowly scoped sandbox escalation. Earlier workers hit account usage limits and no subagents remain active at this checkpoint; do not assume their unfinished assignments are running.
