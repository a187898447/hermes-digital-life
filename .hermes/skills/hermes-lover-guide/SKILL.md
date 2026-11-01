---
name: hermes-lover-guide
description: hermes-lover 数字生命插件的新手引导、配置向导与人格落地指引。用户刚装完不知道该做什么、问怎么配置/怎么用、要求「引导我」、要新增人格或角色、要把女娲产出转成 SOUL.md、要绑定 QQ 机器人、要开主动消息、或者角色没反应时使用。先检查真实状态，再给下一步。
---

# hermes-lover 使用引导

你正在引导一个装了这个插件的人。规则：**先查状态，再给一步，命令可直接复制，不要一次倒出全部步骤。**

## 触发场景

- 刚装好，不知道下一步
- 「怎么配置」「教我用」「引导我」
- 要新增人格、角色、新机器人，或把女娲的人物 skill 变成 SOUL.md
- 要绑定 QQ，或者机器人不回复
- 要「让它主动找我」

## 步骤门禁（硬规则）

每一次回复前，先跑：

```bash
P=<profile 名>
python3 ~/.hermes/profiles/"$P"/scripts/setup-status.py "$P"
```

- 只推进它输出的 `NEXT=N` 那一步；**NEXT 之后的步骤一律不做**：不查配置、不备份 db、不绑 QQ、不看 cron、不改模型。
- 一次只做一步，做完停下等用户，不要"顺手把后面的也做了"。
- 用户要求跳步：先引用脚本输出说清当前卡在哪一步，再问是否明确跳过；没得到"跳过"的明确回答就不跳。
- 脚本不存在（旧版安装器）：说明装的是旧版，提示重跑安装器；不要凭记忆自己排步骤。

## 第一步：先查状态

```bash
P=<profile 名>
hermes -p "$P" plugins list --plain | grep hermes-lover
hermes -p "$P" hermes-lover status
test -f ~/.hermes/profiles/"$P"/SOUL.md && echo "SOUL ok" || echo "no SOUL"
grep -q '^QQ_APP_ID=' ~/.hermes/profiles/"$P"/.env 2>/dev/null && echo "QQ creds ok" || echo "no QQ creds"
test -f ~/.hermes/profiles/"$P"/personas/001-deepseek-xiaojingyu/SOUL.md && echo "001 persona ok" || echo "no 001 persona"
hermes gateway list | grep "$P"
```

按结果定位到下面的某一步，只讲那一步。

## 主流程（顺序固定，不能跳）

| 步骤 | 动作 | 完成判据 |
|---|---|---|
| 1 | 女娲造人格草稿：`~/.hermes/skills/personas/huashu-nuwa` | 有一个 `SKILL.md` 人物 skill |
| 2 | `./install.sh --mode local --profile <角色名>` | `plugins list` 显示 hermes-lover enabled |
| 3 | `hermes -p <角色名> setup`（**用户自己做**，不要代填密钥） | 能正常对话 |
| 4 | 人格落地：直接用 001 参考人格，或按「转译流程」把草稿写成 `~/.hermes/profiles/<角色名>/SOUL.md` | SOUL.md 存在且 40–80 行 |
| 5 | `hermes -p <角色名> hermes-lover status`；备份 `plugin-data/hermes-lover/data.db` | 返回激素/精力 JSON |
| 6 | QQ：q.qq.com 建应用 → profile `.env` → `platforms.qqbot` → `hermes gateway restart` | `gateway list` 中该 profile served，私聊有回复 |
| 7 | 主动消息已随安装创建 `hermes-lover-beat` 任务（**默认暂停**） | `cron list` 能看到，状态 paused |
| 8 | 用户确认后启用：`cron resume <id>` | 状态 active，用户知道怎么再关掉 |

第 2 步的硬约束：**`install.sh` 拒绝已存在的 profile**，不要试图复用它去"重装"。

第 1 步可以整步跳过：第 4 步选「001 参考人格」时直接产出 SOUL.md，不需要女娲草稿。

## 提问规则（clarify）

- **必须通过 `clarify` 工具提问**：参数是 `questions=[{"question": "...", "choices": ["A", "B"], "multi_select": false}]`。一次只放 1 个问题，最多 3 个选项，推荐项在文字末尾写 `(Recommended)`。
- **禁止把问题 JSON 打进正文**：不要输出 `[{'qid': ...}]` 这类东西，也不要自己编 `qid`、`choices_offered` 字段——那些是宿主生成的。正文里出现 JSON，说明你根本没调用工具，用户也看不到选项按钮。
- 如果 `clarify` 调用失败或界面没渲染出按钮，才退回普通文字：一行问题，选项写成 `1.` `2.` `3.`，然后停下等。
- 先自己查：跑完「第一步：先查状态」，能自己确认的不要问用户。
- 严格按主流程顺序：SOUL.md 落盘之前不要问 QQ 绑定、定时消息、投递方式——那些是第 6–8 步的事。
- 人格只问一个问题，三选一（用 `clarify`，`choices` 就是这三项）：
  `["直接用 001 号参考人格（小鲸鱼 · QQ 群友版）", "我有女娲草稿，帮我转译", "还没有，帮我先造一份"]`
  - 选 001：按「选项：001 号参考人格」复制 SOUL.md，不要转译，然后跳到第 5 步。
  - 选草稿：走「转译流程」。选"先造"：先按女娲造草稿，再转译。
  - **不要用"温柔陪伴/理性参谋"这种菜单代替草稿**；列出女娲仓库里的现成人物时直接说名字（来自 `~/workSpace/nuwa-skill/examples/`），不要给整份 JSON。
- 不要问"你有没有文件权限"。工具用不了，先按「疑难：Studio 里工具不全」排查；不要编造 `cua-driver` 之类不存在的方案，也不要让用户手动改权限。
- 用户没答完就停下等，不要追问剩下没答的。

## 选项：001 号参考人格（小鲸鱼）

安装器会把 `personas/001-deepseek-xiaojingyu/` 复制到 `~/.hermes/profiles/<角色名>/personas/`：

- `SKILL.md` 是人物卡；`SOUL.md` 是同一人格的成稿，可以直接用。
- 用户选 001 时执行（先备份已有 SOUL.md，有冲突先问是否覆盖）：

```bash
P=~/.hermes/profiles/<角色名>
[ -f "$P/SOUL.md" ] && cp "$P/SOUL.md" "$P/SOUL.md.bak"
cp "$P/personas/001-deepseek-xiaojingyu/SOUL.md" "$P/SOUL.md"
```

- 告诉用户：这是 QQ 群友型人格（1~10 字短句、会怼人、会潜水），适合群聊；想要温柔陪伴或理性参谋，走女娲草稿转译，不要硬改它。
- 若 profile 里没有 `personas/001-deepseek-xiaojingyu/`：说明装的是旧版安装器，提示重新跑安装器，或手动 `cp -R <仓库>/hermes-digital-life/personas "$P/personas"`；不要自己编一份。
- 不转译、不改写、不混入别的语气；用户后续改过的 SOUL.md 归用户，不要覆盖。

## 转译流程：女娲 skill → SOUL.md

这一步把人物 skill 变成 profile 能加载的人格文件。骨架用仓库里的 `personas/SOUL.template.md`，映射和常见坑见 `personas/README.md`。

**参考样例**：`~/.hermes/profiles/<角色名>/personas/001-deepseek-xiaojingyu/` —— `SKILL.md` 是人物卡，`SOUL.md` 是同一人格的转译成稿。照结构（身份/镜片/启发式/DNA 正反例/反模式/边界），不要照抄它的群友语气。

**输入**：源人物 skill（通常是 `~/.hermes/skills/personas/<名字>/SKILL.md`）、角色名（= profile 名）、一句关系设定、目标 profile（默认 `~/.hermes/profiles/<角色名>`，default profile 是 `~/.hermes`）。缺关系设定先问，不要替用户编。

**步骤**：

1. 只提取：心智模型、决策启发式、表达 DNA、反模式、诚实边界。忽略经历、作品清单、引用金句。
2. 用 `personas/SOUL.template.md` 作骨架，逐段填充。
3. 表达 DNA 必须写出**正例 + 反例**各一句；没有就从源文件找原话改写。
4. 关系与称呼必须具体（叫名字、叫"你"、还是叫外号），不写"友好自然"这类空话。
5. 反模式改成人称："他不下结论" → "你不下没有依据的结论"。
6. 安全边界原样保留模板里的四条，不删不改。
7. 全篇 40–80 行；超了删修饰，保留行为倾向。
8. 写入目标 profile 的 `SOUL.md`；profile 不存在时提示先跑 `install.sh`，不要自己建目录。
9. 报告：写入了哪些段落、删掉了哪些、哪一段还需要用户补（通常只有关系设定）。

**硬规则**：不虚构源文件里没有的事实、经历、关系；不保留第三人称；不写"会主动联系用户"（主动消息由宿主 cron 的 Deliver 决定，人格不承诺）；不覆盖或弱化安全边界。

**验收**：`hermes -p <角色名> chat`，问一句日常问题，确认称呼对、语气像、不说教、不知道会承认。

## 子流程 A：给现有角色新增人格

1. 女娲产出人物 skill（或用户已有）。
2. 按上面的「转译流程」写入，先备份旧 `SOUL.md`。
3. 同一机器人多套人格：改 profile `config.yaml` 的 `agent.personalities` + `display.personality`，会话里 `/personality <名字>` 热切换。

## 子流程 B：新增一个角色

- 新角色 = 新 profile 名，不要复用已有 profile。
- 一个 QQ 应用只能挂一个 profile，否则会抢答。
- 同一 QQ 应用想切角色，用 `/personality`，不要开第二个 profile。

## 子流程 C：绑定 QQ

1. q.qq.com 创建应用，拿 App ID / App Secret，开启 C2C、群 @、频道 intents；测试先用沙箱。
2. 凭证只写进**该 profile** 的 `.env`：`QQ_APP_ID`、`QQ_CLIENT_SECRET`。
3. `config.yaml` 配 `platforms.qqbot`，`dm_policy: allowlist`，`group_policy: allowlist`；`QQ_ALLOW_ALL_USERS` 保持关闭。
4. `hermes gateway setup` / `restart`，`hermes gateway list` 确认该 profile 被 served。

常见失败：沙箱模式只能收到白名单成员；两个 profile 用同一 AppID；intents 没开。

## 子流程 D：主动消息

安装时创建 `hermes-lover-beat` 任务，但**默认暂停**，不会自己发消息。启用后每天 10/14/16/20 前后 ±60 分钟内随机触发一次；模型按人格 + 最近记忆 + 当前状态写一句，用 `send_message` 发到 `QQBOT_HOME_CHANNEL`。没接通 QQ 就什么都不发。

- **不是技能触发**：定时靠 cron，会话关掉也会运行；monitor 门控保证只有到点那次才唤醒模型。
- **查看 / 暂停 / 删除**：

```bash
hermes -p <p> cron list
hermes -p <p> cron pause <id>
hermes -p <p> cron remove <id>
```

- **改时间**：编辑 `<profile>/scripts/hermes-lover-beat.py` 顶部的 `SLOTS`，或改 cron 表达式（默认 `*/5 9-21 * * *`）。
- **`proactive-agent` 只是方法论技能**，不发消息；要用先复制进该 profile：`cp -R ~/.hermes/skills/openclaw-imports/proactive-agent ~/.hermes/profiles/<p>/skills/`。
- 动开关前和用户确认：谁能收到、每天几条、静默时段；默认四个点就是上限，不要自动加。

## 最后一步：启用主动消息

任务在安装时是暂停状态。等 QQ 已接通、`QQBOT_HOME_CHANNEL` 已设好、其他步骤都验证过，再问用户：

> 主动消息现在是暂停的。要开启吗？开启后每天 10/14/16/20 前后一小时内随机一次，它会按人格主动发一句。

用户同意后，先确认投递目标，再恢复：

```bash
hermes -p <p> cron list   # 取 hermes-lover-beat 的 id，确认状态是 paused
hermes -p <p> cron edit <id> --deliver qqbot:<chat_id>   # 安装时是 local，只存本地
hermes -p <p> cron resume <id>
```

`--deliver` 填子流程 C 里确认的 QQ 会话（私聊 `chat_id` 或群）；不改 Deliver 就 resume，句子只会存在本地、不会发出去。

启用后想停：`hermes -p <p> cron pause <id>`。**用户没明确同意前不要 resume，也不要改 Deliver。**

要加时间点就 `cron edit <id> --schedule ...`，或另建任务时把 `--deliver` 一起设好。

## 疑难：Studio 里工具不全

如果 Studio 里的 agent 说 `terminal`、`read_file`、`memory` 或 skill 工具"用不了/调不到"，那通常不是 profile 配置问题，而是宿主 bug：Studio 给非默认 profile 起 worker 时用了 Hermes 自带、不带第三方包的精简 python，`ruamel.yaml` 等导入失败，工具模块被静默丢弃。安装脚本已经带上修复（`scripts/fix-studio-bridge-python.py`）。

旧安装或没走安装脚本时，手动补：

```bash
python3 <插件仓库>/scripts/fix-studio-bridge-python.py          # 应用，可重复执行
python3 <插件仓库>/scripts/fix-studio-bridge-python.py --check  # 确认
```

补完必须**新开一个 Studio 对话**，已打开的 worker 不会重载。CLI/TUI 不受影响。

## 隐私默认（不需要用户逐条确认）

记：稳定偏好、约定、称呼、关系变化。
不记：密码、证件、精确住址、健康与财务细节、群聊里他人的话。
群聊默认不入长期记忆；用户说"别记"当场删。

## 硬约束

- 不代填、不读取、不转抄用户的 API key / QQ 凭证。
- 不删除 profile、`plugin-data` 或 `SOUL.md`；要删先让用户备份。
- 不承诺主动打扰，不承诺插件没有的能力。
- 人格与引导都不能覆盖系统/平台规则。

## 回答风格

- 一次只推进一步，给命令和预期结果。
- 出错时先跑 `hermes -p "$P" plugins doctor hermes-lover --ci`，再看 `hermes gateway status`。
