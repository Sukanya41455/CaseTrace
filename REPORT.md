# CaseTrace Design Report

## Architecture

CaseTrace turns a model-guided browser exploration into a bounded capability that can later run without model judgment. The submitted vertical slice investigates incoming payments in a synthetic, server-rendered banking application.

The workflow has two deliberately separate phases:

```text
typed goal + invocation
        |
        v
model-guided UI discovery -> sanitized recording -> deterministic compiler
                                                    |
                                                    v
                                           unvalidated capability
                                                    |
                                                    v
                                          scenario validation
                                                    |
                                                    v
new invocation -> validated capability -> deterministic browser replay
```

Discovery observes the rendered interface through Playwright and proposes one bounded UI action at a time. Policy checks every action before execution. After the model reaches the verified payment-detail finish gate, trusted code qualifies the recording and compiles the reusable graph; the model does not author the final executable artifact. The submitted discovery used `qwen3.5:9b` and recorded 17 model calls.

Validation attaches results from eight browser scenarios to the compiled capability. Replay then interprets that validated graph using typed inputs and tenant bindings. The replay path contains no model decision loop, and every submitted replay event records zero model calls and no provider response ID.

The evidence bundle in `evidence/` links discovery, validation, successful replay, business-outcome replay, hard-failure replay, and human handoff through one executable artifact digest (`0a39dc8932aedbefb41ed77e60fdaa83a98fcb72894724d03b6531cc6509f22a`) and one binding digest (`1869961fb3cebe61276ca89bf8f7eb8d0ea3fecfe8ccec0b2a9f1d7d0b521680`).

## Artifact schema

The capability is a typed, versioned Pydantic document rather than a script or recording of raw clicks. It contains:

- a declared input contract for member, amount, currency, date range, and optional reference;
- reviewed targets with bounded locator strategies and cardinality expectations;
- typed navigation, fill, click, read, assertion, branch, loop, pagination, and return steps;
- symbolic input references instead of discovery-time customer values;
- checkpoints for identity, filters, page exhaustion, and payment confirmation;
- explicit execution limits and recovery handlers;
- provenance distinguishing observed operations from compiler-added generalized control flow; and
- validation records with scenario outcomes and evidence references.

Tenant bindings are stored separately from the capability. They define the permitted origin, application identity and version, supported surface features, approved actions, and compatibility constraints. Capability and binding digests are carried independently so a reviewed workflow cannot silently run against a different tenant configuration.

The discovery artifact is initially unvalidated. `evidence/validation/capability.json` is the same executable capability with one attached validation report covering eight passing scenarios: posted, pending, reversed, not found, ambiguous, member not found, no accounts, and session-expired handoff. Validation metadata does not change the executable artifact identity used by the submitted replays.

## Determinism & error handling

Replay resolves only values and control flow represented in the validated artifact. It does not evaluate arbitrary expressions, choose new locators, or ask a model how to recover. Loops, pagination, retries, checkpoints, and limits are explicit and bounded. Fresh browser observations are reduced into one of three typed result families:

- `success` for one confirmed payment;
- `business_outcome` for an expected domain result such as `NOT_FOUND`; or
- `failure` for an execution or safety invariant that could not be satisfied.

The submitted evidence demonstrates each important class:

- `evidence/replay-posted/result.json` returns `success` with status `POSTED` using the distinct `posted-new-member.json` invocation.
- `evidence/replay-not-found/result.json` returns `NOT_FOUND` only after complete coverage of two accounts, five pages, and all declared sources.
- `evidence/replay-checkpoint-failure/result.json` returns the typed hard failure `CHECKPOINT_FAILED` at `read-accounts`, with sanitized expected and observed conditions.
- `evidence/replay-handoff/result.json` resumes after human intervention and returns `POSTED`.

The discovery query uses member `12345` and amount `250.00`; the posted replay command uses member `54321` and amount `175.50`. The root evidence manifest records that replay inputs differ. Every run manifest seals its event and result files with SHA-256 hashes, while the root manifest requires the same artifact and binding digests across the chain.

## Heterogeneity & multi-tenant

The core runtime depends on a `Surface` boundary rather than directly on application-specific browser calls. The implemented surface is a protected Playwright browser adapter for one hostile legacy-style web application, including normal navigation, forms, tables, frames, pagination, and session expiry. A desktop surface is a designed extension point, not an implemented deliverable.

Application-specific details live in tenant bindings and the capability target catalog. This separation permits the same execution model to support different origins, versions, locator strategies, and approved controls without embedding those values in the replay engine. Compatibility checks prevent a capability from running when its schema, application version, required features, or binding digest do not match.

The submission demonstrates one tenant configuration. It does not claim production multi-tenant orchestration, a tenant-management service, or cross-application portability. The implemented contribution is the contract boundary and digest isolation needed for those future deployments.

## Escalation & handoff

Replay can pause at an artifact-declared recovery boundary and transfer exclusive ownership of the existing browser session to a human operator. The session controller moves through explicit ownership generations; automation cannot act while the human owns the page. Resume closes the human gate, verifies the live page, and returns ownership to automation only when the expected state is restored.

`evidence/replay-handoff/events.jsonl` records a genuine headed handoff:

```text
automation -> none -> human_operator -> none -> automation
```

The evidence includes redacted manual UI actions and preserves one browser ID, context ID, and page ID throughout the transition. After verification, replay restarts from the declared read-only recovery root and completes with a `POSTED` result. This proves same-session transfer and resumption rather than a simulated operator or a replacement browser.

Handoff waits, active automation time, human time, retries, and interventions are separately bounded. Cancellation and timeout return typed failures instead of leaving ownership ambiguous.

## Safety

CaseTrace is read-only by construction for this capability. The tenant policy allowlists origins, navigation paths, and semantic actions; a transfer control exists in the fixture specifically to demonstrate that a visible write action remains denied. Browser interception also blocks unapproved redirects, frames, popups, and subrequests before they can reach another origin.

Targets require reviewed locator strategies and cardinality. Checkpoints verify visible state before dependent steps proceed, and `NOT_FOUND` is returned only after complete declared search coverage. Replay verifies capability compatibility and digests before mutating browser state.

Evidence persistence is allowlisted and redacted. Typed inputs, credentials, cookies, provider bodies, tracebacks, and URL query strings are excluded or sanitized. Screenshots are retained only when the surface considers them safe. Public results expose the minimum required business information, such as payment status, coverage, or a typed failure condition.

The most important separation is operational: the model may explore during discovery, but it is absent from replay decisions. The submitted replay logs contain zero positive `model_call_count` values and no provider response identifiers.

## Cuts

This submission intentionally proves one narrow workflow deeply rather than presenting a general automation platform.

Included are genuine model-guided browser discovery, deterministic recording compilation, a typed and validated capability, different-input replay, explicit business outcomes and failures, policy enforcement, digest-linked evidence, and genuine same-session human handoff.

Deliberate cuts are:

- production banking integrations, credentials, and customer data;
- write-capable payment or transfer workflows;
- desktop automation and additional application surfaces;
- production multi-tenant control planes and tenant provisioning;
- distributed workers, queues, schedulers, and service orchestration;
- encrypted evidence storage and managed retention;
- a capability catalog or agent-facing registry; and
- model fallback during replay.

The synthetic bank is therefore a behavioral test surface, not a production-bank simulation. The evidence supports the bounded claim that a model can discover this workflow, trusted code can compile and validate it, and the resulting artifact can execute deterministically with new inputs and controlled human recovery.
