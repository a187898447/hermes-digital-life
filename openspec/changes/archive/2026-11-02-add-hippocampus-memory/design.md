# 设计：插件事件记忆库（海马体）

## 结论

两个层次分开：Hermes 的 `MEMORY.md`/`USER.md` 管"用户画像与稳定事实"；插件的事件库管"经历、决定、日常"。本期只做事件库的写入、检索、重建与衰减，不做每轮注入。

P0 交付：`memory_write`、`memory_recall`、JSONL 重放、bigram 检索、`rebuild_index()`、评分与衰减、测试。
P1：middleware 注入 top-2（≤220 字、只读、不刷新访问时间）。
P2：forget/compact CLI、冷仓物化、更细的来源审计。

## 存储布局

```text
plugin-data/hermes-lover/
├── memory/
│   ├── .lock
│   ├── 2026/11/2026-11-02.jsonl      # add / touch / forget 事件
│   └── cold/2026-11.jsonl            # 可选物化，不是真相
└── memory-index.db                   # FTS5 + entries 表，可删除重建
```

索引使用 `plugin_db("hermes-lover", filename="memory-index.db")`，沿用宿主的 profile 隔离与 SQLite 连接管理。宿主支持 filename 参数，但只允许单个文件名，不能传目录。

## D1 文件格式：JSONL 事件日志（已定）

```json
{"op":"add","id":"hp-20261102-0001","kind":"daily","weight":1.0,"cortisol":50,"created_at":1762000000000,"text":"今天下午在群里聊到熬夜"}
{"op":"touch","id":"hp-20261102-0001","last_access_at":1762003600000,"hits":1,"weight":1.2}
{"op":"forget","id":"hp-20261102-0001","archived_at":1764600000000}
```

- `id` = `hp-<YYYYMMDD>-<4 位序号>`，同日递增。
- `add` 保存完整初始元数据（含 `source_session_id`、`last_access_at=created_at`、`hits=0`）；`touch` 保存更新后的绝对 `last_access_at`、`hits`，重复写入时还保存绝对 `weight`；普通 recall 的 touch 不改变 weight。`forget` 只标记归档。
- 事件写入操作当天的日文件，引用的 id 可属于之前日期；日期、ID、额度使用同一次操作捕获的宿主本地日期。重放按文件日期升序、文件内行顺序 fold 到各 id，不按 id 或事件时间重排。重复 add ID 和无对应 add 的引用跳过并告警。截断末行只读重放时忽略并 warn，不中断重建。
- 人工编辑允许追加合法行；非法行跳过。
- 拒绝 Markdown 行格式：元数据与正文需要分隔符，正文可含 `]`，无法无歧义解析。

## D2 中文检索：字符 bigram（已定）

- 写入时把正文转成 gram 串：CJK 取所有相邻二字，ASCII 取小写词，写入 FTS5 `grams` 列（`unicode61`）。
- 查询同样转 gram，空格连接（隐式 AND）；单字查询走受限 `LIKE '%x%'`；混合文本按中文/英文分别转换。
- 不删除输入中的 OR/AND/NOT、引号或连字符来规避语法。对生成的每个 gram/token 使用 FTS5 双引号字面量并将内嵌双引号加倍，以 AND 组合并参数化执行；原始 query 永不直接作为 MATCH 表达式。标点按统一分词规则作为边界，因此是词元检索，不承诺严格子串或语义匹配。
- 单字查询和非空但不能生成 token 的查询走字面子串 LIKE，参数绑定并转义 `%`、`_`、反斜杠，指定 ESCAPE；不得将纯标点查询退化成空查询返回全部。
- 不用 trigram 分词器：要求 ≥3 字符，`熬夜`/`群里` 会失效。
- 单测固定覆盖：两字词、三字词、整句、单字、中英混排、含引号与运算符的输入。

## D3 一致性：单写入协议（已定）

```text
flock(memory/.lock, EX)
  ├─ 扫描当天 JSONL 得到下一个序号（索引坏了也能分配）
  ├─ 追加一行完整 JSON（O_APPEND）并 fsync
  ├─ 在 SQLite 事务中更新索引
  └─ 释放锁
```

- 追加前在独占锁内检查末行：完整 JSON 缺换行则补换行；截断尾部先保存到不参与重放的恢复文件，再截断至最后完整换行，然后追加新事件。处理短写/中断，不把 O_APPEND 当作任意长度写入的原子保证。
- 索引写失败不回滚已 fsync 的文件；返回已持久化 ID 并标明 `indexed=false`。下次正常操作先同步日志再执行去重、额度检查或查询，避免把落盘未索引事件当成不存在。
- `rebuild_index()` 在同一锁和一个 SQLite 事务内删表、建表、完全重放，失败回滚；只读权威日文件，永不重放 cold 或恢复文件。同日同文本去重时追加含更新后 weight 的 touch，不追加第二个 add。
- P0 只支持 forget 事件的重放和 archived 过滤，不提供归档生成入口、不物化冷仓；这两项明确留 P2。未来 cold 只从日事件重放结果生成。
- 同进程线程互斥与跨进程 flock 使用同一锁顺序，覆盖去重、额度、序号、touch 和重建。同步可维护已处理文件偏移；人工修改或删除已有行必须显式 rebuild，不用 ID 集合比较冒充完整一致性检查。

## D4 急停语义（已定）

`memory_recall` 两种模式：

- 普通：返回结果，对展示条目追加 `touch`，更新 `hits`/`last_access_at`。
- 急停：照常返回，不追加 `touch`、不修索引、不执行 forget/compact、不写任何文件或 SQLite。
- 急停且索引缺失、过时或损坏时，只读日事件在内存中重放/检索，不创建目录、索引或 WAL/SHM；实现阶段验证该降级路径。记忆工具读取生命状态须使用不结算、不初始化落盘的只读路径，不能调用会写入的 status_snapshot。
- P1 的 middleware 注入始终只读。

## D5 写入上下文（已定，接受宿主限制）

Hermes 0.22.5 的普通插件工具只拿得到 `task_id`、`session_id`、`user_task`（`model_tools.py:820-826`）；`agent_context` 属于 `MemoryProvider.initialize`（`agent/agent_init.py:1180`），不在工具分发链路。

因此 v1 不做来源隔离，做可审计降级：

- 每条 `add` 记 `source_session_id`，缺失写 `"unknown"`。
- 每会话每日最多 20 条新 `add`；去重产生的 `touch` 不占额度；超限返回可解释错误。
- `memory.writes_enabled` 是插件自己的 profile 配置（默认开），不是 Hermes 内置 memory 配置；manifest 声明并通过宿主插件配置 API 读取。关闭时拒绝 memory_write（含重复加权），recall 访问记录仍允许更新；急停才冻结全部记忆写入。
- README 写明已知限制；将来宿主注入 platform 后再加 `cron/subagent` deny-list。

## D6 评分与衰减（已定）

```text
query 非空: score = 0.40*rel + 0.20*min(1.5, effective_weight) + 0.15*recency + 0.25*rel*mult
query 为空: score = 0.55*min(1.5, effective_weight) + 0.25*recency + 0.20*attention_bias
```

- `strength=max(0,-bm25)`；`rel=strength/(1+strength)`，有限输入下单调且在 [0,1)；LIKE 命中使用 rel=1。不得直接使用 1/(1+bm25)。常见词 BM25 强度可能很小，此映射不保证相关度绝对压过权重。
- `effective_weight=weight * 0.5^(max(0,idle_days)/7)`；`recency=1/(1+max(0,age_days)/30)`，沿用上游，不用指数 recency。idle 从 last_access_at 算，age 从 created_at 算。
- 分支以 query 是否为空选择，不以 rel 是否等于 0 选择；同分按 created_at 降序、id 升序稳定排序。
- `attention_bias = clamp((mult-0.1)/2.9, 0, 1)`。
- 三档注意力：皮质醇 ≥80 → crisis 3.0 / daily 0.1；≥55 → 1.8 / 0.4；否则 1.0。
- 空查询单独一支，使高皮质醇时危机记忆在自动回忆中上浮。
- A 类：有效权重七天半衰期、上游 recency、三档注意力、非空查询评分系数。B 类：BM25 relevance 映射、空查询评分/attention_bias、按天 JSONL、bigram、kind 默认权重、同日去重、写入额度与开关；P1 的 220 字符预算亦为 B 类。写进 README 或注释。

## D7 冷仓与遗忘

- P2 归档条件为检查时 effective_weight <0.08 且 idle_days>=30，不要求记录“连续低于”的时长。P0 仅重放已有 forget 事件为 archived，不因评分衰减自动归档。
- `include_cold=false` 默认不查归档；`=true` 时参与检索与排序。
- 冷仓物化文件与事件日志不一致时，以事件日志重放结果为准。

## D8 工具契约

`memory_write`：

```json
{"kind": "daily|crisis|fact|decision|note", "text": "string", "weight": 1.0}
```

返回 ok、id、date、created_at、duplicate、indexed；重复文本保留 id，weight=min(3.0,weight+0.2)，刷新 last_access_at，hits 不增加，将新 weight 写入 touch。
- text 必须是字符串，去首尾空白后为空则拒绝；规范化后最多 4000 个 Unicode 字符，超限提示拆分；保留内部换行与空白。
- kind 默认 note，限 crisis/daily/fact/decision/note，非法 kind 拒绝。默认权重分别为 2.0/1.0/1.2/1.5/0.8；显式 weight 为有限数字，拒绝 bool/NaN/Infinity，夹取 [0.2,3.0]。
- 同日规范化文本精确去重，不区分来源 session 或 kind；20 条额度只计 source_session_id 对应的新 add，重建后额度不丢失。source_session_id 仅取宿主上下文，缺失为 unknown。

`memory_recall`：

```json
{"query": "string|null", "k": 8, "include_cold": false}
```

返回：

```json
{"total": 120, "matched": 8, "returned": 8, "shown": [{"id","kind","weight","text","created_at","hits","source_session_id"}]}
```

- total=未归档总数，与 query 无关；cold_total=归档总数；matched=本次 include_cold 过滤后截取前匹配数；returned=展示数。include_cold 不自动解档。shown 另含 date、score、effective_weight、last_access_at、archived；返回 attention。
- 普通 recall 对展示项追加 touch，hits+=1；评分用访问前快照，返回访问元数据用提交后值。空结果不写 touch。
- query 为字符串或 None（按空串）；k 默认 8，必须为非 bool 整数并夹取 [1,20]；include_cold 必须为布尔值。

## D9 验收

- 先运行当前 unittest 确认实际基线（近期新增过多块 system 的修复，不硬编码 49），再执行 `cd tests && PYTHONPATH=.. python3 -m unittest test_domain test_events test_proactive test_memory`。
- `hermes plugins validate .` 通过。
- 重放等价：随机生成 200 条事件 → 建索引 → 删除索引 → 重放，结果集完全一致。
- 中文检索：`熬夜`、`群里` 命中；含 `"`、`*`、`OR` 的查询不报错且按字面量处理。
- 急停：`emergency_stop=true` 时 recall 不产生文件/SQLite 写入（mtime 与事件行数不变）。
- 并发：两个线程各写 20 条，序号不重复、无坏行。
- 根拷贝同步后逐文件 sha256 一致；在 `hermes-digital-life` 提交。
- 补测 BM25 负值/接近 -1/强度单调、weight 影响排名、重复加权重建保留、损坏尾部再追加、索引失败恢复、冷仓不参与重放、跨进程额度/ID 并发、急停索引不存在/损坏时零磁盘写入。
- macOS/Linux 的 flock 为本期平台边界；FTS5 必须实际探测，缺失返回明确不可用错误，不宣称所有 Python 构建必然提供。
