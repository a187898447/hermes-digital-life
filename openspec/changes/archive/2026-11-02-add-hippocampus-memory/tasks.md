## 1. Storage and Replay

- [x] 1.1 Add `digital_life/memory.py` with per-day JSONL event log (`add`/`touch`/`forget`), `flock` writer, id allocation by scanning the day file, and `O_APPEND` line writes with fsync.
- [x] 1.2 Fold events by date-file order then line order; touch carries absolute hits/access values and optional updated weight. Reject invalid references/duplicate add IDs; preserve truncated tails before repairing under the writer lock.
- [x] 1.3 Implement transactional rebuild and pre-operation index synchronization from authoritative daily event logs only; never replay cold/recovery files. Handle persisted-but-unindexed events before deduplication, quota checks, or recall.
- [x] 1.4 Use plugin_db("hermes-lover", filename="memory-index.db") for normal index connections; verify FTS5 availability and use the same in-process/cross-process lock order for all writers and rebuild.

## 2. Retrieval

- [x] 2.1 Build the FTS5 `grams` column with CJK adjacent-bigram plus lowercase ASCII tokens; index entries and keep raw `text` for display.
- [x] 2.2 Quote and escape generated FTS tokens without deleting operator words from input; parameterize MATCH and escape LIKE wildcards for single-character/non-tokenizable literal fallback.
- [x] 2.3 Implement bounded negative-BM25 relevance, weight-multiplied effective decay, upstream reciprocal recency and nonempty-query coefficients; use the B-class empty-query formula and deterministic tie ordering.
- [x] 2.4 Implement `include_cold` filtering and `total`/`matched`/`returned` accounting.

## 3. Tools and Safety

- [x] 3.1 Register only memory_write and memory_recall and declare them in plugin.yaml. Validate blank/4000-character text, kinds, default/clamped finite weights, k, and include_cold.
- [x] 3.2 Enforce replayable session/day add quota (20), host-derived source_session_id, plugin-scoped memory.writes_enabled configuration, and same-day duplicate touch with capped +0.2 weight.
- [x] 3.3 Read life-state without lazy settlement/initialization writes; emergency stop forbids memory writes and recall touches/repair, including missing/corrupt index read-only fallback.
- [x] 3.4 Confirm the tools are model-callable without granting model authority to mutate chemistry, gaps, or emergency stop.

## 4. Tests

- [x] 4.1 Add `tests/test_memory.py`: event fold, torn-line tolerance, replay equivalence (build → delete → rebuild), id uniqueness under concurrent writers.
- [x] 4.2 Cover Chinese retrieval (`熬夜`, `群里`), three-character and full-sentence queries, single-character fallback, mixed CJK/ASCII, and inputs containing quotes and `MATCH` operators.
- [x] 4.3 Cover scoring branches (rel>0 vs rel==0), attention bias, decay/recency monotonicity, cold filtering, quota enforcement, duplicate `touch`, and emergency-stop no-write via file mtime and event line count.
- [x] 4.4 Confirm current baseline, then run the four unittest modules and hermes plugins validate; run temporary-profile write/recall/rebuild/no-state-mutation checks. P0 adds no middleware injection.
- [x] 4.5 Cover BM25 negative/near-minus-one bounds, weight-dependent rank, persistent duplicate weighting, tail repair then append, index failure recovery/rollback, cold-view exclusion, cross-process ID/quota consistency, and stop-mode missing/corrupt index zero-write behavior.

## 5. Documentation and Delivery

- [x] 5.1 Document the two-layer memory model, per-day event files, rebuild command, known 0.22.5 write-context limitation, and P1 injection plan in README plus `使用引导.md`.
- [x] 5.2 Add a `rebuild_index()` operator entry point (function or CLI subcommand) and mention it in troubleshooting.
- [x] 5.3 Synchronize changed files to the root copy and verify SHA-256 equality per file.
- [x] 5.4 Commit in `hermes-digital-life` with a conventional English message and record test/validation output plus commit hash.
