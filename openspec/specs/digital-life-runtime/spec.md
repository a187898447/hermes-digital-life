# digital-life-runtime Specification

## Purpose
TBD - created by archiving change build-hermes-digital-life-plugin. Update Purpose after archive.
## Requirements
### Requirement: Installable Hermes plugin
The project SHALL provide a generic Hermes standalone plugin that can be installed independently of Hermes core and SHALL register through the supported general-plugin lifecycle.

#### Scenario: Plugin is discovered and enabled
- **WHEN** an operator installs and enables the plugin in a supported Hermes profile
- **THEN** Hermes discovers the plugin and its declared tools/hooks load through the documented plugin entry point

#### Scenario: Plugin is disabled or fails to load
- **WHEN** the plugin is disabled or its registration fails
- **THEN** Hermes continues operating without the plugin and the failure is visible through plugin diagnostics

### Requirement: Profile-scoped durable life state
The plugin SHALL persist its versioned life state within the active Hermes profile and SHALL restore it after a new session or process restart.

#### Scenario: State survives a session boundary
- **WHEN** a session updates life state and a later session starts in the same profile
- **THEN** the later session reads the persisted state after applying elapsed-time settlement

#### Scenario: Separate profiles use separate state
- **WHEN** the plugin is loaded in two different Hermes profiles
- **THEN** each profile reads and writes only its own plugin state namespace

### Requirement: DLC-aligned chemical and energy semantics
The plugin SHALL implement the adopted `digital-life-core` domain semantics without adding undeclared core chemicals or replacing its energy-rest rules.

#### Scenario: Default core chemicals are initialized
- **WHEN** the plugin initializes a new life state
- **THEN** it initializes dopamine, cortisol, and serotonin according to the adopted genome defaults and does not introduce oxytocin as a core chemical

#### Scenario: Elapsed time is settled lazily
- **WHEN** life state is read after an elapsed interval
- **THEN** chemical decay and rest-energy settlement are calculated from elapsed time using the adopted DLC rules without requiring a continuously running timer

#### Scenario: Frozen state is settled
- **WHEN** emergency stop is active and a state read or scheduled event occurs
- **THEN** state-changing settlement and wake delivery remain frozen until an explicit operator resume action

### Requirement: No model-callable life-state mutation
The plugin SHALL NOT register any model-callable tool or event interface that changes hormone, energy, gap, emergency-stop, or wake state. State changes SHALL originate only from validated internal lifecycle or scheduled workflows and operator controls; status inspection SHALL be read-only.

#### Scenario: Model inspects state
- **WHEN** the model requests life-state status through a plugin capability
- **THEN** it receives a bounded read-only snapshot and cannot mutate stored state through that capability

#### Scenario: Model attempts a state mutation
- **WHEN** the model attempts to change raw hormone, energy, gap, or emergency-stop values
- **THEN** no model-callable mutation interface exists and persisted state remains unchanged

#### Scenario: Operator changes emergency-stop state
- **WHEN** an authorized operator invokes a supported operator-only control
- **THEN** emergency stop or resume is applied without registering an LLM-callable mutation tool

### Requirement: Configurable generic integration
The plugin SHALL NOT hard-code a persona, model provider, QQ identity, channel identifier, credential, or installation-specific schedule.

#### Scenario: Plugin is installed in a new environment
- **WHEN** an operator installs the plugin without copying the original PRD author's local configuration
- **THEN** the plugin loads as a generic capability and obtains instance-specific behavior only from the target profile's own configuration

### Requirement: Serialized chemistry with genome-declared bounds
The plugin SHALL serialize the chemistry domain into pure `chemistry.py`, a tolerant `genome.py`, and a `ChemicalPool` whose decay, event rules, and bounds come from the genome document rather than hard-coded branches.

#### Scenario: Genome is malformed or partial
- **WHEN** the genome document is invalid JSON or omits a field
- **THEN** the plugin restores the core chemical defaults field by field, keeps custom values that remain valid, and never raises

#### Scenario: Event rules come from the genome
- **WHEN** a declared event such as `request` or `turn_complete` is applied
- **THEN** the pool applies the genome's per-chemical deltas with sensitivity/metabolism and the genome's min/max bounds, and unknown events are no-ops

### Requirement: Gap pool with epiphany closure and synonym revival
The plugin SHALL keep unresolved loops in a bounded pool whose tension grows with idle time, close them on the first successful tool result that answers them, and revive a resolved synonym in place instead of duplicating it.

#### Scenario: A tool answers an open loop
- **WHEN** an internal tool succeeds and its query or output answers a loop
- **THEN** the loop is resolved with its tension cleared and `closed_by` naming the tool, and the close also retires any queued wake for that loop

#### Scenario: A similar loop reappears
- **WHEN** the same or a synonymous loop is reopened after being resolved
- **THEN** the resolved entry is revived with its original id and a higher revival count rather than a duplicate entry

### Requirement: Stress-aware recall and anchored memory
The plugin SHALL reorder, never filter, retrieved memories by cortisol-dependent attention weights, prepend a bounded stress directive when cortisol is high, and boost entries whose anchors appear in the current query.

#### Scenario: High stress reorders memory
- **WHEN** cortisol is at or above the stress threshold
- **THEN** crisis-tagged entries are weighted up, daily entries weighted down, all entries are still delivered, and the system prompt states that no memory was lost

#### Scenario: Query anchors match a stored entry
- **WHEN** a captured entry carries anchors that occur in the current query
- **THEN** that entry scores higher than it would without the anchor match

### Requirement: Host vitals in the affect section
The plugin SHALL append a bounded host-vitals line (CPU, load, memory, disk) to the affect section when the host exposes that data, and omit the line silently when it does not.

#### Scenario: psutil is unavailable
- **WHEN** the host lacks psutil
- **THEN** the line still renders the available facts, and if nothing is available the line is omitted without raising

### Requirement: Opt-in self-wake with delivery locks
The plugin SHALL queue at most a self-wake only when explicitly enabled, deliver the bubble body `...` through the host's gateway injection, keep the wake's details in a dedicated system prompt block, and drop every queued or delivered wake whose loop is closed.

#### Scenario: Wake is disabled or unsupported
- **WHEN** the wake config is off or the host exposes no gateway injection
- **THEN** no wake is queued, no message is sent, and the plugin keeps working

#### Scenario: Wake delivery is re-verified before sending
- **WHEN** a wake is due
- **THEN** the plugin re-reads durable state by wake id, sends nothing if the loop was resolved or the agent is running, and deletes the queue entry when delivery is refused

### Requirement: Optional billion-context integration
The plugin SHALL detect an installed billion-context without installing, invoking, or depending on it, and SHALL degrade silently when it is absent.

#### Scenario: Detection reports the install state
- **WHEN** the detection script runs
- **THEN** it prints `{"installed": bool, "version": str|null}` and exits 0 whether or not billion-context is present
