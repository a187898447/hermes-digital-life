# 宿主脆弱点：doctor 与降级计划

## 背景

宿主（Hermes/Studio）升级会覆盖我们打的补丁、踢掉插件、重构内部模块。今天已中两次：
`psutil` 依赖冲突导致插件被 `disabled_by_update`；Studio 桥接 sitecustomize 被更新清掉。
目标是让这些变化**可检测、可一键修、失败可降级**，而不是靠人记得。

## 一、`hermes-lover doctor [--fix]`

`digital_life/cli.py` 增加子命令（沿用现有 `hermes-lover <verb>` 结构）：

`hermes -p <profile> hermes-lover doctor [--fix] [--json]`

检查项（只读，不写任何文件）：

1. 插件启用：profile `config.yaml` 的 `plugins.enabled` 是否包含 `hermes-lover`；
   是否落在 `plugins.disabled` 或 `plugins.disabled_by_update`。
2. QQ 工具集：`platform_toolsets.qqbot` 是否含 `digital_life` 与 `no_mcp`（qqbot 启用时）。
3. 宿主补丁三项 `--check`：`fix-qq-dialogue-split.py`、`fix-studio-bridge-python.py`、
   `fix-studio-cross-profile.py`（Studio 不存在时该项记 `skip`）。
4. 运行时配置：`cron.wrap_response` 为 false、`plugins.entries.hermes-lover.settings.wake.enabled` 可读。
5. 文件同步：仓库 `personas/` 与 profile `personas/`、`SOUL.md`、`scripts/*.py`、`.hermes/skills/` 逐文件比较。
6. 输出：每行 `OK / FIX / SKIP / WARN` + 一句话说明；任一 `FIX` 时退出码 1。

`--fix` 只修 1/2/3/5 中可自动修的部分：重新启用插件、重打宿主补丁、拷 person/scripts/skills；
不改 cron、不重启网关、不碰 profile 数据；修完重新汇总一次。

实现约束：
- 所有宿主路径从 `HERMES_HOME` / `Path.home()` 推导，脚本自身用相对仓库位置找 `scripts/`。
- 不 import 宿主内部模块；只读 YAML/文件 + `subprocess` 调 `python3 <fix> --check`。
- `--fix` 必须幂等：第二次运行全 `OK`。

## 二、宿主 import 降级

现在直接依赖宿主内部模块的地方：

| 位置 | 依赖 | 降级行为 |
| --- | --- | --- |
| `__init__.py` beat_send / `/help` / `/core` 回复 | `tools.send_message_tool` | 捕获 `ImportError`/`AttributeError`，记日志，返回 `{"ok": false, "error": "send unavailable"}`；命令仍返回 skip，不崩回合 |
| `digital_life/proactive.py` | `plugins.plugin_storage` | 抛出自定义 `HostAPIUnavailable`，beat 相关调用返回 false，订阅功能静默关闭 |
| `digital_life/proactive.py` | `cron.jobs` | 同上；`sync_beat_delivery` 失败只记日志，不影响消息处理 |

要求：降级路径都要有测试（`unittest.mock.patch.dict(sys.modules, {...})` 或 patch 导入函数），
并保证正常路径测试不变。

## 三、验证与交付

- `cd tests && PYTHONPATH=.. python3 -m unittest discover -s . -p 'test_*.py'` 全绿（当前 225+）。
- `HERMES_DISABLE_LAZY_INSTALLS=1 hermes plugins validate .` 通过。
- 线上实跑 `hermes -p hermes-lover hermes-lover doctor` 输出合理；`--fix` 连跑两次第二次全 OK。
- 根副本逐文件 `cmp` 同步；三个 commit：`feat(cli): add hermes-lover doctor`、
  `feat(cli): let doctor repair host patches and profile copies`、`fix(host-api): degrade when internal modules move`。
- 文档：`使用引导.md` 疑难节加“升级后先跑 doctor”；README 已知问题加一条。
