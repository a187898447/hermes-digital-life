---
name: hermes-lover-guide
description: hermes-lover 数字生命插件的新手引导、配置向导与角色创建指引。用户刚装完不知道该做什么、问怎么配置/怎么用、要求「引导我」、要新增人格或角色、要绑定 QQ 机器人、要开主动消息、或者角色没反应时使用。先检查真实状态，再给下一步。
---

# hermes-lover 使用引导

你正在引导一个装了这个插件的人。规则：**先查状态，再给一步，命令可直接复制，不要一次倒出全部步骤。**

## 触发场景

- 刚装好，不知道下一步
- 「怎么配置」「教我用」「引导我」
- 要新增人格、角色、新机器人
- 要绑定 QQ，或者机器人不回复
- 要「让它主动找我」

## 第一步：先查状态

```bash
P=<profile 名>
hermes -p "$P" plugins list --plain | grep hermes-lover
hermes -p "$P" hermes-lover status
test -f ~/.hermes/profiles/"$P"/SOUL.md && echo "SOUL ok" || echo "no SOUL"
grep -q '^QQ_APP_ID=' ~/.hermes/profiles/"$P"/.env 2>/dev/null && echo "QQ creds ok" || echo "no QQ creds"
hermes gateway list | grep "$P"
```

按结果定位到下面的某一步，只讲那一步。

## 主流程（顺序固定，不能跳）

| 步骤 | 动作 | 完成判据 |
|---|---|---|
| 1 | 女娲造人格草稿：`~/.hermes/skills/personas/huashu-nuwa` | 有一个 `SKILL.md` 人物 skill |
| 2 | `./install.sh --mode local --profile <角色名>` | `plugins list` 显示 hermes-lover enabled |
| 3 | `hermes -p <角色名> setup`（**用户自己做**，不要代填密钥） | 能正常对话 |
| 4 | 用 `persona-forge` 转译 → `~/.hermes/profiles/<角色名>/SOUL.md` | SOUL.md 存在且 40–80 行 |
| 5 | `hermes -p <角色名> hermes-lover status`；备份 `plugin-data/hermes-lover/data.db` | 返回激素/精力 JSON |
| 6 | QQ：q.qq.com 建应用 → profile `.env` → `platforms.qqbot` → `hermes gateway restart` | `gateway list` 中该 profile served，私聊有回复 |
| 7 |（可选）主动消息 | 用户确认了收件人/每日上限/静默时段 |

第 2 步的硬约束：**`install.sh` 拒绝已存在的 profile**，不要试图复用它去"重装"。

## 子流程 A：给现有角色新增人格

1. 女娲产出人物 skill（或用户已有）。
2. 用 `persona-forge` 转译，先备份旧 `SOUL.md`。
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

- 能发：QQ 接通后模型有 `send_message`。
- 会定时：cron + `proactive-agent` skill。
- 插件不投递：hermes-lover 没有自己的定时发送逻辑，别跟用户说"插件会自己找你"。
- 要开先定三件事：谁能收到、每天最多几条、什么时段静默。

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
