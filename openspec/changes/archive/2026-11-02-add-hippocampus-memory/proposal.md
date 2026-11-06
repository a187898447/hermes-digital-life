## Why

Hermes 的内置记忆只有两块常驻文本：`MEMORY.md`（默认 2200 字）和 `USER.md`（默认 1375 字），每轮整段注入、写满只能批量合并删除。它适合放"用户画像和稳定事实"，但不适合放"某天下午在群里聊过熬夜"这类会持续增多的经历。插件目前只做两件事：按情绪重排已渲染的记忆块、把情绪参数写进请求；它没有自己的记忆。

结果是：过去聊过的事**找不回来**，除非刚好还在那 2200 字里。

本变更给插件加一个独立的事件记忆库（"海马体"）：按天追加、可全文检索、按遗忘曲线衰减、索引可随时重建。Hermes 常驻记忆的所有权不变，插件不接管 `MEMORY.md`/`USER.md`。

## What Changes

- 新增按天分片的追加式 JSONL 事件日志作为唯一记忆真相源，完整重放 add/touch/forget。
- 新增字符 bigram 全文索引（SQLite FTS5），支持两字及以上中文词检索，单字回退，原始 query 永不直接进 `MATCH`。
- 新增 `memory_write`、`memory_recall` 两个模型可调用工具；写入受 profile 开关与每会话每日额度约束。
- 新增按 7 天半衰期的权重衰减、三档情绪注意力、上游评分系数与空查询独立排序。
- 新增 `rebuild_index()`；索引可删除后只从权威日事件日志完整重放。P0 支持归档标记的重放/过滤，归档操作入口和冷仓物化留 P2。
- 急停时 `memory_recall` 进入只读模式：返回结果但不写访问记录、不修索引、不物化。
- 本期不注册 MemoryProvider，不改 Hermes 常驻记忆语义，不做 middleware 自动注入（留 P1）。

## Capabilities

### New Capabilities
- `hippocampus-memory`: 插件自有的事件记忆库——追加写入、按天分片、bigram 检索、衰减与冷仓、可重建索引、急停只读。

### Modified Capabilities

None.

## Impact

- 新增 `digital_life/memory.py`、`tests/test_memory.py`，改动 `__init__.py`（工具注册）、`digital_life/storage.py`（共用插件数据目录）、`plugin.yaml`（工具声明）、README 与 `使用引导.md`。
- 新增运行期文件：`plugin-data/hermes-lover/memory/`（事件日志、锁）与 `memory-index.db`（可重建）。
- 无新第三方依赖；要求运行时 SQLite 支持 FTS5，使用前实际探测。文件锁支持 macOS/Linux，Windows 不在本期范围。
- 根拷贝与 `hermes-digital-life` 需保持同步；实现在 `hermes-digital-life` 提交。
