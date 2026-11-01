# digital-life Hermes plugin

This is a generic, independently installable Hermes standalone plugin. It does not include a persona, provider/model choice, channel, credentials, or recipient. Operators supply those in their own Hermes profile.

Install the repository into the target Hermes home as `plugins/digital-life/`, then enable it with `hermes plugins enable digital-life`. It is opt-in. Requires Hermes `>=0.22` and a Python runtime with the host's plugin APIs.

The manifest declares these optional settings under `plugins.entries.digital-life.settings`:

```yaml
plugins:
  enabled: [digital-life]
  entries:
    digital-life:
      settings:
        dream_enabled: false
        dream_cron: "0 3 * * *"
        proactive_enabled: false
        quiet_hours: [23, 8]
        daily_message_limit: 2
        delivery_platform: ""
        delivery_chat_id: ""
```

Schedules and delivery remain disabled by default. These settings are reserved until those workflows are implemented; they do not start jobs or send messages in this release.

Uninstall with `hermes plugins disable digital-life` followed by `hermes plugins remove digital-life`. Plugin removal leaves profile data under `plugin-data/digital-life/`; handle that directory separately if deleting the saved state is desired.

For upgrades, back up the target profile's `plugin-data/digital-life/` directory before replacing plugin code. State is schema-versioned; this release performs bounded migrations when reading older state. Hermes profiles keep plugin data separate at runtime but are not an OS filesystem security boundary. Do not grant gateway injection for this release because no proactive delivery workflow uses it.

## Current package scope

- Three DLC core chemicals with lazy time settlement.
- DLC sleep-energy recovery and bounded temperament.
- Profile-scoped SQLite life state using Hermes `plugin_db()`.
- Read-only `digital_life_status` tool and per-provider-request affect middleware.
- Lossless memory affinity ranking helpers.
- Operator CLI: `hermes digital-life status|stop|resume|resolve-gap <id>`.

No model-callable tool mutates life state or accepts user-supplied hormone/energy values. Fixed plugin rules respond only to host-observed provider requests, completed turns, transient errors, search results, and elapsed time. Operator stop/resume and gap closure require typing an explicit confirmation. No cron job is installed automatically; the optional “Quiet digital-life reflection” blueprint creates a local-only scheduled turn when an operator chooses it. Proactive delivery and gateway injection remain disabled: Hermes deliberately does not expose `send_message` as a model tool, and automated delivery needs a persisted wake claim/recipient policy that this release does not yet implement.

Hermes's built-in `memory` tool remains responsible for memory writes. Per-request middleware reorders the exact rendered `MEMORY.md`/`USER.md` blocks by DLC affinity and preserves every entry; it does not replace or own the memory store. The reflection blueprint may write identity-judged memory through Hermes's built-in tool; it cannot mutate life-state values. Proactive wakes are not implemented.

## Validate

From this repository root:

```sh
python3 -m unittest discover -s tests
```

The tests do not require Hermes or an API key. The live plugin contract can be checked with `hermes plugins doctor . --ci` in a compatible Hermes installation.

## Upstream attribution

Selected domain rules are derived from `digital-life-core` by mashangshui (温水) and 鱼小水, upstream revision `d984665ff2e452cf9f57d845cfb93bfb9a64b6b1`. The upstream README welcomes MIT forks and its package metadata declares MIT. See `LICENSE` for attribution and the notice shipped with this project.
