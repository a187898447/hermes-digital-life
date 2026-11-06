## ADDED Requirements

### Requirement: Scheduled reflection and dreams
The plugin SHALL support configurable scheduled reflection/dream work that can settle or consolidate life state and persist bounded memory results without requiring a user message at that moment.

#### Scenario: Scheduled dream runs
- **WHEN** an operator enables a dream schedule and its configured trigger fires
- **THEN** the plugin performs an isolated reflection turn, applies validated domain updates, and persists bounded results in the active profile

#### Scenario: Dream schedule is disabled
- **WHEN** no dream schedule is configured or the operator disables it
- **THEN** no autonomous dream turn is launched

#### Scenario: Dream work fails
- **WHEN** the reflection model call or persistence operation fails
- **THEN** the failure is reported operationally and no partial or malformed state is committed

### Requirement: Safeguarded proactive delivery
The plugin SHALL make proactive message delivery optional and operator-configurable, with bounded cadence, quiet-hour handling, explicit destination configuration, and duplicate/busy safeguards.

#### Scenario: Proactive delivery is configured
- **WHEN** proactive delivery is enabled and a valid configured destination is available
- **THEN** an eligible wake may be delivered subject to rate limits, quiet hours, emergency stop, and current-session busy checks

#### Scenario: No destination is configured
- **WHEN** proactive delivery is enabled without a valid destination or compatible delivery adapter
- **THEN** no message is sent and the plugin reports that delivery is unavailable

#### Scenario: Wake is stale, duplicated, or session is busy
- **WHEN** a queued wake is stale, duplicates a recent wake, or targets a running session
- **THEN** the wake is discarded without queuing a later surprise delivery

#### Scenario: Emergency stop is active
- **WHEN** emergency stop is active at delivery time
- **THEN** the plugin sends no proactive message

### Requirement: Explicit gateway-injection capability
The plugin SHALL request and use Hermes gateway message injection only when the operator has explicitly enabled the required plugin capability.

#### Scenario: Injection capability is granted
- **WHEN** the plugin attempts an eligible gateway wake with the capability granted
- **THEN** it submits the wake through the documented Hermes injection API and records whether the host accepted it

#### Scenario: Injection capability is absent
- **WHEN** the plugin attempts to wake a gateway session without the capability
- **THEN** the attempt fails closed and no session turn is created
