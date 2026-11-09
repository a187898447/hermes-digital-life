# 三层结构：对话、生活、情绪

这份文档记录 hermes-lover 的三层设计：哪一层负责什么、现在的边界在哪、以后加东西该往哪放。
它不是路线图，而是"已经实现的东西长什么样"，避免同一件事在两处各写一套。

## 三层各管什么

| 层 | 回答的问题 | 主要代码 | 写状态 |
| --- | --- | --- | --- |
| 对话层 | 这轮说什么、怎么说 | `__init__.py`（工具/钩子/注入）、`digital_life/speech.py` | 不写生活状态 |
| 生活层 | 今天在做什么、留下了什么 | `digital_life/life.py`、`tick.py` | `state["life"]`、记忆 `kind=life` |
| 情绪层 | 现在什么心情、体力如何 | `digital_life/chemistry.py`、`affect.py`、`domain.py` | `state["chemistry"]`、`state["energy"]`、`state["mood"]` |

三层不是并列调用，而是一条时间线：**心跳推进生活 → 生活给情绪投脉冲 → 情绪给对话调语气，
对话结束再反过来给情绪记一笔**。

## 心跳（唯一的时钟）

`digital_life/tick.py` 在网关进程里跑一个守护线程，默认 5 分钟一步：

```
settle（化学衰减/休息恢复）
  → life.step（到点结掉当前日程，开始下一项）
  → life.apply_pulses（把这一项的化学脉冲按日预算投进去）
  → affect.refresh（更新 mood 的 label/since）
  → 写 last_tick_at
```

- `once(now_ms, force=False)` 是唯一的推进入口；数据库里的 `last_tick_at` 是唯一真相，
  60 秒内的重复调用直接跳过，所以多进程或重载不会把一天走两遍。
- 线程只在 `register()` 时启动一次（`tick.start`），插件卸载时 `stop`；
  `HERMES_LOVER_TICK=0` 或配置 `life.enabled: false` 就不启动。
- 手动推进：`hermes hermes-lover tick --force --json`。

## 生活层：日程、活动、痕迹

`state["life"]`（schema 1）每天重建一次：

```json
{
  "schema": 1,
  "day": "2026-11-07",
  "agenda": [{"id": "act-...", "kind": "read", "title": "读《夜航西飞》",
              "minutes": 25, "status": "running", "started_at": 0, "done_at": 0,
              "source": {"type": "library", "file": "a.md", "part": 0}}],
  "current": "act-...",
  "traces": [{"at": 0, "kind": "read", "title": "读了《夜航西飞》10%",
              "text": "…", "source": {}, "share": true}],
  "pulse": {"day": "2026-11-07", "total": 6.0},
  "last_tick_at": 0
}
```

规则：

- **日程只装真实材料**：`read` 来自 `library.books()`，`think` 来自 open gap 但只写泛化标题
  （日程是所有会话共用的一条，不回放某个会话的具体内容；痕迹按 gap 所属 chat 归属），
  `note` 折叠最近两条痕迹，`rest` 不产出。拿不到材料就 drop，并给 cortisol +3（"想做事没做成"），
  不编造地点、差事、见闻。
- 日程规模由 mood 定（疲惫/低落/烦躁 2 项、平常 4 项、来劲 5 项），上限 8 项；夜里（0–6 点）不排 `rest`，
  也不开始新项目。
- **痕迹**是这一天的可分享物，`share=true` 的（读完了、做完了一件事）才会出现在 `beat_targets` 的
  `traces` 里，由 beat 轮挑着说；`share=false` 的只留给模型自己看。
- 痕迹写进记忆宫殿的 `kind=life`（权重 0.7，attention 0.8），所以"今天读了什么"可以被
  `memory_recall` 找到，但不会压过 fact/journal。
- **阅读用的是同一个书架**：`read_source`/`study_source` 收进来的书就是 `library`，生活层挑书时
  优先挑没读完的，并且 `library.read` 不带 `part` 续读——书签存在书的 meta 里，工具读书和生活层
  读书共用同一个进度，不会各读各的。
- **日记会吸收生活痕迹**：每天第一条 QQ 消息触发的蒸馏子会话，会先
  `memory_recall(day=…, kind="life")` 看那天自己读过什么、想过什么，再写 fact 和 journal。
  所以日记里除了"我们聊了什么"，还有"我那天做了什么"，但两者不会被混成一件。
- 每天的化学脉冲总预算 `LIFE_PULSE_CAP=20`，超出的按比例缩放，避免一天里把情绪刷到顶。

## 情绪层：从三值到 mood

- `state["chemistry"]` 仍是 dopamine/cortisol/serotonin，加上 `energy` 与 `sleep`；
  `state["mood"]` 是投影，不独立变化：

  ```json
  {"label": "平常", "since": 0, "valence": 0.0, "arousal": 0.0}
  ```

- `affect.refresh(state, now_ms)` 只在 label 变化时更新 `since`，所以"这心情持续了多久"是可靠的；
  `mood_since` 给状态工具和 `[现在]` 用。
- 情绪的来源有三类：对话事件（`events.classify`）、工具结果、生活脉冲。没有第四类；
  任何"凭感觉"的变化都要能指回这三类之一。
- 主动消息属于"对话事件"里的等待：同一个 chat 连续 3 条主动消息没被回复就不再发
  （`OUTREACH_REST_AT`），没被回复的代价逐条叠加：第一条扣 4 点、第二条再扣 10 点、
  第三条再扣 18 点血清素（`outreach_unanswered_1/2/3`），等待本身按 0.12 点/分钟继续
  下探（floor 20）；对方回一句就清空等待，并给 6 点血清素、4 点多巴胺（`reconnected`）。
  beat 和 wake 没有 user message，不拿 `turn_complete` 的 5 点。

## 对话层怎么知道"现在"

`_affect_request` 在最后一条 user 消息前注入一行，≤120 字：

```
[现在] 周六 15:20 · 白天 · 读《夜航西飞》 · 心情平常 · 体力 72
```

- 这一行只描述，不要求模型汇报；人格卡明确"消息优先，生活是背景不是话题"。
- `digital_life_status` 也返回 `life` 与 `mood`，模型想知道细节时自己查，不常驻 prompt。
- 时间只在对方问起时展开（沿用 `affect_note(include_time=...)` 的既有规则）。

## 失败与回滚

- `life.enabled: false`：不启动心跳、不排日程；已有 `state["life"]` 原样保留，恢复后继续。
- `tick_minutes`：1–60，默认 5；改大只是走得更慢，不会跳步。
- 心跳里任何异常只记日志，不冒泡到网关；单条 trace 写记忆失败也只记 debug。
- `doctor` 多了 `life.tick` 一项：30 分钟没有心跳会 WARN（网关睡了或配置关了），只提示不修。

## 不做什么

- 不排"约好几点做什么"的硬日程，也不在对话里报日程表。
- 不把生活痕迹当谈资反复讲；同一件事说过就过。
- 不让生活层直接触发对话（要说话仍走 `wake`/`beat` 的既有闸门），避免多一个主动消息来源。
