# hippocampus-memory Specification

## Purpose
TBD - created by archiving change add-hippocampus-memory. Update Purpose after archive.
## Requirements
### Requirement: Independent profile event memory
The plugin SHALL store event memories independently of Hermes MEMORY.md and USER.md in the current profile. It SHALL NOT register a MemoryProvider, modify life-state values, replace the built-in memory tool, or add middleware memory injection in P0. P0 SHALL expose only two new model tools: memory_write and memory_recall.

#### Scenario: Event memory is written
- **WHEN** a model explicitly calls memory_write
- **THEN** the plugin writes to its profile event store without modifying built-in memories, chemistry, energy, gaps, or emergency-stop state

#### Scenario: Profile changes
- **WHEN** calls run under different profiles
- **THEN** their event files, indexes, and quotas remain scoped to their respective profile data directories

### Requirement: Replayable daily event source
Daily UTF-8 JSONL files SHALL be the sole authoritative source. Events SHALL be folded by file date and line order. Add events SHALL contain initial text, kind, weight, cortisol, creation/access times, hits and host-derived source_session_id. Touch events SHALL contain absolute updated access metadata and optional updated weight. Forget events SHALL mark an existing entry archived. Derived cold files SHALL NOT be replayed.

#### Scenario: Index is lost
- **WHEN** an operator rebuilds a deleted index
- **THEN** entry content, weights, hits, access times, archive flags and source-based quota counts are restored from daily events alone

#### Scenario: Cold materialization is stale
- **WHEN** a cold view contains values older than the authoritative daily events
- **THEN** rebuilding ignores the view and retains the authoritative event result

#### Scenario: Invalid event is encountered
- **WHEN** replay encounters malformed JSON, a duplicate add ID or a reference without a prior add
- **THEN** it reports a warning and skips the invalid event without overwriting valid state or discarding unrelated entries

### Requirement: Serialized durable writes and recovery
Writers and rebuild SHALL share an in-process mutex and cross-process file-lock protocol. Event bytes SHALL be fully written and fsynced before updating the index transaction. Allocation, deduplication, quota checks and touch updates SHALL use synchronized state under the lock. Rebuild SHALL replace derived tables transactionally and roll back on failure.

#### Scenario: Multiple writers allocate IDs
- **WHEN** threads or processes concurrently add memories on the same local date
- **THEN** IDs remain unique and increasing and session quotas are enforced without lost updates

#### Scenario: A truncated last line precedes another write
- **WHEN** a writer finds a partial JSON tail
- **THEN** it preserves the damaged tail outside replay inputs and repairs the file boundary under the lock before appending a new event that remains replayable

#### Scenario: Complete final event lacks a newline
- **WHEN** a writer finds a valid complete JSON final event with no newline
- **THEN** it adds the missing newline before appending without losing that event

#### Scenario: Index update fails after event persistence
- **WHEN** an event has been fsynced but its index transaction fails
- **THEN** the event remains authoritative, the write result identifies the persisted entry with indexed=false, and a later normal operation synchronizes before deduplication, quota checks or retrieval

### Requirement: Validated bounded event writes
memory_write SHALL reject empty/blank text and normalized text over 4000 Unicode characters, preserve internal whitespace/newlines, and accept only crisis/daily/fact/decision/note kinds. Kind defaults SHALL be 2.0/1.0/1.2/1.5/0.8 respectively. Explicit weight SHALL be a finite non-boolean number clamped to [0.2,3.0]. Same-local-day normalized text equality SHALL reuse the existing ID, increase weight by 0.2 up to 3.0 and refresh access time without increasing hits. The updated weight SHALL be persisted as a touch event.

#### Scenario: Repeated text is written
- **WHEN** the same normalized text is written twice on the same local date
- **THEN** only one add exists and the repeated write's weight increase survives index deletion and replay

#### Scenario: Invalid text or weight is supplied
- **WHEN** text is blank or over 4000 characters, kind is unsupported, or weight is boolean or non-finite
- **THEN** the tool returns an explanatory rejection without persisting an event

### Requirement: Auditable write controls
The plugin SHALL enforce at most 20 new add events per host-supplied session_id per local date, using unknown when absent. Duplicate touches SHALL NOT consume add quota. Plugin-scoped memory.writes_enabled SHALL default to true; false SHALL reject all memory_write calls including duplicate reinforcement but SHALL NOT disable normal recall touches. The plugin SHALL document that session auditing and quotas do not provide primary/subagent/cron isolation.

#### Scenario: Daily quota is exhausted
- **WHEN** a session requests its twenty-first new add on the same local date
- **THEN** the plugin refuses the add and returns an explanatory error, including after process restart or index rebuild

#### Scenario: Writes are disabled
- **WHEN** memory.writes_enabled is false
- **THEN** new and duplicate memory_write operations are rejected while recall remains available

### Requirement: Safe Chinese token retrieval
The plugin SHALL index CJK adjacent bigrams and lowercase ASCII tokens through FTS5. Generated query tokens SHALL be quoted and escaped as literals, combined with AND, and passed by SQL parameters; input operator words SHALL NOT be removed to avoid FTS syntax. Single-character and non-tokenizable nonempty queries SHALL use parameterized literal substring matching with escaped LIKE wildcards. FTS5 availability SHALL be checked and unavailable builds SHALL return a clear error.

#### Scenario: Two-character Chinese query matches
- **WHEN** a memory contains 今天下午在群里聊到熬夜 and the query is 熬夜 or 群里
- **THEN** the matching memory is included among retrieval candidates

#### Scenario: Query includes syntax characters
- **WHEN** a query includes quotes, OR, asterisks, percent signs or underscores
- **THEN** these are processed by the documented token/literal rules and never executed as user-supplied FTS operators or LIKE wildcards

#### Scenario: Nonempty punctuation query has no tokens
- **WHEN** a nonempty query cannot generate index tokens
- **THEN** literal substring fallback is used rather than returning the empty-query result set

### Requirement: Weight-aware bounded ranking
Effective weight SHALL equal weight multiplied by 0.5 raised to nonnegative idle_days/7. Recency SHALL equal 1/(1+nonnegative age_days/30). FTS relevance SHALL equal strength/(1+strength), where strength=max(0,-bm25); literal fallback matches SHALL use relevance=1. Nonempty query scoring SHALL use coefficients 0.40 relevance, 0.20 min(1.5,effective_weight), 0.15 recency and 0.25 relevance times attention multiplier. Empty query scoring SHALL use 0.55 min(1.5,effective_weight), 0.25 recency and 0.20 attention_bias. Attention bias SHALL be clamp((multiplier-0.1)/2.9,0,1). Crisis/daily multipliers SHALL be 3.0/0.1 at cortisol>=80, 1.8/0.4 at cortisol>=55, otherwise 1/1; other kinds SHALL use 1. Ties SHALL use descending creation time then ascending ID.

#### Scenario: SQLite BM25 is negative
- **WHEN** BM25 is negative, including near -1 or with larger negative magnitude
- **THEN** relevance is finite and in [0,1), increasing monotonically with matching strength

#### Scenario: Base weights differ
- **WHEN** otherwise equal entries have different base weights below the effective-weight score cap
- **THEN** the higher effective weight receives a larger weight contribution

### Requirement: Recall accounting and access updates
memory_recall SHALL accept query string or null, integer k defaulting to 8 and clamped to [1,20], and boolean include_cold defaulting to false. It SHALL return total unarchived count, cold_total, matched count before truncation, returned count, attention and shown entries. include_cold SHALL include archived candidates without unarchiving them. Normal recall SHALL persist hits+1 and the current access time only for shown entries; score SHALL use pre-access state and returned access metadata SHALL reflect committed touches.

#### Scenario: Search matches fewer entries than the store contains
- **WHEN** a query matches a subset of the store
- **THEN** total remains the unarchived inventory count while matched and returned describe the query and k limit

#### Scenario: Archived memory is recalled
- **WHEN** include_cold is true and an archived memory matches
- **THEN** it can appear in shown without its archived flag being cleared

### Requirement: Emergency-stop read-only recall
Memory operations SHALL read life-state without settling or initializing it on disk. During emergency stop, memory_write SHALL be rejected and recall SHALL return results without creating or changing event files, directories, lock files, SQLite files or access metadata. Missing, corrupt or stale indexes SHALL be handled by read-only event replay and in-memory retrieval rather than disk repair.

#### Scenario: Emergency stop with a usable index
- **WHEN** recall runs while emergency stop is active
- **THEN** it returns matching memories without touches, index repair, archive operations or life-state mutation

#### Scenario: Emergency stop with no usable index
- **WHEN** emergency stop is active and the index is missing, corrupt or stale
- **THEN** recall derives results from authoritative files without persisting a replacement index or any other file

### Requirement: P0 archive boundary
P0 SHALL support replay and filtering of forget events but SHALL NOT generate archive events, expose forget/compact tools or CLI actions, materialize cold files, or inject top-two memories into middleware. Future archive eligibility SHALL use effective_weight<0.08 and idle_days>=30 at the time of checking, not an unrecorded continuous-below-threshold duration.

#### Scenario: A memory fades during P0
- **WHEN** an unarchived memory meets the future archive thresholds through elapsed time alone
- **THEN** its score decreases but it remains unarchived unless a forget event is present in the authoritative history

