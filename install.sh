#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
用法:
  ./install.sh --mode local [--profile NAME] [--source PATH] [--configure]
  ./install.sh --mode remote --repo URL [--ref REF] [--profile NAME] [--configure]
  可选: --no-beat 不创建主动消息任务

模式:
  local   将当前仓库或 --source 指定的工作树链接到新 profile。
  remote  在新 profile 中通过 Hermes 插件管理器从 Git URL 安装。

默认 profile 名: hermes-lover
不会克隆、改写或删除现有 profile、主 agent 配置、密钥或记忆。
EOF
}

fail() { printf '错误: %s\n' "$*" >&2; exit 1; }

MODE=""
PROFILE="hermes-lover"
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO=""
REF=""
CONFIGURE=0
WITH_BEAT=1

while (($#)); do
  case "$1" in
    --mode) MODE="${2:?--mode 需要 local 或 remote}"; shift 2 ;;
    --profile) PROFILE="${2:?--profile 需要名称}"; shift 2 ;;
    --source) SOURCE="${2:?--source 需要路径}"; shift 2 ;;
    --repo) REPO="${2:?--repo 需要 Git URL}"; shift 2 ;;
    --ref) REF="${2:?--ref 需要 Git ref 或 commit}"; shift 2 ;;
    --configure) CONFIGURE=1; shift ;;
    --no-beat) WITH_BEAT=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "未知参数: $1" ;;
  esac
done

[[ "$MODE" == local || "$MODE" == remote ]] || { usage >&2; fail "必须指定 --mode local 或 --mode remote"; }
command -v hermes >/dev/null 2>&1 || fail "找不到 hermes 命令，请先安装 Hermes。"
PLUGIN_NAME="$(sed -n 's/^name:[[:space:]]*//p' "$SOURCE/plugin.yaml" | head -n1)"
[[ -n "$PLUGIN_NAME" ]] || fail "无法从 plugin.yaml 读取插件名: $SOURCE/plugin.yaml"
[[ "$PROFILE" =~ ^[a-z0-9][a-z0-9_-]{0,63}$ ]] || fail "profile 名称须以小写字母/数字开头，只含小写字母、数字、下划线和连字符，最长 64 字符。"

if hermes profile show "$PROFILE" >/dev/null 2>&1; then
  fail "profile '$PROFILE' 已存在。为避免覆盖其配置或数据，本脚本不会复用/清理它；请用另一个 --profile 名称。"
fi

if [[ "$MODE" == local ]]; then
  [[ -f "$SOURCE/plugin.yaml" && -f "$SOURCE/__init__.py" ]] || fail "本地源码目录缺少 plugin.yaml 或 __init__.py: $SOURCE"
else
  [[ -n "$REPO" ]] || fail "remote 模式必须提供 --repo URL。"
  [[ "$REPO" =~ ^(https://|ssh://|git@) ]] || fail "--repo 必须是 HTTPS 或 SSH Git URL。"
fi

CREATED=0
cleanup_on_error() {
  status=$?
  if ((status != 0)) && ((CREATED == 1)) && [[ ${BASH_SUBSHELL:-0} -eq 0 ]]; then
    printf "安装未完成。新 profile '$PROFILE' 已保留以便排查；未自动删除其中任何数据。\n" >&2
  fi
  exit "$status"
}
trap cleanup_on_error ERR

hermes profile create "$PROFILE"
CREATED=1
PROFILE_HOME="$(hermes profile show "$PROFILE" | sed -n 's/^Path:[[:space:]]*//p')"
[[ -n "$PROFILE_HOME" ]] || fail "无法从 Hermes 解析新 profile 的路径。"
if [[ "$MODE" == local ]]; then
  PLUGIN_HOME="$PROFILE_HOME/plugins/$PLUGIN_NAME"
  mkdir -p "$(dirname "$PLUGIN_HOME")"
  [[ ! -e "$PLUGIN_HOME" ]] || fail "插件目标已存在: $PLUGIN_HOME"
  ln -s "$(cd "$SOURCE" && pwd)" "$PLUGIN_HOME"
  printf '已将本地源码链接到: %s\n' "$PLUGIN_HOME"
else
  if [[ -n "$REF" ]]; then
    hermes -p "$PROFILE" plugins install "$REPO" --ref "$REF" --no-enable
  else
    hermes -p "$PROFILE" plugins install "$REPO" --no-enable
  fi
fi

hermes -p "$PROFILE" plugins enable "$PLUGIN_NAME"
hermes -p "$PROFILE" plugins doctor "$PLUGIN_NAME" --ci

# Safe messaging defaults, for every install rather than per machine: a companion
# plugin only talks, so a QQ-bound bot must not inherit the host's full
# `hermes-qqbot` toolset (terminal, file writes, MCP). `no_mcp` is a sentinel that
# keeps globally enabled MCP servers out of the resolved set. Operators who want
# more can widen this afterwards; existing choices are never overwritten.
# Beat deliveries are just the sentence: without this, every cron push carries a
# "Cronjob Response: ... / To stop or manage this job" wrapper.
hermes -p "$PROFILE" config set cron.wrap_response false >/dev/null 2>&1 || true

# Narrowing is written at install time, before QQ is bound, so a later
# `qq-bind.py` / `gateway setup` never opens the full hermes-qqbot bundle.
# An operator-set value is preserved.
if ! hermes -p "$PROFILE" config get platform_toolsets.qqbot >/dev/null 2>&1; then
  hermes -p "$PROFILE" config set platform_toolsets.qqbot '["web","vision","todo","digital_life","no_mcp"]'
  printf '已设置 QQ 工具集: web/vision/todo/digital_life（不含自带 memory，终端与写文件关闭，MCP 关闭）\n'
fi
if [[ "$(hermes -p "$PROFILE" config get human_delay.mode 2>/dev/null)" == "off" ]]; then
  hermes -p "$PROFILE" config set human_delay.mode natural
fi
# Memory split: CLI/Studio keep the host's native MEMORY.md tools; only QQ turns
# enter the palace (capture is platform-gated in the plugin, and the QQ toolset
# above has no `memory` toolset). Nothing here turns the host store off.
hermes -p "$PROFILE" config set memory.memory_enabled true >/dev/null 2>&1 || true
hermes -p "$PROFILE" config set memory.user_profile_enabled true >/dev/null 2>&1 || true

# Ship the bundled onboarding guide as a profile-local
# skills so the new agent can walk the user through setup, personas, QQ binding
# and message delivery itself, and can translate 女娲 output into SOUL.md.
SKILLS_SRC="$SOURCE/.hermes/skills"
if [[ -d "$SKILLS_SRC" ]]; then
  for skill_dir in "$SKILLS_SRC"/*/; do
    [[ -f "$skill_dir/SKILL.md" ]] || continue
    skill_name="$(basename "$skill_dir")"
    mkdir -p "$PROFILE_HOME/skills/$skill_name"
    cp "$skill_dir/SKILL.md" "$PROFILE_HOME/skills/$skill_name/SKILL.md"
  done
  printf '已安装随插件附带的 skill 到: %s\n' "$PROFILE_HOME/skills"
else
  printf '提示: 未找到随插件附带的 skill 目录 %s，跳过安装。\n' "$SKILLS_SRC" >&2
fi

# Ship the reference personas next to the template: a fresh install should have
# one worked example of 女娲 → SOUL.md instead of only an empty skeleton.
PERSONAS_SRC="$SOURCE/personas"
if [[ -d "$PERSONAS_SRC" ]]; then
  mkdir -p "$PROFILE_HOME/personas"
  cp -R "$PERSONAS_SRC/." "$PROFILE_HOME/personas/"
  printf '已安装参考人格到: %s\n' "$PROFILE_HOME/personas"
fi

# Proactive beats: four daily check-ins (10/14/16/20) with a per-day ±60 minute
# jitter. The monitor script keeps the agent asleep until a slot is actually
# due, so the ticks that do not fire cost nothing.
# Deterministic onboarding gate: the guide must print its NEXT step instead of
# guessing the order (users hit agents that skipped straight to step 5+).
GATE_SCRIPT="$SOURCE/scripts/setup-status.py"
if [[ -f "$GATE_SCRIPT" ]]; then
  mkdir -p "$PROFILE_HOME/scripts"
  cp "$GATE_SCRIPT" "$PROFILE_HOME/scripts/setup-status.py"
  if [[ -f "$SOURCE/scripts/qq-bind.py" ]]; then
    cp "$SOURCE/scripts/qq-bind.py" "$PROFILE_HOME/scripts/qq-bind.py"
  fi
  printf '已安装初始化检查脚本: %s/scripts/setup-status.py\n' "$PROFILE_HOME"
fi

BEAT_SCRIPT="$SOURCE/scripts/hermes-lover-beat.py"
if ((WITH_BEAT == 0)); then
  printf '已跳过主动消息任务（--no-beat）。需要时按 hermes-lover-guide 第 4 步手动创建。\n'
elif [[ -f "$BEAT_SCRIPT" ]]; then
  mkdir -p "$PROFILE_HOME/scripts"
  cp "$BEAT_SCRIPT" "$PROFILE_HOME/scripts/hermes-lover-beat.py"
  chmod +x "$PROFILE_HOME/scripts/hermes-lover-beat.py"
  BEAT_PROMPT='这是 hermes-lover 的主动联系触发点，监控输出里的 FIRE 标记表示刚刚到点（MORNING 上午 / MIDDAY 午后 / AFTERNOON 傍晚前 / NIGHT 夜里）。按顺序做：1) 读当前 profile 的 SOUL.md 人格；2) 参考最近记忆与 hermes-lover 当前状态（激素、精力、gap）；3) 结合此刻时间段，用这个人物的口吻写一句话（不超过 40 字，像他/她真的会主动发的那一句；不报状态数字，不客套，不重复最近发过的内容）；4) 只输出这句话，不要再加解释，也不要自己调用发送工具——投递目标由这个 cron 任务的 Deliver 决定。'
  if hermes -p "$PROFILE" cron create "*/5 9-21 * * *" "$BEAT_PROMPT" \
      --name hermes-lover-beat \
      --monitor-script "hermes-lover-beat.py" \
      --deliver local \
      --paused --paused-reason "安装时默认停用，启用方法见 hermes-lover-guide 最后一步" >/dev/null; then
    printf '已创建主动消息计划（默认停用）: 每天 10/14/16/20 点前后 60 分钟内随机触发一次\n'
    printf '启用方法: 先把 Deliver 改成你的 QQ，再 resume；步骤见 hermes-lover-guide 最后一步\n'
  else
    printf '警告: 主动消息 cron 创建失败；稍后可手动创建。\n' >&2
  fi
fi

# Daily diary: no scheduler — a sleeping laptop would miss a fixed 04:00 job.
# The plugin checks on the first QQ message of each day whether yesterday's
# diary exists and asks the model to write it from the palace + life state.
JOURNAL_SCRIPT="$SOURCE/scripts/hermes-lover-journal.py"
if [[ -f "$JOURNAL_SCRIPT" ]]; then
  mkdir -p "$PROFILE_HOME/scripts"
  cp "$JOURNAL_SCRIPT" "$PROFILE_HOME/scripts/hermes-lover-journal.py"
  chmod +x "$PROFILE_HOME/scripts/hermes-lover-journal.py"
fi

# Studio starts each non-default profile's agent in a bridge worker pinned to
# Hermes' managed tools python, which ships no third-party packages.  Every tool
# module that imports ruamel.yaml then fails to load and the agent silently
# loses terminal/files/memory/skills.  A normal install always creates a new
# profile, so apply the idempotent host fix here too (README has the details).
# Hermes Studio runs each non-default profile's agent in a bridge worker pinned to
# Hermes' managed tools python, which ships no third-party packages; and Studio
# 0.8.x validates clarify/approval answers against the connection's profile. Both
# only matter when Studio is installed AND the affected code is still present, so
# detect first and fix by default only then. Plain CLI users get no host changes.
STUDIO_MAIN=""
for candidate in "${HERMES_WEBUI_DIR:-}" /usr/local/lib/node_modules/hermes-web-ui "$HOME/.npm-global/lib/node_modules/hermes-web-ui"; do
  [[ -n "$candidate" && -f "$candidate/dist/server/index.js" ]] || continue
  STUDIO_MAIN="$candidate/dist/server/index.js"
  break
done

if [[ -z "$STUDIO_MAIN" ]]; then
  printf '未检测到 Hermes Studio，跳过宿主修复（CLI/TUI 不需要这些补丁）。\n'
elif ! command -v python3 >/dev/null 2>&1; then
  printf '检测到 Hermes Studio，但没有 python3，无法应用宿主修复。\n' >&2
else
  BRIDGE_FIX="$SOURCE/scripts/fix-studio-bridge-python.py"
  if [[ -f "$BRIDGE_FIX" ]]; then
    if python3 "$BRIDGE_FIX" --check >/dev/null 2>&1; then
      printf 'Studio 桥接环境已正常，无需修复。\n'
    else
      python3 "$BRIDGE_FIX" ||
        printf '提示: Studio 桥接补丁未应用；若在 Studio 里遇到工具缺失，见 README 已知问题。\n' >&2
    fi
  fi

  CROSS_FIX="$SOURCE/scripts/fix-studio-cross-profile.py"
  if [[ -f "$CROSS_FIX" ]] && ! python3 "$CROSS_FIX" --check >/dev/null 2>&1; then
    python3 "$CROSS_FIX" ||
      printf '提示: Studio 跨 profile 答复补丁未应用；选项按钮点了没反应时见 README 已知问题。\n' >&2
  fi
fi

# `hermes profile create` drops a generic alias wrapper that forwards straight
# to the host CLI, which makes `hermes-lover status` print Hermes' own status.
# Route the plugin's operator verbs back to the plugin command; keep chat etc.
WRAPPER="$HOME/.local/bin/$PROFILE"
if [[ -d "$(dirname "$WRAPPER")" ]]; then
  HERMES_BIN="$(command -v hermes)"
  cat > "$WRAPPER" <<EOF
#!/bin/sh
case "\${1:-}" in
  status|stop|resume|resolve-gap) exec "$HERMES_BIN" -p $PROFILE $PLUGIN_NAME "\$@" ;;
  *) exec "$HERMES_BIN" -p $PROFILE "\$@" ;;
esac
EOF
  chmod +x "$WRAPPER"
  printf '已更新命令包装: %s（hermes-lover status 等走插件命令）\n' "$WRAPPER"
fi

if ((CONFIGURE == 1)); then
  hermes -p "$PROFILE" setup
else
  printf '\n插件 profile 已创建并启用。首次运行前配置模型/API 凭证：\n  hermes -p %q setup\n' "$PROFILE"
fi

printf '\n验证状态读取：\n  hermes -p %q hermes-lover status\n' "$PROFILE"
printf '后续独立会话：\n  hermes -p %q chat\n' "$PROFILE"

printf '绑定消息平台（以 QQ 为例）：\n  hermes -p %q gateway setup   # 平台选 QQ Bot，手机 QQ 扫码\n  hermes -p %q config set platforms.qqbot.enabled true\n  hermes gateway start          # 宿主级网关，不要加 -p\n' "$PROFILE" "$PROFILE"
if hermes config get platforms.qqbot.enabled 2>/dev/null | grep -qx true; then
  printf '提示: default profile 也启用了 qqbot；同一个 QQ 应用只能挂一个 profile，二选一关掉。\n'
fi
printf '新建 profile 已把 QQ 工具集收窄到 web/vision/todo/digital_life；你手动改过的值会保留。\n'

printf '首次使用让 agent 引导你：\n  hermes -p %q chat  然后说「用 hermes-lover-guide 引导我配置」\n' "$PROFILE"
printf '\n注意：这是独立 Hermes profile，不是 delegate_task 子 agent。插件状态/记忆与主 profile 分开；宿主用户级 HOME 凭证仍按 Hermes 默认策略共享。\n'
