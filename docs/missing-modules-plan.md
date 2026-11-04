# 补齐 PRD M5–M12 缺失模块：实施计划（交给 codex）

## 0. 背景与硬约束

PRD.md §8.2 写的是"一次性做全，不分期"，当前实现只覆盖了一部分：M5 的三种化学/衰减、M6 持久化、M7 记忆重排、M8 的 open gap 注入与"不知道"捕获、M9 的 affect 注入与状态工具、M10 人格、M11 的 beat 主动消息。**下面的缺口对照 PRD 原文逐条列出，不是新增需求。**

必须遵守的约束（违反任意一条即视为未完成）：

1. 不改宿主 Hermes 源码（`~/.hermes/hermes-agent` 只读参考）。插件能力边界以 `register_tool` / `register_hook` / `register_middleware` / `register_system_prompt_section` 为准。
2. **不新增"模型/用户可任意指定激素、能量"的工具**（PRD 明确禁止）。所有状态变更只能来自真实事件：请求、回合完成、失败、工具结果、时间流逝。
3. 现有 88 个测试必须继续通过。runner：`cd tests && PYTHONPATH=.. python3 -m unittest test_domain test_events test_proactive test_memory`；**pytest 不被支持**（tests/ 没有 `__init__.py`）。
4. 状态 schema 变更必须在 `normalize_state` 内完成迁移且**永不抛错**；旧 state（含已删除的 `commitment`/`wake`）必须能读。
5. 每次改动后：`hermes plugins validate .` 通过；脚本/文档同步到根副本 `/Users/pengyu/workSpace/hermes-lover`（根副本无 git）；在 `hermes-digital-life` 内提交。
6. 每阶段一个 commit，消息说明"对应 PRD 哪一条"。

## 1. 缺口清单（PRD → 现状 → 目标）

| PRD | 现状 | 目标 |
| --- | --- | --- |
| M5.3 `genome.py` | 缺；化学参数硬编码在 `digital_life/domain.py:18 DEFAULT_CHEMICALS` | 新增 `digital_life/genome.py`：激素声明 `baseline/min/max/half_life_minutes/rules`，`parse_genome()` 坏字段降级不抛错 |
| M5.4 `chemical_pool.py` | 缺；只有固定 3 种化学 + 固定衰减 | 新增通用池：惰性衰减（读时按时间差）、规则引擎（事件 → 激素增减）、内部 `inflict()` 语义（AI 自己造激素，**不是工具**） |
| M7.1 记忆锚点 | 重排有了（`domain.py:270 reorder_formatted_memory_blocks`），但宫殿条目没有写入时的激素快照 | `capture()` 时记录 `anchors`（cortisol/dopamine/serotonin），recall 时按当前激素做 ×3/×0.1 注意力倾斜 |
| M7.4 应激压制 | 缺 | 皮质醇 ≥80：在 system prompt **最前**注入"先处理危机"，并声明"记忆仍在、只是靠后"；≤400 字符 |
| M8.1 张力随时间长 | `_gap_tension` 已随时间增长（确认后在测试里加断言） | 明确半衰/增长曲线，补测试 |
| M8.3 顿悟 | 缺 | 新工具"首次成功"→ 对 open gaps 做模糊匹配（CJK 二元组 + 词面重叠）→ 命中则闭合（张力清零、`closed_by="epiphany:<tool>"`） |
| M8.4 同义兄弟闸门 | 缺 | 唤醒复核时，若命中的 open gap 与近期闭合的某条是同义 → 取消唤醒并一并闭合 |
| M8.5 同义复活 | 缺 | 已闭合 gap 的同义文本再次出现 → 复活**原条目（保留 id）**，不产生 open/resolved 双胞胎 |
| M8.6 未解注入 | 已有（`runtime.py append_open_loops`，top-3 ≤400） | 保持；补"张力最高的那条"优先级测试 |
| M9.6 host metrics | 缺 | 新增 `digital_life/host_metrics.py`：CPU/负载/内存（`psutil` 可选，缺则降级 `os.cpu_count`/`loadavg`），作为"身体状态"注入 affect 段；不改变模型可写边界 |
| M11.3–M11.8 唤醒系统 | 缺（当前只有 beat 定时发消息，没有"执念驱动的自主唤醒"） | 见 P4：需要宿主 `allow_gateway_injection`，做成 opt-in、可禁用、可测试；没开就整体不生效 |
| M12 billion-context | 缺（宿主没装 bili） | 只做探测 + 文档 + 优雅降级，不改宿主、不装第三方 |

## 2. 分阶段计划

### P0 化学内核泛化（1 天）
- 新增 `digital_life/genome.py`、`digital_life/chemistry.py`（从 domain.py 抽出 `clamp/decay_toward/apply_metabolism`）、`digital_life/chemical_pool.py`。
- `chemical_pool.py` 接口（内部 API，不注册工具）：
  - `ChemicalPool(genome)`；`pool.decay(state, now_ms)`；`pool.apply(event, state)`；`pool.inflict(state, chemical, amount)`。
  - 事件类型至少：`request`（能量扣减）、`turn_complete`（多巴胺 +5）、`provider_error`（皮质醇 +20）、`tool_failure`、`tool_success`、`idle`。
- `domain.py` 保留 `DEFAULT_CHEMICALS` 作为 genome 默认值来源；`default_state/normalize_state` 改走 pool。
- 测试：`test_chemistry.py` 新增；旧 88 测试全绿；旧 state payload（含 commitment/wake）能读。
- 验收：衰减单调性、clamp 边界、事件效果与现有行为等价（对拍现有测试断言）。

### P1 执念池生命周期（1–2 天）
- 新增 `digital_life/gaps.py`：`propose_gap`、`close_gap(kind, by)`、`match_gap(text, open_gaps)`（CJK 二元组 Jaccard）、`revive_gap(closed, text)`、`echo_suppressed(now)`。
- 接线：
  - `runtime.observe_tool_result`：工具首次成功 → 收集当回合 gap 候选 → `epiphany` 闭合；失败/超时 → 张力 +N 或新 gap。
  - `runtime.observe_provider_error` → 皮质醇 +20（已有）。
  - 唤醒入口（P4）在注入前调用 `match_gap` 复核。
- 状态：gap 增加 `closed_by`、`revived_from`；`closed_at`。
- 测试：闭合保留 `closed_by`；同义复活保留 id；10 分钟回声抑制；张力上限。
- 验收：`hermes-lover status` 里 gap 的 `status/closed_by` 可见。

### P2 应激压制 + 记忆锚点（1 天）
- `domain.py` 新增 `stress_directive(cortisol)` → 返回 ≤400 字提示；在 `_affect_request` 的 system 段最前注入（顺序：压力指令 → 情绪指令 → 记忆）。
- `memory.capture()` 写 `anchors`；`MemoryStore.recall(..., cortisol=…)` 排序加权 ×3（crisis/高皮质醇）/×0.1（日常低张力）。
- 测试：皮质醇 80 边界；锚点写入/读取；重排字节保持。

### P3 host metrics（0.5 天）
- `digital_life/host_metrics.py`：`snapshot()` 返回 `{cpu, load1, mem_used_pct, disk_free_pct}`；异常返回 `{}`（绝不抛错）。
- 在 affect 段追加一行"身体状态"（限 1 行、≤160 字符）。
- 声明 `python_dependencies: [psutil]` 于 `plugin.yaml`（Hermes 只校验不安装；缺包走降级）。

### P4 唤醒系统（2–3 天，opt-in）
- 前置：`plugin.yaml` 增 `plugins.entries.hermes-lover.allow_gateway_injection: true` 的说明；代码里 `inject_message` 不可用时整体跳过并打日志。
- 新增 `digital_life/wake.py`：四重锁（1 秒重复丢弃、投递前重读磁盘按 id 重查、闭合时删队列、`agent.status==running` 丢弃）、气泡正文只 `...`、执念详情进 system prompt `digital-life:wake` 段、行为承诺（`done` 核对：解决→张力清零，未解决→+10）。
- 接线：`post_llm_call` 末尾调用 `wake.maybe_wake()`；钩子失败不得影响正常回复。
- 测试：四锁各自用例（用 fake host 注入器）；未开 injection 时零副作用。
- 验收：CLI 里手工触发一次，能在下回合 system prompt 里看到 wake 段。

### P5 M12 billion-context（0.5 天）
- 不做安装：`scripts/detect-billion-context.py` 探测宿主是否安装，README 增一节"如何自行安装/关闭"，失败静默降级。
- 验收：探测脚本返回 JSON `{installed: bool, version: str|null}`。

## 3. 依赖与顺序

```
P0 (genome/pool) ─┬─> P1 (gaps/epiphany) ─┬─> P4 (wake)
                  ├─> P2 (stress/anchors) ─┘
                  └─> P3 (host metrics)
P5 独立
```

P4 依赖 P1（需要 gap 匹配）与 P2（需要压力指令）；P3 可随时插入。

## 4. 整体验收（每个阶段结束都要跑）

1. `cd tests && PYTHONPATH=.. python3 -m unittest test_domain test_events test_proactive test_memory test_chemistry test_gaps` 全绿。
2. `hermes plugins validate /Users/pengyu/workSpace/hermes-lover` → Validation passed。
3. `hermes -p hermes-lover plugins doctor hermes-lover --ci` 无 WARN。
4. 根副本同步：`for f in $(git -C hermes-digital-life ls-files); do cmp -s "$f" "/Users/pengyu/workSpace/hermes-lover/$f"; done`（只容 docs/ 新文件缺根副本）。
5. 重启网关后 `hermes-lover status` 返回 JSON，`/core` 正常。
6. 文档：`使用引导.md`、`README.md` 增加新模块一句话说明；`openspec/specs/digital-life-runtime/spec.md` 增需求条目。

## 5. 明确不在本轮范围（写清楚，避免蔓延）

- 器官自编译/热编译、WebUI、抖音浏览、QQ 小号。
- 上游 TS 逐位对拍：上游源码（`src/chemistry-core.ts`、`src/chemical-pool.ts`、`src/epiphany.ts` 等）**不在本机**，无法做"数值逐位一致"；用 PRD 语义 + 属性测试替代，并在此文档标注偏差。
- 催产素、M12 插件安装、任何宿主补丁（Studio 补丁已独立存在，不属于本计划）。

## 6. 给 codex 的执行方式

1. 先读 `PRD.md` §5 M5–M12、`openspec/changes/archive/2026-11-02-build-hermes-digital-life-plugin/design.md`、当前 `__init__.py`/`digital_life/*.py`。
2. 按 P0→P5 顺序实现，每阶段一个 commit；每阶段结束按 §4 自查并贴出命令输出。
3. 遇到宿主能力缺失（M11.3 injection、M12 bili）**不要伪造**：记录 `BLOCKED` 与需要用户做的动作，继续下一阶段。
4. 所有新增行为必须有测试；不删现有测试；不改测试 runner。

## 7. 实现记录与偏差（本轮 P0–P5）

- 上游 `digital-life-core` 的 TS 源码不在本机，未做逐位对拍；P0/P1 用 PRD 语义 + 属性测试覆盖
  （衰减参考向量、事件规则、张力增长、同义复兴 id 稳定性）。
- 偏差 1：`resilience` 下限保留 0.5。PRD 写作 0.1，但现有测试
  `test_temperament_normalization_matches_default_ranges` 断言 0.5–2.0；改下限会破坏既有契约，
  故沿用 0.5 并在 `tests/test_chemistry.py` 注明。
- M11.3 `inject_message`：宿主存在该能力，但需要
  `plugins.entries.hermes-lover.allow_gateway_injection: true` 且当前有活跃网关；已按"opt-in +
  不可用即静默跳过"实现，未改宿主配置。因此**不是 BLOCKED**，默认关闭。
- M12 bili：本机未安装（`import bili` → ModuleNotFoundError）。按计划只做探测
  `scripts/detect-billion-context.py`，不安装、不启用，返回 `{"installed": false, "version": null}`。
- `hermes plugins doctor` 的 `⚠ install out of sync (venv: out of sync with uv.lock)` 是宿主
  `~/.hermes/hermes-agent` 在更新中途的既有状态，与插件无关；未运行 `hermes pm install`
  （不改宿主）。
