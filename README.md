# hermes-lover Hermes plugin

This is a generic, independently installable Hermes standalone plugin. It does not include a persona, provider/model choice, channel, credentials, or recipient. Operators supply those in their own Hermes profile.

Install the repository into the target Hermes home as `plugins/hermes-lover/`, then enable it with `hermes plugins enable hermes-lover`. It is opt-in. Requires Hermes `>=0.22` and a Python runtime with the host's plugin APIs.

End-to-end walkthrough (persona → life system → QQ bot), in Chinese: [使用引导.md](使用引导.md). The installer also bundles it as the `hermes-lover-guide` skill into the new profile, so the agent can guide the setup and the persona translation itself, and creates the `hermes-lover-beat` cron job (paused) for four jittered daily check-ins.

Default messaging permissions are deliberately narrow: the installer sets `platform_toolsets.qqbot` to `["web","vision","todo","memory","no_mcp"]` and `human_delay.mode: natural`, so a QQ-bound bot can converse but not run terminal commands, write files, or call MCP tools. Widen `platform_toolsets.qqbot` per profile when the operator needs more.

Event-memory writes default on. Set `plugins.entries.hermes-lover.settings.memory.writes_enabled: false` to reject `memory_write` calls; recall still works and normally refreshes access history. Emergency stop freezes all event-memory writes, including recall touches. Reflection remains an explicitly chosen automation blueprint; no job is started by this setting.

Uninstall with `hermes plugins disable hermes-lover` followed by `hermes plugins remove hermes-lover`. Plugin removal leaves profile data under `plugin-data/hermes-lover/`; handle that directory separately if deleting the saved state is desired.

For upgrades, back up the target profile's `plugin-data/hermes-lover/` directory before replacing plugin code. State is schema-versioned; this release performs bounded migrations when reading older state. Hermes profiles keep plugin data separate at runtime but are not an OS filesystem security boundary. Do not grant gateway injection for this release because no proactive delivery workflow uses it.

## Current package scope

- Three DLC core chemicals with lazy time settlement.
- DLC sleep-energy recovery and bounded temperament.
- Profile-scoped SQLite life state using Hermes `plugin_db()`.
- Read-only `digital_life_status` tool and per-provider-request affect middleware.
- Bounded open-loop system context (up to three gaps, at most 400 appended characters).
- Chinese assistant uncertainty/inquiry detection with existing gap deduplication and reactivation.
- Task-defined chemistry/energy temperature mapping, only when request temperature is omitted or null.
- Lossless memory affinity ranking helpers.
- Operator CLI: `hermes hermes-lover status|stop|resume|resolve-gap <id>`.
- Bundled profile skills: `hermes-lover-guide` (onboarding, 女娲→SOUL.md translation, QQ binding, beat enablement).
- Bundled personas: `SOUL.template.md` plus the worked example `personas/001-deepseek-xiaojingyu/` (persona card + finished `SOUL.md`), copied into every new profile's `personas/`.

No model-callable tool mutates life state or accepts user-supplied hormone/energy values. Fixed plugin rules respond only to host-observed provider requests, completed turns, transient errors, search results, and elapsed time. Operator stop/resume and gap closure require typing an explicit confirmation. The installer creates one paused `hermes-lover-beat` cron job that delivers locally; it stays silent until an operator points its Deliver target at a real channel and resumes it (guide, last step). The optional “Quiet digital-life reflection” blueprint creates a local-only scheduled turn when an operator chooses it. The plugin itself implements no delivery logic and registers no gateway injection hook.

Hermes's built-in `memory` tool remains responsible for memory writes. Per-request middleware reorders the exact rendered `MEMORY.md`/`USER.md` blocks by DLC affinity and preserves every entry; it does not replace or own the memory store. The reflection blueprint may write identity-judged memory through Hermes's built-in tool; it cannot mutate life-state values. The plugin itself implements no proactive wakes; the installer's paused `hermes-lover-beat` cron is a host-side scheduled turn the operator must enable.

## Dated Event Memory

Hermes's built-in `memory` remains the store for stable profile facts. `memory_write(text, kind, weight)` saves experiences and decisions in `<profile>/plugin-data/hermes-lover/memory/YYYY/MM/YYYY-MM-DD.jsonl`. Entries are limited to 4000 characters and 20 new entries per session/local day; same-day exact duplicates refresh access time and reinforce weight by 0.2 (maximum 3.0).

`memory_recall(query, k=8, include_cold=false)` searches Chinese bigrams and lowercase English tokens, returning inventory/match counts and ranked entries. Displayed entries refresh access history except during emergency stop. Queries are token-based, not semantic search; punctuation-only or single-character queries use literal substring matching. Neither tool changes life state. No event memories are automatically injected into prompts in P0.

Daily JSONL add/touch/forget events are the only authority. `memory-index.db` is disposable: rebuild with `hermes hermes-lover memory-rebuild`. Back up the daily logs before manual edits; malformed lines are reported and skipped, and interrupted tails are preserved in recovery files. Cold views are never replayed. P0 understands archive events but does not generate them or automatically delete/archive memories; injection is P1 and archive/compact controls are P2.

A-class upstream rules: seven-day effective-weight half-life, reciprocal recency, three-level attention, and nonempty-query score coefficients. B-class policies: daily JSONL, bigram/FTS BM25 relevance, kind defaults, same-day reinforcement, empty-query attention bias, quotas and the write switch. Current implementation fully scans the logs to validate the cache on each operation; large archives will need incremental indexing. Requires macOS/Linux file locking and SQLite FTS5.

Hermes 0.22.5 does not pass reliable primary/subagent/cron context to ordinary plugin tools. Source session IDs and quotas are recorded, but do not isolate callers: subagents or scheduled agents may write. Missing session IDs share the `unknown` quota bucket. Disable writes or use emergency stop when necessary.

## Known issue: Studio loses most tools for non-default profiles

Hermes Studio runs each non-default profile's agent in a bridge worker pinned to Hermes' managed `tools` python. That runtime ships no third-party packages, and the worker never imports `hermes_bootstrap`, so `ruamel.yaml` and friends are missing; every tool module that needs them (terminal, files, memory, skills, web) fails to import and the agent silently sees only a handful of tools.

`install.sh --studio-fixes` applies an idempotent host fix that adds the install venv to that runtime through a `sitecustomize.py` (a plain install skips host changes). For an existing install run it manually and then open a new Studio conversation:

```sh
python3 scripts/fix-studio-bridge-python.py          # apply, safe to repeat
python3 scripts/fix-studio-bridge-python.py --check  # verify
python3 scripts/fix-studio-bridge-python.py --remove # undo
```

CLI and TUI sessions, and the default profile, are unaffected.

## Known issue: Studio drops option-card answers for a non-default profile

Hermes Studio 0.8.x binds each chat connection to one profile and then checks every clarify/approval answer against that connection profile. When the conversation belongs to another profile (`hermes-lover`, the usual case after install), the click is rejected with `Profile "<name>" is not available on this connection`; the card closes, the agent waits out the timeout, and the card reappears when you come back to the tab.

`install.sh --studio-fixes` applies an idempotent host fix that lets Studio serve a conversation from any profile that exists on the machine. Only Studio's own JS bundle is touched; Hermes and the CLI are not modified. For an existing install run it manually and then restart Studio:

```sh
python3 scripts/fix-studio-cross-profile.py          # apply, safe to repeat
python3 scripts/fix-studio-cross-profile.py --check  # verify
python3 scripts/fix-studio-cross-profile.py --remove # undo
hermes-web-ui restart
```

If a Studio upgrade overwrites the bundle, re-run the installer or the script above.

## Validate

From this repository root:

```sh
python3 -m unittest discover -s tests
```

The tests do not require Hermes or an API key. The live plugin contract can be checked with `hermes plugins doctor . --ci` in a compatible Hermes installation.

## Upstream attribution

Selected domain rules are derived from `digital-life-core` by mashangshui (温水) and 鱼小水, upstream revision `d984665ff2e452cf9f57d845cfb93bfb9a64b6b1`. The upstream README welcomes MIT forks and its package metadata declares MIT. See `LICENSE` for attribution and the notice shipped with this project.
