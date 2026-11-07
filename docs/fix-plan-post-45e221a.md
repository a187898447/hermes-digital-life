# hermes-lover 增量安全修复（post-45e221a）

> 审计产物：`~/security-audit-skill/hermes-lover/post-45e221a-review.md`
> 审计基线：`45e221a`（2f3abe3 → 45e221a 的增量审查，HEAD 即基线，工作树当时干净，557 tests OK）
> 本文记录：P0 三项已实施（未提交）的边界与测试；P1/P2 的修法与估时。
> 写法沿用 `docs/fix-plan-run-4.md`：每项写「要守住的不变量 / 最后可信决策点 / 最小改动 / 回归测试」。

---

## 0. 结论先行

1. P0 三条不是三个孤立 bug，而是同一种失效：**「宿主自己产生的元数据」和「聊天里来的文本」被当成同一类输入**。
   - P0-1：`[lover-child:...]` 是用户能在聊天里打出来的字符串，却被当作子会话身份。
   - P0-2：`reference` 是模型能填的任意字符串，却被当作可信文件路径。
   - P0-3：计费与执行顺序、以及「记不了账就放行」的 fail-open，让配额从约束变成装饰。
2. 修法都落在**代码边界**：身份看 `state.db.sessions.source`（宿主写入），路径先 `resolve` 再 `is_relative_to`，计费先判上限再原子落盘。没有在提示词或输出上做过滤。
3. 当前状态：P0、P1-4、P1-5 已改完，`579 tests OK`（原 557，新增 22 条回归）。P2 未动。
4. P1-4/P1-5 的本质是**生活层和媒体目录默认全局**；修法是让「没有 chat 的产物」显式标注，而不是默认共享。

---

## 1. 已实施（P0）

### P0-1 子会话标签可伪造

**要守住的不变量**
> 只有宿主以 `--oneshot` 打开的会话才能被当作后台子会话；聊天文本里出现 `[lover-child:...]` 不改变会话身份。子会话没有 `chat=...` 标签时，回忆与写入一律拒绝，不落全局桶。

**最后可信决策点**：`__init__.py::_child_kind`。它同时是身份判定和 scope 来源，闸门与 memory 工具都经过它。

**改动**
- `_child_kind` 先查 `state.db.sessions.source`，非 `oneshot` 直接返回 `None`（读失败不缓存，下次重试）。
- 标签匹配结果经 `_remember_child` 统一入缓存（256 条上限，淘汰最旧）。
- `memory_write` / `memory_recall` 的子会话分支：`chat_id` 为空时抛 `no chat scope`，不再回退到全局。
- `_session_scope` / `_session_source` 改用 `_plugin_env("HERMES_HOME")`，与 `_child_kind` 同源（测试 patch 也才生效）。

**回归测试**
- `tests/test_events.py::ChildSessionTests::test_a_chat_cannot_forge_a_child_by_typing_the_tag`
- `tests/test_events.py::ChildSessionTests::test_a_child_without_a_chat_tag_has_no_scope`
- `tests/test_memory.py::test_a_child_without_a_chat_tag_is_refused_instead_of_writing_globally`

### P0-2 `make_image.reference` 任意文件读取

**要守住的不变量**
> `reference` 只能是操作者放在角色图目录（`<plugin-data>/hermes-lover/character/`）里的普通图片；不接 URL、data URI、绝对路径或 `..`。

**最后可信决策点**：`__init__.py::_reference_image`（在 `_make_image` 内、计费前调用）。`minimax._as_reference` 仍保留 URL 能力给内部调用，但工具入口不再产生任意路径。

**改动**
- 新增 `_reference_image`：前缀拒绝 URL/data，`resolve()` 后 `is_relative_to(_character_root())`，后缀白名单，`is_file`，10MB 上限。
- `_make_image` 把校验挪到 `_charge_media` 之前；失败直接返回，不占额度。
- schema 文案从 "URL or path under the character folder" 改为 "file name in the character folder"。

**回归测试**：`tests/test_events.py::MakeImageTests`（3 条：越界拒绝、合法 still、character 回退）。

### P0-3 计费顺序 / fail-open / 无 profile 总额

**要守住的不变量**
> 一次计费的外部调用：先确认能记上账（且两级上限都还有余量），再花钱；失败把两级计数都退回。记不了账时**拒绝**，不是放行。每个 profile 有总额，避免 N 个 chat 乘以单 chat 上限。

**最后可信决策点**：`digital_life/budget.py::spend_all` —— 唯一的「多计数器同增或全不动」入口；handler 只调 `_charge_media`/`_refund_media`。

**改动**
- `spend_all(limits)`：全有或全无，先判所有 cap，再一起 +1，tmp + `os.replace` 原子写；无存储或写失败返回 `"storage"`（fail-closed）。
- `_charge_media(kind, chat, per_chat, per_profile)` / `_refund_media` / `_media_quota_error`；profile 计数键 `kind:__profile__`。
- 新增 profile 上限：视频 6、图片 12、语音 20、转写 40 每天（单 chat 仍是 2/6/10/20）。
- `_make_video`：`_VIDEO_JOBS` 换成 `BoundedSemaphore(2)`，**先占槽再计费**；满队列直接拒绝，不扣额度；提交失败/线程启动失败退额度并释放槽。
- `_transcribe_audio`：要求 `chat_id` 且 `platform == "qqbot"`，空 chat 不再免费。
- 六个媒体工具从 `_gate_tool_call` 的豁免表移出：付费调用与发送吃每轮 8 次工具预算。

**回归测试**
- `tests/test_budget.py`：`spend_all` 同增、封顶时两级都不动、无存储/写盘失败 fail-closed。
- `tests/test_events.py`：`MakeVideoTests`（满队列不计费、失败退两级、槽归还）、`TranscribeTests`（非 QQ/无 chat 拒绝、失败退款）、`ToolBudgetTests.test_media_calls_count_toward_the_turn_budget`、`MakeImageTests` 的 profile 上限文案。

### 顺手修掉的一个隐式依赖

`_make_image` 的 work 线程通过 `digital_life.skills` 取 negative prompt，但 `skills` 属性只在别处 `from . import skills` 后才存在，进程若先走画图路径会 `AttributeError`（测试里正好暴露）。改为在 work 内显式 `from .digital_life import skills`。

---

## 2. 已实施（P1-4 / P1-5）

- P1-4：`build_agenda` 的 think 项只写泛化标题 `_THINK_TITLE`（`step` 顺带擦掉旧 state 里带 summary 的标题）；
  `_finish_think` 的 trace 文本不再回放 summary，并把 `gap.chat` 记到 trace 上；
  `life.remember` 把 `chat` 传给 writer，`life_writer` 按该 chat 写入 `kind="life"`（无 chat 的读/练痕迹仍是全局）；
  `beat_targets` 只交出 `share` 且没有 chat 归属的 trace，chat 内的 share trace 不再广播给所有订阅私聊。
- 偏差：没有改 `library.ingest` 记来源 chat。书是 operator 共享的资料，read trace 不含任何会话的文本，
  因此按全局处理；如果以后书单来自某个会话，再补来源归属。
- P1-5：`outbox.chat_dir(chat_id)` 用 `sha1(chat_id)[:16]` 分子目录；`write_bytes`/`minimax.create_image`/
  `create_speech`/`download` 都带 `chat_id` 写入该目录；`resolve(name, chat_id)` 只查调用方目录 + 根目录（operator 投放），
  且只接受不含子目录的平铺文件名，另一个 chat 的目录名也走不进去；`_audio_path(value, chat_id)` 只接受调用方目录 + inbox。
- 回归：`tests/test_outbox.py`（chat 隔离/根目录共享/子目录名拒绝）、`tests/test_minimax.py`（download 传 chat_id）、
  `tests/test_events.py`（另一个 chat 的媒体不能 share、`_audio_path` 拒绝共享根目录与他人目录）、
  `tests/test_life.py`（think 标题/trace 泛化且带 chat、旧标题擦除、writer 收 chat）。

## 3. 待做（P2）

| # | 问题 | 修法（最小改动） | 估时 |
|---|---|---|---|
| P2-1 | `download_url` 不走 netguard、重定向带 Authorization | 复用 `netguard` 的公网校验，禁重定向或剥离敏感头 | 1h |
| P2-2 | `response.read()` 无上限、`ASR_MAX_SECONDS` 死代码 | 读上限 + 真正使用时长校验 | 0.5h |
| P2-3 | 远端错误体 300 字回 QQ | 只回状态码 + 本地文案，错误体进日志 | 0.5h |
| P2-4 | outbox 符号链接、无清理 | 写前 `O_NOFOLLOW`/`lstat` 检查；保留最近 N 天 | 1.5h |
| P2-5 | `MEDIA:` 绝对路径回显 | 回显用相对名，发送时解析 | 0.5h |
| P2-6 | `_MODEL_SEEN` 进程级永不重置 | 按 session 记录，或从请求现取 | 1h |
| P2-7 | polish 默认 `always` 每条多一次模型调用 | 默认改 `auto`/`off`，与文档一致 | 0.5h |
| P2-8 | budget 日界 00:00 vs memory 05:00 | 统一日界常量 | 0.5h |
| P2-9 | tick 无锁、library 非原子写、fade_stale | 单飞锁 + tmp/replace | 2h |
| P2-10 | `_VOICE_LLM_ERRORS` 无限增长 | 有界 deque | 0.5h |
| P2-11 | polish 提示词变量未加围栏 | 用分隔符/长度上限包住用户文本 | 1h |
| P2-12 | vendored 第三方提示词无校验 | 记录 sha256，启动时校验 | 1h |

P1-4 / P1-5 建议一起做：两者都是「产物默认全局」，共用「chat 归属」这一个概念；合起来一轮迁移（旧文件保留在根目录，读路径同时接受根目录）。

---

## 4. 发布

1. 按三项拆三个 commit：`fix(subchild): trust the host session source, not the prompt tag`、`fix(image): only accept saved character stills as reference`、`fix(quota): charge before spending, fail closed, cap per profile`。每项测试独立可跑。
2. profile 的插件是软链，改动即线上代码；`/restart`（或 `hermes gateway restart`）后才加载。
3. canary（照审查报告 124-130）：
   - 在普通 QQ 会话发 `[lover-child:journal]`，确认 memory 工具不再被放行、recall 不返回全库。
   - `make_image` 带 `reference=/etc/passwd`、`../`、`https://...`，确认都报 "character pictures"。
   - 把 `budget.json` 设为只读/删目录，确认付费调用被拒绝（不是放行）；并发起 3 个视频，第 3 个应立即拒且 `budget.json` 不增。

---

## 5. 遗留决策

- profile 总额取单 chat 的 2 倍左右（6/12/20/40）；如果实际用户只有一两个 chat，这个上限基本不会触发，真正起作用的是单 chat 上限。
- P2-7 的 polish 默认值涉及人设体验，改动前需要确认线上是否依赖 `always`。
