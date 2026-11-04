# 记忆宫殿升级计划：原子事实 · 混合检索 · 冷层摘要

## 0. 背景

对照腾讯云 Agent Memory（四层金字塔 + 混合检索 + 白盒溯源，见 `docs/memory-comparison.md`），
本地记忆宫殿（`digital_life/memory.py`）目前只有两层：原始 turn + 日记（journal），
检索只有 FTS5 字面匹配，溯源只到 `source_session_id`。

本计划只补三项**性价比最高**的能力，不做四层全量蒸馏、不接云端、不改宿主。

## 1. 硬约束（违反任一条视为未完成）

1. 不改宿主 `~/.hermes/hermes-agent` 源码；只经 `register_tool` / `register_hook` /
   `register_middleware` / `register_system_prompt_section` 扩展。
2. 不新增"模型可任意写状态"的工具；`memory_write` / `memory_recall` 的权限边界不变。
3. 测试 runner 固定：`cd tests && PYTHONPATH=.. python3 -m unittest test_domain test_events test_proactive test_memory`
   + 新增 `test_memory` 用例；pytest 不支持（tests/ 无 `__init__.py`）。
4. **CLI 调用一律加 `HERMES_DISABLE_LAZY_INSTALLS=1`**：宿主 launcher 当前卡在
   "completing source-update dependencies"（venv 与 uv.lock 不同步），不加会挂死。
   `hermes plugins validate` 同样加。
5. 根副本同步：`/Users/pengyu/workSpace/hermes-lover/`（无 git），逐文件 `cmp`。
6. 每阶段一个 commit，消息写明对应 PRD / 本计划条目。

## 2. P0 · 原子事实层 + 溯源（最高优先）

目标：turn 归 turn，事实归事实；日记与 recall 都能指向具体事实，事实再指向原始 turn。

- 新增 kind `fact`，写入路径：
  - 每轮 `capture_turn()` 之后，延迟到 **当日第一条 QQ 消息** 或 **每日一次**，
    对该日 `turn` 做一次提炼：1–3 条原子事实，格式 `{"kind":"fact","text":"…","source_ids":["hp-…","hp-…"]}`。
  - 提炼不调用新工具：复用现有 `journal_check` 的触发时机，在返回的提示里要求模型
    `memory_write(kind="fact", text="…", source="hp-…,hp-…")`；或新增可选参数 `sources`。
- `memory_write` schema 增加可选 `sources: string[]`（仅 fact 有效），校验 id 存在。
- `memory_recall` 返回值增加 `sources` 字段：fact 返回其 source_ids，journal 返回它引用的 fact id。
- 日记写入时，prompt 要求引用事实 id（`memory_write(kind="journal", refs=[...])`）。
- 去重键：`(date, text, chat_id)` 对 fact 同样生效；同文本再次出现只 touch。
- 测试：提炼→fact 落盘→recall 返回 sources→journal refs 可追溯；坏 source id 忽略不抛错。

验收：
- `memory_recall(kind?)` 能看到 fact；
- 对一条 journal 能拿到 `sources` 与非空 `refs`；
- 166+ 测试全绿。

## 3. P1 · 混合检索（关键词 + 向量 + RRF）

目标：中文同义/口语改写能召回；当前 FTS5 只匹配字面。

- 接口：`MemoryStore.recall(query, ..., hybrid=True)`；`hybrid` 默认开，失败自动降级 FTS5。
- 向量来源（按可用性排序，全部可缺省）：
  1. 本地 `sentence-transformers` / `bge-small-zh`（若已安装）；
  2. 宿主可用的 embedding API（读 profile `.env`，缺 key 就跳过）；
  3. 都没有 → 纯 FTS5，`recall` 仍返回 `"mode":"fts"`。
- 索引：新表 `vectors(id, dim, blob)`，与 `entries` 同库；写入 fact/journal 时同步写向量，
  失败只告警不阻断（与现有 `_save_index` 语义一致）。
- 融合：RRF（k=60），对 FTS 与向量各取 top 20，融合后按 RRF 分排序，再叠加现有
  `score()` 的权重/新近/注意力作为 tie-break；`matched` 仍按去重后的 id 计。
- 降级：向量库损坏/维度不符 → 清空 vectors 表、记录 warning、回退 FTS。
- 测试：同义词召回（熬夜 vs 通宵）、无 embedding 时降级、向量损坏降级、RRF 顺序稳定。

验收：
- `recall("通宵")` 能命中存了「熬夜」的 fact；
- 无网络/无 key 环境 166+ 测试全绿且 recall 正常返回。

## 4. P2 · 冷层摘要

目标：archived 条目不再永久占检索面；不是删除，是"压缩成摘要 + 保留原文"。

- 触发：手动子命令 `hermes-lover memory-compact --days 30`（CLI，不是模型工具）。
- 规则：`archived=1` 且 `last_access_at` 超过 N 天 → 按 `(date, chat_id)` 分组，
  每组生成 1 条 `kind="summary"`（≤200 字，引用组内 id），原文件保留，索引里标记
  `compacted_into=<summary id>`；`include_cold` 时只返回摘要，不展开原文。
- 幂等：同一组不重复生成；摘要写入失败保留原状态。
- 测试：分组、幂等、include_cold 只出摘要、原文仍可按 id 读取。

验收：
- 连续两次 compact 不产生重复摘要；
- `include_cold=True` 的条目数下降，`total` 不变。

## 5. 明确不做

- 四层金字塔的"场景记忆/核心记忆"全量蒸馏（成本高，日记已覆盖大半）。
- 云端托管、多租户/团队共享、权限资产模型。
- 短期记忆窗口管理/冷数据卸载（宿主已做，会打架）。
- 重排 `MEMORY.md`（`domain.py:270`）——与本计划无关，别动。

## 6. 执行顺序与验证

1. P0 → 跑 §1.3 测试 + `HERMES_DISABLE_LAZY_INSTALLS=1 hermes plugins validate .` + 根副本 cmp。
2. P1 → 同上，另加降级用例。
3. P2 → 同上，另加 CLI 冒烟：`hermes-lover memory-compact --days 30` 两次。
4. 全部完成后：`HERMES_DISABLE_LAZY_INSTALLS=1 hermes -p hermes-lover gateway restart`，
   `hermes-lover status` 返回 JSON，`/core` 正常。
5. 文档：`README.md` 记忆一节补一句；`docs/memory-comparison.md` 的"建议路线"标注完成状态。
