#!/bin/bash
# bob-smoke.sh — compare the long-running verbs (web, mcp) between the frozen
# baseline and the current tree on bob. See bob_smoke.py.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
BOB=bob@localhost
VPY=/home/bob/.local/share/pipx/venvs/ctxttools/bin/python
fingerprint() { "$HERE/bob-sweep.sh" --fingerprint; }
rsync -az --delete --exclude='.git' --exclude='__pycache__' --exclude='*.egg-info' \
  --exclude='ctools-original' "$ROOT/" "$BOB":~/ctools-current/ || { echo "rsync failed"; exit 2; }
before=$(fingerprint)
ssh -n -o BatchMode=yes "$BOB" "cd ~/ctools-current && MCP_PYTHON=${MCP_PYTHON:-} $VPY dev/bob_smoke.py"; rc=$?
after=$(fingerprint)
echo "fingerprint: before=$before after=$after"
[ "$before" = "$after" ] || { echo "FIXTURE CHANGED — results untrustworthy"; rc=1; }
exit $rc
