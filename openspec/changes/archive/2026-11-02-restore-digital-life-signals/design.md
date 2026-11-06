## Context

The standalone Hermes plugin stores open gaps in its transactional life-state row and already has same-gap reactivation/deduplication. Its `llm_request` middleware reorders rendered memory blocks and appends an affect note to the final user message. The plugin also maintains dopamine, cortisol, serotonin, and energy, but currently does not use these values to set request temperature. The plugin has no model-callable state mutation tool, and this change does not need a persisted schema change.

The requested behavior deliberately distinguishes two provenance classes:

- **A: adopted upstream/PRD semantics.** Bounded unresolved-gap visibility and the structural pipeline for finding a meaningful, non-self-referential, gap-worthy sentence: split, clean/select candidate, filter, then select the longest valid candidate.
- **B: task-defined rules.** Chinese-only keyword sets for uncertainty/inquiry/expository filtering; continuous temperature mapping; and the Hermes request policy that changes temperature only when absent or `None`.

## Goals / Non-Goals

**Goals:**

- Make high-tension unresolved gaps visible in the existing system text without changing user messages or adding messages.
- Capture eligible assistant uncertainty/inquiry using the existing internal gap lifecycle and synonym deduplication.
- Apply a pure affect-to-temperature mapping only when the request leaves temperature unspecified.
- Keep behavior independently testable and preserve both synchronized package copies.

**Non-Goals:**

- No new model-callable state mutation surface, state fields, or schema migration.
- No memory-provider/storage changes, memory deletion/truncation, gateway injection, wake delivery, or new plugin registration.
- No changes to explicitly supplied numeric temperature or other sampling parameters.
- No attempt to port all upstream English-language regular expressions verbatim.

## Decisions

1. **Keep gap prompt construction pure and separate.** Implement `append_open_loops(system_text, gaps, now_ms)` as a pure helper with an explicit reference timestamp. It returns the exact input when no gap is open; otherwise it sorts open gaps by tension, considers at most three, formats elapsed age from `created_at`, appends one bounded suffix, and truncates low-tension entries first to a maximum total suffix length of 400 characters. The middleware first performs existing lossless memory reordering, then applies this helper to each existing system text location using the request's captured timestamp. This isolates the byte-identical empty-gap contract from the middleware's existing memory and user-affect transforms.

2. **Reuse the existing gap transition for assistant-derived gaps.** Put pure sentence recognition in `domain.py` with the requested `is_unknown_claim`, `find_gap_sentence`, and `is_gap_worthy` functions. Follow the upstream pipeline shape: sentence splitting, Markdown/table candidate extraction, meaningful-content filtering, self-reference and expository rejection, and longest eligible sentence selection capped at 100 characters. Explicitly annotate that the Chinese keyword sets are a task-defined B-class adaptation, not a verbatim upstream regex port. In the post-LLM hook, call the existing `_open_or_reactivate_gap` through a runtime transition with source `assistant_unknown_claim`; wrap extraction and transition in a broad exception guard so reply processing cannot fail. Slash commands and empty/non-meaningful replies are ignored.

3. **Make temperature mapping a pure domain policy.** Add `pick_temperature(chemistry, energy)` implementing the specified ordering and arithmetic exactly: cortisol >= 80 returns 1.0; otherwise dopamine >= 70 returns 0.85; otherwise start at 0.70, add the normalized cortisol, low-serotonin, and high-dopamine contributions, subtract 0.15 below energy 20, then clamp to [0.30, 1.10]. Threshold precedence borrows only the cortisol-before-dopamine ordering from upstream; the continuous formula is new B-class behavior.

4. **Respect caller-owned request settings.** Middleware derives a snapshot from the existing runtime state and inserts the selected temperature only if the request has no `temperature` key or its value is `None`. Any explicit numeric value remains unchanged. Do not change `top_p`, `max_tokens`, or other provider settings. No manifest capability changes are expected because registrations remain unchanged.

5. **Keep persistence and emergency-stop policy centralized.** Gap creation must use `storage.update_state` and the established emergency-stop guard, never direct writes. When emergency stop is active, assistant-derived gap processing performs no state write. Temperature and prompt rendering may read state, subject to existing settlement semantics.

6. **Synchronize after implementation, not during design.** Implementation and tests run in the Git plugin copy; the same changed package files are then copied to the root distribution and compared by SHA-256. The root copy is not committed.

## Risks / Trade-offs

- [Prompt suffix truncation could create a partial final item] -> Truncate by dropping lower-tension items first; test the 400-character bound and retained ordering.
- [Chinese pattern matching could capture explanatory or self-referential text] -> Keep independent filters and focused negative tests for mechanism and expository phrases; do not use whole-reply fallback.
- [Provider/model temperature behavior differs] -> Only fill an omitted value and preserve explicit caller choices; no broader compatibility claims are introduced.
- [Middleware request shapes contain system content in multiple supported forms] -> Apply the helper only to existing system/developer system content paths and test supported shapes without modifying user content.
- [State-write failures must not break a completed assistant response] -> Enclose the whole recognition-and-transition path in a silent exception guard.

## Migration Plan

No persistent schema migration is needed. Deploy the Python changes with the existing schema and registration declarations, run focused unittest modules and Hermes plugin validation, exercise middleware with representative fake requests, synchronize matching package files to the root copy, verify hashes, then commit only the implementation repository changes.

## Open Questions

None. The detailed behavior and acceptance cases are specified in the accompanying capability specs.
