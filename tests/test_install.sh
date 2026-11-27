#!/usr/bin/env bash
# Runs the real lfg.sh in throwaway home folders: no terminal, piped, an update, someone else's lfg already there,
# a pyenv-style python3 shim first on PATH, a cut-short file, uninstall. Never touches the real home or any Mac
# setting: before an installed lfg runs a command that reads the Mac (status, uninstall), its sudo, pmset,
# sysadminctl, ps and the rest are pointed at stand-ins in the test folder, and it refuses to run if one is missed.
# Run: bash tests/test_install.sh   (exits non-zero if anything failed)
set -euo pipefail
cd "$(dirname "$0")/.."
./build.sh >/dev/null
ROOT="$(mktemp -d "${TMPDIR:-/tmp}/lfg-install-test.XXXXXX")"
trap 'rm -rf "$ROOT"' EXIT
NOTTY="$ROOT/notty"  # runs a command in a session of its own: no terminal at all, so nothing can ask at the keyboard
printf '#!/usr/bin/env python3 -I\nimport os, sys\nos.setsid()\nos.execvp(sys.argv[1], sys.argv[1:])\n' > "$NOTTY"
chmod 755 "$NOTTY"
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
  limit 60 "$NOTTY" env -i HOME="$h" PATH="${EXTRA_PATH:-}$h/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin" \
    ${LFG_QUIET:+LFG_QUIET=1} "$@" </dev/null 2>&1
}

# Stand-ins for everything lfg would run on the Mac. Each one logs; none changes anything.
STUBS="$ROOT/stubs"
mkdir -p "$STUBS"
for t in sudo caffeinate osascript curl visudo chown; do
  printf '#!/bin/sh\necho "%s $*" >> "$HOME/mac.log"\nexit 1\n' "$t" > "$STUBS/$t"
done
printf '#!/bin/sh\necho "pmset $*" >> "$HOME/mac.log"\n[ "$1" = -g ] && printf " SleepDisabled\\t\\t0\\n"\n' > "$STUBS/pmset"
printf '#!/bin/sh\necho "x sysadminctl[1:2] screenLock delay is immediate" >&2\n' > "$STUBS/sysadminctl"
printf '#!/bin/sh\nexit 0\n' > "$STUBS/ps"
printf '#!/bin/sh\necho "staff everyone admin"\n' > "$STUBS/id"
chmod 755 "$STUBS"/*
stub_mac() {  # point the lfg installed in home $1 at the stand-ins (its sudoers folder: $1/sudoers.d)
  mkdir -p "$1/sudoers.d"
  python3 -I - "$1/.local/share/lfg/lfg" "$STUBS" "$1/sudoers.d" <<'PY'
import sys
path, stubs, sudoers = sys.argv[1:]
src = open(path).read()
s = lambda name: "'%s/%s'" % (stubs, name)
for real, fake in (
        ("'/usr/bin/sudo', '/usr/bin/pmset', '/usr/sbin/sysadminctl', '/usr/bin/id'",
         ', '.join(s(n) for n in ('sudo', 'pmset', 'sysadminctl', 'id'))),
        ("'/usr/bin/caffeinate', '/usr/bin/osascript', '/bin/ps', '/bin/sh'",
         ', '.join([s('caffeinate'), s('osascript'), s('ps'), "'/bin/sh'"])),
        ("CURL, NOTHING = '/usr/bin/curl'", "CURL, NOTHING = " + s('curl')),
        ("'/etc/sudoers.d', '/usr/sbin/visudo', '/usr/sbin/chown'", "'%s', %s, %s" % (sudoers, s('visudo'), s('chown')))):
    if src.count(real) != 1:
        sys.exit('stub_mac: %r not found once: refusing to run the installed lfg' % real)
    src = src.replace(real, fake)
for real in ('/usr/bin/sudo', '/usr/bin/pmset', '/usr/sbin/sysadminctl', '/usr/bin/caffeinate', '/usr/bin/osascript',
             "'/bin/ps'", '/usr/bin/curl', '/usr/sbin/visudo', '/usr/sbin/chown', "'/etc/", "'/usr/bin/id'"):
    if real in src:
        sys.exit('stub_mac: %s is still in the installed lfg: refusing to run it' % real)
open(path, 'w').write(src)
PY
}

echo "lfg.sh, in throwaway homes:"

H="$(fresh plain)"
out="$(run "$H" bash "$PWD/lfg.sh")" && [ -x "$H/.local/share/lfg/lfg" ] && [ -L "$H/.local/bin/lfg" ] \
  && ok "bash lfg.sh installs (no terminal)" || bad "bash lfg.sh: $out"
v="$(run "$H" lfg --version)" || true; [ "$v" = "lfg $(sed -n "s/^VERSION = '\(.*\)'$/\1/p" lfg)" ] \
  && ok "lfg --version answers ($v)" || bad "lfg --version: $v"
grep -q "is installed" <<<"$out" && ok "says what to do next" || bad "no closing words: $out"
grep -q $'\x1b' <<<"$out" && bad "colour codes with no terminal: $out" || ok "plain text with no terminal"
stub_mac "$H" || { bad "could not point lfg at the stand-ins"; exit 1; }
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
for form in 'alias -g lfg=x' 'function lfg {' 'lfg() {' '  function lfg() {'; do
  printf '%s\n' "$form" > "$H/.zshrc"
  out="$(run "$H" bash "$PWD/lfg.sh")" || true
  grep -q "own lfg alias" <<<"$out" && ok "...also when written as: $form" || bad "not pointed out: $form"
done
printf 'alias lfgx=1\nfunction lfgo {\n' > "$H/.zshrc"
out="$(run "$H" bash "$PWD/lfg.sh")" || true
grep -q "own lfg alias" <<<"$out" && bad "lfgx/lfgo mistaken for lfg" || ok "...and not for other names (lfgx, lfgo)"

H="$(fresh lockedrc)"
mkdir -p "$H/x" && touch "$H/.zshrc" && chmod 444 "$H/.zshrc"
out="$(limit 60 env -i HOME="$H" PATH="/usr/bin:/bin:/usr/sbin:/sbin" SHELL=/bin/zsh bash "$PWD/lfg.sh" </dev/null 2>&1)" \
  && [ -L "$H/.local/bin/lfg" ] && grep -q "couldn't add" <<<"$out" \
  && ok "a read-only .zshrc: still installs, and says the PATH line couldn't be added" || bad "read-only .zshrc: $out"
chmod 644 "$H/.zshrc"

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
stub_mac "$H" || { bad "could not point lfg at the stand-ins"; exit 1; }
mkdir -p "$H/.lfg" && echo x > "$H/.lfg/log"
out="$(run "$H" lfg uninstall)" || true
[ ! -e "$H/.local/share/lfg" ] && [ ! -L "$H/.local/bin/lfg" ] && [ ! -e "$H/.lfg" ] && grep -q "removed" <<<"$out" \
  && ok "lfg uninstall removes the program, the command and ~/.lfg" || bad "uninstall: $out"
[ ! -e "$H/mac.log" ] || ! grep -qv '^pmset -g' "$H/mac.log" && ok "...and runs nothing on the Mac but reads" \
  || bad "uninstall ran: $(cat "$H/mac.log")"

H="$(fresh rule)"
run "$H" bash "$PWD/lfg.sh" >/dev/null || true
stub_mac "$H" || { bad "could not point lfg at the stand-ins"; exit 1; }
touch "$H/sudoers.d/lfg-$(id -u)"  # as if lfg setup had added its no-password rule
s="$(run "$H" lfg status)" || true
grep -q "No password needed" <<<"$s" && ok "lfg status says when no password is needed" || bad "status with the rule: $s"
out="$(run "$H" lfg uninstall)" && bad "uninstall with the rule and no terminal said it worked: $out" || true
[ -x "$H/.local/share/lfg/lfg" ] && [ -L "$H/.local/bin/lfg" ] && [ -e "$H/sudoers.d/lfg-$(id -u)" ] \
  && grep -q "run lfg uninstall in Terminal" <<<"$out" \
  && ok "with the no-password rule in and no terminal, uninstall removes nothing and says why" || bad "uninstall with the rule: $out"
grep -qs '^sudo' "$H/mac.log" && bad "uninstall ran sudo with no terminal: $(cat "$H/mac.log")" || ok "...and never runs sudo"
out="$(run "$H" lfg setup)" || true
grep -q "already never asks" <<<"$out" && ok "lfg setup with the rule in: says so, asks nothing" || bad "setup: $out"
out="$(run "$H" lfg setup --undo)" || true
grep -q "run lfg setup --undo in Terminal" <<<"$out" && [ -e "$H/sudoers.d/lfg-$(id -u)" ] \
  && ok "lfg setup --undo with no terminal: keeps the rule, says where to run it" || bad "undo: $out"

exit "$FAILED"
