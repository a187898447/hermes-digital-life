# affect-temperature Specification

## Purpose
TBD - created by archiving change restore-digital-life-signals. Update Purpose after archive.
## Requirements
### Requirement: Chemistry and energy determine an omitted request temperature
The plugin SHALL provide a pure temperature policy using chemistry and energy. This policy is task-defined B-class behavior, not an implementation of upstream `pickTemperature`; it borrows only the cortisol-before-dopamine threshold precedence. The policy SHALL start at 0.70; return 1.00 when cortisol is at least 80; otherwise return 0.85 when dopamine is at least 70; otherwise add `0.20 * clamp((cortisol - 40) / 40, 0, 1)`, `0.10 * clamp((70 - serotonin) / 35, 0, 1)`, and `0.10 * clamp((dopamine - 50) / 20, 0, 1)`, subtract 0.15 when energy is below 20, then clamp the result to [0.30, 1.10].

#### Scenario: Cortisol threshold takes precedence
- **WHEN** cortisol is at least 80, regardless of dopamine, serotonin, or energy
- **THEN** the policy returns 1.00

#### Scenario: Dopamine threshold applies below the cortisol threshold
- **WHEN** cortisol is below 80 and dopamine is at least 70
- **THEN** the policy returns 0.85

#### Scenario: Continuous mapping accounts for chemistry and low energy
- **WHEN** neither threshold branch applies
- **THEN** the policy applies the specified cortisol, serotonin, dopamine, and low-energy contributions

#### Scenario: Result stays within policy bounds
- **WHEN** input values produce a result outside the supported interval
- **THEN** the policy clamps the output to [0.30, 1.10]

### Requirement: Preserve explicit request temperature
The middleware SHALL populate temperature only when the request has no `temperature` field or its value is `None`. It SHALL preserve every explicit numeric temperature and SHALL NOT modify `top_p`, `max_tokens`, or other sampling settings as part of this behavior.

#### Scenario: Missing temperature is populated
- **WHEN** a supported provider request omits temperature
- **THEN** the middleware adds the pure policy's result to that request

#### Scenario: Null temperature is populated
- **WHEN** a supported provider request has `temperature` set to `None`
- **THEN** the middleware adds the pure policy's result

#### Scenario: Explicit temperature is preserved
- **WHEN** a supported provider request contains any numeric temperature, including 0.3
- **THEN** the middleware returns the same explicit value and leaves other sampling fields unchanged

