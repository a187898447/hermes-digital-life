## Context

The repository contains the original PRD but no application code or existing capability specs. The PRD mixes reusable runtime behavior with one machine's QQ credential handoff, route identifiers, profile setup, and a private persona. This change extracts the reusable product: an independently installable Hermes plugin whose behavior is generic and whose operator-specific channel, model, schedule, and persona remain deployment configuration.

The upstream `digital-life-core` repository is public and its README welcomes MIT forks; `package.json` also declares MIT. There is no standalone LICENSE file and GitHub reports no license metadata. Derived code must preserve attribution and include an MIT notice consistent with upstream statements, with provenance pinned to a reviewed revision. Community-maintained status describes support expectations, not the reuse license.

Hermes API facts verified locally against `v0.22.5+1852.g085f562.dirty`: standalone plugins load through `register(ctx)`; `ctx.state.get/set` are individually atomic but not a transaction; `plugins.plugin_storage.plugin_db()` provides profile-scoped SQLite; system prompt sections are frozen per session; `pre_llm_call` runs once per turn; `llm_request` middleware runs before every provider request; gateway injection requires `plugins.entries.digital-life.allow_gateway_injection: true`. Hermes file-safety rules do not establish strict sibling-profile filesystem isolation.

The product policy for life-state changes is resolved: fixed plugin rules may respond to host-observed provider requests, completed turns, transient errors, empty/failed search results, and elapsed time. No user/model-supplied value or arbitrary set-state request is accepted.

## Goals / Non-Goals

**Goals:**
- Ship a generic, separately installable Hermes standalone plugin, not a machine-specific configuration bundle.
- Implement adopted `digital-life-core` domain semantics in Python using supported Hermes extension points.
- Persist bounded state transactionally in the active profile.
- Apply affect on each provider request without replacing Hermes's active context engine.
- Provide state-aware, lossless memory ranking.
- Make reflection, schedules, and proactive delivery opt-in and configurable.
- Document installation, configuration, attribution, privacy, and operational limitations.

**Non-Goals:**
- No QQ credentials, user/chat identifiers, local channel migration, private persona, or profile bootstrap in the distributable plugin.
- No model-callable tool or event API to provide arbitrary state values. Plugin-owned transitions are limited to deterministic rules triggered by host-observed events and elapsed time.
- No hard OS-level isolation guarantee between Hermes profiles.
- No DLC organ self-compilation, WebUI, or Douyin/browser activity.
- No initial Hermes plugin-catalog submission requirement.

## Decisions

1. **Use Hermes general standalone plugin lifecycle.** Implement `register(ctx)` and supported registration APIs. `register_tools(ctx)` is not the standalone entry point.
2. **Separate domain from host integration.** Port/reference selected pure calculations from a pinned upstream revision; do not port DSH-facing `index.ts`. Use the default three chemicals (dopamine, cortisol, serotonin) and DLC sleep-energy rules; do not add oxytocin.
3. **Use transactional profile-scoped SQLite for life state.** `ctx.state.get/set` are individual atomic operations, not a transaction. Use `plugins.plugin_storage.plugin_db("digital-life")` and apply updates inside SQLite transactions.
4. **Reject arbitrary model/user-supplied state values.** The model may inspect bounded read-only status. Internal code may change state only through fixed rules triggered by observed provider requests, completed turns, transient API errors, empty/failed searches, and elapsed time, or by an explicit operator CLI stop/resume. No interface accepts caller-supplied hormone or energy values.
5. **Keep memory policy identity-led and lossless.** Hermes's built-in memory tool remains responsible for deciding and writing what the configured identity considers worth retaining. Per-request middleware parses host-rendered `MEMORY.md` and `USER.md` blocks by their exact UTF-16 length headers, reorders entries with DLC affinity, and preserves every entry and surrounding prompt text. The plugin neither replaces the active MemoryProvider nor owns memory writes. No private persona is shipped.
6. **Separate reflection from proactive messaging.** These are opt-in and configurable. Delivery uses an operator-configured destination, limits, quiet hours, duplicate/stale/busy safeguards, emergency stop, and Hermes's explicit injection capability. Keep state-changing autonomous schedules disabled until transition policy and operator stop controls are resolved.
7. **Use verified per-request affect middleware.** `llm_request` middleware can rewrite current request messages/input. Use it for concise volatile affect context. System prompt sections are frozen per session; `pre_llm_call` is once per turn. Do not replace Hermes's single active context engine.
8. **Describe profile separation honestly.** State and memories follow the active profile, but Hermes profiles are logical/runtime separation, not a filesystem sandbox.
9. **Publish independently first.** Include reproducible tests and install/configuration documentation. Hermes catalog submission is optional later.

## Risks / Trade-offs

- [Hermes APIs evolve quickly] -> Verify the supported range with plugin doctor and a clean temporary profile.
- [Upstream has no standalone LICENSE file] -> Preserve attribution, cite the pinned commit, and include an MIT notice consistent with upstream README/package metadata.
- [SQLite data can grow] -> Keep life state bounded and separate from conversation memory.
- [Middleware rewrites request payloads] -> Add only a short affect note and test supported request shapes.
- [Profiles are not hard security boundaries] -> Document logical isolation only.
- [Autonomous work can surprise operators] -> Schedules and delivery default off; resolve operator stop/resume control before enabling state-changing jobs.
- [Python port may drift] -> Compare deterministic vectors from the pinned upstream revision.

## Migration Plan

There is no deployed implementation to migrate. First load creates a versioned SQLite state row in the active profile. Future schema changes migrate transactionally and preserve user-authored memory. Installation/removal must not alter existing channels, credentials, sessions, or default-profile files.

## Open Questions

- May scheduled dreams persist identity-judged memory without changing chemicals/energy? Autonomous jobs remain disabled until specified.
- What quiet-hours/cadence defaults should the first release use? Autonomous schedules remain disabled until configured.
- Which Hermes release range exposes the same middleware and cron contracts?
- Review upstream MIT packaging and attribution before public release; upstream has no standalone license file.
