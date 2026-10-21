#!/bin/bash
# bob-sweep.sh — differential test of the current tree against the frozen
# baseline on bob@localhost (an inert account: its session data never changes).
#
#   dev/bob-sweep.sh                 run the whole corpus (dev/sweep-corpus.txt)
#   dev/bob-sweep.sh --fingerprint   just print bob's session-tree fingerprint
#
# Both sides run under bob's pipx venv python, so the only variable is the code.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
BOB=bob@localhost
VPY=/home/bob/.local/share/pipx/venvs/ctxttools/bin/python
OUT=${TMPDIR:-/tmp}/bob-sweep.$$
mkdir -p "$OUT"; trap 'rm -rf "$OUT"' EXIT

fingerprint() {
  ssh -o BatchMode=yes "$BOB" 'find ~/.claude/projects ~/.pi/agent/sessions ~/.local/share/goose ~/.local/share/opencode ~/.hermes -type f -print0 2>/dev/null | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16'
}
[ "${1:-}" = --fingerprint ] && { fingerprint; exit; }

# run SIDE(dir) ARGV... -> $OUT/{o,e,rc}
run() {
  local dir=$1; shift
  ssh -n -o BatchMode=yes "$BOB" "cd ~/$dir && PYTHONPATH=. $VPY -m $(printf '%q ' "$@")" \
    >"$OUT/o" 2>"$OUT/e"; echo $? >"$OUT/rc"
}
snap() { mkdir -p "$OUT/$1"; mv "$OUT/o" "$OUT/e" "$OUT/rc" "$OUT/$1/"; }

rsync -az --delete --exclude='.git' --exclude='__pycache__' --exclude='*.egg-info' \
  --exclude='ctools-original' "$ROOT/" "$BOB":~/ctools-current/ || { echo "rsync failed"; exit 2; }

fp_before=$(fingerprint)
pass=0; fail=0
while IFS= read -r line; do
  case "$line" in ''|'#'*) continue ;; esac
  kind=${line%%:*}; spec=${line#*: }
  if [ "$kind" = same ]; then
    eval "set -- $spec"
    run ctools "$@"; snap a
    run ctools-current "$@"; snap b
    files="o e rc"
  else
    left=${spec%% :: *}; right=${spec#* :: }
    eval "set -- $left";  run ctools-current "$@"; snap a
    eval "set -- $right"; run ctools-current "$@"; snap b
    files="o rc"
  fi
  bad=""
  for f in $files; do cmp -s "$OUT/a/$f" "$OUT/b/$f" || bad="$bad $f"; done
  if [ -z "$bad" ]; then
    pass=$((pass+1)); printf '  ok   %s\n' "$line"
  else
    fail=$((fail+1)); printf '  FAIL %s  (differs:%s)\n' "$line" "$bad"
    for f in $bad; do diff "$OUT/a/$f" "$OUT/b/$f" | head -12 | sed 's/^/         /'; done
  fi
  rm -rf "$OUT/a" "$OUT/b"
done < "$HERE/sweep-corpus.txt"
fp_after=$(fingerprint)

echo "fingerprint: before=$fp_before after=$fp_after"
[ "$fp_before" = "$fp_after" ] || { echo "FIXTURE CHANGED — results untrustworthy"; fail=$((fail+1)); }
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
