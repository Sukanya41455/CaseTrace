# CaseTrace Design Report

## 1. System Overview

CaseTrace is designed around one core separation:

> **Use a model to discover a workflow, then use a validated artifact to execute that workflow deterministically.**

The model is not part of the production replay path.

At a high level:

```text
                     DISCOVERY
┌────────────────────────────────────────────────────────────┐
│ Natural-language goal                                     │
│          ↓                                                 │
│ Model receives sanitized UI observation                    │
│          ↓                                                 │
│ Model proposes one constrained action                      │
│          ↓                                                 │
│ Policy authorizes or rejects action                        │
│          ↓                                                 │
│ Surface executes action                                    │
│          ↓                                                 │
│ Evidence + checkpoints recorded                            │
│          ↓                                                 │
│ Recording compiled into candidate capability               │
│          ↓                                                 │
│ Capability validated                                       │
└────────────────────────────────────────────────────────────┘
                              │
                              ▼
                         REVIEWED ARTIFACT
                              │
                              ▼
                       DETERMINISTIC REPLAY
┌────────────────────────────────────────────────────────────┐
│ Typed invocation                                           │
│          ↓                                                 │
│ Validated capability                                       │
│          ↓                                                 │
│ Fresh browser session                                      │
│          ↓                                                 │
│ Replay engine interprets saved graph                       │
│          ↓                                                 │
│ Policy + checkpoints + limits enforced                     │
│          ↓                                                 │
│ Typed result                                               │
└────────────────────────────────────────────────────────────┘
```

This gives CaseTrace two different execution modes with different responsibilities:

| Mode      | Purpose                              | Model involved? | Output                                      |
| --------- | ------------------------------------ | --------------: | ------------------------------------------- |
| Discovery | Learn a workflow from the live UI    |             Yes | Structured recording / capability candidate |
| Replay    | Execute an already-reviewed workflow |              No | Typed business result or typed failure      |

The separation is intentional.

Models are useful for exploring unfamiliar interfaces and discovering workflow structure. They are less appropriate as an unrestricted production execution mechanism where behavior should be bounded, repeatable, reviewable, and auditable.

---

# 2. Architecture

## 2.1 Main Components

CaseTrace currently separates responsibilities across the following components:

```text
CLI
 │
 ├── Provider Adapter
 │      └── model-guided discovery
 │
 ├── Policy Layer
 │      └── authorization / deny-by-default checks
 │
 ├── Surface
 │      └── UI observation and interaction
 │
 ├── Evidence Writer
 │      └── recording, redaction, checkpoints
 │
 ├── Capability Compiler / Validator
 │      └── converts discovery into reviewed artifact
 │
 ├── Replay Engine
 │      └── deterministic execution
 │
 └── Session Controller
        └── automation ↔ human ownership
```

The current implementation runs in a **single Python process**.

That is deliberate for this vertical slice. Distributed workers, queues, and service orchestration would add infrastructure without improving the core demonstration.

The important architectural work is instead in the boundaries between components.

Those boundaries make it possible to split the system into separate services later without redefining the execution contract.

---

## 2.2 Discovery Inputs

The command-line interface accepts:

* a natural-language goal
* target origin
* tenant bindings
* typed payment parameters

The typed input for the current capability is a `PaymentQuery`.

The current workflow is deliberately narrow and focused on payment investigation rather than general-purpose browser automation.

---

## 2.3 Provider Adapter

Discovery uses a **provider-neutral model adapter**.

The model does not receive unrestricted access to the browser or arbitrary tools.

Instead, it receives:

* a sanitized observation of the current UI
* a constrained set of declared UI operations

The model must propose **exactly one declared action at a time**.

Conceptually:

```text
observe()
   ↓
sanitize()
   ↓
model proposes:
    click(target)
       OR
    fill(target, symbolic_value)
       OR
    read(target)
       OR
    ...
   ↓
policy.authorize(action)
   ↓
surface.execute(action)
```

The model cannot directly bypass the policy layer.

This means model intent and executable action remain separate concepts.

---

## 2.4 Policy Layer

Every proposed action passes through policy before execution.

Policy is responsible for determining whether the requested operation is allowed within the capability's reviewed scope.

For the current payment-investigation capability, policy is intentionally restrictive.

It considers:

* target origin
* route
* HTTP method
* reviewed control meaning
* target cardinality
* supported primitive
* capability scope

The intended behavior is **fail closed**.

An unknown or ambiguous action is rejected instead of being interpreted optimistically.

---

## 2.5 Surface Abstraction

UI-specific behavior is hidden behind a `Surface` protocol.

The protocol is responsible for operations conceptually equivalent to:

```text
observe
act
read
check
capture
bind typed inputs
resolve observation-scoped targets
```

The rest of the system does not need to know whether those operations are implemented through:

* Playwright
* a desktop accessibility API
* screenshot-based interaction
* another reviewed execution mechanism

This is the main heterogeneity boundary in the architecture.

---

## 2.6 Current Browser Implementation

The concrete implementation today is:

```text
BrowserSurface
    ↓
Playwright
    ↓
Local server-rendered banking application
```

The fixture is deliberately more complicated than a clean demo SPA.

It includes:

* frames
* tables
* pagination
* dialogs
* session expiration
* pages without test IDs

These cases force discovery and replay to handle realistic traversal problems instead of depending on idealized DOM structure.

Browser-specific perception and interaction remain inside `BrowserSurface`.

The following systems depend on the `Surface` contract rather than on Playwright directly:

* discovery
* replay
* policy
* target matching
* evidence generation
* session ownership

---

# 3. Discovery Execution

## 3.1 Single-Action Loop

During discovery, interaction proceeds one action at a time.

```text
1. Observe UI
2. Sanitize observation
3. Send observation + available tools to provider
4. Provider chooses one action
5. Validate action structure
6. Run policy authorization
7. Execute through Surface
8. Capture resulting state
9. Record evidence
10. Repeat
```

Restricting the model to one action per iteration is important because each state transition can be:

* authorized independently
* observed independently
* recorded independently
* rejected independently

The model therefore proposes intent incrementally rather than emitting an opaque script that executes without intermediate review.

---

## 3.2 Evidence During Discovery

After actions execute, the evidence writer records information such as:

* observation
* action
* checkpoint state
* provider metadata

Discovery event IDs later become part of capability provenance.

A behavior learned from the UI therefore has a trace back to the event where it was observed.

---

## 3.3 Capability Compilation

A discovery recording is not automatically treated as a production capability.

Compilation only succeeds when the recording satisfies the capability contract.

Relevant requirements include:

* declared operations
* required checkpoints
* symbolic input handling
* provenance
* complete control flow
* bounded search paths

Compilation rejects artifacts containing problems such as:

* frozen private input values
* missing evidence
* unsupported generalization
* incomplete search paths
* control-flow paths without a typed return

The result is intended to be a reviewed executable artifact, not simply a replay of model-generated clicks.

---

# 4. Replay Architecture

Replay is intentionally independent from discovery.

Its inputs are:

```text
Validated capability
        +
Typed invocation
        +
Tenant binding
```

Replay then creates a **fresh browser session** and interprets the stored capability graph.

The model provider is not loaded or consulted.

```text
model calls during replay = 0
```

This matters because production behavior is now defined by the artifact rather than by a new model decision at every step.

---

## 4.1 Replay Properties

Replay is designed to be:

* deterministic within the declared UI contract
* bounded
* typed
* observable
* auditable
* fail-closed

The replay engine does not have access to arbitrary expression evaluation or unrestricted scripting.

It resolves only value types explicitly represented in the artifact.

---

# 5. Capability Artifact

## 5.1 Schema

Capabilities are validated using Pydantic.

Current versions:

```text
Schema version:     1.0
Capability version: 0.1.0
```

The public capability contract declares:

* capability ID
* supported vendor
* supported application versions
* required surface features
* typed input
* typed output
* execution constraints

---

## 5.2 Current Capability Scope

The current capability supports:

| Property                    | Current Scope     |
| --------------------------- | ----------------- |
| Direction                   | Incoming payments |
| Currency                    | USD               |
| Sources                     | History, Pending  |
| Maximum date range          | 31 days           |
| Side effects                | Read-only         |
| Surface                     | Browser           |
| Tenant configs demonstrated | 1                 |

The narrow scope is deliberate.

The project prioritizes enforcing a strong execution contract for one workflow before generalizing to many unrelated operations.

---

# 6. Typed Inputs and Outputs

## 6.1 Input

The artifact accepts a typed:

```text
PaymentQuery
```

Invocation-specific values are represented symbolically inside the reusable artifact.

They are not frozen into the recorded workflow.

This allows the same capability to be replayed using different payment queries.

---

## 6.2 Output

The public result is a discriminated union:

```text
Result
 ├── Success
 ├── BusinessOutcome
 └── Failure
```

This distinction is important because an expected business result is not necessarily a system failure.

For example:

```text
NOT_FOUND
```

can mean:

> The workflow executed correctly, exhausted its declared search space, and found no matching payment.

That is different from:

```text
CHECKPOINT_FAILURE
```

which means:

> The system could not prove the expected execution state.

---

# 7. Targets and Locators

Targets are defined independently from execution steps.

A target describes the semantic UI element that the workflow expects to interact with.

Each target can declare:

* frame path
* expected cardinality
* reviewed control meaning
* one or more locator strategies

---

## 7.1 Locator Strategies

Current strategies include:

* accessible role + accessible name
* exact visible text
* adjacent labeled controls
* table relationships

The architecture intentionally avoids making generated CSS selectors or test IDs the primary contract.

That makes targets less dependent on implementation-specific DOM structure.

---

## 7.2 Cardinality

Targets declare how many matching controls are expected.

For example:

```text
expected cardinality = 1
```

means exactly one reviewed target must resolve.

If resolution produces:

```text
0 matches
```

the control is missing.

If it produces:

```text
2+ matches
```

the control is ambiguous.

Replay does not pick one heuristically.

It fails instead.

This prevents an ambiguous locator from silently becoming an incorrect action.

---

# 8. Step Graph

A capability is represented as a typed control-flow graph.

Supported primitives include:

```text
navigate
fill
click
read
assert
branch
for_each
paginate
return
```

Loops and pagination are bounded.

---

## 8.1 Values

Step values can come from only three categories:

```text
Literal
InvocationInput
PreviouslyReadVariable
```

Unsupported behavior includes:

* arbitrary expression evaluation
* arbitrary runtime scripting
* unrestricted string interpolation

This keeps runtime behavior inside the reviewed artifact model.

---

## 8.2 Control-Flow Requirements

Every executable path must terminate in a declared result.

The compiler rejects graphs that can fall through without returning a typed result.

For example:

```text
          ┌─ match ───────→ SUCCESS
search ───┤
          └─ no match
                ↓
           pages left?
            /      \
          yes      no
           │        │
        continue  NOT_FOUND
```

There is no implicit end state.

Every terminal outcome must be declared.

---

# 9. Checkpoints

Checkpoints represent semantic facts about the current execution state.

Examples include:

* member identity
* source identity
* filter state
* page exhaustion
* payment identity

A checkpoint is stronger than simply knowing that an action completed.

For example:

```text
click("Pending")
```

only proves that a click was attempted.

A source checkpoint verifies that the UI actually represents the expected payment source afterward.

This distinction is important for deterministic replay.

---

# 10. Payment Matching

A list-row match is not enough to return a payment.

The workflow must verify payment identity from the transaction detail view.

The replay path is:

```text
Candidate row found
       ↓
Open transaction detail
       ↓
Read detail fields
       ↓
Correlate fields with PaymentQuery
       ↓
Payment identity checkpoint
       ↓
Return result
```

A payment cannot be returned before the identity checkpoint passes.

This reduces the risk of returning a superficially similar row from a payment table.

---

# 11. Search Exhaustion

`NOT_FOUND` has a strict meaning.

It is only permitted after the capability has exhausted its declared search dimensions.

Those include:

* accounts
* payment sources
* pages

Conceptually:

```text
for account in bounded_accounts:
    for source in [History, Pending]:
        for page in bounded_pages:
            search(page)

if every declared search path exhausted:
    return NOT_FOUND
```

A partial search is therefore not allowed to produce a confident negative result.

---

# 12. Determinism and Runtime Limits

Replay determinism comes from several constraints working together:

* validated artifact
* declared value references
* exact target cardinality
* visible-state checks
* typed control flow
* bounded iteration
* typed terminal results

Execution budgets include limits for:

* accounts
* sources
* pages
* rows
* actions
* retries
* interventions
* total execution time

The runtime cannot search indefinitely.

---

# 13. Business Outcomes vs. Failures

## 13.1 Business Outcomes

Expected caller-visible outcomes include:

```text
NOT_FOUND
AMBIGUOUS
MEMBER_NOT_FOUND
NO_ACCOUNTS
```

These describe valid conclusions reached by the workflow.

---

## 13.2 Failures

Failures represent an execution or verification problem.

Examples include:

* policy denial
* access denial
* locator ambiguity
* failed checkpoint
* exhausted search limits
* timeout
* inconsistent records
* unknown state
* lost session
* model failure
* handoff failure

Failure records include:

```text
current step
expected condition
observed condition
evidence references
```

This makes failures diagnosable without reducing them to a generic exception string.

---

# 14. Retry Behavior

Known transient conditions can use artifact-declared retry handlers.

Current retry policy:

```text
Maximum attempts: 2
Backoff: fixed
```

The runtime does not generically retry every failure.

A retry must be declared for a recognized state.

This distinction prevents unexpected UI behavior from being hidden by uncontrolled retry loops.

---

# 15. Handler Model

Handlers have two important parts:

```text
Detector
    +
Disposition
```

Supported dispositions are:

```text
retry
handoff
fail
```

For example:

```text
Known transient dialog
        ↓
detector matches
        ↓
retry
```

or:

```text
Known state requiring human action
        ↓
detector matches
        ↓
handoff
```

Anything outside the declared handler set fails closed.

---

# 16. Provenance

Capability behavior carries provenance.

There are two broad sources.

### Observed behavior

Behavior directly seen during discovery references:

```text
discovery event IDs
```

### Generalized or authored behavior

Behavior that was not directly observed during that discovery must identify:

```text
validation scenario
```

This distinction allows reviewers to tell whether a behavior was:

* directly observed
* generalized
* explicitly authored

rather than treating all artifact logic as equally derived.

---

# 17. Validation Records

Validation records are attached to the capability.

They include:

* artifact digest
* tenant-binding digest
* validation scenario

The digest pair provides a concrete link between:

```text
workflow definition
        +
tenant configuration
        +
validation evidence
```

This is particularly important for multi-tenant use because the same workflow may execute under different tenant-specific mappings.

---

# 18. Evidence Integrity

Replay records:

* artifact digest
* tenant-binding digest
* contiguous event sequence numbers
* sealed manifest

The goal is to make later modification detectable.

Every replay event also records that the replay path performed:

```text
0 model calls
```

This makes the distinction between discovery-time model use and replay-time deterministic execution visible in evidence.

---

# 19. Multi-Tenant Design

The reusable execution model is split into:

```text
Vendor/version capability
          +
Tenant binding
```

The capability describes the reusable workflow.

The binding provides institution-specific configuration.

---

## 19.1 Tenant Binding Contents

Bindings can provide:

* origin
* application version
* label aliases
* frame aliases
* timezone
* privacy mappings

This prevents tenant-specific names or origins from being permanently embedded in the reusable capability graph.

---

## 19.2 Compatibility Checks

Before replay begins, the runtime verifies:

* vendor
* application version
* surface features
* artifact digest
* binding digest

A mismatch stops execution.

The system does not silently apply a validated artifact to an unsupported version.

---

## 19.3 Intended Versioning Model

A larger deployment would treat validated tenant bindings as versioned overlays.

Conceptually:

```text
Vendor v3 capability
 ├── Tenant A binding
 ├── Tenant B binding
 └── Tenant C binding
```

If the vendor application changes:

```text
drift detected
      ↓
revalidate binding
      OR
create new capability version
```

An already-approved capability should not be silently mutated in place.

---

# 20. Surface Portability

The `Surface` protocol is intended to make UI execution replaceable.

Today:

```text
Surface
   ↓
BrowserSurface
   ↓
Playwright
```

A future implementation could add:

```text
Surface
   ├── BrowserSurface
   └── DesktopSurface
```

The desktop implementation could translate target intent into mechanisms such as:

* accessibility nodes
* reviewed screen regions

while preserving the same:

* capability graph
* result taxonomy
* policy contract
* replay limits
* evidence format
* handoff model

---

## 20.1 Current Limitation

The project currently implements:

```text
Browser surfaces:       1
Tenant configurations:  1
Desktop surfaces:       0
```

The current locator set is **not claimed to transfer unchanged to arbitrary desktop applications**.

The portability claim is architectural:

> New surfaces should be implementable behind the existing execution contract.

It is not a claim that current browser selectors automatically solve desktop automation.

---

# 21. Human Escalation and Handoff

Some states cannot or should not be handled automatically.

CaseTrace models those states through explicit ownership transfer.

The session ownership state machine is:

```text
AUTOMATION
     ↓
PAUSING
     ↓
HUMAN
     ↓
VERIFYING
     ↓
TERMINAL / AUTOMATION
```

---

# 22. Starting a Handoff

When a declared handler requests intervention:

1. The session controller stops accepting new automation actions.
2. Any already-running action is allowed to drain.
3. The ownership generation is incremented.
4. An intervention record is created.
5. Control becomes available to the human operator.

The intervention includes:

* run ID
* current step
* reason
* browser identifier
* browser-context identifier
* page identifier

---

# 23. Operator Interface

A small local operator endpoint exposes:

* pending intervention
* take control
* resume
* abort

The UI itself is intentionally minimal.

The important behavior is the ownership model underneath it.

---

# 24. Same-Session Operation

The human does not open a replacement browser.

They operate the **same live browser session** that automation was using.

```text
automation browser session
          ↓
pause
          ↓
human operates same session
          ↓
verification
          ↓
automation may resume
```

This preserves session state and evidence continuity.

---

# 25. Human Evidence

Manual UI actions are recorded as:

```text
actor: human_operator
```

Field values are omitted from the evidence stream.

This preserves the fact that a manual action occurred without persisting sensitive form contents.

---

# 26. Resume Verification

Automation does not trust the page simply because the human presses **Resume**.

Before reclaiming control, the system verifies:

* supported vendor
* supported application version
* expected authentication marker
* absence of blocking dialogs
* recognized checkpoint

Only after those checks pass can automation continue.

If verification fails:

```text
Human retains ownership
        ↓
Typed checkpoint failure
```

This prevents automation from resuming into an unknown UI state.

---

# 27. Ownership Generations

Each ownership transition increments a generation.

The purpose is to reject late results produced by a previous owner.

Example:

```text
Generation 7: automation starts action
        ↓
handoff begins
        ↓
Generation 8: human owns session
        ↓
old Generation 7 action returns late
        ↓
result rejected
```

This protects the session from race conditions during control transfer.

---

# 28. Intervention Budgets

Human waiting time is accounted for separately from active automation time.

Interventions also have explicit:

* count limits
* time limits

This prevents a run from remaining indefinitely suspended without being represented in the execution budget.

---

# 29. Safety Model

The current payment-investigation capability is **read-only by construction**.

Risky write operations are not merely discouraged.

They are outside the declared capability scope.

---

## 29.1 Allowlisted Behavior

Policy allowlists:

* exact origins
* route templates
* HTTP methods
* reviewed control meanings

Checks apply to:

* navigation
* document requests
* frames
* images
* redirects
* popups

The runtime checks those operations before allowing execution outside the permitted boundary.

---

## 29.2 Control Authorization

Before an action is authorized, its locator must resolve to:

```text
exactly one reviewed control
```

The following classes of targets are denied:

* transfer controls
* credential controls
* unknown controls
* unreviewed form destinations
* unsupported primitives
* ambiguous targets

The system therefore avoids relying on the model to decide whether a risky UI action is acceptable.

---

# 30. Sensitive Data Handling

Sensitive invocation values are stored symbolically in:

* discovery recordings
* compiled artifacts

The evidence writer persists only allowlisted fields.

Redacted categories include:

* member identifiers
* amounts
* dates
* payment references
* secret assignments
* provider bodies
* tracebacks
* URL query strings

---

# 31. Screenshot Safety

Screenshots are saved only when recognized sensitive regions can be completely masked.

If safe masking cannot be guaranteed:

```text
screenshot capture fails
```

The system prefers missing visual evidence over persisting an unsafe image.

---

# 32. Replay Result Privacy

Replay exposes only the information required by the capability contract.

For the payment workflow, this means outputs such as:

* payment status
* aliases

rather than unrestricted:

* account information
* transaction details

Credentials are loaded from the environment and excluded from version control.

---

# 33. Production Security Boundary

The controls implemented here reduce the risk of:

* model-driven overreach
* accidental sensitive-data persistence
* ambiguous UI actions
* unbounded traversal

They are **not** intended to replace production security infrastructure.

A production deployment would still require areas such as:

* identity management
* encryption
* key management
* managed secrets
* audit retention
* institutional authorization review
* encrypted evidence storage

The intent is that the same fail-closed policy and redaction boundaries remain in place around those production systems.

---

# 34. Demo Replay

The validated demo capability is replayed with an invocation different from the one used while authoring the traversal.

The replay:

1. Starts a fresh browser session.
2. Navigates through the visible banking UI.
3. Searches bounded accounts.
4. Searches `History`.
5. Searches `Pending`.
6. Traverses bounded pages.
7. Opens the matching transaction detail.
8. Verifies payment identity.
9. Returns only the redacted payment status.

The replay path does not contact a model provider.

---

# 35. Intentional Project Cuts

The project intentionally favors depth over breadth.

The goal is to demonstrate one bounded workflow with strong execution guarantees rather than a shallow implementation of many features.

---

## 35.1 Included

The current project demonstrates:

* model-guided discovery
* deterministic replay
* capability validation
* typed inputs
* typed outcomes
* bounded control flow
* semantic checkpoints
* policy authorization
* target ambiguity handling
* evidence recording
* provenance
* replay limits
* multi-tenant bindings
* human handoff
* same-session ownership transfer
* redaction
* browser execution

---

## 35.2 Not Included

The implementation does not currently include:

* write transactions
* capability catalog API
* distributed worker system
* desktop automation
* automatic cross-tenant override generation
* full co-browsing console

These are intentional cuts, not hidden claims of completeness.

---

# 36. Why a Synthetic Bank Is Used

The banking application and local operator interface are synthetic.

This allows the project to exercise important behaviors such as:

* frames
* pagination
* exceptional states
* session expiration
* transaction details
* handoff
* session ownership

without requiring:

* real customer credentials
* real banking systems
* real financial data

The fixture therefore exists to test execution behavior, not to simulate a production banking integration in full.

---

# 37. Current End-to-End Status

There are currently two important paths to distinguish.

## 37.1 Dependable Demo Path

The dependable presentation path is:

```text
Explicitly authored capability
        ↓
Validation
        ↓
Deterministic replay
```

This path is working and is the capability used by the replay demonstration.

---

## 37.2 Model-Guided Discovery Path

A genuine model-guided discovery run successfully:

* navigated the live browser UI
* traversed the payment workflow
* reached payment-detail confirmation
* generated a structured discovery recording

This demonstrates that the discovery system can navigate the workflow.

Those historical runs did **not** produce a usable capability. They remain diagnostic records, not
submission evidence.

---

# 38. Current Discovery Boundary

After the model reaches the verified transaction-detail finish gate:

1. The model does not generate a capability candidate.
2. Trusted code qualifies the typed recording and deterministically compiles the capability.
3. Compiler-added branches remain generalized and validation-bound.
4. A fresh discovery, validation, replay, and same-session handoff are still required for
   submission evidence.

Therefore:

```text
Model-guided discovery recording
            ↓
Deterministic recording compiler
            ↓
Unvalidated capability
```

The capability used by the dependable replay demo is therefore **not the artifact emitted from that discovery run**.

The project intentionally does not present those two artifacts as if they formed one completed discovery-to-replay chain.

---

# 39. What Is Proven Today

The current implementation demonstrates the following pieces independently:

### Discovery

A provider-guided agent can navigate the realistic banking fixture and record the workflow.

### Capability Model

A payment workflow can be represented as a validated, bounded artifact.

### Replay

A validated artifact can execute with different invocation data without consulting a model.

### Safety

Replay can enforce policy, cardinality, checkpoints, limits, and redaction.

### Human Handoff

Automation can transfer ownership of the same browser session to a human and verify state before resuming.

### Evidence

Execution can be associated with artifact and tenant-binding digests and recorded as an ordered evidence stream.

---

# 40. What Is Not Yet Proven End-to-End

The remaining important gap is a single continuous chain:

```text
Provider-guided discovery
        ↓
Capability candidate emitted
        ↓
That exact candidate validated
        ↓
That exact candidate replayed
        ↓
Different invocation data
        ↓
Complete linked evidence bundle
```

That is the next milestone.

---

# 41. Next Steps

## Priority 1 — Complete One True Discovery → Replay Chain

Run a provider-backed discovery that successfully compiles a valid capability from its recording.

Then:

1. Preserve that exact compiled capability.
2. Validate it.
3. Replay that same artifact.
4. Use invocation values different from discovery.
5. Preserve the linked evidence chain.

The important constraint is:

```text
discovered artifact
    ==
validated artifact
    ==
replayed artifact
```

The artifact should not be replaced manually between those stages.

---

## Priority 2 — Complete Evidence Coverage

The final evidence bundle should include runs covering:

* successful posted payment
* `NOT_FOUND`
* checkpoint failure
* genuine same-session human handoff

The evidence should consistently reference the same relevant:

* artifact digest
* tenant-binding digest

The final repository evidence should then pass:

```bash
evidence-check
```

---

## Priority 3 — Make Candidate Synthesis Resumable

Currently, successful browser discovery and capability synthesis are too tightly coupled from an operational perspective.

A provider failure after navigation may force unnecessary repetition of an already-successful browser traversal.

The desired architecture is:

```text
Browser discovery
      ↓
Structured recording persisted
      ↓
Candidate synthesis
      ↓
provider failure?
   /        \
 no          yes
 │            │
continue    retry synthesis
             │
             └── no browser re-traversal required
```

This would make the discovery recording a durable intermediate artifact.

Provider failures during candidate generation could then be retried independently.

---

# 42. Later Work

After the core evidence chain is complete, logical extensions include:

### Desktop Surface

Implement a desktop `Surface` while preserving the same replay and capability contracts.

### Versioned Tenant Overlays

Support reviewed tenant-specific configuration for multiple vendor/application versions.

### Encrypted Evidence Storage

Move evidence into production-appropriate encrypted storage with managed retention.

### Capability Catalog

Expose validated capabilities through an agent-facing catalog once their provenance and execution contracts are dependable.

### Larger Deployment Architecture

Separate the current single-process components only when deployment scale requires it.

The existing interfaces between:

* provider
* surface
* policy
* replay
* evidence
* session ownership

are intended to provide those future service boundaries.

---

# 43. Key Design Invariants

For reviewers, the main invariants of the current design are:

```text
1. Replay never requires a model.

2. Every executable capability path has a typed result.

3. UI targets must satisfy declared cardinality.

4. Unknown or ambiguous states fail closed.

5. NOT_FOUND requires complete declared search exhaustion.

6. A list-row match is insufficient to prove payment identity.

7. Invocation-specific sensitive values stay symbolic in reusable artifacts.

8. Human handoff transfers exclusive ownership of the same session.

9. Automation must re-verify state before resuming after human control.

10. Artifact and tenant configuration are independently versioned and digested.

11. Unsupported application versions stop execution rather than silently reusing stale behavior.

12. Evidence persistence is allowlisted and redacted.

13. The current capability is read-only by construction.
```

These invariants are the core of CaseTrace's execution model.

The project is intentionally not trying to solve general-purpose autonomous browser automation. It is testing whether model-assisted discovery can produce **bounded, reviewable, evidence-backed capabilities that can later execute without model judgment**.
