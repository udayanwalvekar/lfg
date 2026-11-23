#!/usr/bin/env bash
# Runs the real lfg.sh in throwaway home folders: no terminal, piped, an update, someone else's lfg already there,
# a pyenv-style python3 shim first on PATH, a cut-short file, uninstall. Never touches the real home or any Mac
# setting (lfg status only reads).
# Run: bash tests/test_install.sh   (exits non-zero if anything failed)
set -euo pipefail
cd "$(dirname "$0")/.."
./build.sh >/dev/null
ROOT="$(mktemp -d "${TMPDIR:-/tmp}/lfg-install-test.XXXXXX")"
trap 'rm -rf "$ROOT"' EXIT
FAILED=0
ok()  { printf '  ok    %s\n' "$*"; }
bad() { printf '  FAIL  %s\n' "$*"; FAILED=1; }
limit() { perl -e 'alarm shift; exec @ARGV' "$@"; }  # nothing may run away: 60 s at most

fresh() {  # a new home with ~/.local/bin on PATH; prints its path
  local h="$ROOT/home-$1"
  mkdir -p "$h/.local/bin"
  echo "$h"
}
run() {  # run a command in that home, with no terminal
  local h="$1"; shift
  case "$h" in "$ROOT"/*) ;; *) echo "refusing: $h is not a test home" >&2; exit 99 ;; esac
  limit 60 env -i HOME="$h" PATH="${EXTRA_PATH:-}$h/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin" \
    ${LFG_QUIET:+LFG_QUIET=1} "$@" </dev/null 2>&1
}

echo "lfg.sh, in throwaway homes:"

H="$(fresh plain)"
out="$(run "$H" bash "$PWD/lfg.sh")" && [ -x "$H/.local/share/lfg/lfg" ] && [ -L "$H/.local/bin/lfg" ] \
  && ok "bash lfg.sh installs (no terminal)" || bad "bash lfg.sh: $out"
v="$(run "$H" lfg --version)" || true; [ "$v" = "lfg $(sed -n "s/^VERSION = '\(.*\)'$/\1/p" lfg)" ] \
  && ok "lfg --version answers ($v)" || bad "lfg --version: $v"
grep -q "is installed" <<<"$out" && ok "says what to do next" || bad "no closing words: $out"
grep -q $'\x1b' <<<"$out" && bad "colour codes with no terminal: $out" || ok "plain text with no terminal"
s="$(run "$H" lfg status)" && grep -Eq "lfg is (on|off)|Lid sleep is off" <<<"$s" && ok "lfg status reads the Mac" \
  || bad "lfg status: $s"
[ ! -e "$H/.zshrc" ] && ok "no settings file touched (~/.local/bin already on PATH)" || bad "wrote $H/.zshrc"

H="$(fresh piped)"
out="$(run "$H" bash -c "cat '$PWD/lfg.sh' | bash; echo AFTER")" || true
grep -q AFTER <<<"$out" && [ -x "$H/.local/share/lfg/lfg" ] && ok "cat lfg.sh | bash installs and the shell carries on" \
  || bad "piped: $out"

out="$(LFG_QUIET=1 run "$H" bash "$PWD/lfg.sh")" || true
grep -q "is installed" <<<"$out" && bad "an update showed the closing words: $out" || ok "an update is quiet"

H="$(fresh nopath)"
out="$(limit 60 env -i HOME="$H" PATH="/usr/bin:/bin:/usr/sbin:/sbin" SHELL=/bin/zsh bash "$PWD/lfg.sh" </dev/null 2>&1)" || true
grep -q 'local/bin' "$H/.zshrc" 2>/dev/null && grep -q "new terminal tab" <<<"$out" && [ -L "$H/.local/bin/lfg" ] \
  && ok "no usable folder on PATH: adds ~/.local/bin to .zshrc and says so" || bad "no PATH folder: $out"

H="$(fresh foreign)"
printf '#!/bin/sh\ntouch "$HOME/FOREIGN-RAN"\n' > "$H/.local/bin/lfg" && chmod +x "$H/.local/bin/lfg"
printf 'alias lfg="echo mine"\n' > "$H/.zshrc"
out="$(run "$H" bash "$PWD/lfg.sh")" || true
[ ! -e "$H/FOREIGN-RAN" ] && ok "someone else's lfg is never run" || bad "the installer ran someone else's lfg"
grep -q "another program" <<<"$out" && [ ! -L "$H/.local/bin/lfg" ] && ok "...left in place, and it says so" \
  || bad "foreign lfg: $out"
grep -q "own lfg alias" <<<"$out" && ok "an lfg alias in .zshrc is pointed out" || bad "alias not mentioned: $out"

H="$(fresh shim)"
mkdir -p "$H/shims"
REAL="$(command -v python3)"
printf '#!/usr/bin/env bash\nexec "%s" "$@"\n' "$REAL" > "$H/shims/python3" && chmod +x "$H/shims/python3"
out="$(EXTRA_PATH="$H/shims:" run "$H" bash "$PWD/lfg.sh")" || true
py="$(head -n 1 "$H/.local/share/lfg/lfg" 2>/dev/null | sed 's/^#!//; s/ -I$//' || true)"
[ -n "$py" ] && [ "$(head -c 2 "$py")" != '#!' ] && ok "a python3 shim first on PATH is not written into lfg ($py)" \
  || bad "with a shim first on PATH: ${py:-not installed}: $out"
v="$(EXTRA_PATH="$H/shims:" run "$H" lfg --version)" || true
[ "${v#lfg }" != "$v" ] && ok "...and lfg runs" || bad "lfg under a shim: $v"

H="$(fresh cut)"
run "$H" bash "$PWD/lfg.sh" >/dev/null || true
before="$(shasum "$H/.local/share/lfg/lfg" 2>/dev/null || true)"
head -c 20000 lfg.sh > "$ROOT/cut.sh"
if run "$H" bash "$ROOT/cut.sh" >/dev/null; then bad "a cut-short file said it worked"; else ok "a cut-short file fails"; fi
[ -n "$before" ] && [ "$(shasum "$H/.local/share/lfg/lfg")" = "$before" ] && ok "...and changes nothing" || bad "a cut-short file changed lfg"

H="$(fresh gone)"
run "$H" bash "$PWD/lfg.sh" >/dev/null || true
mkdir -p "$H/.lfg" && echo x > "$H/.lfg/log"
out="$(run "$H" lfg uninstall)" || true
[ ! -e "$H/.local/share/lfg" ] && [ ! -L "$H/.local/bin/lfg" ] && [ ! -e "$H/.lfg" ] && grep -q "removed" <<<"$out" \
  && ok "lfg uninstall removes the program, the command and ~/.lfg" || bad "uninstall: $out"

exit "$FAILED"
