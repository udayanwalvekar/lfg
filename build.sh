#!/usr/bin/env bash
# Makes lfg.sh (the one file people run) from lfg.sh.in and the program in ./lfg. Run after changing either.
set -euo pipefail
cd "$(dirname "$0")"
if grep -qx 'LFG_PROGRAM_END' lfg; then echo "build: the program contains the end marker" >&2; exit 1; fi
python3 -I - <<'PY'
src = open('lfg.sh.in').read()
prog = open('lfg').read().split('\n', 1)[1].rstrip('\n')  # without its first line: the installer writes its own
assert src.count('@@PROGRAM@@') == 1
open('lfg.sh', 'w').write(src.replace('@@PROGRAM@@', prog))
PY
chmod 755 lfg.sh
bash -n lfg.sh
echo "built lfg.sh ($(wc -c < lfg.sh | tr -d ' ') bytes)"
