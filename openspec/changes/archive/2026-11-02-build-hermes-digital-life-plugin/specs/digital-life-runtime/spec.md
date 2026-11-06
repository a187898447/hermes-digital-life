## ADDED Requirements

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
