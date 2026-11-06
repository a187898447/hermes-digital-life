# hermes-lover Hermes plugin

This is a generic, independently installable Hermes standalone plugin. It does not include a persona, provider/model choice, channel, credentials, or recipient. Operators supply those in their own Hermes profile.

Install the repository into the target Hermes home as `plugins/hermes-lover/`, then enable it with `hermes plugins enable hermes-lover`. It is opt-in. Requires Hermes `>=0.22` and a Python runtime with the host's plugin APIs.

End-to-end walkthrough (persona → life system → QQ bot), in Chinese: [使用引导.md](使用引导.md). The installer also bundles it as the `hermes-lover-guide` skill into the new profile, so the agent can guide the setup and the persona translation itself, and creates the `hermes-lover-beat` cron job (paused) for four jittered daily check-ins.

Default messaging permissions are deliberately narrow: the installer sets `platform_toolsets.qqbot` to `["web","vision","todo","digital_life","no_mcp"]` and `human_delay.mode: natural`, so a QQ-bound bot can converse but not run terminal commands, write files, or call MCP tools. Widen `platform_toolsets.qqbot` per profile when the operator needs more.

Event-memory writes default on. Set `plugins.entries.hermes-lover.settings.memory.writes_enabled: false` to reject `memory_write` calls; recall still works and normally refreshes access history. Emergency stop freezes all event-memory writes, including recall touches. Reflection remains an explicitly chosen automation blueprint; no job is started by this setting.

Uninstall with `hermes plugins disable hermes-lover` followed by `hermes plugins remove hermes-lover`. Plugin removal leaves profile data under `plugin-data/hermes-lover/`; handle that directory separately if deleting the saved state is desired.

For upgrades, back up the target profile's `plugin-data/hermes-lover/` directory before replacing plugin code. State is schema-versioned; this release performs bounded migrations when reading older state. Hermes profiles keep plugin data separate at runtime but are not an OS filesystem security boundary. The installer enables gateway injection for the plugin's own wake-up nudge; the scheduled beats use the host's cron job and `beat_send` instead.

## Current package scope

- Three DLC core chemicals with lazy time settlement.
- DLC sleep-energy recovery and bounded temperament.
- Profile-scoped SQLite life state using Hermes `plugin_db()`.
- Read-only `digital_life_status` tool and per-provider-request affect middleware.
- Bounded open-loop system context (up to three gaps, at most 400 appended characters).
- Chinese assistant uncertainty/inquiry detection with existing gap deduplication and reactivation.
- Task-defined chemistry/energy temperature mapping, only when request temperature is omitted or null.
- Lossless memory affinity ranking helpers.
- Dated memory palace with FTS5 recall, optional hybrid (RRF) recall, facts/journals, `read_source`/`library_list`/`study_source`, and a pre-send speech gate.
- QQ Zone read tools (`qzone_feed`), background jobs (`background_task`, `search_later`), and the `beat_targets`/`beat_send` pair used only by scheduled beats.
- Operator CLI: `hermes hermes-lover core|status|stop|resume|resolve-gap <id>`.
- Bundled profile skills: `hermes-lover-guide` (onboarding, 女娲→SOUL.md translation, QQ binding, beat enablement).
- Bundled personas: `SOUL.template.md` plus the worked example `personas/001-deepseek-xiaojingyu/` (persona card + finished `SOUL.md`), copied into every new profile's `personas/`.

No model-callable tool mutates life state or accepts user-supplied hormone/energy values. Fixed plugin rules respond only to host-observed provider requests, completed turns, transient errors, search results, and elapsed time. Operator stop/resume and gap closure require typing an explicit confirmation. The installer creates one paused `hermes-lover-beat` cron job that delivers locally; it stays silent until an operator resumes it (guide, last step). The optional “Quiet digital-life reflection” blueprint creates a local-only scheduled turn when an operator chooses it. The plugin itself implements no delivery logic and registers no gateway injection hook.

Hermes's built-in `memory` tool remains responsible for memory writes. Per-request middleware reorders the exact rendered `MEMORY.md`/`USER.md` blocks by DLC affinity and preserves every entry; it does not replace or own the memory store. The reflection blueprint may write identity-judged memory through Hermes's built-in tool; it cannot mutate life-state values. The plugin itself implements no proactive wakes; the installer's paused `hermes-lover-beat` cron is a host-side scheduled turn the operator must enable.

## Dated Event Memory

Hermes's built-in `memory` remains the store for stable profile facts. `memory_write(text, kind, weight)` saves experiences and decisions in `<profile>/plugin-data/hermes-lover/memory/YYYY/MM/YYYY-MM-DD.jsonl`. Entries are limited to 4000 characters and 20 new entries per session/local day; same-day exact duplicates refresh access time and reinforce weight by 0.2 (maximum 3.0).

`memory_recall(query, k=8, include_cold=false)` searches Chinese bigrams and lowercase English tokens, returning inventory/match counts and ranked entries. Displayed entries refresh access history except during emergency stop. Queries are token-based, not semantic search; punctuation-only or single-character queries use literal substring matching. Neither tool changes life state. No event memories are automatically injected into prompts in P0.

Daily JSONL add/touch/forget events are the only authority. `memory-index.db` is disposable: rebuild with `hermes hermes-lover memory-rebuild`. Back up the daily logs before manual edits; malformed lines are reported and skipped, and interrupted tails are preserved in recovery files. Cold views are never replayed. Archive events are understood but never generated automatically; compaction is the explicit `memory-compact` CLI, and injection into prompts is opt-in per kind.

A-class upstream rules: seven-day effective-weight half-life, reciprocal recency, three-level attention, and nonempty-query score coefficients. B-class policies: daily JSONL, bigram/FTS BM25 relevance, kind defaults, same-day reinforcement, empty-query attention bias, quotas and the write switch. Current implementation fully scans the logs to validate the cache on each operation; large archives will need incremental indexing. Requires macOS/Linux file locking and SQLite FTS5.

Hermes 0.22.5 does not pass reliable primary/subagent/cron context to ordinary plugin tools. Source session IDs and quotas are recorded, but do not isolate callers: subagents or scheduled agents may write. Missing session IDs share the `unknown` quota bucket. Disable writes or use emergency stop when necessary.

## Conversation capture

Every completed turn is appended to the profile's dated event log as a `kind=turn` entry
(`plugin-data/<name>/memory/YYYY/MM/DD.jsonl`), with no model call and no quota. Set
`memory.capture: false` in the plugin settings to turn it off. The dated library is
searchable from any platform that includes the `digital_life` toolset
(`memory_recall`), and QQ sessions get that toolset by default since install.

## Known issue: Studio loses most tools for non-default profiles

Hermes Studio runs each non-default profile's agent in a bridge worker pinned to Hermes' managed `tools` python. That runtime ships no third-party packages, and the worker never imports `hermes_bootstrap`, so `ruamel.yaml` and friends are missing; every tool module that needs them (terminal, files, memory, skills, web) fails to import and the agent silently sees only a handful of tools.

When Hermes Studio is installed, `install.sh` checks for this and applies an idempotent host fix that adds the install venv to that runtime through a `sitecustomize.py`. No Studio, no host change. For an existing install run it manually and then open a new Studio conversation:

```sh
python3 scripts/fix-studio-bridge-python.py          # apply, safe to repeat
python3 scripts/fix-studio-bridge-python.py --check  # verify
python3 scripts/fix-studio-bridge-python.py --remove # undo
```

CLI and TUI sessions, and the default profile, are unaffected.

## Known issue: Studio drops option-card answers for a non-default profile

Hermes Studio 0.8.x binds each chat connection to one profile and then checks every clarify/approval answer against that connection profile. When the conversation belongs to another profile (`hermes-lover`, the usual case after install), the click is rejected with `Profile "<name>" is not available on this connection`; the card closes, the agent waits out the timeout, and the card reappears when you come back to the tab.

When Hermes Studio is installed and its bundle still contains that guard, `install.sh` applies an idempotent host fix that lets Studio serve a conversation from any profile that exists on the machine. No Studio, no host change. Only Studio's own JS bundle is touched; Hermes and the CLI are not modified. For an existing install run it manually and then restart Studio:

```sh
python3 scripts/fix-studio-cross-profile.py          # apply, safe to repeat
python3 scripts/fix-studio-cross-profile.py --check  # verify
python3 scripts/fix-studio-cross-profile.py --remove # undo
hermes-web-ui restart
```

If a Studio upgrade overwrites the bundle, re-run the installer or the script above.

## Known issue: a host update can disable the plugin or move its internals

Hermes updates can add `hermes-lover` to `plugins.disabled_by_update`, drop `digital_life`/`no_mcp` from `platform_toolsets.qqbot`, or move internal modules the plugin reads (`tools.send_message_tool`, `cron.jobs`, `plugins.plugin_storage`). A moved module now degrades one feature instead of breaking the turn, and `hermes-lover doctor` reports every host condition the plugin depends on:

```sh
hermes -p hermes-lover hermes-lover doctor        # read-only; exit 1 when something needs a fix
hermes -p hermes-lover hermes-lover doctor --fix  # re-enable, re-patch, re-sync profile files
```

`--fix` never edits cron settings, restarts the gateway, or overwrites `SOUL.md`; do the restart yourself after it reports clean.

## Optional: billion-context (bili)

The plugin works without [billion-context](https://github.com/ranxianglei/billion-context);
Hermes then uses its built-in context compaction. bili is only a drop-in
compressor, and nothing here installs it.

- Detect: `python3 scripts/detect-billion-context.py` → `{"installed": bool, "version": str|null}`.
- Install: follow the billion-context README, then enable it for the profile
  (`hermes plugins enable billion-context`) and start a new session.
- Disable: `hermes plugins disable billion-context` (or remove its directory under
  `~/.hermes/plugins/billion-context`). Life state is untouched either way.
- Failure is silent by design: a missing or broken bili never blocks a turn, and
  `status`/`/core` keep working on the plugin's own state.

## Validate

From this repository root:

```sh
cd tests && PYTHONPATH=.. python3 -m unittest discover -p 'test_*.py'
```

The tests do not require Hermes or an API key. The live plugin contract can be checked with `hermes plugins doctor . --ci` in a compatible Hermes installation.

## Upstream attribution

Selected domain rules are derived from `digital-life-core` by mashangshui (温水) and 鱼小水, upstream revision `d984665ff2e452cf9f57d845cfb93bfb9a64b6b1`. The upstream README welcomes MIT forks and its package metadata declares MIT. See `LICENSE` for attribution and the notice shipped with this project.

## 记忆归属

两层，按平台分开：

- **CLI / Studio** 继续用 Hermes 自带的 `MEMORY.md` / `USER.md`（`memory.memory_enabled=true`），
  这些会话的回合不写宫殿。
- **QQ（qqbot）** 只用按天分片的记忆宫殿（`plugin-data/<profile>/memory/`）。QQ 工具集不含自带
  `memory`，插件也只对 `platform == "qqbot"` 的回合做自动捕获。每条捕获都带
  `chat_id`/`chat_type`/`speaker`，召回按当前会话隔离，私聊与群聊不会混记。
- 在 QQ 里对机器人发 `/new`（或 `/reset`）会清空当前会话在记忆宫殿里的条目、并把 digital-life
  状态恢复成默认值（化学物质/精力/未解决的事）；`/new all` 连其他会话的宫殿记录一起清空。
  只有 `QQ_OWNER_ID` 或 `QQ_ALLOWED_USERS` 里的所有者/管理者能触发，其他人发 `/new` 只新开会话、
  不动记忆。
- 日记不靠定时任务（笔记本合盖就会错过）：每天第一条 QQ 消息到达时，插件检查昨天有没有
  `journal` 条目，没有就让模型读昨天的宫殿记录和当前 digital-life 状态，写一篇 120–400 字的
  第一人称日记（`memory_write(kind="journal", day="YYYY-MM-DD")`，权重 2.5，锚定在被总结的那天）。
  问到某一天时先命中日记，日记没写到的细节再用 `memory_recall(day="YYYY-MM-DD")` 查当天原始条目。
- 日记之前先提炼原子事实：同样在每天第一条 QQ 消息时，插件检查前一天有没有 `fact`，没有就让
  模型读当天的 turn，写 1–3 条 `memory_write(kind="fact", day=..., sources=["hp-…"])`；日记再用
  `refs` 引用这些事实。`memory_recall` 返回的每条结果都带 `sources`，可以顺着
  日记 → 事实 → 原始对话往下查。
- 检索默认混合：装了 `sentence-transformers` 且本地有 `BAAI/bge-small-zh-v1.5`，或在 profile 的
  `.env` 里配了 embedding API 时，FTS5 与向量结果用 RRF（k=60）融合；没有依赖/key、向量损坏或
  维度变化时自动退回 FTS，不会报错，结果里的 `mode` 会写明 `hybrid` 还是 `fts`。
- `hermes-lover memory-compact --days 30` 是手动命令（不是模型工具）：把 30 天未访问的归档条目
  按 `(date, chat_id)` 折叠成一条 ≤200 字的 `summary`，原文保留、仍可按 id 读回；`include_cold=true`
  时只返回摘要，不展开原文。重复执行不会产生重复摘要。

## 工具、预算与口吻闸门

QQ 会话里模型能调用的工具分几组，权限和预算都不一样：

- **记忆**：`memory_write` / `memory_recall` / `memory_revoke`（只能撤 10 分钟内）/ `memory-compact` CLI。
  自动捕获按轮写 `turn`；每天第一条 QQ 消息会先补前一天的 `fact` 和 `journal`。
- **读源**：`read_source` 单页最多 6000 字，书没读完时返回自动续读提示，支持 GitHub blob/repo/tree；
  `library_list` 列出 `plugin-data/<profile>/library/` 里的书（每本 ≤2MB）；`study_source` 把长链接交给
  后台会话，后台只能用 `read_source` 和 `memory_write`（`pre_tool_call` 硬拦 terminal/搜索/代码执行），
  读完给会话回一句结论。
- **空间**：`qzone_feed` 每轮最多 2 页、5 条，且调用前必须先 `memory_recall` 查这段是否读过。
- **后台**：`background_task` / `search_later` 交出去后不占本轮预算，结果直接发回会话。
- **只读状态**：`digital_life_status` 里 `proactive` 字段说明 beat 是否排程、今天响过几次、哪些工具只在
  定时轮可用；普通聊天轮调 `beat_send` 会被拒，这是设计，不是权限没开。

预算：QQ 每一轮最多 8 次外部工具调用（搜索、抓取、空间、终端、写文件等）；记忆类、读源类、状态、
后台任务不计入。超了会先让模型回话，把余下工作交给后台。

口吻闸门：QQ 发送前，`digital_life/speech.py` 先标出回执腔、结算腔、填充词、客服腔和反问尾巴；
命中就让子会话改写（`HERMES_LOVER_POLISH_CHILD=1`），仍不合格按分句删，删光回「嗯」。
相关规则也写在人格卡与 `shuorenhua` skill 里。

已学（`kind=learned`）只收「什么场合 → 你会怎么做」的社会习惯：可带 `applies_dow`（0=周一…6=周日）
限定星期；超过 60 字、引用原文 ≥15 字、带元信息词（仓库/API/格式/文档…）会被拒；同一事由只留一条，
第二条要求把细节并进第一条。条数不设配额，由材料本身决定。

## 文档索引

- [使用引导.md](使用引导.md)：从零到 QQ 机器人能说话的完整步骤。
- [安装说明.md](安装说明.md)：安装/卸载/升级与排错。
- [docs/memory-palace-upgrade-plan.md](docs/memory-palace-upgrade-plan.md)：记忆宫殿 P0–P2 的设计与阶段划分。
- [docs/memory-comparison.md](docs/memory-comparison.md)：与腾讯 Agent Memory 的取舍对比。
- [docs/missing-modules-plan.md](docs/missing-modules-plan.md)：化学/基因组/间隙等模块的补齐计划与偏差记录。
- [docs/host-fragility-plan.md](docs/host-fragility-plan.md)：宿主升级会踩到的坑与降级策略。

## 宿主补丁

有些行为只能改宿主才能做到（QQ 拆泡、打字节奏、关停横幅、Studio 工具链和跨 profile），
所以插件带一组幂等补丁脚本。`install.sh` 会在检测到对应宿主时自动应用；也可以手动跑：

| 脚本 | 作用 | 标记 |
| --- | --- | --- |
| `scripts/fix-qq-dialogue-split.py` | 只在引号/括号外断句、短句并泡、系统卡整条发 | `# hermes-lover: dialogue split` |
| `scripts/fix-qq-typing-pace.py` | 气泡之间加人类打字停顿 | `# hermes-lover: typing pace` |
| `scripts/fix-qq-shutdown-notice.py` | 关停/重启横幅不再广播到 QQ | `# hermes-lover: qq shutdown notice` |
| `scripts/fix-studio-bridge-python.py` | 让非默认 profile 的 Studio worker 看到宿主依赖 | `HERMES_STUDIO_BRIDGE_FIX` |
| `scripts/fix-studio-cross-profile.py` | 让 Studio 接受来自非默认 profile 的选项卡回答 | `HERMES_LOVER_STUDIO_CROSS_PROFILE` |

每个脚本都支持 `--check`（已打返回 0）和 `--remove`（还原备份或反向替换）；宿主升级覆盖后会失效，
`hermes-lover doctor --fix` 会重新应用，`doctor` 只报不改。
