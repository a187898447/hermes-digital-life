#!/usr/bin/env bash
# Copy a shipped reference persona into a profile: SOUL.md + wake words.
# Usage: scripts/sync-persona.sh <profile> [persona-dir-name]
set -euo pipefail

PROFILE="${1:-hermes-lover}"
PERSONA="${2:-001-deepseek-xiaojingyu}"
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
PROFILE_HOME="$HERMES_HOME_DIR/profiles/$PROFILE"
PERSONA_DIR="$SOURCE/personas/$PERSONA"

[[ -d "$PROFILE_HOME" ]] || { echo "profile not found: $PROFILE_HOME" >&2; exit 2; }
[[ -f "$PERSONA_DIR/SOUL.md" ]] || { echo "persona not found: $PERSONA_DIR" >&2; exit 2; }

backup="$PROFILE_HOME/SOUL.md.bak-$(date +%Y%m%d%H%M%S)"
[[ -f "$PROFILE_HOME/SOUL.md" ]] && cp "$PROFILE_HOME/SOUL.md" "$backup"
cp "$PERSONA_DIR/SOUL.md" "$PROFILE_HOME/SOUL.md"
[[ -f "$PERSONA_DIR/wake-words.json" ]] && cp "$PERSONA_DIR/wake-words.json" "$PROFILE_HOME/wake-words.json"

printf '{"persona":"%s","soul_sha256":"%s","synced_at":"%s"}\n' \
  "$PERSONA" "$(shasum -a 256 "$PERSONA_DIR/SOUL.md" | cut -d' ' -f1)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  > "$PROFILE_HOME/.persona-source.json"

echo "synced $PERSONA -> $PROFILE_HOME/SOUL.md"
echo "备份: $backup"
echo "运行中的会话不会热加载，到对应聊天里发一次 /new 才会用上新卡。"
