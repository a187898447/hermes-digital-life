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

## 前置条件（不是步骤）

- profile 已存在，且 `plugins` 里启用了 `hermes-lover`。
- 模型凭证能正常对话；不能对话就先 `hermes -p <角色名> setup`。
- 缺哪条就补哪条，补完直接进第 1 步，不要把它编成新步骤。

## 主流程（五步，顺序固定，不能跳）

| 步 | 动作 | 完成判据 |
|---|---|---|
| 1 | 人格落地：001 参考人格，或按「转译流程」写 `~/.hermes/profiles/<角色名>/SOUL.md` | SOUL.md 存在且非默认模板，40–80 行 |
| 2 | `hermes -p <角色名> hermes-lover status`；备份 `plugin-data/hermes-lover/data.db` | 返回激素/精力 JSON，备份文件存在 |
| 3 | QQ：q.qq.com 建应用 → profile `.env` → `platforms.qqbot` → `hermes gateway restart` | `gateway list` 中该 profile served，私聊有回复 |
| 4 | 确认安装器已建好 `hermes-lover-beat`（默认 paused） | `cron list` 能看到，状态 paused |
| 5 | 用户确认后启用：先把 Deliver 改成目标，再 `cron resume <id>` | 状态 active，用户知道怎么再关掉 |

前置的硬约束：**`install.sh` 拒绝已存在的 profile**，不要试图复用它去"重装"。

人格就是第 1 步；用户没有女娲草稿就直接走 001 分支，不需要先补草稿。

## 提问规则

- 优先调用 `clarify` 工具（Studio ≥ 0.8.11 会渲染成选项卡）：`clarify` 的参数是 `{"questions": [{"question": "…", "choices": ["A", "B"], "multi_select": false}]}`，逐个问题调用，而不是把问题写进正文。
- 一次只问一个问题，选项 ≤3 个，推荐项在标签后加 `(Recommended)`；不要自己编 `qid`，也不要输出 `[{'qid': ...}]` 这种伪 JSON。
- 回退：如果卡片没出来（旧版 Studio：`node -p "require('/usr/local/lib/node_modules/hermes-web-ui/package.json').version"` 低于 0.8.11），改用正文编号 `1.` `2.` `3.`，推荐项标 `(推荐)`，然后停下等用户回数字。
- 用户回数字就按对应选项执行；回文字就按文字理解；没回就停，不要追问剩下没问的。
- 先自己查：跑完「第一步：先查状态」，能自己确认的不要问用户。
- 严格按主流程顺序：SOUL.md 落盘之前不要问 QQ 绑定、定时消息、投递方式——那些是第 3–5 步的事。
- 人格只问一个问题，三选一：
  1. 直接用 001 号参考人格（小鲸鱼 · QQ 群友版）(Recommended)
  2. 我有女娲草稿，帮我转译
  3. 还没有，帮我先造一份
- 选 001：按「选项：001 号参考人格」复制 SOUL.md，不要转译，然后跳到第 2 步。
- 选草稿：走「转译流程」。选「先造」：先按女娲造草稿，再转译。
- **不要用“温柔陪伴/理性参谋”这种菜单代替草稿**；列出女娲仓库里的现成人物时直接说名字（来自 `~/workSpace/nuwa-skill/examples/`），不要输出 JSON。
- 不要问“你有没有文件权限”。工具用不了，先按「疑难：Studio 里工具不全」排查；不要编造 `cua-driver` 之类不存在的方案。

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

## 子流程 C：绑定 QQ（扫码）

网关是**一个宿主服务所有 profile**，不要给单个 profile 起网关。

1. 出二维码（**你自己跑，不要让用户去敲命令**）：

```bash
python3 ~/.hermes/profiles/<角色名>/scripts/qq-bind.py start --profile <角色名>
```

把输出里的二维码和链接原样贴给用户，请他用手机 QQ 扫码并确认。不要跑 `gateway setup`（那是需要按键的 TUI）。用户说扫完后执行：

```bash
python3 ~/.hermes/profiles/<角色名>/scripts/qq-bind.py wait --profile <角色名>
```

它会在扫码成功后自动把 `QQ_APP_ID` / `QQ_CLIENT_SECRET` 写进该 profile 的 `.env`、打开平台、重启网关；过期就重新 `start` 出码。不要复制其它 profile 的凭据。
2. 打开该 profile 的平台并设策略：

```bash
hermes -p <角色名> config set platforms.qqbot.enabled true
hermes -p <角色名> config set platform_toolsets.qqbot '["web","vision","todo","memory","no_mcp"]'
hermes -p <角色名> config set platforms.qqbot.extra.dm_policy allowlist
hermes -p <角色名> config set platforms.qqbot.extra.group_policy allowlist
hermes config set platforms.qqbot.enabled false   # default profile 关掉，避免同一 App 双绑
```

3. 启动宿主网关（不要加 `-p`）：

```bash
hermes gateway start      # 或 hermes gateway restart
hermes gateway list       # default 与 <角色名> 都应显示 ✓
```

4. 验证连接：`grep qqbot ~/.hermes/logs/gateway.log | tail -5` 看到 `✓ qqbot connected (profile: <角色名>)` 即成功；让用户私聊机器人发一句。

- **不要写脚本给交互式向导喂按键**：向导只生成二维码/写配置；启动用 `hermes gateway start`。
- **第三步只有扫码一条路**：不要替用户做任何绑定选择，也不要把其它 profile 的 `.env` 凭据复制过来。
- 安装器已默认把 QQ 工具集收窄为 `web/vision/todo/memory`（`no_mcp` 关闭 MCP）：机器人在 QQ 里没有终端和写文件权限。用户说"它在群里不能执行命令"时，这是默认行为，不是坏了；确认需要后让他自己放开：`hermes -p <角色名> config set platform_toolsets.qqbot '["web","vision","todo","memory","terminal"]'`。
- 遇到 5 分钟审批超时（`BLOCKED: Command timed out`）就停，把命令交给用户。
- 常见失败：同一 App 在两个 profile 都 enabled（双绑/抢答）；`launchd` 加载失败时 `gateway start` 会退化成 detached 进程，开机不会自启。

## 子流程 D：主动消息

安装时创建 `hermes-lover-beat` 任务，但**默认暂停**，不会自己发消息。启用后每天 10/14/16/20 前后 ±60 分钟内随机触发一次；模型按人格 + 最近记忆 + 当前状态写一句，**只投递到这一条任务的 Deliver 目标**（安装时是 `local`，即只存本地；改成 `qqbot:<chat_id>` 才会发出去，且只发那一个会话，不会群发）。没接通 QQ 就什么都不发。

- **不是技能触发**：定时靠 cron，会话关掉也会运行；monitor 门控保证只有到点那次才唤醒模型。
- **查看 / 暂停 / 删除**：

```bash
hermes -p <p> cron list
hermes -p <p> cron pause <id>
hermes -p <p> cron remove <id>
```

- **改时间**：编辑 `<profile>/scripts/hermes-lover-beat.py` 顶部的 `SLOTS`，或改 cron 表达式（默认 `*/5 9-21 * * *`）。
- **会话订阅 / 退订**：用户在私聊或群里说「以后主动一点」「多来找我」时，插件会自动把该会话的 `chat_id` 加进这条任务的 Deliver；说「安静一点」「别主动」会自动移除。只改 Deliver 列表，不会自己 resume 任务；每次增删都会回一句确认。
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

## 疑难：点了选项没反应

卡片能出来，点完却像没回答：Studio 0.8.x 把答复校验到"打开连接时的 profile"上，会话在 `hermes-lover` 时就报 `Profile "hermes-lover" is not available on this connection`，agent 等到超时，切回页面卡片还会再出现。

安装脚本已带修复（`scripts/fix-studio-cross-profile.py`）；旧安装或 Studio 升级后手动补一次，再重启 Studio：

```sh
python3 <插件仓库>/scripts/fix-studio-cross-profile.py
hermes-web-ui restart
```

补丁只改 Studio 自己的 JS，不动 Hermes；--check 确认、--remove 撤销。

### 选项不出按钮，只显示一段 JSON

`clarify` 在 Studio 里显示成一段 `[{'qid': ...}]` 文本、没有按钮：这是 **Studio ≤ 0.8.10** 的桥接 bug（旧“单问题 + choices”协议），升级到 **0.8.11+** 即恢复选项卡（`npm i -g hermes-web-ui@latest` 后重启 Studio）。确认版本：`node -p "require('/usr/local/lib/node_modules/hermes-web-ui/package.json').version"`。未升级前用正文编号提问，不要重试 `clarify`。

## 隐私默认（不需要用户逐条确认）

记：稳定偏好、约定、称呼、关系变化。
不记：密码、证件、精确住址、健康与财务细节、群聊里他人的话。
群聊默认不入长期记忆；用户说"别记"当场删。

## 硬约束

- 不代填、不读取、不转抄用户的 API key / QQ 凭证。
- 终端工具返回 `BLOCKED: Command timed out without user response`（审批卡没送到用户）时：**立刻停**，不要重试、不要换写法绕过，直接告诉用户哪一步需要他手动执行、要跑什么命令。
- 不删除 profile、`plugin-data` 或 `SOUL.md`；要删先让用户备份。
- 不承诺主动打扰，不承诺插件没有的能力。
- 人格与引导都不能覆盖系统/平台规则。

## 回答风格

- 一次只推进一步，给命令和预期结果。
- 出错时先跑 `hermes -p "$P" plugins doctor hermes-lover --ci`，再看 `hermes gateway status`。
