## 1. Compatibility and Project Foundation

- [x] 1.1 Pin the `digital-life-core` source revision and audit the exact algorithms/data structures to adopt; record attribution and include the required MIT notice.
- [x] 1.2 Define the supported Hermes version range and verify standalone plugin discovery, `register(ctx)`, tool/hook registration, and out-of-tree installation in a temporary Hermes home.
- [x] 1.3 Verify the `ctx.state` read-modify-write race and validate profile-scoped `plugin_db()` transactional storage using concurrent hook/cron-style writers.
- [x] 1.4 Verify `llm_request` middleware can append volatile affect context to chat-completions `messages` and Responses API `input`, preserving all other request fields.
- [x] 1.5 Create the independent plugin package layout, manifest, license/attribution files, and deterministic test harness without local user credentials or persona data.

## 2. Domain Runtime

- [x] 2.1 Define the versioned, validated profile state schema for three core chemicals, energy, temperament, gaps, commitments, emergency stop, and wake bookkeeping.
- [x] 2.2 Port the adopted pure chemistry, genome normalization, and DLC sleep-energy settlement functions into Python.
- [x] 2.3 Add reference-vector tests comparing Python calculations with the pinned upstream implementation, including elapsed-time, invalid-number, boundary, and frozen-state cases.
- [x] 2.4 Implement lazy settlement and bounded transactional SQLite persistence in the active profile.
- [x] 2.5 Implement internal validated lifecycle transitions and read-only status; provide no model-callable state mutation tool and identify/test an operator-only emergency-stop control surface.
- [x] 2.6 Implement gap lifecycle, tension settlement, same-ID synonym reactivation/family closure, and focused tests from adopted DLC semantics.

## 3. Memory and Prompt Integration

- [x] 3.1 Implement DLC-aligned memory affinity ranking and verify ranking never deletes or silently drops stored entries.
- [x] 3.2 Integrate per-request ranking with Hermes's rendered profile-scoped built-in memory blocks without replacing the active MemoryProvider or taking over memory writes.
- [x] 3.3 Implement state-aware stress emphasis and memory availability wording without embedding a private persona.
- [x] 3.4 Register session-stable prompt context and verified per-request affect middleware; test that middleware never drops or rewrites unrelated request fields.

## 4. Reflection and Proactive Agency

- [x] 4.1 Define validated, opt-in settings for reflection cadence, quiet hours, delivery destination/platform, rate limits, and emergency stop.
- [x] 4.2 Implement a user-configurable local-only reflection blueprint with bounded prompt behavior and memory writes delegated to Hermes's built-in atomic memory tool.
- [ ] 4.3 Implement stale-entry revalidation, duplicate suppression, busy-session refusal, echo cancellation, and operator-configured delivery.
- [ ] 4.4 Gate gateway injection behind explicit operator capability/configuration and test fail-closed behavior when it is absent.
- [ ] 4.5 Add deterministic tests for disabled schedules, delivery unavailable, quota/rate limit, quiet hours, active emergency stop, and model/persistence failures.

## 5. Packaging and Release Verification

- [x] 5.1 Add plugin doctor/validation coverage for the real standalone discovery path and ensure declared capabilities match registrations.
- [x] 5.2 Test installation and removal from an independent checkout in a clean temporary Hermes profile; verify no default-profile or credential mutation.
- [x] 5.3 Document installation, supported Hermes versions, configuration, memory behavior, proactive capability grants, privacy boundaries, limitations, upgrades, and uninstall.
- [x] 5.4 Run the full test suite and a clean-profile smoke test; record actual outputs and any unsupported PRD behavior before declaring the change complete.
