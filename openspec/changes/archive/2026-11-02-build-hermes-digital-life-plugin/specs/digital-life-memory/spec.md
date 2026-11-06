## ADDED Requirements

### Requirement: Identity-led memory capture
The plugin SHALL allow the configured model identity and conversation context to determine which durable memories are useful, without requiring a separate fixed user-authored memory taxonomy.

#### Scenario: Model identifies a durable fact
- **WHEN** the configured model determines that conversational information is worth retaining
- **THEN** the plugin stores it through the active profile's supported memory mechanism or its bounded profile-scoped event memory store

#### Scenario: Information is not judged durable
- **WHEN** the configured model does not judge information useful for future continuity
- **THEN** the plugin does not force it into durable memory solely to satisfy a fixed category quota

### Requirement: State-aware, lossless memory retrieval
The plugin SHALL use adopted DLC memory-affinity behavior to rank relevant stored memories while retaining all stored entries; ranking SHALL NOT be implemented by deleting or silently omitting lower-priority memories.

#### Scenario: Retrieval is reordered by current state
- **WHEN** memory retrieval occurs under a non-default chemical state
- **THEN** the plugin applies the configured affinity ranking to retrieval order while preserving every stored entry in the retrieval contract

#### Scenario: Stress changes retrieval emphasis
- **WHEN** the adopted stress threshold is exceeded
- **THEN** the prompt or retrieval context prioritizes crisis-relevant memories and explicitly indicates that other memories remain available

#### Scenario: Low-ranked memory remains retrievable
- **WHEN** a memory receives a low affinity score in the current state
- **THEN** it remains stored and can be retrieved in a later state or broader recall

### Requirement: Memory is profile-scoped
The plugin SHALL keep its memory records and references scoped to the active Hermes profile and SHALL NOT bundle an author's private persona or personal memories.

#### Scenario: Same plugin used by separate profiles
- **WHEN** two profiles use the plugin with different identities
- **THEN** each profile's memory operations access only its own profile-scoped records
