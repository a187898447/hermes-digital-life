# hermes-lover Hermes plugin

This is a generic, independently installable Hermes standalone plugin. It does not include a persona, provider/model choice, channel, credentials, or recipient. Operators supply those in their own Hermes profile.

Install the repository into the target Hermes home as `plugins/hermes-lover/`, then enable it with `hermes plugins enable hermes-lover`. It is opt-in. Requires Hermes `>=0.22` and a Python runtime with the host's plugin APIs.

End-to-end walkthrough (persona → life system → QQ bot), in Chinese: [使用引导.md](使用引导.md).

This release reads no plugin settings: there is no `settings` block to configure. Scheduled reflection is created explicitly through the `dream-reflection` automation blueprint, and delivery is not implemented, so no job starts and no message is sent from configuration.

Uninstall with `hermes plugins disable hermes-lover` followed by `hermes plugins remove hermes-lover`. Plugin removal leaves profile data under `plugin-data/hermes-lover/`; handle that directory separately if deleting the saved state is desired.

For upgrades, back up the target profile's `plugin-data/hermes-lover/` directory before replacing plugin code. State is schema-versioned; this release performs bounded migrations when reading older state. Hermes profiles keep plugin data separate at runtime but are not an OS filesystem security boundary. Do not grant gateway injection for this release because no proactive delivery workflow uses it.

## Current package scope

- Three DLC core chemicals with lazy time settlement.
- DLC sleep-energy recovery and bounded temperament.
- Profile-scoped SQLite life state using Hermes `plugin_db()`.
- Read-only `digital_life_status` tool and per-provider-request affect middleware.
- Lossless memory affinity ranking helpers.
- Operator CLI: `hermes hermes-lover status|stop|resume|resolve-gap <id>`.

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
