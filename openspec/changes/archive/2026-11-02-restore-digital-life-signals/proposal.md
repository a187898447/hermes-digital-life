## Why

The Python plugin already tracks unresolved gaps and affect state, but two useful signals from the upstream design are not visible or captured in normal requests, and affect no longer influences sampling temperature. Restoring these behaviors makes unresolved knowledge boundaries available to the model and lets validated plugin state influence generation without expanding model authority.

## What Changes

- Append the three highest-tension open gaps to the existing system prompt as a bounded suffix after memory reordering, without changing user content or creating a new message.
- Detect eligible assistant statements of uncertainty or cognitive inquiry and open/reactivate a gap through the existing deduplication transition.
- Add a pure, continuously mapped temperature policy and apply it only when the provider request does not specify a temperature.
- Add focused unit and middleware tests, and update the package-scope documentation.
- Preserve the existing memory store ownership, state mutation controls, plugin registrations, and emergency-stop behavior.

## Capabilities

### New Capabilities
- `open-loop-signals`: Surface bounded unresolved gaps in system context and recognize eligible assistant uncertainty statements as gaps.
- `affect-temperature`: Derive request temperature from current chemistry and energy when the request leaves temperature unspecified.

### Modified Capabilities

None.

## Impact

- Affects `digital_life/domain.py`, `digital_life/runtime.py`, `__init__.py`, focused `tests/`, and the README package-scope description in both synchronized plugin copies.
- No new dependency, tool, hook, middleware registration, memory provider, gateway injection, or persisted state field is required.
- The implementation repository is `hermes-digital-life`; the root copy is synchronized without committing there.
