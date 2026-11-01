#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
用法:
  ./install.sh --mode local [--profile NAME] [--source PATH] [--configure]
  ./install.sh --mode remote --repo URL [--ref REF] [--profile NAME] [--configure]

模式:
  local   将当前仓库或 --source 指定的工作树链接到新 profile。
  remote  在新 profile 中通过 Hermes 插件管理器从 Git URL 安装。

默认 profile 名: digital-life
不会克隆、改写或删除现有 profile、主 agent 配置、密钥或记忆。
EOF
}

fail() { printf '错误: %s\n' "$*" >&2; exit 1; }

MODE=""
PROFILE="digital-life"
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO=""
REF=""
CONFIGURE=0

while (($#)); do
  case "$1" in
    --mode) MODE="${2:?--mode 需要 local 或 remote}"; shift 2 ;;
    --profile) PROFILE="${2:?--profile 需要名称}"; shift 2 ;;
    --source) SOURCE="${2:?--source 需要路径}"; shift 2 ;;
    --repo) REPO="${2:?--repo 需要 Git URL}"; shift 2 ;;
    --ref) REF="${2:?--ref 需要 Git ref 或 commit}"; shift 2 ;;
    --configure) CONFIGURE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "未知参数: $1" ;;
  esac
done

[[ "$MODE" == local || "$MODE" == remote ]] || { usage >&2; fail "必须指定 --mode local 或 --mode remote"; }
command -v hermes >/dev/null 2>&1 || fail "找不到 hermes 命令，请先安装 Hermes。"
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
  if ((status != 0)) && ((CREATED == 1)); then
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
  PLUGIN_HOME="$PROFILE_HOME/plugins/digital-life"
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

hermes -p "$PROFILE" plugins enable digital-life
hermes -p "$PROFILE" plugins doctor digital-life --ci

if ((CONFIGURE == 1)); then
  hermes -p "$PROFILE" setup
else
  printf '\n插件 profile 已创建并启用。首次运行前配置模型/API 凭证：\n  hermes -p %q setup\n' "$PROFILE"
fi

printf '\n验证状态读取：\n  hermes -p %q digital-life status\n' "$PROFILE"
printf '后续独立会话：\n  hermes -p %q chat\n' "$PROFILE"
printf '\n注意：这是独立 Hermes profile，不是 delegate_task 子 agent。插件状态/记忆与主 profile 分开；宿主用户级 HOME 凭证仍按 Hermes 默认策略共享。\n'
