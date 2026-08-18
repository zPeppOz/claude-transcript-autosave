#!/usr/bin/env bash
# Front door: installs the skill into ~/.claude/skills and registers the hooks.
#
# Everything here is idempotent — rerun it after a `git pull` to update both.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_NAME="transcript-autosave"
DEST="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}/$SKILL_NAME"
MODE="copy"
UNINSTALL=0
HOOK_ARGS=()

for arg in "$@"; do
  case "$arg" in
    # A symlink keeps the installed skill identical to the repo while you edit it,
    # but Claude Code's skill loader is not guaranteed to follow one — hence copy
    # by default and link only on request.
    --link) MODE="link" ;;
    --copy) MODE="copy" ;;
    --uninstall) UNINSTALL=1; HOOK_ARGS+=("$arg") ;;
    *) HOOK_ARGS+=("$arg") ;;
  esac
done

run_hooks() {
  python3 "$REPO/scripts/install_hooks.py" ${HOOK_ARGS[@]+"${HOOK_ARGS[@]}"}
}

if [ "$UNINSTALL" -eq 1 ]; then
  if [ -L "$DEST" ] || [ -d "$DEST" ]; then
    rm -rf "$DEST"
    echo "skill rimossa da $DEST"
  fi
  run_hooks
  echo "l'archivio in ${CLAUDE_TRANSCRIPT_DIR:-$HOME/.claude/session-archive} non è stato toccato"
  exit 0
fi

# --dry-run must not install the skill either: it promises to change nothing.
case " ${HOOK_ARGS[*]:-} " in
  *" --dry-run "*|*" --status "*) run_hooks; exit 0 ;;
esac

mkdir -p "$(dirname "$DEST")"
rm -rf "$DEST"
if [ "$MODE" = "link" ]; then
  ln -s "$REPO" "$DEST"
  echo "skill collegata: $DEST -> $REPO"
else
  mkdir -p "$DEST/scripts" "$DEST/references"
  cp "$REPO/SKILL.md" "$DEST/SKILL.md"
  cp "$REPO"/scripts/*.py "$DEST/scripts/"
  cp "$REPO"/references/*.md "$DEST/references/"
  echo "skill installata: $DEST"
fi

# The hook always points at the repo, so the code that runs is the code you edit,
# whichever install mode was used.
run_hooks
echo
echo "Prova: al prossimo fine turno controlla"
echo "  tail -3 ${CLAUDE_TRANSCRIPT_DIR:-$HOME/.claude/session-archive}/_autosave.log"
