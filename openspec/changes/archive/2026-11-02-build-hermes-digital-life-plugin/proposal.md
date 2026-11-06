## Why

Hermes has durable profiles and an extensible plugin API, while `digital-life-core` provides a tested domain model for persistent life-state behavior. A reusable, independently installable plugin can bring that model to Hermes without tying it to one machine, QQ account, profile, or private persona.

## What Changes

- Create an open-source, independently installable Hermes standalone plugin that provides a digital-life core: persistent chemical/energy state, time-based settlement, memory-affinity behavior, unresolved gaps, and dream/wake workflows.
- Preserve `digital-life-core` domain semantics where adopted, including its three core chemicals and sleep-energy rules; document attribution and MIT provenance.
- Keep the plugin generic: installation-specific profile, channel, model, schedule, and persona are configured by each adopter, not embedded in the plugin.
- Let the model decide what is worth remembering based on its configured identity and context; do not impose a separate user-authored memory taxonomy.
- Do not expose an unrestricted tool for the model to directly set hormones or energy. State changes follow plugin rules and validated domain events.
- Use supported Hermes plugin contracts and provide installation, configuration, privacy, and operational documentation.

## Capabilities

### New Capabilities
- `digital-life-runtime`: Profile-scoped durable life state, domain calculations, prompt influence, and validated state transitions.
- `digital-life-memory`: Memory capture/retrieval behavior and state-aware memory affinity without dropping stored memories.
- `digital-life-agency`: Configurable scheduled reflection, dreams, and proactive wake workflows with delivery safeguards.

### Modified Capabilities

None.

## Impact

- New standalone Hermes plugin source and its tests/documentation; target runtime compatibility must be verified against the supported Hermes plugin API.
- Hermes surfaces include `register(ctx)`, `ctx.state.get/set`, supported hooks, tools, prompt integration, cron/profile behavior, and optional gateway injection capability.
- Domain behavior references `digital-life-core` at the upstream MIT-licensed repository; preserve author attribution and license notices for reused or derived material.
- No default-profile configuration, QQ credentials, user identifiers, private SOUL/persona, or local channel migration is part of the distributable plugin.
