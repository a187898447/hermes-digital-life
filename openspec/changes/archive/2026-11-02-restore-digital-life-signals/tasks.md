## 1. Domain Rules

- [x] 1.1 Add pure open-loop formatting/append behavior with top-three tension ordering, elapsed-age formatting, 400-character bound, and exact passthrough when no gaps are open.
- [x] 1.2 Add pure assistant-response extraction helpers following the upstream pipeline structure, with documented task-defined Chinese keyword sets, meaningful-content filtering, self-reference/expository rejection, and longest-candidate selection capped at 100 characters.
- [x] 1.3 Add pure `pick_temperature(chemistry, energy)` implementing the specified threshold precedence, continuous contributions, low-energy adjustment, and output clamp.

## 2. Runtime and Middleware Integration

- [x] 2.1 Add an internal runtime transition for assistant-derived gaps that uses `storage.update_state`, respects emergency stop, delegates deduplication/reactivation to `_open_or_reactivate_gap`, and records source `assistant_unknown_claim`.
- [x] 2.2 Integrate assistant-response recognition in `post_llm_call` with slash-command/empty-response filters and a silent exception boundary.
- [x] 2.3 Integrate open-loop append after existing memory reordering for supported existing system text shapes, without adding messages or modifying user content.
- [x] 2.4 Populate temperature from current chemistry/energy only when the request omits temperature or supplies `None`; preserve explicit numeric values and all unrelated sampling fields.
- [x] 2.5 Update README Current package scope to describe open-loop prompt context, assistant uncertainty capture, and affect-based temperature behavior.

## 3. Verification and Delivery

- [x] 3.1 Add focused unittest coverage for empty-gap passthrough, top-three formatting/truncation, uncertainty extraction, self-reference/exposition rejection, slash/empty/non-meaningful input, synonym deduplication, and all temperature branches.
- [x] 3.2 Add middleware tests for supported system shapes, memory reorder followed by gap append, user-content preservation, omitted/null temperature insertion, and explicit temperature preservation.
- [x] 3.3 Run the specified unittest modules and `hermes plugins validate .`; verify existing memory losslessness tests remain unchanged and pass.
- [x] 3.4 Run the middleware end-to-end fake-request acceptance checks, including exact expected affect-note transformation and suffix reconstruction through the pure append helper.
- [x] 3.5 Synchronize changed package files to the root copy and verify SHA-256 equality for each synchronized file.
- [x] 3.6 Confirm only intended changes are staged, commit in `hermes-digital-life` with a conventional English message, and record test/validation output plus commit hash.
