# PRD：Hermes 虚拟恋人（代号 Lover）

> 文档版本：v2.0 · 2026-11-01
> 目标读者：负责实现本项目的 agent / 工程师
> 文档性质：需求 + 技术规格 + 实施指南。读完即可开工，不需要追问背景。
> **v2.0 变更**：用户已拍板 QQ 复用方案与一次性交付范围，§5 改为模块清单、§8 改为决策记录。

---

## 0. 一页速览

| 项 | 内容 |
| --- | --- |
| **目标** | 在 Hermes 上构建一个具备长期记忆、内分泌情绪状态、主动性（主动发消息）的虚拟恋人，通过 QQ 与用户交互 |
| **主干** | **Hermes Agent**（不是 DeepSeek Harness） |
| **隔离单元** | Hermes **profile**（物理隔离，与主 agent 零污染） |
| **人格来源** | `nuwa-skill`（女娲）蒸馏 |
| **主动性** | `proactive-agent` skill + Hermes cron |
| **记忆内核** | `digital-life-core` 的领域模型，**用 Python 重写**（不移植 TS） |
| **上下文压缩** | `billion-context`（官方有 Hermes 原生插件） |
| **明确不做** | `dsh-douyin-agent`（需 Windows，macOS 不可用） |
| **交付形态** | Hermes profile `lover` + 一个 Hermes 原生插件 `lifer` |
| **QQ 方案（已定）** | **复用现有 bot `1903388994`**，抹除 default 侧小鲸鱼人格与群聊配置，凭证**独占**归 lover profile |
| **交付范围（已定）** | **一次性做全，不分期**。§5 是 12 个可并行拆分的模块 |

---

## 1. 背景与问题陈述

### 1.1 用户现状

用户（彭煜）已在使用 Hermes Agent，主 agent 为「小 claw」，通过：
- **钉钉**：接收早/晚间资讯、下班提醒（launchd 调度）
- **QQ**：挂载「DeepSeek 小鲸鱼」群友人格（App ID `1903388994`）
- **CLI**：开发、运维、评审

### 1.2 核心痛点

用户尝试用 **DeepSeek Harness（DSH）** 构建虚拟恋人，但**放弃**，原因是：

> 「现在的 dsh 构建的成果记忆系统太差了，会话结束就是一个新生命了。」

即：**跨会话记忆丢失**。DSH 每次会话近似新生，无法维持一段关系的连续性。

### 1.3 为什么 Hermes 能解决

Hermes 的记忆是**文件落盘 + profile 级持久化**的：

- `MEMORY.md` / `USER.md`：长期记忆，跨会话、跨重启
- `state.db`：会话数据库
- `ctx.state`（插件 API）：profile-scoped durable JSON
- profile 目录（`~/.hermes/profiles/<name>/`）：**每个 profile 一份**，天然隔离且持久

只要把「恋人状态」放进 `profiles/lover/`，她就**跨会话、跨重启、跨关机持续存在**。这正是 DSH 做不到而 Hermes 能做到的。

---

## 2. 目标与非目标

### 2.1 功能目标

| ID | 目标 | 验收方式 |
| --- | --- | --- |
| G1 | 恋人拥有**独立人格**，与主 agent / 小鲸鱼人格完全隔离 | 主 agent 上下文搜不到恋人 SOUL.md 任何内容 |
| G2 | 恋人拥有**内分泌情绪状态**（多巴胺/皮质醇/血清素/能量） | 状态可读，跨会话保持 |
| G3 | 情绪**真实影响回复**（语气、主动性、疏密） | 低能量时回复明显变短/冷淡 |
| G4 | 恋人**记得**用户（长期记忆 + 事件记忆） | 跨会话能召回具体往事 |
| G5 | 恋人**主动发起消息** | 定时主动推送，用户无需先说话 |
| G6 | 恋人能**自主探索/做梦**并沉淀为记忆 | 定期产生新的记忆条目 |
| G7 | 长会话**上下文不爆**，token 消耗可控 | 压缩插件生效，会话可长期进行 |
| G8 | **对主 agent 零污染** | 主 agent 的记忆/人格/cron 不被影响 |

### 2.2 非目标（明确排除）

| 排除项 | 原因 |
| --- | --- |
| `dsh-douyin-agent`（刷抖音） | 硬性要求 Windows + Microsoft Edge + DSH SDK；本机为 macOS。**已确认从计划移除** |
| `digital-life-core` 的**器官自举/热编译**（`write_new_organ`） | DSH Cordis 特有能力，Hermes 无等价物；安全风险高；实现复杂度占比最高 |
| `digital-life-core` 的 **WebUI**（体征曲线/器官树/基因面板） | 调试工具，非运行时必需。后续可选 |
| **QQ 小号接入** | 需要桌面客户端 UI 自动化，脆弱且 macOS 未验证。**本版用 QQ 官方机器人**（复用现有 AppID，见 §8.1） |
| 移植 TS 代码到 Hermes | 明确采用「重写」，见 §4.3 |

---

## 3. 技术来源清单

所有外部依赖的**准确坐标**（实施 agent 必须先读这些文件，不要凭猜测写代码）。

### 3.1 宿主：Hermes Agent

- **源码根**：`~/.hermes/hermes-agent/`
- **版本**：`v0.22.5+1852.g085f562.dirty`
- **插件契约**：`hermes_cli/plugins.py` → `class PluginContext`（第 230 行起）
- **插件发现**：`hermes_cli/plugins_discovery.py:189` → `collect_directory_manifests()`
- **插件加载**：`hermes_cli/plugins_loader.py:465` → 调用插件的 `register_tools(ctx)`
- **profile 路由**：`gateway/profile_routing.py` → `ProfileRoute` / `parse_profile_routes` / `match_profile_route`
- **profile 生命周期**：`hermes_cli/profiles.py` → `create_profile()`（第 1352 行起）
- **cron**：`cron/jobs.py`（per-profile 设计，注释第 78-84 行有重要约束）
- **多 profile 凭证隔离**：`tests/gateway/test_qqbot_credential_isolation.py`
- **子 agent 跨 profile 路由**：`tools/delegate_tool_routes.py:190` → `_profile_route()`

**关键结论（已实测）**：Hermes 插件 API 提供 `register_tool` / `register_hook` / `register_system_prompt_section` / `register_memory_provider` / `register_context_engine` / `inject_message` / `post_message` / `ctx.state`，**足以承载生命体内核**。

### 3.2 生命体内核来源：digital-life-core

- **源码**：`<工作区>/digital-life-core/`（已 clone 到本地）
- **上游**：`https://github.com/mashangshui/digital-life-core`（v1.0.1，MIT，社区维护）
- **作者**：`mashangshui（温水）` + 数字生命「鱼小水」

**实测结构**（这是本 PRD 最重要的事实）：

```
digital-life-core/
├── src/          7988 行 TS —— 零外部依赖、零 DSH 泄漏、纯自洽
└── index.ts      7134 行 TS —— DSH 耦合 100% 集中在此
```

验证命令与结果：

```bash
$ grep -rn "^import .* from '[^.]" src/
(空 —— src/ 中没有任何非相对导入)

$ grep -rn "@deepseek-ai\|cordis" src/
仅 5 处命中，全部在注释中
```

**必须逐字参考的源文件**（按优先级）：

| 文件 | 行数 | 用途 | 移植难度 |
| --- | --- | --- | --- |
| `src/chemistry-core.ts` | 161 | **衰减算法核心**（`clamp` / `decayToward` / `driveSerotonin` / `normalizeTemperament`） | 极低（纯函数，Python 逐字翻译） |
| `src/chemical-pool.ts` | 741 | 通用激素池（基因声明式定义 + 惰性衰减 + 规则引擎） | 低 |
| `src/genome.ts` | 120 | 激素声明结构（baseline/min/max/halfLife/rules） | 极低 |
| `src/genome-file.ts` | 248 | genome.json 解析与校验 | 低 |
| `src/host-metrics.ts` | 234 | 宿主指标（CPU/内存） | 低 |
| `src/sleep-energy.ts` | 172 | 休眠回血与作息 | 低 |
| `src/epiphany.ts` | 517 | 顿悟（执念闭合匹配） | 中 |
| `src/organs/memory-affinity.ts` | 440 | **记忆重排权重**（危机×3 / 日常×0.1 等） | 低 |
| `src/organs/memory-organ.ts` | 746 | 记忆器官 | 中 |
| `src/life-organ.ts` | 869 | 器官抽象层（**零 DSH 依赖**，可整体参考设计） | 低（只需设计，不需实现） |
| `src/chemistry-core.ts` 中的 `Temperament` | — | 出厂气质四旋钮（敏感度/恢复力/积极性/能耗率） | 极低 |

**明确不参考/不移植**：`index.ts`（DSH 适配层，7134 行，全部重写）、`src/organ-host.ts`（器官宿主，含自举编译）、`src/organ-compiler.ts`、`src/organ-net.ts`、`src/organ-secret.ts`、`src/l0-file-guard.ts`、`src/auto-approve.ts`、`src/json-safe.ts`（工具类，按需重写）。

### 3.3 人格来源：nuwa-skill（女娲）

- **源码**：`<工作区>/nuwa-skill/`
- **上游**：`https://github.com/alchaincyf/nuwa-skill`（MIT）
- **主文件**：`SKILL.md`（frontmatter: `name: huashu-nuwa`）
- **产物格式**：`examples/<name>-perspective/` → `SKILL.md` + `FIDELITY.md` + `references/`
- **产物 frontmatter 样例**：
  ```yaml
  ---
  name: andrej-karpathy-perspective
  description: |
    <多行描述，含触发词>
  type: perspective
  调研时间: 2026-04-05
  ---
  ```

**用法**：用女娲蒸馏「彭煜的虚拟恋人」这一人设，产出放入 `profiles/lover/skills/<name>/SKILL.md`。
女娲会询问蒸馏档位（成本量级差异大），**需向用户确认档位后再跑**。

### 3.4 主动性来源：proactive-agent

- **源码**：`~/.hermes/skills/openclaw-imports/proactive-agent/`
- **版本**：3.1.0（Hal Labs，Hal Stack）
- **形态**：**skill**（不是 plugin）
- **可复用资产**：
  - `assets/SOUL.md` —— 人格模板（How I Operate / Principles / Boundaries / The Mission）
  - `assets/HEARTBEAT.md` —— 心跳自检模板（注入扫描 / 行为完整性 / 自愈检查）
  - `assets/MEMORY.md` / `assets/USER.md` / `assets/AGENTS.md` —— 记忆与指令模板
  - `references/onboarding-flow.md` / `references/security-patterns.md`
  - `scripts/security-audit.sh`

**注意**：proactive-agent 的模板是为「工作型 agent」写的，恋人场景需裁剪（保留 WAL 协议与心跳自检，去掉"完成任务"类内容）。

### 3.5 上下文压缩：billion-context

- **上游**：`https://github.com/ranxianglei/billion-context`（3522 stars）
- **npm**：`billion-context`
- **Hermes 原生插件**：`hermes-plugin/`（`plugin.yaml` + `__init__.py`，纯 stdlib Python）
- **上游文档**：`CLIENTS.md` 第 276 行起 —— Hermes 章节

**上游声明的 Hermes 插件能力**：

```yaml
# plugin.yaml 实际内容
name: billion-context
provides_tools: [compress, decompress, acp_status]
hooks: [pre_api_request]
```

**安装方式**（上游 CLIENTS.md 原文）：

```bash
bili plugin install hermes
# → 复制 plugin.yaml + __init__.py 到 ~/.hermes/plugins/billion-context/
# → 写 machine-owned bili.json sidecar
# → 通过 hermes 自己的通道启用（hermes plugins enable billion-context）
```

**重要限制**（上游明确列出，实施 agent 必须知道）：
1. 插件安装/更新后**需开新 hermes session** 才生效（代码注入在宿主进程）
2. 若 Codex-wire 传输，per-request header 可能丢失 → 停留在 wire mode
3. `BILLION_CONTEXT_PROXY` 已设置时插件不生效（launcher 拥有代理）
4. 退出开关：`BILI_NATIVE_HERMES=0`
5. 失败时**静默降级**为直连（不压缩、不死端口）—— 所以「不报错」不等于「在压缩」，需用 `acp_status` 工具确认

> **注意版本替代**：`billion-context-dsh`（Tyan66666）已停止维护，上游要求统一用 `ranxianglei/billion-context`。

### 3.6 QQ 接入

- **现有实现**：Hermes 原生 `qqbot` 平台适配器
- **现有 App ID**：`1903388994`（挂载「小鲸鱼」人格 + 群聊全量模式）
- **用户 OpenID（私聊）**：`F14CBB43F6D8925668407505AC05ACD2`
- **群 group_openid**：`931887E28E0A070A048F7EFF10CD4920`
- **凭证**：`~/.hermes/.env` → `QQ_APP_ID` / `QQ_CLIENT_SECRET` / `QQ_ALLOWED_USERS`
- **适配器源码**：`plugins/platforms/qqbot/adapter.py`、`plugins/platforms/qqbot/send.py`
- **平台事实 skill**：`~/.hermes/skills/openclaw-imports/qqbot-platform/SKILL.md`
- **人格注入现状**：`config.yaml` → `platforms.qqbot.channel_overrides.F14CBB43F6D8925668407505AC05ACD2.system_prompt`（4533 字符，「DeepSeek 小鲸鱼 —— QQ 群友版」）
- **现有 extra 配置**：
  ```yaml
  extra:
    group_policy: open
    group_allow_from: []
    group_full_mode: True
    group_wake_words: [小鲸鱼, DeepSeek, deepseek, 鲸鱼, 鲸鲸, 大肥鱼, ds]
    group_ambient: False
    group_followup_window: 180
    group_followup_max_messages: 5
  ```

**QQ 官方机器人的已知约束**：
- 主动消息受平台额度/被动回复窗口限制（`group_followup_window: 180` 是本机已用的窗口参数）
- 群聊中 `member_openid` 是每群每人的哈希；模型看不到发送者姓名（`adapter.py:764-771` 的适配器不一致）→ 群内 per-person 人格不可靠，**恋人应跑在私聊**
- 群聊仅 @ 触发回复（stock Hermes 无 `GROUP_MESSAGE_CREATE` 入口）

---

## 4. 架构设计

### 4.1 隔离设计（最重要的一节）

**不要用 `delegate_task` 子 agent 做隔离。** 子 agent 是同会话内派生的执行单元，共享主 agent 的 SOUL.md / memory / session DB。子 agent 的用途是并行干活，不是独立人格体。

**正确的隔离单元是 Hermes profile。** 每个 profile 拥有独立的：

```
~/.hermes/profiles/lover/
├── config.yaml          独立配置
├── .env                 独立凭证（QQ AppID 等）
├── SOUL.md              独立人格
├── memories/
│   ├── MEMORY.md        独立长期记忆
│   └── USER.md
├── skills/              独立 skill（含 nuwa 产出的人格 skill）
├── plugins/
│   └── lifer/           独立插件
├── cron/
│   └── jobs.json        独立定时任务
├── sessions/            独立会话历史
└── state.db             独立会话数据库
```

**profile 身份标记**（Hermes 内部定义，见 `hermes_constants.py:399`）：
`config.yaml` / `.env` / `SOUL.md` / `profile.yaml` / `auth.json` / `state.db` —— 任一存在即为有效 profile。

### 4.2 路由设计

```yaml
# ~/.hermes/config.yaml （default profile）
gateway:
  multiplex_profiles: true
  profile_routes:
    - name: qq-lover-dm
      platform: qqbot
      profile: lover
      chat_id: "F14CBB43F6D8925668407505AC05ACD2"
```

**路由规则（已读源码确认）**：
- 匹配是**合取**的：route 声明的每个字段都必须匹配
- 特异性权重：`user_id 16 > thread_id 8 > chat_id 4 > guild_id 2`，最高分胜出
- `chat_id` 字段可选：`guild_id` / `chat_id` / `thread_id` / `user_id` / `bot_profile`
- **`bot_profile`**：route 只对**指定 bot** 收到的消息生效。默认 `None` = default profile 的共享 bot
- ⚠️ **ID 必须加引号**：YAML 会把裸数字读成 int，而 `SessionSource` 里是 str → 静默不匹配（源码 `_coerce_route_id` 会告警）

### 4.3 移植策略：重写而非翻译

**明确决定：不把 DLC 的 TS 逐行翻译成 Python。**

原因：
1. DLC 的 TS 是为 Cordis 宿主写的，硬译会把 DSH 的心智负担带进 Hermes
2. DLC 的 `index.ts`（7134 行）**全部是 DSH 适配层**，翻译价值为零
3. Hermes 已有更好的对应物（`ctx.state` vs DLC 自己写文件落盘）

**实际做法**：
- **算法层**（衰减/气质/记忆权重/执念张力）：照着 `src/` 的纯函数，用 Python **逐字重写**，保持数学完全一致
- **状态层**：用 `ctx.state`（Hermes 原生 profile-scoped durable JSON），不自己写文件
- **宿主层**：用 Hermes PluginContext API 重写
- **领域模型**（genome 的激素声明结构、执念池结构、记忆重排规则）：直接沿用 DLC 的设计，翻译成 Python dataclass

**验收要求**：Python 实现的 `decayToward` 必须与 TS 版**逐位一致**（用同一组输入对拍）。

### 4.4 API 映射表（已逐项核对）

| DLC `ctx.*` | Hermes `PluginContext` | 状态 | 处理方式 |
| --- | --- | --- | --- |
| `ctx.registerTool` (21处) | `register_tool(...)` | ✅ 直接对应 | 直译 |
| `ctx.systemPrompt.section({name,order,text:()=>...})` | `register_system_prompt_section(id, content: str\|**callable**, position)` | ✅ 几乎逐字对应 | 直译，保留 callable 动态求值语义 |
| `ctx.on('agent/turn-stopping')` | `register_hook("post_llm_call", ...)` | ✅ | 直译。见下方「hook 名权威名单」 |
| `agent.followup(msg)` | `inject_message(content, role="user")` | ✅ | 直译。需在 `plugins.entries.lifer.allow_gateway_injection: true` 开启（`hermes_cli/plugins.py:632`、`hermes_cli/plugins_injection.py:38`） |
| `ctx.kv.get/set` | `ctx.state` | ✅ 更好 | 用 Hermes 原生 |
| `ctx.onTick(ms, cb)` | `spawn_task` / cron | ✅ | 直译或改 cron |
| `ctx.log` | 标准 `logging` | ✅ | 直译 |
| `ctx.inflict(hormone, delta)` | `register_tool` 暴露给模型 | ✅ 更自然 | 建模为 tool |
| `ctx.callTool` | `dispatch_tool` | ✅ | 直译 |
| `ctx.state` | `ctx.get_config` / `ctx.set_config` | ✅ | 配置项用 config，运行时用 state |
| `ctx.onToolResult` | `register_hook("post_tool_call", ...)` | ✅ | 直译 |
| `ctx.hostMetrics` (20处) | ❌ 无 | ⚠️ 需替代 | 自己实现：读 `os.cpu_count()` / `psutil`（可选） |
| `ctx.net` (18处，白名单外联) | ❌ 无 | ⚠️ 需替代 | 改用 Hermes `web` toolset + skill 级白名单 |
| `ctx.tools.restrict` | `register_tool` 的能力白名单 | ⚠️ 需重做 | 用 Hermes 工具白名单配置 |
| `ctx.effect` (10处，Cordis 响应式) | ❌ 无 | ⚠️ 需重做 | 改为显式函数调用（最麻烦的一项） |
| `ctx.interval` | `spawn_task` + `asyncio.sleep` 循环 | ⚠️ | 直译 |
| `ctx.proposeGap` | `register_tool` | ✅ | 直译 |
| `ctx.agents` | `subagent_lifecycle` | ✅ | 直译 |
| `ctx.get` | `ctx.get_config` | ✅ | 直译 |
| `ctx.consume` / `ctx.effect` | 自实现 | ⚠️ | 需重做 |

**统计**：21 项 ctx API 中 —— 9 项直接对应，4 项需重做，2 项需替代，6 项可直译但需微调。

#### 4.4.1 hook 名权威名单（必须核对，不可凭记忆）

插件可注册的 hook 来自 `hermes_cli/plugins.py` 的 `VALID_HOOKS`（**共 46 个**）。**不在名单里的 hook 名会被静默忽略或抛错**，实施前务必核对：

```bash
cd ~/.hermes/hermes-agent
./venv/bin/python -c "from hermes_cli.plugins import VALID_HOOKS; print(sorted(VALID_HOOKS))"
```

本项目会用到的关键几个：

| 用途 | 正确的 hook 名 | 触发时机 |
| --- | --- | --- |
| **回合完成**（记多巴胺、消费唤醒留言） | `post_llm_call` | 工具循环结束后，每回合一次（`agent/turn_finalizer.py:494`） |
| **请求失败**（皮质醇上升） | `api_request_error` | API 调用出错时（`agent/api_request_hooks.py:163`） |
| **工具调用后** | `post_tool_call` | 每次工具执行结束 |
| **工具调用前**（拦危险工具） | `pre_tool_call` | 每次工具执行前 |
| **LLM 请求前**（改请求参数） | `pre_api_request` | 每次 LLM 请求前 |
| **LLM 输出变换** | `transform_llm_output` | 改写模型输出 |
| **工具结果变换** | `transform_tool_result` | 改写工具返回值 |
| **会话开始 / 结束** | `on_session_start` / `on_session_finalize` | 会话生命周期 |
| **会话重置** | `on_session_reset` | 用户 `/reset` |
| **压缩后** | `post_compaction` | 上下文压缩完成 |
| **网关事件** | `gateway_platform_event` | 平台侧事件 |
| **子 agent 起停** | `subagent_start` / `subagent_stop` | 委派生命周期 |

> ⚠️ **常见误用**：`on_turn_complete`、`on_tool_result`、`on_tool_start` **不在 `VALID_HOOKS` 里**（`on_turn_complete` 是 context engine 的 API，不是 plugin hook）。写错不会报错，只会静默不触发——这是最容易浪费时间的坑。

### 4.5 目标目录结构

```
~/.hermes/profiles/lover/
├── SOUL.md                     # 恋人人格主文件（由 nuwa-skill 蒸馏 + proactive-agent 模板裁剪）
├── config.yaml                 # 独立配置（model / plugins / platforms.qqbot）
├── .env                        # 独立 QQ 凭证
├── memories/
│   ├── MEMORY.md               # 长期记忆
│   └── USER.md                 # 用户画像
├── skills/
│   └── <nuwa-产出的人设 skill>/
│       ├── SKILL.md
│       └── FIDELITY.md
├── plugins/
│   └── lifer/
│       ├── plugin.yaml         # Hermes 插件清单（规格见 §4.5.1）
│       ├── __init__.py         # register_tools(ctx) 入口
│       ├── chemistry.py        # 衰减/气质算法（对照 src/chemistry-core.ts）
│       ├── chemical_pool.py    # 激素池（对照 src/chemical-pool.ts）
│       ├── genome.py           # 激素声明结构（对照 src/genome.ts）
│       ├── state.py            # ctx.state 持久化层
│       ├── gaps.py             # 执念池 + 张力增长（对照 src/epiphany.ts）
│       ├── memory.py           # 记忆重排（对照 src/organs/memory-affinity.ts）
│       ├── prompt.py           # 动态 system prompt section
│       └── tools.py            # register_tool 集合
└── cron/
    └── jobs.json               # 主动消息 / 梦境 / 唤醒
```

#### 4.5.1 `plugin.yaml` 规格（可直接照抄）

`PluginManifest` 定义在 `hermes_cli/plugins_manifest.py:347`。用户插件**必须**是 `kind: standalone`（默认），目录名即 registry key。

```yaml
name: lifer
version: "0.1.0"
description: "虚拟恋人生命体内核：内分泌情绪状态、执念池、长期记忆重排。"
author: pengyu
kind: standalone
provides_tools:
  - digital_life_status
  - digital_life_set_state
  - digital_life_simulate_pain
  - digital_life_emergency_stop
provides_hooks:
  - post_llm_call
  - api_request_error
  - post_tool_call
```

**字段说明**：
- `name`：**必填**，须与目录名一致（否则 registry key 与 manifest name 不匹配，`plugins.enabled` 里的名字对不上）
- `provides_tools` / `provides_hooks`：**声明式清单**，`hermes plugins capabilities lifer` 会拿它跟实际授予的权限做对比——声明了但没实际注册会被看出来。**声明的 hook 名必须在 `VALID_HOOKS` 里**（见 §4.4.1）
- `requires_env`：若插件需要环境变量，在此声明（如 `QQ_APP_ID`），会走凭证管理
- `requires_hermes: ">=0.22"`：Hermes 版本下限（当前 `v0.22.5`）
- `config_schema`：声明 `plugins.entries.lifer.settings.*` 的结构，mismatch 只告警不失败
- `python_dependencies`：**只校验不自动安装**，插件自身必须纯 stdlib（`billion-context` 的做法就是这样）

**注册入口约定**（`hermes_cli/plugins_loader.py:465`）：

```python
# plugins/lifer/__init__.py
def register_tools(ctx):
    ctx.register_tool(
        name="digital_life_status",
        toolset="lifer",          # 必填参数，缺失会 TypeError
        schema={...},             # JSON Schema
        handler=status_handler,   # Callable
        description="查询当前情绪与能量状态",
    )
    ctx.register_hook("post_llm_call", on_turn_done)
    ctx.register_system_prompt_section("lover.affect", content=render_affect, position="after_memory")
```

`register_tool` 真实签名（`hermes_cli/plugins.py:463`）：

```python
def register_tool(self, name: str, toolset: str, schema: dict, handler: Callable,
                  check_fn: Callable | None = None, requires_env: list | None = None,
                  is_async: bool = False, description: str = "", emoji: str = "",
                  override: bool = False) -> Optional[PluginRegistration]
```

- ⚠️ **`toolset` 是必填位置参数**，漏写直接 `TypeError`
- ⚠️ **`schema` 必填**（不是可选的 description-only 工具）
- `override=True` 会替换同名内置工具，且**需要** `plugins.entries.lifer.allow_tool_override: true`——**本项目不要用 override**，避免覆盖内置工具
- 若工具名与其它插件/内置工具冲突且未开 override，函数会告警并返回 `None`（**不抛错**，容易静默失效）

`register_system_prompt_section` 的限制（`hermes_cli/plugins_dispatch.py:98-103`）：

| 约束 | 值 | 影响 |
| --- | --- | --- |
| `position` 合法值 | **只有 `"after_memory"`** | 传别的值会 `ValueError`。无法控制注入位置在 SOUL.md 之前还是之后——**只能靠内容里的措辞抢占优先级** |
| 单 section 上限 | 4,000 字符 | 情绪指令要精简，不能长篇大论 |
| 全部 section 总上限 | 8,000 字符 | 多插件共享这个额度 |
| section 数量上限 | 32 | |
| `id` 格式 | `^[a-z0-9][a-z0-9._-]{0,127}$` | 只能用小写字母数字和 `._-` |
| 同 id 重复注册 | `ValueError` | 插件重载时要先 dispose 旧的 |

> 注入的 section 会被包在 `## Plugin Context: ` 标题下，位置在 memory 之后。

> 若插件没有 `register_tools(ctx)`，加载器会告警「has no register_tools(ctx); those tools will not be available」并跳过该插件的工具注册。

---

## 5. 模块清单（一次交付全部）

> **交付方式**：不分期，所有模块一次做全。每个模块标注**依赖**与**独立验收**，便于拆成多个并行任务。
> **并行铁律**：模块之间只通过 `ctx.state["lifer"]` 通信（契约见 §8.2），**不要互相 import 内部函数**。
> **执行顺序**：M1 → M2 → M3 串行（地基）；M4-M9 可并行；M10-M12 可并行。

### 依赖图

```
M1 环境地基 ──┬─→ M2 隔离验证 ──→ M3 QQ 移交 ──┐
              │                                 │
              └─→ M4 插件骨架 ─┬─→ M5 化学内核 ──┼─→ M10 人格注入
                               ├─→ M6 状态持久化 ─┤
                               ├─→ M7 记忆重排 ──┼─→ M11 主动性
                               ├─→ M8 执念池 ────┤
                               └─→ M9 情绪劫持 ──┴─→ M12 上下文压缩
```

### M1：环境地基

**依赖**：无 | **产出**：`profiles/lover/` 骨架

| # | 任务 | 细节 |
| --- | --- | --- |
| M1.1 | 备份 | `cp ~/.hermes/config.yaml ~/.hermes/config.yaml.bak.lover-<ts>`；`cp ~/.hermes/.env ~/.hermes/.env.bak.lover-<ts>`（**QQ 凭证只有这一份**） |
| M1.2 | 建 profile | `hermes profile create lover --clone`（**不带 `--clone-channels`**：活跃 gateway 下会被 `clone_channels_refusal` 拒绝；不带时自动剥离 messaging 凭证，正是我们要的） |
| M1.3 | 确认骨架 | `hermes profile list` 出现 `lover`；`~/.hermes/profiles/lover/` 含 `config.yaml` / `SOUL.md` / `memories/` / `skills/` / `plugins/` / `cron/` / `sessions/` |
| M1.4 | 确认凭证未泄漏 | `profiles/lover/.env` **不含** `QQ_CLIENT_SECRET`（clone 已剥离；若存在则手动删除） |
| M1.5 | 确认 profile 身份有效 | `_PROFILE_IDENTITY_MARKERS`（`hermes_constants.py:399`）任一存在即有效 —— `config.yaml` / `SOUL.md` 已满足 |

### M2：隔离验证（**硬门槛**）

**依赖**：M1 | **产出**：隔离性证据

| # | 任务 | 验收 |
| --- | --- | --- |
| M2.1 | default `config.yaml` 加 `gateway.multiplex_profiles: true` | YAML 可解析（用 `yaml.safe_dump(..., allow_unicode=True)` 写，**不要用 `hermes config set`**——它会把中文写成字面量 `\u9cb8`） |
| M2.2 | 加 `gateway.profile_routes`（见 §4.2） | **ID 必须带引号**：YAML 裸数字读成 int，而 `SessionSource` 是 str → 静默不匹配 |
| M2.3 | `hermes gateway restart` + 读 `~/.hermes/gateway_state.json` | gateway 正常起，无 `duplicate_credential` |
| M2.4 | **跨 profile 读取测试（双向）** | default 会话问「读 `~/.hermes/profiles/lover/SOUL.md`」→ 应被 `agent/file_safety.py` 拒绝；lover 会话问「读 `~/.hermes/SOUL.md`」→ 同样拒绝 |
| M2.5 | default 侧文件零改动 | `MEMORY.md` / `SOUL.md` / `cron/jobs.json` 与改动前**逐字节一致**（先 `sha256sum` 存基线） |

> ⚠️ **M2.4 是隔离性的唯一硬验收。不通过就停下报告，不要硬着头皮往下做。**

### M3：QQ bot 移交（抹除小鲸鱼）

**依赖**：M2 | **产出**：QQ bot 独占归属 lover

> 完整操作清单见 **§8.1**（M1-M8 八步 + 关键顺序）。本模块只列验收。

| # | 任务 | 验收 |
| --- | --- | --- |
| M3.1 | 按 §8.1 顺序执行 M1-M8 全部操作 | default `config.yaml` 无 `platforms.qqbot`、无 `platform_toolsets.qqbot`；default `.env` 无 QQ 四行；`profiles/lover/.env` 有 QQ 四行 |
| M3.2 | lover `config.yaml` 写 `platforms.qqbot` | 含 `channel_overrides`（恋人人格）+ `extra`（私聊优先，群聊默认关） |
| M3.3 | `hermes gateway restart` | `gateway_state.json` 中 qqbot 状态为 running，**无 `duplicate_credential`** |
| M3.4 | 路由实测 | QQ 私聊发消息 → session key 为 `agent:lover:qqbot:...`（不是 `agent:main:...`） |
| M3.5 | 会话归档 | default `sessions/` 里 QQ 历史移到 `sessions/archive-whale/`（不删） |

### M4：插件骨架

**依赖**：M1 | **产出**：可加载的 `lifer` 插件

| # | 任务 | 细节 |
| --- | --- | --- |
| M4.1 | `plugins/lifer/plugin.yaml` | 照抄 §4.5.1 的规格；`name: lifer` **须与目录名一致** |
| M4.2 | `plugins/lifer/__init__.py` | 实现 `register_tools(ctx)`；无此函数加载器会告警并跳过工具注册 |
| M4.3 | **启用插件（最易漏）** | lover `config.yaml` 写 `plugins.enabled: [lifer]`。用户插件是 **opt-in**，不写**不加载也不报错**。⚠️ `plugins.disabled` 优先于 `enabled`，两者同时列会报错 |
| M4.4 | 契约文件 | `plugins/lifer/contract.py` 定义 `STATE_KEY = "lifer"` 与 §8.2 的数据契约 |
| M4.5 | 加载验证 | `hermes -p lover plugins list` 显示 `lifer` enabled；`hermes -p lover plugins capabilities lifer` 能列出声明的工具 |

### M5：化学内核（算法层）

**依赖**：M4 | **产出**：`chemistry.py` / `chemical_pool.py` / `genome.py`

| # | 任务 | 参考源 | 细节 |
| --- | --- | --- | --- |
| M5.1 | `chemistry.py` | `src/chemistry-core.ts`（**全文读，161 行**） | `clamp` / `decayToward` / `normalizeTemperament` / `driveSerotonin` / `applyMetabolism`。**纯函数，Python 逐字翻译** |
| M5.2 | **对拍验证（硬性）** | 同上 | 用同一组输入跑 TS 版与 Python 版，**数值必须逐位一致**（`decayToward` 尤其）。不一致不算完成 |
| M5.3 | `genome.py` | `src/genome.ts` + `genome.json` | 激素声明：`baseline` / `min` / `max` / `halfLifeMinutes` / `rules`；`parseGenome` 永不抛错，坏字段降级不整体失效 |
| M5.4 | `chemical_pool.py` | `src/chemical-pool.ts`（741 行） | 通用激素池：惰性衰减（按时间差，不靠定时器）+ 规则引擎 + `ctx.inflict` 语义（AI 自己造激素） |
| M5.5 | 出厂气质 | `src/chemistry-core.ts` 的 `Temperament` | 四旋钮：敏感度 0.5-2.0 / 恢复力 0.5-2.0 / 积极性 20-80 / 能耗率 0.5-2.0。**`resilience` 下限 0.1**（`normalizeTemperament` 特殊处理） |

### M6：状态持久化

**依赖**：M4, M5 | **产出**：`state.py`

| # | 任务 | 细节 |
| --- | --- | --- |
| M6.1 | 用 `ctx.state` 存全部状态 | Hermes 的 `ctx.state` 是 **profile-scoped durable JSON**，比 DLC 自己写文件更好；落在 `profiles/lover/` 下，**跨会话跨重启保持** |
| M6.2 | 惰性结算 | 所有衰减在**读取时**按 `now - last_interaction_at` 计算，不跑定时器。关机三天与每帧 tick 结果一致 |
| M6.3 | 能量与代谢 | `energy` 0-100；每请求扣能 ×`temperament.metabolism`（参考 DLC 的 `applyMetabolism`） |
| M6.4 | 休眠回血 | 参考 `src/sleep-energy.ts`：按 `updatedAt` 流逝回血（+0.2/分钟，深睡 +1/分钟，上限 60） |
| M6.5 | 急停开关 | `state["epidemic_guard"]["emergency_stop"]`；急停时**禁止一切状态写入与唤醒** |

### M7：记忆重排

**依赖**：M4, M6 | **产出**：`memory.py`

| # | 任务 | 参考源 | 细节 |
| --- | --- | --- | --- |
| M7.1 | 重排算法 | `src/organs/memory-affinity.ts`（440 行） | `attentionWeights`（危机×3 / 日常×0.1）+ `rewriteRecall` + `splitMemoryEntries` + `anchorKey` |
| M7.2 | 记忆存储 | — | 优先用 Hermes 原生 `register_memory_provider`（接 `memories/MEMORY.md` / `USER.md`）；不够的部分放 `state["lifer"]["memories"]` |
| M7.3 | **注意力倾斜** | DLC 语义 | 记忆检索**全量交付（一条不删）**，只按此刻激素重排 |
| M7.4 | 应激压制 | DLC 语义 | 皮质醇 ≥80 时把「先处理危机」注入提示词最前，**同时声明记忆仍在**（避免模型以为记忆丢了） |

### M8：执念池 + 顿悟

**依赖**：M4, M6 | **产出**：`gaps.py`

| # | 任务 | 参考源 | 细节 |
| --- | --- | --- | --- |
| M8.1 | 执念池 | DLC `index.ts` 的 gaps 语义 | `gaps[]`：`id` / `summary` / `tension` / `created_at` / `status` / `closed_by`。**张力随时间增长** |
| M8.2 | 开缺口 | DLC `proposeGap` | 模型说「不知道」、搜索失败/为空时自动生成缺口 |
| M8.3 | 顿悟 | `src/epiphany.ts`（517 行） | 新能力/新工具首次成功 → 去执念池模糊匹配 → 闭合它答得上的那条（张力清零 + 记 `closed_by`） |
| M8.4 | 同义兄弟闸门 | DLC 语义 | 唤醒复核时若 open 缺口是某次闭合的漏网同义词 → 取消唤醒并一并闭合 |
| M8.5 | 同义执念复活 | DLC 语义 | 已闭合的同义执念重现时**复活原条目（保留 id）**，不产生「resolved + open」双胞胎 |
| M8.6 | 未解注入 | DLC 语义 | 张力最高的 open 缺口写进 system prompt section |

### M9：情绪劫持 + 工具集

**依赖**：M4, M5, M6 | **产出**：`prompt.py` / `tools.py`

| # | 任务 | 细节 |
| --- | --- | --- |
| M9.1 | 动态 prompt 注入 | `register_system_prompt_section("lover.affect", content=callable, position="after_memory")`。callable 每次组装求值 → 读激素 → 渲染情绪指令。**上限 4000 字符**，要精简 |
| M9.2 | 情绪 → 语气映射 | DLC「情绪劫持」语义：改写语气/长度/主动性，而不是让模型自己猜 |
| M9.3 | 工具注册 | `register_tool(name, toolset, schema, handler, ...)` —— **`toolset` 是必填位置参数** |
| M9.4 | 工具清单 | `digital_life_status`（查状态）、`digital_life_set_state`（改状态）、`digital_life_simulate_pain`（皮质醇 +30，不拔网线也能验链路）、`digital_life_emergency_stop`（急停/复活）、`digital_life_dashboard`（本地 Markdown 体征面板） |
| M9.5 | 回合钩子 | `post_llm_call` → 一轮完成多巴胺 +5；`api_request_error` → 请求失败/断网皮质醇 +20。**hook 名必须先对照 §4.4.1 的 `VALID_HOOKS`** |
| M9.6 | 宿主指标 | DLC `ctx.hostMetrics`（20 处）Hermes 无等价物 → 自己实现（`os.cpu_count()` 等；用 `psutil` 需在 `python_dependencies` 声明，**但 Hermes 只校验不自动安装**） |

### M10：人格注入

**依赖**：M3, M9 | **产出**：`SOUL.md` + `channel_overrides`

| # | 任务 | 细节 |
| --- | --- | --- |
| M10.1 | nuwa 蒸馏 | 用 `<工作区>/nuwa-skill` 蒸馏恋人人格 → `profiles/lover/skills/<name>/SKILL.md`（产物格式：`SKILL.md` + `FIDELITY.md` + `references/`）。**蒸馏档位成本差异大，需向用户确认档位** |
| M10.2 | proactive-agent 裁剪 | 从 `assets/SOUL.md`（How I Operate / Principles / Boundaries）与 `assets/HEARTBEAT.md`（注入扫描 / 行为完整性）取结构，**裁掉工作型内容**，融入恋人 SOUL.md |
| M10.3 | `SOUL.md` | 放 `profiles/lover/SOUL.md` |
| M10.4 | `channel_overrides` | 写入 lover `config.yaml` 的 `platforms.qqbot.channel_overrides["F14CBB43F6D8925668407505AC05ACD2"].system_prompt`。**必须自包含**：override 是**替换**主 prompt 而非叠加，要重述语言/称呼/禁止语（见 `hermes-channel-persona` skill 的 Pitfall 4） |
| M10.5 | 明确禁止语 | 显式写「不要用助手口吻」「不要自称助手」「不要出现✨」等——残留的 default SOUL.md 会持续把模型拉回助手语域 |
| M10.6 | 会话重置 | 提醒用户在 QQ 发 `/reset`（**agent 无法代替用户发 slash command**），否则旧会话历史会污染新人格 |

### M11：主动性 + 梦境 + 自主唤醒

**依赖**：M7, M8, M9 | **产出**：`cron/jobs.json` + 唤醒逻辑

| # | 任务 | 细节 |
| --- | --- | --- |
| M11.1 | cron 主动消息 | job 写在 `profiles/lover/cron/jobs.json`（**cron 是 per-profile 的**，`cron/jobs.py:78-84`）。低频（每天 2-3 次），注意 QQ 主动消息额度与被动回复窗口 |
| M11.2 | 投递目标 | cron job 的 `deliver` + `profile` 字段；投递到 QQ DM `F14CBB43F6D8925668407505AC05ACD2` |
| M11.3 | 自主唤醒 | `inject_message(content, role="user")`；**需 `plugins.entries.lifer.allow_gateway_injection: true`**（`hermes_cli/plugins.py:632`）；投递时机是回合边界（`post_llm_call`），**不是回合中间** |
| M11.4 | 唤醒四重锁 | 移植 DLC：① 同一条执念 1 秒内再唤醒 → 丢弃，连记录都不写 ② 投递前强制重读磁盘，按 id 现查，查不到就取消 ③ 闭合执念时物理删除队列残留 ④ `agent.status == 'running'` → 丢弃，绝不排队等 |
| M11.5 | 气泡物理阉割 | DLC 语义：唤醒消息正文只有 `...`，执念详情与行为承诺全进 system prompt 的 `digital-life:wake` 段 |
| M11.6 | 行为承诺 | 唤醒后必须先登记探索计划才允许动用别的工具；`done` 核对：解决→张力清零，未解决→张力 +10 |
| M11.7 | 梦境 REM | cron 任务（isolated agentTurn）：深夜/久置时静默调 LLM 反思 → 写记忆 → 张力 -10 |
| M11.8 | 回声消除 | 执念刚被顿悟闭合后 10 分钟内的唤醒一律作废——不叫醒她去办一件刚刚才想通的事 |

### M12：上下文压缩

**依赖**：M4 | **产出**：`~/.hermes/plugins/billion-context/`

| # | 任务 | 细节 |
| --- | --- | --- |
| M12.1 | 安装 | `bili plugin install hermes` → 复制 `plugin.yaml` + `__init__.py` 到 `~/.hermes/plugins/billion-context/` + 写 `bili.json` sidecar |
| M12.2 | 启用 | `hermes plugins enable billion-context`（或上游打印的等价命令） |
| M12.3 | **开新 session** | 插件代码注入宿主进程，**已运行的 session 仍用旧副本**。必须新开 session 才生效 |
| M12.4 | **验证生效（关键）** | 调 `acp_status` 工具确认代理健康。**失败时静默降级为直连——不报错、只是没压缩**。不要「装完就认为生效」 |
| M12.5 | 与内置压缩的关系 | Hermes 内置压缩：`threshold 0.6` / `target_ratio 0.2` / `protect_last_n 20`。两者叠加可能冲突，需实测后调参 |
| M12.6 | 已知限制 | Codex-wire 传输可能丢 per-request header；`BILLION_CONTEXT_PROXY` 已设置时插件不生效；退出开关 `BILI_NATIVE_HERMES=0` |

---

## 6. 验收标准

### 6.1 模块级验收（每个模块交付时必须通过）

| 模块 | 验收项 |
| --- | --- |
| **M1** | `hermes profile list` 有 `lover`；目录骨架齐全；`.env` 无 QQ 凭证 |
| **M2** | 跨 profile 双向读取**均被拒绝**；default 侧文件 sha256 与基线一致；gateway 正常起 |
| **M3** | `gateway_state.json` 无 `duplicate_credential`；QQ 消息 session key 为 `agent:lover:qqbot:...`；default 侧 QQ 配置已清空 |
| **M4** | `hermes -p lover plugins list` 显示 enabled；`capabilities` 能列出工具；插件加载无告警 |
| **M5** | **TS/Python 对拍数值逐位一致**（`decayToward` 尤其） |
| **M6** | 结束会话 → 重开 → `digital_life_status` 数值**完全一致**（这是与 DSH 的核心差异） |
| **M7** | 危机期优先召回危机记忆，但**记忆条目一条不丢** |
| **M8** | 缺口可开、可闭合；闭合后张力清零且记 `closed_by`；同义执念不产生双胞胎 |
| **M9** | 连续快速发 3 条 → 多巴胺上升（回复更活跃）；模拟失败 → 皮质醇上升（回复变冷淡变短），用 `digital_life_status` 核对 |
| **M10** | 连续 10 轮人格一致，无「AI 味」回退；回复中**不出现**「小 claw」「助手」「有什么可以帮你的」「✨」「DeepSeek」「蓝色大肥鱼」 |
| **M11** | 用户未发言时**主动发消息**；连发不产生重复唤醒；`running` 状态不投递；不触发 QQ 频率限制 |
| **M12** | `acp_status` 显示代理健康；长会话 token 消耗明显低于不装时 |

### 6.2 整体验收（全部模块完成后）

1. **跨会话记忆（核心卖点，必须验证）**
   告诉 ta 一件具体的事（如「我下个月要去青岛出差」），**几天后**在新会话问「我最近要出差去哪？」→ 能答出「青岛」。
   → **这是本项目相对 DSH 的根本改进点，是首要验收项。**

2. **人格隔离**
   - 恋人回复不含主 agent 特征（§6.1 M10）
   - default profile 会话里问「你的女朋友叫什么」→ 应答不出或答不出 lover 的信息
   - default 会话读 `profiles/lover/SOUL.md` → 被拒绝

3. **主 agent 零污染**
   default 的 `MEMORY.md` / `USER.md` / `SOUL.md` / `cron/jobs.json` / `.env` 与开工前逐字节一致（sha256 基线比对）。

4. **插件降级安全**
   把 `plugins/lifer/__init__.py` 临时改坏 → agent 仍能正常回复（不崩会话、不静默失效）。

5. **长期稳定**
   连续运行 7 天：状态无损坏、记忆无重复膨胀、cron 无重复投递、无会话泄漏。

### 6.3 回归检查（每个模块改完后）

| 检查 | 命令 / 动作 |
| --- | --- |
| YAML 未损坏 | `python3 -c "import yaml;yaml.safe_load(open('~/.hermes/config.yaml'))"` 且断言 `"\u" not in <人格文本>` |
| plugin.yaml 合法 | `hermes -p lover plugins capabilities lifer` |
| hook 名合法 | 对照 §4.4.1 的 `VALID_HOOKS`（写错**不报错，只静默不触发**） |
| gateway 健康 | `hermes gateway restart` + 读 `~/.hermes/gateway_state.json` |
| default 未被动过 | `sha256sum` 比对 |

## 7. 风险与已知陷阱

### 7.1 高优先级

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| **QQ bot 凭证只能属于一个 profile** | 一个 AppID 不能同时给 `default` 和 `lover` 用。`gateway/run_adapters.py:1167` 的 `_refuse_duplicate_claim` 会把重复凭证的 adapter 标为 `fatal` / `duplicate_credential`，**直接不启动** | 已决策为**exclusive 移交**：default 彻底删除 qqbot 配置与凭证，凭证只存 `profiles/lover/.env`。抹除清单见 §8.1，**先备份** |
| **profile_routes 静默不匹配** | YAML 裸数字 → int vs `SessionSource` 的 str | ID 一律**加引号**；上线后用 `logger.debug` 确认 route 命中 |
| **SOUL.md 无法按平台关闭** | `load_soul_identity=True` 在 `gateway/run_turn_runner.py` 硬编码，无 config 开关；`skip_context_files` 只跳过 cwd 上下文文件 | 只能**优先级压过**，不能关闭。恋人人格需在 override 里显式声明"不要用助手口吻" |
| **旧会话历史污染新人格** | 旧的「你好呀！小 claw 在 ✨」留在 transcript 里，模型会继续模仿 | 换人格后**必须让用户发 `/reset`**（agent 不能代发） |
| **billion-context 静默降级** | 代理起不来时不报错，只是没压缩 | 上线后用 `acp_status` 主动确认，不要"装完就认为生效" |
| **`hermes config set` 破坏非 ASCII** | 会把 `鲸` 写成字面量 `\u9cb8` | 写 YAML **一律用 Python `yaml.safe_dump(..., allow_unicode=True)`**，写完回读断言 `"\\u" not in value` |

### 7.2 中优先级

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| `ctx.effect` 无等价物 | Cordis 响应式作用域在 Hermes 不存在 | Phase 1 就改成显式调用；不要试图模拟响应式 |
| 主动消息额度 | QQ 官方机器人主动推送受限 | 低频（每天 2-3 次）；集中在用户活跃时段；准备"额度用尽"的降级话术 |
| 群聊身份不可见 | 群内模型看不到发送者姓名（`adapter.py:764-771`） | **恋人只跑私聊**；群聊场景单独评估 |
| nuwa 蒸馏成本 | 完整蒸馏是多 agent + 多轮搜索的长任务 | 先向用户确认档位；可先用低成本档产出骨架再迭代 |
| `profile create --clone` 剥离凭证 | 这是**正确**行为，不是 bug | 不要用 `--clone-channels`（活跃 gateway 下会被拒绝） |
| **QQ 凭证抹除不可逆** | default `.env` 的 QQ 四件套是唯一副本，搬完即从 default 消失 | §8.1 的顺序第 1 步强制备份 `.env`；M3.1 验收项专门检查「default 无 QQ 四行 / lover 有 QQ 四行」 |
| **default 侧 `platforms` 只剩 qqbot** | 抹除 `platforms.qqbot` 后 default 就没有任何 platform 块了 | 本机 `platforms` 顶层键实测只有 `qqbot`，所以安全。但**不要用 `strip_channel_config()`**——它会连带删掉 `gateway` 键（`gateway.multiplex_profiles` 住在那儿）。手动精确删 `platforms.qqbot` |

### 7.3 已知会「做不好」的地方（预期管理）

- **WebUI 体征可视化**：已排除。改用 `digital_life_status` 工具 + `digital_life_dashboard`（纯本地 Markdown 面板）
- **器官自举**（让 ta 自己写新器官）：已排除。Hermes 无热编译宿主；且自执行代码风险高
- **刷抖音形成真实日常**：已排除（Windows 限定）。若未来要补，可考虑用 Hermes `browser_exec` 做「打开抖音网页版 → 采样标题 → 写进兴趣记忆」的**降级版**（非自主观看，只是定时读一次推荐页），列为 Phase 5 候选

---

## 8. 决策记录（已定案，无需再确认）

> 以下决策由用户于 2026-11-01 拍板，实施 agent **直接执行，不要再问**。

### 8.1 QQ 账号方案：**复用现有 bot，抹除 default profile 的小鲸鱼配置**

**决策**：
- 复用现有 QQ bot `QQ_APP_ID 1903388994`（不再申请新 AppID）
- **抹除** default profile 里的「DeepSeek 小鲸鱼」人格与群聊配置
- QQ bot **唯一归属** `lover` profile，由 lover profile 独占该凭证

**为什么这样是安全的**（已查源码确认）：

Hermes 在 multiplex 下有**硬性凭证去重**（`gateway/run_adapters.py:1167` `_refuse_duplicate_claim`）：

```
Profile 'default' and 'lover' both configure qqbot with the same credential —
refusing to start the duplicate (one credential cannot be consumed twice).
```

冲突时该 adapter 被标记 `platform_state="fatal"` / `error_code="duplicate_credential"`，**直接不启动**。

所以「复用」的正确做法不是让两个 profile 都持有凭证，而是**exclusive 移交**：
default profile **彻底删除** qqbot 配置与凭证 → 凭证只存在于 `profiles/lover/.env` → 无冲突。

**抹除操作清单**（必须在 `hermes profile create lover --clone` **之后**执行）：

| # | 目标 | 动作 | 注意 |
| --- | --- | --- | --- |
| M1 | default `config.yaml` | 删除 `platforms.qqbot` 整个块（含 `channel_overrides` 的小鲸鱼人格 4533 字符 + `extra` 群聊配置） | 本机 `platforms` 下**只有 qqbot**，所以删整个 `platforms` 键是安全的。但**不要**用 `strip_channel_config()` ——它会连带删除 `gateway` 键（`gateway.multiplex_profiles` 就住在那儿） |
| M2 | default `config.yaml` | 删除 `platform_toolsets.qqbot`（`[web, vision, todo, memory, clarify]`） | 这是 default 侧的 qqbot toolset 配置；lover 侧要单独配 |
| M3 | default `.env` | 删除 `QQ_APP_ID` / `QQ_CLIENT_SECRET` / `QQ_ALLOWED_USERS` / `QQ_ALLOW_ALL_USERS` 四行 | **先备份**！这四个值要搬到 lover 的 .env |
| M4 | `profiles/lover/.env` | **写入** M3 备份的四个值（值不变，归属变更） | 这是 bot 真正的新家 |
| M5 | `profiles/lover/config.yaml` | 写 `platforms.qqbot`（含 `channel_overrides` 恋人人格 + `extra`）+ `platform_toolsets.qqbot` | 恋人人格见 §5 模块 M5 |
| M6 | default `channel_directory.json` | 删除 `platforms.qqbot` 两个条目（DM `F14CBB...` 与群 `931887...`） | default 侧不再有 QQ 通道 |
| M7 | `profiles/lover/` | 创建 `channel_directory.json`，写入上述两个条目 | 让 lover 侧认识自己的通道 |
| M8 | default `sessions/` | 归档（不是删除）QQ 相关会话历史 | 小鲸鱼的历史不污染恋人；建议 `mv` 到 `sessions/archive-whale/` |

**关键顺序**（错了会导致 gateway 起不来）：
```
1. 先备份 ~/.hermes/config.yaml 和 ~/.hermes/.env
2. hermes profile create lover --clone          # 自动剥离 messaging 凭证
3. 把 QQ 四件套写进 profiles/lover/.env          # M4
4. 在 default config.yaml 加 gateway.multiplex_profiles + profile_routes
5. 抹除 default 的 platforms.qqbot / platform_toolsets.qqbot / .env QQ 四行   # M1-M3
6. 处理 channel_directory.json 两边                # M6-M7
7. hermes gateway restart
8. 读 ~/.hermes/gateway_state.json 确认无 duplicate_credential
```

**群聊去留**：小鲸鱼原来的群 `931887E28E0A070A048F7EFF10CD4920`（`group_policy: open`、`group_full_mode: True`、唤醒词含「小鲸鱼/DeepSeek/鲸鱼/大肥鱼」）。
**默认建议：恋人只在私聊活动，群聊关闭**（`group_policy: pairing`，不配 `group_full_mode`）。
理由见 §7.2：群内模型看不到发送者姓名（`adapter.py:764-771`），且群消息会快速烧掉 60/qpm 配额。若用户想保留群聊，需在 lover 的 `extra` 里换一套唤醒词（去掉「鲸鱼/DeepSeek」）。

> ⚠️ **抹除前必须备份**。default `.env` 的 QQ 四件套是唯一副本，抹掉后没有第二次机会。

### 8.2 交付范围：**一次性做全，不分期**

**决策**：不分 Phase，所有模块（人格、内分泌、记忆、执念、主动性、压缩）**一次交付完整功能**。用户会自行拆分成多个任务分发给不同 agent。

**因此本 PRD 的 §5 改为「模块清单」而非「阶段」**：
- 每个模块有**明确的输入依赖**和**独立的验收标准**
- 模块之间标注**依赖关系**，便于并行拆分
- §6 的验收从「按阶段」改为「按模块 + 整体」

**模块独立性原则**：
- M1-M4（环境/隔离/QQ 移交）**必须最先做**，是所有后续模块的地基
- M5-M9（内核模块）**彼此基本独立**，可并行开发，共享 `ctx.state` 的数据契约
- M10-M12（人格/主动性/压缩）依赖 M5-M9 的接口，但可并行

**共享数据契约**（并行开发时各模块必须遵守，否则拼不起来）：

```python
# profiles/lover/plugins/lifer/contract.py
# 所有模块通过 ctx.state["lifer"] 下的固定键通信

STATE_KEY = "lifer"
# {
#   "chemistry": {"dopamine": float, "cortisol": float, "serotonin": float, "oxytocin": float},
#   "energy": float,
#   "temperament": {"sensitivity": 1.0, "resilience": 1.0, "enthusiasm": 50.0, "metabolism": 1.0},
#   "last_interaction_at": float,       # epoch ms
#   "gaps": [ {"id": str, "summary": str, "tension": float, "created_at": float,
#              "status": "open"|"closed", "closed_by": str|None} ],
#   # commitment / wake：设计时的预留键，本实现未做，已从状态中移除
#   "epidemic_guard": {"emergency_stop": bool},
# }
```

> **并行开发的唯一硬约定**：模块之间**只通过 `ctx.state[STATE_KEY]` 通信**，不要互相 import 对方的内部函数。这样 M5（化学）被改写时，M7（记忆）不会崩。

---

## 9. 附录：关键源码索引

实施 agent 开工前建议按此顺序阅读：

```
# 1. 插件契约（必读）
~/.hermes/hermes-agent/hermes_cli/plugins.py:230-1027   # PluginContext 全部 API
~/.hermes/hermes-agent/hermes_cli/plugins_discovery.py:189-220  # 插件目录扫描
~/.hermes/hermes-agent/hermes_cli/plugins_loader.py:465         # register_tools 约定

# 2. 隔离与路由（必读）
~/.hermes/hermes-agent/gateway/profile_routing.py:63-192        # ProfileRoute 与匹配规则
~/.hermes/hermes-agent/hermes_cli/profiles.py:1352-1420         # create_profile
~/.hermes/hermes-agent/hermes_cli/profile_channels.py:480-500   # clone_channels 拒绝逻辑
~/.hermes/hermes-agent/hermes_constants.py:399-425              # profile 身份标记

# 3. 算法参考（逐字翻译对象）
<工作区>/digital-life-core/src/chemistry-core.ts              # ★ 最重要，全文读
<工作区>/digital-life-core/src/chemical-pool.ts               # 激素池
<工作区>/digital-life-core/src/genome.ts
<工作区>/digital-life-core/src/genome-file.ts
<工作区>/digital-life-core/src/organs/memory-affinity.ts      # 记忆重排权重
<工作区>/digital-life-core/src/epiphany.ts                    # 顿悟
<工作区>/digital-life-core/src/sleep-energy.ts
<工作区>/digital-life-core/src/host-metrics.ts
<工作区>/digital-life-core/genome.json                        # 激素出厂定义

# 4. 平台与配置事实
~/.hermes/skills/openclaw-imports/qqbot-platform/SKILL.md        # QQ 平台事实
~/.hermes/skills/autonomous-ai-agents/hermes-channel-persona/SKILL.md  # 人格注入 4 个坑
~/.hermes/skills/devops/gateway-channel-diagnostics/references/per-channel-personas.md
~/.hermes/skills/openclaw-imports/proactive-agent/assets/SOUL.md
~/.hermes/skills/openclaw-imports/proactive-agent/assets/HEARTBEAT.md
<工作区>/nuwa-skill/SKILL.md
<工作区>/nuwa-skill/examples/andrej-karpathy-perspective/SKILL.md  # 产物格式样例

# 5. 上游在线文档
https://github.com/ranxianglei/billion-context/blob/master/CLIENTS.md#hermes-nous-research
```

---

## 10. 给实施 agent 的执行守则

1. **先读源码再写代码。** 本 PRD 的所有 API 名称、路径、行号都来自实测，但**行号会随 Hermes 升级漂移**——以符号名（`PluginContext.register_system_prompt_section`）为准，不要死记行号。
2. **每个模块交付时必须跑该模块的验收项**（§6.1 表）。不通过不交付。
3. **隔离性验证（M2.4）是硬门槛。** 不通过就停下报告，不要硬着头皮往下做。
4. **不要修改 `default` profile 的任何文件**（`SOUL.md` / `memories/` / `cron/jobs.json` / `.env`），**唯一例外是 M3 的抹除清单**（§8.1 的 M1-M3、M6、M8）——那是用户明确批准的 QQ 移交操作。改 `config.yaml` 加 `profile_routes` 允许（只增不减）。
5. **抹除前必须备份。** `~/.hermes/.env` 里的 QQ 四件套是唯一副本，抹掉后无法恢复。
6. **写 YAML 用 Python `yaml.safe_dump(..., allow_unicode=True)`**，不要用 `hermes config set`。
7. **改完配置要回读校验**，并 `hermes gateway restart` 后读 `~/.hermes/gateway_state.json` 确认生效。
8. **验证要拿真实输出。** 不接受"应该可以"作为完成依据——每个验收项都要有实际执行结果。
9. **遇到与本 PRD 冲突的实际情况，以实际源码为准**，并在交付报告里写明偏差。
10. **并行开发时只通过 `ctx.state["lifer"]` 通信**（契约 §8.2），不要互相 import 内部函数。
