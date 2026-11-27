#!/usr/bin/env python3 -I
"""lfg tests. Everything that would touch the Mac (sudo, pmset, sysadminctl, caffeinate, osascript, ps, kill)
(and id) is a stand-in script in a throwaway folder that only writes to a log. The tests refuse to run if any of them
resolves to the real program. Run: python3 -I tests/test_lfg.py"""
import importlib.util, io, os, pwd, re, shutil, signal, subprocess, sys, tempfile, time, unittest, warnings
warnings.simplefilter('ignore', ResourceWarning)  # the background loops are meant to outlive lfg
from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
from unittest import mock

def load(name, path):
    """A copy of lfg as a module (it has no .py name, so its loader is given by hand)."""
    spec = importlib.util.spec_from_loader(name, SourceFileLoader(name, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lfg = load('lfg', os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lfg'))
ME = pwd.getpwuid(os.getuid()).pw_name
PW = 'testpw'

STUBS = {
    'sudo': r'''#!/bin/bash
D="$LFG_TEST_DIR"
if [ "$1" = "-k" ] && [ $# -eq 1 ]; then echo "sudo -k" >> "$D/calls.log"; exit 0; fi
if [ "$1" = "-n" ]; then
  # Like sudoers: only the exact commands listed after NOPASSWD in this account's rule, and only if sudo reads
  # the folder (no_includedir = it doesn't) and the rule names this account.
  shift
  rule="$D/sudoers.d/lfg-$(/usr/bin/id -u)"
  if [ -f "$rule" ] && [ ! -f "$D/no_includedir" ]; then
    line="$(grep -v '^#' "$rule" | grep NOPASSWD)"
    who="${line%% *}"
    if [ "$who" = "$(/usr/bin/id -un)" ] || [ "$who" = "#$(/usr/bin/id -u)" ]; then
      IFS=',' read -ra cmds <<< "${line#*NOPASSWD: }"
      for c in "${cmds[@]}"; do
        c="${c# }"
        if [ "$c" = "$*" ]; then echo "sudo-n $*" >> "$D/calls.log"; exec "$@"; fi
      done
    fi
  fi
  echo "sudo-n-refused $*" >> "$D/calls.log"
  echo "sudo: a password is required" >&2
  exit 1
fi
IFS= read -r pw || pw=""
if [ "$1" != "-S" ] || [ "$2" != "-p" ]; then echo "BADARGS $*" >> "$D/calls.log"; exit 2; fi
shift 3
if [ "$pw" != "testpw" ]; then echo "sudo-refused $*" >> "$D/calls.log"; exit 1; fi
echo "sudo $*" >> "$D/calls.log"
case "$1" in
  -k) exit 0;;
  -b) shift; [ -f "$D/fail_background" ] && exit 1; "$@" </dev/null >/dev/null 2>&1 & exit 0;;
esac
exec "$@"
''',
    'pmset': r'''#!/bin/bash
D="$LFG_TEST_DIR"
case "$1" in
  -g) printf ' SleepDisabled\t\t%s\n sleep 1\n' "$(cat "$D/sleepdisabled" 2>/dev/null || echo 0)";;
  -a) echo "$3" > "$D/sleepdisabled"; echo "pmset disablesleep $3 $(date +%s)" >> "$D/calls.log";;
esac
''',
    'sysadminctl': r'''#!/bin/bash
D="$LFG_TEST_DIR"
if [ "$2" = status ]; then
  echo "sysadminctl $*" >> "$D/reads.log"
  v="$(cat "$D/lock" 2>/dev/null)"
  case "$v" in
    off) echo "x sysadminctl[1:2] screenLock is off" >&2;;
    [0-9]*) echo "x sysadminctl[1:2] screenLock delay is $v seconds" >&2;;
    *) echo "x sysadminctl[1:2] screenLock delay is immediate" >&2;;
  esac
  exit 0
fi
IFS= read -r pw
echo "sysadminctl $*" >> "$D/calls.log"
[ "$pw" = testpw ] || exit 253
echo "$2" > "$D/lock"
''',
    'caffeinate': r'''#!/bin/bash
echo "caffeinate $*" >> "$LFG_TEST_DIR/calls.log"
w=; while [ $# -gt 0 ]; do [ "$1" = -w ] && w=$2; shift; done
while kill -0 "$w" 2>/dev/null; do sleep 0.2; done
echo "caffeinate-ended" >> "$LFG_TEST_DIR/calls.log"
''',
    'osascript': r'''#!/bin/bash
echo "osascript $*" >> "$LFG_TEST_DIR/calls.log"
''',
    # Real ps, but only the sandbox's own processes.
    'ps': r'''#!/bin/bash
D="$LFG_TEST_DIR"
/bin/ps "$@" | grep -F "$D/state" | grep -v grep
exit 0
''',
    # Never reaches the internet.
    'curl': r'''#!/bin/bash
echo "curl $*" >> "$LFG_TEST_DIR/calls.log"
cat "$LFG_TEST_DIR/download" 2>/dev/null || { echo "curl: (6) Could not resolve host" >&2; exit 6; }
''',
    # The groups this user is in: an admin unless the test says otherwise.
    'id': r'''#!/bin/bash
cat "$LFG_TEST_DIR/groups" 2>/dev/null || echo "staff everyone admin"
''',
    # visudo -c -q -f FILE: like the real one, refuses a file that isn't read-only or isn't exactly lfg's rule
    # shape. visudo -c -q: the whole setup (visudo_full_fail = it fails).
    'visudo': r'''#!/bin/bash
D="$LFG_TEST_DIR"
echo "visudo $*" >> "$D/calls.log"
if [ "$1 $2 $3" = "-c -q -f" ]; then
  [ -f "$D/visudo_fail" ] && exit 1  # refuses lfg's file
  [ "$(stat -f %Lp "$4")" = 440 ] || exit 1
  [ "$(grep -c . "$4")" = 2 ] || exit 1
  head -n 1 "$4" | grep -q '^# ' || exit 1
  tail -n 1 "$4" | grep -Eqx '(#[0-9]+|[A-Za-z_][A-Za-z0-9_.-]*) ALL = \(root\) NOPASSWD: (/[^ ,]+) -a disablesleep 1, \2 -a disablesleep 0' || exit 1
  exit 0
fi
if [ "$1 $2" = "-c -q" ] && [ $# -eq 2 ]; then
  [ -f "$D/visudo_broken" ] && exit 1  # broken before lfg came
  [ -f "$D/visudo_full_fail" ] && ls "$D/sudoers.d" | grep -qv '[.]' && exit 1  # broken with lfg's rule in
  exit 0
fi
exit 2
''',
    # chown 0:0 FILE: can't really hand a file to root here; it only says so.
    'chown': r'''#!/bin/bash
echo "chown $*" >> "$LFG_TEST_DIR/calls.log"
[ "$1" = "0:0" ] && [ -f "$2" ]
''',
}


# Every path lfg would touch outside Python itself. Each must point into the sandbox, or no test runs.
SANDBOXED = ('SUDO', 'PMSET', 'SYSADMINCTL', 'CAFFEINATE', 'OSASCRIPT', 'PS', 'ID', 'CURL', 'VISUDO', 'CHOWN',
             'SUDOERS_D', 'STATE', 'DEADLINE', 'LOG', 'STARTED', 'LOCK_BEFORE', 'ASKED', 'SHARE', 'HOME')


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.d = os.path.realpath(tempfile.mkdtemp(prefix='lfgtest-'))
        os.makedirs(self.d + '/bin')
        for name, body in STUBS.items():
            with open('%s/bin/%s' % (self.d, name), 'w') as f:
                f.write(body)
            os.chmod('%s/bin/%s' % (self.d, name), 0o755)
        self.env = mock.patch.dict(os.environ, {'LFG_TEST_DIR': self.d, 'NO_COLOR': '1'})
        self.env.start()
        b = self.d + '/bin/'
        state = self.d + '/state'
        self.patches = [mock.patch.multiple(
            lfg, SUDO=b + 'sudo', PMSET=b + 'pmset', SYSADMINCTL=b + 'sysadminctl', CAFFEINATE=b + 'caffeinate',
            OSASCRIPT=b + 'osascript', PS=b + 'ps', ID=b + 'id', ROOT=ME, TICK=1,
            STATE=state, DEADLINE=state + '/until', LOG=state + '/log', STARTED=state + '/started', LOCK_BEFORE=state + '/lock-before',
            CURL=b + 'curl', ASKED=state + '/setup-asked',
            SUDOERS_D=self.d + '/sudoers.d', VISUDO=b + 'visudo', CHOWN=b + 'chown',
            SHARE=self.d + '/share', HOME=self.d + '/home')]
        os.makedirs(self.d + '/sudoers.d')
        for p in self.patches:
            p.start()
        # Fail closed: nothing below may run if a real program would be used.
        for name in SANDBOXED:
            if not getattr(lfg, name).startswith(self.d + '/'):
                raise SystemExit('refusing to run: %s is not the sandbox stand-in' % name)
        self.write('sleepdisabled', '0')
        self.write('lock', 'immediate')
        self.asked = []
        self.answers = []
        self.ask = mock.patch.object(lfg, 'ask', side_effect=self.fake_ask)
        self.ask.start()
        self.menu = mock.patch.object(lfg, 'Menu', side_effect=OSError('no terminal in tests'))
        self.menu.start()

    def tearDown(self):
        # Only this sandbox's own processes: every one has the sandbox folder in its command line.
        table = subprocess.run(['/bin/ps', '-axww', '-o', 'pid=,command='], capture_output=True, text=True).stdout
        for line in table.splitlines():
            pid, _, cmd = line.strip().partition(' ')
            if self.d + '/' in cmd and int(pid) != os.getpid():
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except OSError:
                    pass
        for p in self.patches + [self.env, self.ask, self.menu]:
            p.stop()
        shutil.rmtree(self.d, ignore_errors=True)

    def fake_ask(self, prompt):
        self.asked.append(prompt)
        a = self.answers.pop(0) if self.answers else PW
        if isinstance(a, BaseException) or (isinstance(a, type) and issubclass(a, BaseException)):
            raise a
        return a

    def write(self, name, text):
        with open('%s/%s' % (self.d, name), 'w') as f:
            f.write(text)

    def read(self, name):
        try:
            with open('%s/%s' % (self.d, name)) as f:
                return f.read()
        except OSError:
            return ''

    def calls(self):
        return self.read('calls.log').splitlines()

    def lfg(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = lfg.main(list(args))
        self.out = out.getvalue()
        return rc

    def loops(self):
        s = lfg.status()
        return len(s['admin']), len(s['awake'])

    def awake_running(self):
        table = subprocess.run(['/bin/ps', '-axww', '-o', 'command='], capture_output=True, text=True).stdout
        return self.d + '/bin/caffeinate' in table

    def wait(self, test, secs):
        stop = time.time() + secs
        while time.time() < stop:
            if test():
                return True
            time.sleep(0.2)
        return test()


class Start(Sandbox):
    def test_fresh_start_asks_once_and_runs(self):
        t = time.time()
        self.assertEqual(self.lfg('2'), 0, self.out)
        self.assertEqual(len(self.asked), 1)
        self.assertAlmostEqual(lfg.read_end(), t + 7200, delta=3)
        self.assertEqual(self.read('sleepdisabled').strip(), '1')
        self.assertEqual(self.read('lock').strip(), 'off')
        self.assertEqual(self.loops(), (1, 1))
        self.assertTrue(self.wait(lambda: any(c.startswith('caffeinate -dimsu') for c in self.calls()), 3))
        self.assertIn('On until', self.out)

    def test_default_is_three_hours(self):
        t = time.time()
        self.assertEqual(self.lfg(), 0, self.out)
        self.assertAlmostEqual(lfg.read_end(), t + 3 * 3600, delta=3)

    def test_extend_needs_no_password_and_keeps_one_loop_each(self):
        self.lfg('2')
        end = lfg.read_end()
        self.answers = [AssertionError('asked for a password')]
        self.assertEqual(self.lfg('+1'), 0, self.out)
        self.assertEqual(lfg.read_end(), end + 3600)
        self.assertEqual(self.loops(), (1, 1))
        self.assertIn('Extended', self.out)

    def test_number_while_on_never_shortens(self):
        self.lfg('2')
        end = lfg.read_end()
        self.assertEqual(self.lfg('1'), 0)
        self.assertEqual(lfg.read_end(), end)
        self.assertIn('nothing changed', self.out)
        t = time.time()
        self.assertEqual(self.lfg('3'), 0)
        self.assertAlmostEqual(lfg.read_end(), t + 3 * 3600, delta=3)

    def test_picker_add_three_hours(self):
        self.lfg('2')
        end = lfg.read_end()
        with mock.patch.object(lfg, 'pick', side_effect=lambda s: lfg.choices(s)[0]):
            self.assertEqual(self.lfg(), 0, self.out)
        self.assertEqual(lfg.read_end(), end + 3 * 3600)
        self.assertEqual(len(self.asked), 1)  # only the start asked

    def test_picker_leave_changes_nothing(self):
        self.lfg('2')
        end = lfg.read_end()
        with mock.patch.object(lfg, 'pick', side_effect=lambda s: lfg.choices(s)[4]):
            self.assertEqual(self.lfg(), 0)
        self.assertEqual(lfg.read_end(), end)
        self.assertIn('lfg is on', self.out)

    def test_custom_hours_from_the_list(self):
        self.lfg('2')
        end = lfg.read_end()
        with mock.patch.object(lfg, 'pick', side_effect=lambda s: ('add', 1.5, 'Add 1.5 hours', '')):
            self.assertEqual(self.lfg(), 0, self.out)
        self.assertEqual(lfg.read_end(), end + 5400)

    def test_start_time_is_kept_when_extended(self):
        t = int(time.time())
        self.lfg('2')
        s = lfg.status()
        self.assertAlmostEqual(s['started'], t, delta=2)
        time.sleep(1.1)
        self.lfg('+1')
        self.assertAlmostEqual(lfg.status()['started'], t, delta=2)
        self.lfg('status')
        self.assertIn('Started ', self.out)
        self.assertIn(' ago', self.out)

    def test_over_the_cap_is_refused(self):
        self.lfg('70')
        end = lfg.read_end()
        self.assertEqual(self.lfg('+5'), 2)
        self.assertEqual(lfg.read_end(), end)


class Ending(Sandbox):
    def test_extended_run_ends_at_the_new_time_not_the_old(self):
        """The bug in the old lfg: an earlier timer put sleep back early. Here the first end passes quietly."""
        self.lfg('0.003')  # ~11 s
        first = lfg.read_end()
        self.assertEqual(self.lfg('+0.003'), 0, self.out)
        second = lfg.read_end()
        self.wait(lambda: time.time() > first + 3, 20)
        self.assertFalse([c for c in self.calls() if c.startswith('pmset disablesleep 0')], self.calls())
        self.assertEqual(self.read('sleepdisabled').strip(), '1')
        self.assertTrue(self.wait(lambda: self.loops() == (0, 0), second - time.time() + 6))
        self.assertEqual(len([c for c in self.calls() if c.startswith('pmset disablesleep 0')]), 1)
        self.assertEqual(len([c for c in self.calls() if c.startswith('osascript')]), 1)  # the relock window
        self.assertTrue(self.wait(lambda: not self.awake_running(), 3))  # keep-awake ends with it
        self.assertEqual(lfg.status()['kind'], 'off')

    def test_off_puts_everything_back_with_no_window(self):
        self.lfg('2')
        self.assertEqual(self.lfg('off'), 0, self.out)
        self.assertEqual(self.read('sleepdisabled').strip(), '0')
        self.assertEqual(self.read('lock').strip(), 'immediate')
        self.assertTrue(self.wait(lambda: self.loops() == (0, 0), 5))
        self.assertTrue(self.wait(lambda: not self.awake_running(), 3))
        time.sleep(1)
        self.assertFalse([c for c in self.calls() if c.startswith('osascript')])
        self.assertEqual(len([c for c in self.calls() if c.startswith('pmset disablesleep 0')]), 2)  # now + the loop
        self.assertIn('lfg is off', self.out)

    def test_zero_means_off(self):
        self.lfg('2')
        self.assertEqual(self.lfg('0'), 0, self.out)
        self.assertEqual(self.read('sleepdisabled').strip(), '0')

    def test_bad_end_file_keeps_the_last_good_time(self):
        self.lfg('0.003')  # ~11 s: room for the 5 s of junk below on a busy Mac
        end = lfg.read_end()
        for junk in ('abc\n', '', '99999999999\n', '%d\n' % (time.time() + 100 * 3600)):
            with open(lfg.DEADLINE, 'w') as f:
                f.write(junk)
            time.sleep(1.2)
        self.assertEqual(self.loops(), (1, 1))  # nothing ended early
        self.assertTrue(self.wait(lambda: self.loops() == (0, 0), end - time.time() + 6))  # nor ran on
        self.assertEqual(self.read('sleepdisabled').strip(), '0')

    def test_loops_survive_their_starter(self):
        self.lfg('2')
        pids = [p['pid'] for p in lfg.status()['admin'] + lfg.status()['awake']]
        for pid in pids:
            self.assertNotEqual(os.getpgid(pid), os.getpgid(0))  # own session: closing the window won't end them


class Plain(Sandbox):
    def test_plain_lfg_with_no_terminal_only_says(self):
        self.assertEqual(self.lfg('2'), 0, self.out)
        mac = lambda: [c for c in self.calls() if not c.startswith('caffeinate')]  # the loop's own, any time
        before = mac()
        self.assertEqual(self.lfg(), 0)
        self.assertIn('lfg is on', self.out)
        self.assertEqual(mac(), before)
        self.assertNotIn('\x1b', self.out)


class Shared(Sandbox):
    """What the shared copy adds: the admin check, version, uninstall."""

    def test_a_standard_account_is_told_after_one_try(self):
        self.write('groups', 'staff everyone localaccounts')
        self.answers = ['whatever']
        self.assertEqual(self.lfg('2'), 1)
        self.assertEqual(len(self.asked), 1)
        self.assertFalse([c for c in self.calls() if not c.startswith('sudo-refused')])
        self.assertIn('needs an admin account', self.out)

    def test_a_standard_account_with_sudo_rights_can_use_it(self):
        self.write('groups', 'staff everyone localaccounts')
        self.assertEqual(self.lfg('2'), 0, self.out)
        self.assertEqual(lfg.status()['kind'], 'on')

    def test_version(self):
        self.assertEqual(self.lfg('--version'), 0)
        self.assertEqual(self.out.strip(), 'lfg ' + lfg.VERSION)

    def make_install(self):
        os.makedirs(lfg.SHARE)
        open(lfg.SHARE + '/lfg', 'w').close()
        os.makedirs(self.d + '/onpath')
        os.symlink(lfg.SHARE + '/lfg', self.d + '/onpath/lfg')
        open(self.d + '/onpath/other', 'w').close()
        os.makedirs(lfg.STATE, exist_ok=True)
        os.environ['PATH'] = self.d + '/onpath:' + os.environ.get('PATH', '')

    def test_uninstall_is_refused_while_on(self):
        self.assertEqual(self.lfg('2'), 0, self.out)
        self.make_install()
        self.assertEqual(self.lfg('uninstall'), 1)
        self.assertIn('lfg off', self.out)
        self.assertTrue(os.path.exists(lfg.SHARE + '/lfg'))
        self.assertTrue(os.path.islink(self.d + '/onpath/lfg'))
        self.assertEqual(lfg.status()['kind'], 'on')

    def test_uninstall_removes_only_lfg(self):
        self.make_install()
        self.assertEqual(self.lfg('uninstall'), 0, self.out)
        self.assertFalse(os.path.exists(lfg.SHARE))
        self.assertFalse(os.path.lexists(self.d + '/onpath/lfg'))
        self.assertTrue(os.path.exists(self.d + '/onpath/other'))
        self.assertFalse(os.path.exists(lfg.STATE))
        self.assertIn('lfg is removed', self.out)
        self.assertEqual(self.calls(), [])

    def test_uninstall_leaves_someone_elses_lfg(self):
        self.make_install()
        os.unlink(self.d + '/onpath/lfg')
        open(self.d + '/onpath/lfg', 'w').close()
        self.assertEqual(self.lfg('uninstall'), 0, self.out)
        self.assertTrue(os.path.exists(self.d + '/onpath/lfg'))

    def test_update_refuses_what_is_not_lfg_and_never_runs_it(self):
        self.write('download', '<html>404</html>')
        with mock.patch.object(lfg.subprocess, 'run', wraps=lfg.subprocess.run) as ran:
            self.assertEqual(lfg.update([]), 1)
        self.assertEqual([c.args[0][0] for c in ran.call_args_list], [lfg.CURL])
        self.write('download', "#!/usr/bin/env bash\nLFG_PROGRAM_END\nVERSION = '%s'\n" % lfg.VERSION)
        with mock.patch.object(lfg.subprocess, 'run', wraps=lfg.subprocess.run) as ran, \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(lfg.update([]), 0)
        self.assertEqual([c.args[0][0] for c in ran.call_args_list], [lfg.CURL])
        self.assertIn('newest', out.getvalue())

    def test_update_with_no_internet_says_so(self):
        self.assertEqual(lfg.update([]), 1)
        self.assertEqual(len([c for c in self.calls() if c.startswith('curl')]), 1)

    def test_uninstall_is_refused_while_lid_sleep_is_left_off(self):
        self.make_install()
        self.write('sleepdisabled', '1')
        self.assertEqual(self.lfg('uninstall'), 1)
        self.assertIn('lfg off', self.out)
        self.assertTrue(os.path.exists(lfg.SHARE + '/lfg'))

    def test_uninstall_is_refused_while_a_runs_lock_is_left_off(self):
        self.make_install()
        self.write('lock', 'off')
        self.write('state/lock-before', 'immediate')
        self.assertEqual(self.lfg('uninstall'), 1)
        self.assertTrue(os.path.exists(lfg.SHARE + '/lfg'))


class ScreenLock(Sandbox):
    """The screen lock goes back to how each person had it."""

    def test_kept_off_by_choice_stays_off_with_no_window_and_no_warning(self):
        self.write('lock', 'off')
        self.assertEqual(self.lfg('0.0015'), 0, self.out)  # ~5 s
        self.assertEqual(self.read('state/lock-before').strip(), 'off')
        self.assertTrue(self.wait(lambda: self.loops() == (0, 0), 30))
        self.assertTrue(self.wait(lambda: not self.awake_running(), 10))
        time.sleep(1)
        self.assertFalse([c for c in self.calls() if c.startswith('osascript')])
        self.assertEqual(self.read('lock').strip(), 'off')
        self.lfg('status')
        self.assertNotIn('screen lock', self.out)

    def test_off_by_choice_then_lfg_off_leaves_it_off(self):
        self.write('lock', 'off')
        self.assertEqual(self.lfg('2'), 0, self.out)
        self.assertEqual(self.lfg('off'), 0, self.out)
        self.assertEqual(self.read('lock').strip(), 'off')
        self.assertFalse([c for c in self.calls() if c.startswith('sysadminctl')])

    def test_a_delay_comes_back_as_the_same_delay(self):
        self.write('lock', '300')
        self.assertEqual(self.lfg('2'), 0, self.out)
        self.assertEqual(self.read('lock').strip(), 'off')
        self.assertEqual(self.lfg('off'), 0, self.out)
        self.assertEqual(self.read('lock').strip(), '300')

    def test_the_window_at_the_end_puts_back_the_delay(self):
        self.write('lock', '300')
        self.assertEqual(self.lfg('0.0015'), 0, self.out)
        self.assertTrue(self.wait(lambda: [c for c in self.calls() if c.startswith('osascript')], 30))
        osa = [c for c in self.calls() if c.startswith('osascript')][0]
        self.assertIn('screenLock 300 -password', osa)

    def test_an_ignored_window_does_not_make_off_the_new_normal(self):
        self.write('lock', '300')
        self.assertEqual(self.lfg('2'), 0, self.out)
        self.write('state/until', '0')  # it ends; the lock window is ignored, so the lock stays off
        self.assertTrue(self.wait(lambda: self.loops() == (0, 0), 15))
        self.write('sleepdisabled', '0')
        self.assertEqual(self.read('lock').strip(), 'off')
        self.assertEqual(self.lfg('2'), 0, self.out)  # a new run
        self.assertEqual(self.read('state/lock-before').strip(), '300')
        self.assertEqual(self.lfg('off'), 0, self.out)
        self.assertEqual(self.read('lock').strip(), '300')


class Review(Sandbox):
    """What the first review found (22 Nov 2026)."""

    def test_run_ends_while_the_list_is_open_then_add_restarts_it(self):
        self.lfg('0.002')  # ~7 s
        def slow_choice(s):
            self.wait(lambda: self.loops() == (0, 0), 20)  # he looks at the list until the run is over
            return lfg.choices(s)[0]
        with mock.patch.object(lfg, 'pick', side_effect=slow_choice):
            self.assertEqual(self.lfg(), 0, self.out)
        s = lfg.status()
        self.assertEqual((s['kind'], s['sleep_off'], len(s['admin']), len(s['awake'])), ('on', True, 1, 1))
        self.assertAlmostEqual(lfg.read_end(), time.time() + 3 * 3600, delta=5)
        self.assertIn('ended while you were choosing', ' '.join(self.asked) + self.out)

    def test_start_whose_admin_loop_fails_changes_nothing_and_says_so(self):
        self.write('fail_background', '1')
        self.assertEqual(self.lfg('2'), 1)
        self.assertEqual(self.read('sleepdisabled').strip(), '0')
        self.assertEqual(self.read('lock').strip(), 'immediate')
        self.assertEqual(self.loops(), (0, 0))
        self.assertIn('Could not start lfg', self.out)
        self.assertIn('lfg is off', self.out)

    def test_extend_does_not_claim_success_when_lid_sleep_is_back_on(self):
        self.lfg('2')
        self.write('sleepdisabled', '0')  # something else switched it back
        self.assertEqual(self.lfg('+1'), 1)
        self.assertNotIn('Lid shut is fine', self.out)
        self.assertIn('lid sleep is back ON', self.out)

    def test_off_turns_a_left_off_screen_lock_back_on(self):
        self.write('lock', 'off')  # a run ended and the lock window was ignored
        os.makedirs(self.d + '/state', exist_ok=True)
        self.write('state/lock-before', 'immediate')
        self.lfg('status')
        self.assertIn('screen lock is still off', self.out)
        self.assertEqual(self.lfg('off'), 0, self.out)
        self.assertEqual(self.read('lock').strip(), 'immediate')

    def test_a_pipe_or_link_at_the_end_file_neither_stalls_nor_stops_the_loops(self):
        self.lfg('0.003')  # ~11 s
        end = lfg.read_end()
        os.remove(lfg.DEADLINE)
        os.mkfifo(lfg.DEADLINE)
        time.sleep(2.5)
        self.assertEqual(self.loops(), (1, 1))
        os.remove(lfg.DEADLINE)
        os.symlink('/dev/zero', lfg.DEADLINE)
        time.sleep(2.5)
        self.assertEqual(self.loops(), (1, 1))
        self.assertTrue(self.wait(lambda: self.loops() == (0, 0), end - time.time() + 6))  # ends on the last good time
        self.assertEqual(self.read('sleepdisabled').strip(), '0')

    def test_a_second_window_at_the_same_moment_is_told_to_wait(self):
        import fcntl
        os.makedirs(lfg.STATE, exist_ok=True)
        with open(os.path.join(lfg.STATE, 'lock'), 'w') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            self.assertEqual(self.lfg('2'), 1)
        self.assertIn('busy in another window', self.out)
        self.assertEqual(self.calls(), [])


class Password(Sandbox):
    def nothing_changed(self):
        changed = [c for c in self.calls() if not c.startswith(('sudo -k', 'sudo-refused'))]
        self.assertEqual(changed, [])
        self.assertIsNone(lfg.read_end())
        self.assertEqual(self.read('sleepdisabled').strip(), '0')

    def test_three_wrong_passwords_change_nothing(self):
        self.answers = ['nope', 'nope', 'nope']
        self.assertEqual(self.lfg('2'), 1)
        self.assertEqual(len(self.asked), 3)
        self.nothing_changed()

    def test_wrong_then_right(self):
        self.answers = ['nope', PW]
        self.assertEqual(self.lfg('2'), 0, self.out)
        self.assertIn("didn't work", self.out)

    def test_ctrl_c_at_the_password_changes_nothing(self):
        self.answers = [KeyboardInterrupt]
        self.assertEqual(self.lfg('2'), 130)
        self.assertIn('nothing changed', self.out)
        self.nothing_changed()

    def test_no_terminal_says_so_and_changes_nothing(self):
        self.answers = [lfg.NoTerminal]
        self.assertEqual(self.lfg('2'), 1)
        self.assertIn('run lfg in Terminal', self.out)
        self.nothing_changed()

    def test_password_is_never_on_a_command_line_or_in_a_file(self):
        self.answers = ['nope', PW, PW]
        self.lfg('2')
        argv = subprocess.run(['/bin/ps', '-axww', '-o', 'command='], capture_output=True, text=True).stdout
        self.lfg('off')
        found = [p for p, _, fs in os.walk(self.d) for f in fs if PW in open(os.path.join(p, f), errors='ignore').read()
                 and not f in STUBS]
        self.assertEqual(found, [])
        self.assertNotIn(PW, argv)
        self.assertNotIn('nope', self.read('calls.log'))


class Stuck(Sandbox):
    def test_sleep_left_off_with_no_timer_is_shown_and_fixed_by_start(self):
        self.write('sleepdisabled', '1')
        self.assertEqual(lfg.status()['kind'], 'stuck')
        self.lfg('status')
        self.assertIn('nothing will turn it back on', self.out)
        self.assertEqual(self.lfg('1'), 0, self.out)
        self.assertEqual(lfg.status()['kind'], 'on')


class FakeMenu:
    """The terminal question, answered by the test: `picks` are the rows chosen, in order. Showing a question the
    test didn't expect fails it (IndexError), and so does the run picker."""

    def __init__(self, picks=()):
        self.picks, self.shown = list(picks), []

    def __call__(self):  # stands in for the Menu class: Menu() -> this
        return self

    def question(self, title, notes, rows, hint):
        self.shown.append((title, notes, rows, hint))
        return self.picks.pop(0)

    def choose(self, s, rows):
        raise AssertionError('the run picker showed')

    def close(self):
        pass


class NoPassword(Sandbox):
    """lfg setup: the password one last time, then never; the screen lock is never touched."""

    def rule(self):
        return '%s/sudoers.d/lfg-%d' % (self.d, os.getuid())

    def lfg(self, *args):
        rc = super().lfg(*args)
        for l in self.out.replace(self.d + '/sudoers.d', '/etc/sudoers.d').splitlines():  # paths as on a real Mac
            self.assertLess(len(l), 80, 'cut at the edge of an 80-column window: %r' % l)
        return rc

    def at_terminal(self, picks=()):
        """A person at a terminal who will answer the question with `picks`."""
        self.fake = FakeMenu(picks)
        for p in (mock.patch.object(lfg, 'Menu', self.fake), mock.patch.object(lfg, 'can_ask', return_value=True)):
            p.start()
            self.addCleanup(p.stop)

    def set_up_free(self):
        self.at_terminal([0])
        self.assertEqual(self.lfg('setup'), 0, self.out)
        self.assertTrue(os.path.exists(self.rule()))
        self.asked.clear()
        self.answers = [AssertionError('asked for a password')]  # from here on, any password prompt fails the test
        self.mark = len(self.calls())
        os.remove(self.d + '/reads.log')  # the screen lock reads from before the rule

    def solo(self):
        return len(lfg.status()['solo'])

    def since_setup(self):
        return self.calls()[self.mark:]

    def screen_lock_untouched(self):
        self.assertFalse([c for c in self.since_setup() if c.startswith(('sysadminctl', 'osascript'))], self.calls())
        self.assertEqual(self.read('reads.log'), '')  # not even read
        self.assertEqual(self.read('lock').strip(), 'immediate')

    def test_setup_yes_adds_exactly_the_rule_and_asks_once(self):
        self.at_terminal([0])
        self.assertEqual(self.lfg('setup'), 0, self.out)
        self.assertEqual(len(self.asked), 1)
        me = pwd.getpwuid(os.getuid()).pw_name
        pmset = self.d + '/bin/pmset'
        with open(self.rule()) as f:
            text = f.read()
        lines = text.splitlines()
        self.assertEqual(len(lines), 2, text)
        self.assertTrue(lines[0].startswith('# lfg (github.com/udayanwalvekar/lfg) lets '), lines[0])
        self.assertEqual(lines[1], '%s ALL = (root) NOPASSWD: %s -a disablesleep 1, %s -a disablesleep 0'
                         % (me, pmset, pmset))
        self.assertEqual(oct(os.stat(self.rule()).st_mode & 0o777), '0o440')
        self.assertNotIn('.', os.path.basename(self.rule()))  # sudo skips names with a dot: this one it reads
        self.assertEqual(os.listdir(self.d + '/sudoers.d'), [os.path.basename(self.rule())])  # no .new left
        calls = self.calls()
        new = self.rule() + '.new'
        # The order that keeps a broken rule out: the sudo setup checked as it is, the rule written as .new, made
        # root's, checked, moved in, all checked again, then tried for real with no password (after sudo forgets
        # the password it just took).
        order = ['visudo -c -q', 'chown 0:0 ' + new, 'visudo -c -q -f ' + new, 'visudo -c -q', 'sudo -k',
                 'sudo-n %s -a disablesleep 0' % pmset]
        self.assertEqual([c for c in calls if c.startswith(('chown', 'visudo', 'sudo-n')) or c == 'sudo -k'], order)
        self.assertFalse([c for c in calls if c.startswith('pmset disablesleep 1')])  # nothing kept awake yet
        self.assertEqual(self.read('state/setup-asked').split()[0], 'yes')
        self.assertIn("won't ask for your password again", self.out)
        self.assertIn('allows only pmset -a disablesleep 1 and 0', self.out)
        for l in self.out.replace(self.d + '/sudoers.d', '/etc/sudoers.d').splitlines():  # as on a real Mac
            self.assertLess(len(l), 80, l)
        title, notes, rows, _ = self.fake.shown[0]
        words = ' '.join(notes)
        self.assertIn('turn lid sleep off and on with no password', words)
        self.assertIn('Any program on this Mac could then keep it awake', words)
        self.assertIn('Nothing more', words)
        self.assertIn('screen lock', words)
        self.assertEqual(title, 'Never ask for your password again?')

    def test_after_setup_start_extend_off_need_no_password_and_leave_the_screen_lock(self):
        self.set_up_free()
        t = time.time()
        self.assertEqual(self.lfg('2'), 0, self.out)
        self.assertIn('On until', self.out)
        self.assertIn('screen lock is left as you have it', self.out)
        self.assertNotIn('screen lock is still on', self.out)
        self.assertAlmostEqual(lfg.read_end(), t + 7200, delta=3)
        self.assertEqual(self.read('sleepdisabled').strip(), '1')
        s = lfg.status()
        self.assertEqual((len(s['admin']), len(s['awake']), len(s['solo'])), (0, 0, 1))  # one loop, his own
        self.assertEqual(s['solo'][0]['user'], ME)
        self.assertTrue(self.wait(lambda: any(c.startswith('caffeinate -ims ') for c in self.calls()), 3))
        self.assertFalse([c for c in self.calls() if c.startswith('caffeinate -d')])  # the display may sleep and lock
        end = lfg.read_end()
        self.assertEqual(self.lfg('+1'), 0, self.out)
        self.assertEqual(lfg.read_end(), end + 3600)
        self.assertEqual(self.lfg('off'), 0, self.out)
        self.assertIn('lfg is off', self.out)
        self.assertEqual(self.read('sleepdisabled').strip(), '0')
        self.assertTrue(self.wait(lambda: self.solo() == 0, 5))
        self.assertTrue(self.wait(lambda: not self.awake_running(), 3))
        self.assertEqual(self.asked, [])
        self.screen_lock_untouched()
        self.assertFalse([c for c in self.since_setup() if c.startswith(('sudo /', 'sudo -b', 'sudo -k'))])

    def test_no_terminal_can_start_and_stop_it(self):
        """What Alfred (or any agent) needs: no terminal, no password, no question."""
        self.set_up_free()
        self.answers = [lfg.NoTerminal]
        with mock.patch.object(lfg, 'can_ask', return_value=False):
            self.assertEqual(self.lfg('1'), 0, self.out)
            self.assertEqual(lfg.status()['kind'], 'on')
            self.assertEqual(self.lfg('off'), 0, self.out)
        self.assertEqual(self.read('sleepdisabled').strip(), '0')
        self.assertEqual(self.asked, [])
        self.assertNotIn('\x1b', self.out)

    def test_a_natural_end_turns_lid_sleep_back_on_with_no_window(self):
        self.set_up_free()
        self.assertEqual(self.lfg('0.0015'), 0, self.out)  # ~5 s
        self.assertTrue(self.wait(lambda: self.solo() == 0, 30))
        self.assertEqual(self.read('sleepdisabled').strip(), '0')
        self.assertEqual(len([c for c in self.calls() if c == 'sudo-n %s/bin/pmset -a disablesleep 0' % self.d]), 2)
        self.assertTrue(self.wait(lambda: not self.awake_running(), 5))
        time.sleep(1)
        self.assertFalse([c for c in self.calls() if c.startswith('osascript')])
        self.assertEqual(lfg.status()['kind'], 'off')
        self.assertNotIn('NOT back on', self.read('state/log'))

    def test_run_ends_while_the_list_is_open_then_add_restarts_it_with_no_password(self):
        self.set_up_free()
        self.lfg('0.002')  # ~7 s
        def slow_choice(s):
            self.wait(lambda: self.solo() == 0, 20)
            return lfg.choices(s)[0]
        with mock.patch.object(lfg, 'pick', side_effect=slow_choice):
            self.assertEqual(self.lfg(), 0, self.out)
        s = lfg.status()
        self.assertEqual((s['kind'], s['sleep_off'], len(s['solo'])), ('on', True, 1))
        self.assertEqual(self.asked, [])

    def test_picker_turn_off_needs_no_password(self):
        self.set_up_free()
        self.lfg('2')
        self.assertIn('no password', lfg.choices(lfg.status())[3][3])
        with mock.patch.object(lfg, 'pick', side_effect=lambda s: lfg.choices(s)[3]):
            self.assertEqual(self.lfg(), 0, self.out)
        self.assertEqual(self.read('sleepdisabled').strip(), '0')
        self.assertEqual(self.asked, [])

    def test_status_says_no_password_is_needed(self):
        self.set_up_free()
        self.lfg('status')
        self.assertIn('lfg is off', self.out)
        self.assertIn('No password needed', self.out)

    def test_setup_no_changes_nothing_and_is_not_asked_again(self):
        self.at_terminal([1])
        self.assertEqual(self.lfg('setup'), 0, self.out)
        self.assertIn('Nothing changed', self.out)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.asked, [])
        self.assertFalse(os.path.exists(self.rule()))
        self.assertEqual(self.read('state/setup-asked').split()[0], 'no')
        self.assertEqual(self.lfg('2'), 0, self.out)  # the first start: no question again (FakeMenu has no picks)
        self.assertEqual(len(self.fake.shown), 1)
        self.assertEqual(len(self.asked), 1)  # the usual password
        self.assertEqual(self.read('lock').strip(), 'off')  # the usual run

    def test_first_start_asks_once_and_yes_means_one_password_in_all(self):
        self.at_terminal([0])
        self.assertEqual(self.lfg('2'), 0, self.out)
        self.assertEqual(len(self.fake.shown), 1)
        self.assertEqual(len(self.asked), 1)
        self.assertIn("won't ask for your password again", self.out)
        self.assertIn('On until', self.out)
        self.assertEqual(lfg.status()['kind'], 'on')
        self.assertEqual(self.solo(), 1)
        self.assertEqual(self.read('lock').strip(), 'immediate')
        self.assertEqual(self.lfg('off'), 0, self.out)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(self.lfg('2'), 0, self.out)
        self.assertEqual(len(self.fake.shown), 1)  # never asked again

    def test_first_start_no_goes_on_the_usual_way(self):
        self.at_terminal([1])
        self.assertEqual(self.lfg('2'), 0, self.out)
        self.assertIn('keep asking for your password', self.out)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(self.read('lock').strip(), 'off')
        self.assertEqual(self.loops(), (1, 1))
        self.assertFalse(os.path.exists(self.rule()))

    def test_first_start_question_is_never_shown_without_a_terminal(self):
        self.fake = FakeMenu()
        with mock.patch.object(lfg, 'Menu', self.fake):  # can_ask is the real one: output here is not a terminal
            self.assertEqual(self.lfg('2'), 0, self.out)
        self.assertEqual(self.fake.shown, [])
        self.assertFalse(os.path.exists(self.d + '/state/setup-asked'))  # still to be asked, at a terminal

    def test_first_start_question_waits_while_lfg_left_something_off(self):
        self.at_terminal()
        os.makedirs(self.d + '/state', exist_ok=True)
        self.write('lock', 'off')
        self.write('state/lock-before', 'immediate')
        self.assertEqual(self.lfg('2'), 0, self.out)
        self.assertEqual(self.fake.shown, [])
        self.assertFalse(os.path.exists(self.d + '/state/setup-asked'))

    def test_setup_is_refused_while_a_run_is_on(self):
        self.lfg('2')
        mac = lambda: [c for c in self.calls() if not c.startswith('caffeinate')]  # the loop's own, any time
        before = mac()
        self.at_terminal()
        self.assertEqual(self.lfg('setup'), 1)
        self.assertIn('lfg is on right now', self.out)
        self.assertEqual(mac(), before)
        self.assertFalse(os.path.exists(self.rule()))

    def test_setup_is_refused_while_lid_sleep_is_left_off(self):
        self.write('sleepdisabled', '1')
        self.at_terminal()
        self.assertEqual(self.lfg('setup'), 1)
        self.assertIn('Lid sleep is still off', self.out)
        self.assertEqual(self.fake.shown, [])

    def test_setup_without_a_terminal_says_so(self):
        self.assertEqual(self.lfg('setup'), 1)
        self.assertIn('run it in Terminal', self.out)
        self.assertEqual(self.calls(), [])

    def test_setup_on_a_mac_with_no_sudoers_folder_changes_nothing(self):
        os.rmdir(self.d + '/sudoers.d')
        self.at_terminal()
        self.assertEqual(self.lfg('setup'), 1)
        self.assertIn("can't take the rule", self.out)
        self.assertEqual((self.asked, self.calls(), self.fake.shown), ([], [], []))

    def test_setup_takes_nothing_else(self):
        self.assertEqual(self.lfg('setup', 'now'), 2)

    def test_already_set_up(self):
        self.set_up_free()
        self.assertEqual(self.lfg('setup'), 0)
        self.assertIn('already never asks', self.out)
        self.assertEqual(len(self.fake.shown), 1)

    def no_rule_left(self):
        self.assertEqual(os.listdir(self.d + '/sudoers.d'), [])
        self.assertFalse(lfg.free_mode())

    def test_visudo_refusing_the_rule_leaves_nothing(self):
        self.write('visudo_fail', '1')
        self.at_terminal([0])
        self.assertEqual(self.lfg('setup'), 1)
        self.assertIn("visudo didn't accept it", self.out)
        self.no_rule_left()
        self.assertEqual([c for c in self.calls() if c.startswith(('visudo', 'sudo-n'))],
                         ['visudo -c -q', 'visudo -c -q -f %s.new' % self.rule()])  # never moved in, never tried

    def test_visudo_refusing_the_whole_setup_takes_the_rule_out(self):
        self.write('visudo_full_fail', '1')
        self.at_terminal([0])
        self.assertEqual(self.lfg('setup'), 1)
        self.assertIn("didn't pass visudo's check", self.out)
        self.assertIn('nothing changed', self.out)
        self.no_rule_left()

    def test_a_sudo_setup_already_broken_is_left_alone(self):
        self.write('visudo_broken', '1')
        self.at_terminal([0])
        self.assertEqual(self.lfg('setup'), 1)
        self.assertIn('already fail', self.out)
        self.assertIn('nothing changed', self.out)
        self.no_rule_left()
        self.assertEqual([c for c in self.calls() if c.startswith(('visudo', 'chown'))], ['visudo -c -q'])

    def test_a_rule_sudo_ignores_is_taken_out_again(self):
        self.write('no_includedir', '1')
        self.at_terminal([0])
        self.assertEqual(self.lfg('setup'), 1)
        self.assertIn("sudo doesn't use it, so it was taken out again", self.out)
        self.assertIn('nothing changed', self.out)
        self.no_rule_left()

    def test_a_rule_lfg_could_never_see_is_taken_out_again(self):
        """A Mac whose sudoers folder this account can't look into: lfg would add the rule and never know."""
        real, rule = os.path.exists, self.rule()
        self.at_terminal([0])
        with mock.patch.object(lfg.os.path, 'exists', side_effect=lambda p: False if p == rule else real(p)):
            self.assertEqual(self.lfg('setup'), 1)
        self.assertIn("can't see into", self.out)
        self.assertIn('nothing changed', self.out)
        self.no_rule_left()
        self.assertFalse([c for c in self.calls() if c.startswith('sudo-n')])  # never tried, never kept

    def test_a_rule_that_cannot_be_taken_out_again_says_so(self):
        self.write('no_includedir', '1')
        self.at_terminal([0])
        real_run = lfg.run
        def run(args, pw=None, **kw):  # root's rm fails
            if 'lfg-unsetup' in args:
                return subprocess.CompletedProcess(args, 1, '', 'rm: Operation not permitted')
            return real_run(args, pw, **kw)
        with mock.patch.object(lfg, 'run', side_effect=run):
            self.assertEqual(self.lfg('setup'), 1)
        self.assertIn('it is still there', self.out)
        self.assertIn('lfg setup --undo', self.out)
        self.assertNotIn('nothing changed', self.out)

    def test_yes_at_the_first_start_that_fails_starts_nothing_and_says_so(self):
        self.write('visudo_fail', '1')
        self.at_terminal([0])
        self.assertEqual(self.lfg('2'), 1)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(self.read('sleepdisabled').strip(), '0')
        self.assertEqual(self.read('lock').strip(), 'immediate')
        self.assertEqual(self.loops(), (0, 0))
        self.assertIn('nothing changed', self.out)
        self.assertIn("lfg isn't on. Type lfg again to start it the usual way.", self.out)
        os.remove(self.d + '/visudo_fail')
        self.assertEqual(self.lfg('2'), 0, self.out)  # and the next lfg is the usual run, no question
        self.assertEqual(len(self.fake.shown), 1)
        self.assertEqual(self.read('lock').strip(), 'off')

    def test_yes_then_no_password_asks_again_next_time(self):
        self.at_terminal([0, 1])
        self.answers = ['nope', 'nope', 'nope']
        self.assertEqual(self.lfg('2'), 1)
        self.assertFalse(os.path.exists(self.d + '/state/setup-asked'))
        self.assertEqual(self.lfg('2'), 0, self.out)  # asked again; No this time, then the usual run
        self.assertEqual(len(self.fake.shown), 2)
        self.assertEqual(self.read('state/setup-asked').split()[0], 'no')

    def test_ctrl_c_at_the_password_after_yes_changes_nothing_and_asks_again(self):
        self.at_terminal([0])
        self.answers = [KeyboardInterrupt]
        self.assertEqual(self.lfg('2'), 130)
        self.assertIn('nothing changed', self.out)
        self.no_rule_left()
        self.assertFalse(os.path.exists(self.d + '/state/setup-asked'))

    def test_esc_is_no_and_the_screen_says_so(self):
        self.at_terminal([None])
        self.assertEqual(self.lfg('2'), 0, self.out)
        self.assertEqual(self.read('state/setup-asked').split()[0], 'no')
        self.assertIn('Esc is No', self.fake.shown[0][3])
        self.assertEqual(self.read('lock').strip(), 'off')  # the usual run

    def test_uninstall_waits_for_another_window(self):
        """Another window has the setup question open: uninstall must not pull lfg out from under it."""
        import fcntl
        Shared.make_install(self)
        with open(os.path.join(lfg.STATE, 'lock'), 'w') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            self.assertEqual(self.lfg('uninstall'), 1)
        self.assertIn('busy in another window', self.out)
        self.assertTrue(os.path.exists(lfg.SHARE + '/lfg'))
        self.assertTrue(os.path.islink(self.d + '/onpath/lfg'))

    def test_three_wrong_passwords_at_setup_change_nothing(self):
        self.at_terminal([0])
        self.answers = ['nope', 'nope', 'nope']
        self.assertEqual(self.lfg('setup'), 1)
        self.no_rule_left()
        self.assertFalse([c for c in self.calls() if not c.startswith('sudo-refused')])

    def test_the_password_is_never_on_a_command_line_or_in_a_file(self):
        self.at_terminal([0])
        self.answers = ['nope', PW]
        self.assertEqual(self.lfg('setup'), 0, self.out)
        self.lfg('2')
        self.lfg('off')
        found = [p for p, _, fs in os.walk(self.d) for f in fs if f not in STUBS and PW in self.read(
            os.path.relpath(os.path.join(p, f), self.d))]
        self.assertEqual(found, [])
        self.assertNotIn(PW, ' '.join(lfg.rule_lines()))

    def test_undo_takes_the_rule_out_and_the_usual_way_comes_back(self):
        self.set_up_free()
        self.answers = [PW]
        self.assertEqual(self.lfg('setup', '--undo'), 0, self.out)
        self.assertIn('asks for your password again', self.out)
        self.no_rule_left()
        self.answers = [PW]
        self.assertEqual(self.lfg('2'), 0, self.out)
        self.assertEqual(len(self.asked), 2)
        self.assertEqual(self.read('lock').strip(), 'off')
        self.assertEqual(self.loops(), (1, 1))
        self.assertEqual(len(self.fake.shown), 1)  # and the question doesn't come back

    def test_off_ends_its_loop_at_once_so_undo_can_follow_straight_after(self):
        self.set_up_free()
        with mock.patch.object(lfg, 'TICK', 30):  # the real Mac's pace: a loop looks only every so often
            self.assertEqual(self.lfg('2'), 0, self.out)
            t = time.time()
            self.assertEqual(self.lfg('off'), 0, self.out)
            self.assertEqual(self.solo(), 0)
            self.assertTrue(self.wait(lambda: not self.awake_running(), 3))
            self.answers = [PW]
            self.assertEqual(self.lfg('setup', '--undo'), 0, self.out)
            self.assertLess(time.time() - t, 10)
        self.assertNotIn('finishing', self.out)
        self.no_rule_left()
        self.assertEqual(self.read('sleepdisabled').strip(), '0')

    def test_undo_is_refused_while_on(self):
        self.set_up_free()
        self.lfg('2')
        self.assertEqual(self.lfg('setup', '--undo'), 1)
        self.assertIn('lfg off first', self.out)
        self.assertTrue(os.path.exists(self.rule()))

    def test_undo_with_lid_sleep_left_off_puts_it_back(self):
        self.set_up_free()
        self.write('sleepdisabled', '1')
        self.answers = [PW]
        self.assertEqual(self.lfg('setup', '--undo'), 0, self.out)
        self.no_rule_left()
        self.assertEqual(self.read('sleepdisabled').strip(), '0')

    def test_undo_with_nothing_to_undo(self):
        self.assertEqual(self.lfg('setup', '--undo'), 0)
        self.assertIn('nothing to undo', self.out)
        self.assertEqual(self.calls(), [])

    def test_undo_without_a_terminal_changes_nothing(self):
        self.set_up_free()
        self.answers = [lfg.NoTerminal]
        self.assertEqual(self.lfg('setup', '--undo'), 1)
        self.assertIn('run lfg setup --undo in Terminal', self.out)
        self.assertTrue(os.path.exists(self.rule()))

    def make_install(self):
        Shared.make_install(self)

    def test_uninstall_takes_the_rule_out_first(self):
        self.set_up_free()
        self.make_install()
        self.answers = [PW]
        self.assertEqual(self.lfg('uninstall'), 0, self.out)
        self.no_rule_left()
        self.assertFalse(os.path.exists(lfg.SHARE))
        self.assertFalse(os.path.lexists(self.d + '/onpath/lfg'))
        self.assertFalse(os.path.exists(lfg.STATE))

    def test_uninstall_without_a_terminal_removes_nothing(self):
        self.set_up_free()
        self.make_install()
        self.answers = [lfg.NoTerminal]
        self.assertEqual(self.lfg('uninstall'), 1)
        self.assertIn('run lfg uninstall in Terminal', self.out)
        self.assertTrue(os.path.exists(self.rule()))
        self.assertTrue(os.path.exists(lfg.SHARE + '/lfg'))
        self.assertTrue(os.path.islink(self.d + '/onpath/lfg'))

    def test_uninstall_with_a_wrong_password_removes_nothing(self):
        self.set_up_free()
        self.make_install()
        self.answers = ['nope', 'nope', 'nope']
        self.assertEqual(self.lfg('uninstall'), 1)
        self.assertTrue(os.path.exists(self.rule()))
        self.assertTrue(os.path.exists(lfg.SHARE + '/lfg'))

    def test_uninstall_is_refused_while_on(self):
        self.set_up_free()
        self.make_install()
        self.lfg('2')
        self.assertEqual(self.lfg('uninstall'), 1)
        self.assertIn('lfg off', self.out)
        self.assertTrue(os.path.exists(self.rule()))

    def test_a_rule_that_stopped_working_stops_the_start_cleanly(self):
        self.set_up_free()
        self.write('no_includedir', '1')
        self.assertEqual(self.lfg('2'), 1)
        self.assertIn("no-password rule didn't work", self.out)
        self.assertNotIn('No password needed', self.out)
        self.assertIn('lfg is off', self.out)
        self.assertEqual(self.read('sleepdisabled').strip(), '0')
        self.assertTrue(self.wait(lambda: self.solo() == 0, 5))
        self.assertEqual(self.asked, [])

    def test_off_when_the_rule_stopped_working_says_what_to_do(self):
        self.set_up_free()
        self.lfg('2')
        self.write('no_includedir', '1')
        self.assertEqual(self.lfg('off'), 1)
        self.assertIn('lid sleep is still off', self.out)
        self.assertIn('lfg setup --undo', self.out)
        self.assertTrue(self.wait(lambda: self.solo() == 0, 5))
        self.answers = [PW]
        self.assertEqual(self.lfg('setup', '--undo'), 0, self.out)  # that puts it right
        self.assertEqual(self.read('sleepdisabled').strip(), '0')
        self.no_rule_left()

    def test_a_name_sudo_would_misread_is_written_as_the_user_id(self):
        for name, who in (('udayan', 'udayan'), ('first.last', 'first.last'), ('a b', '#%d' % os.getuid()),
                          ('x,y', '#%d' % os.getuid()), ('9lives', '#%d' % os.getuid()), ('BOB', '#%d' % os.getuid()),
                          ('Udayan', '#%d' % os.getuid())):
            fake = pwd.struct_passwd((name, 'x', os.getuid(), 20, '', '/', '/bin/zsh'))
            with mock.patch.object(lfg.pwd, 'getpwuid', return_value=fake):
                comment, rule = lfg.rule_lines()
            self.assertTrue(rule.startswith(who + ' ALL = (root) NOPASSWD: '), rule)
            self.assertTrue(comment.startswith('# '))
            self.assertNotIn('\n', comment + rule)

    def test_the_question_fits_80_columns(self):
        lines = lfg.question_frame(lfg.SETUP_TITLE, lfg.SETUP_NOTES, lfg.SETUP_ROWS, 0, lfg.SETUP_HINT)
        for l in lines:
            self.assertLess(len(''.join(t for t, _ in l)), 80, l)


# lfg 1.0.0 as released (main before no-password mode): what everyone who doesn't opt in must keep getting.
BASE = '4b4ff96'


class SameAsBefore(unittest.TestCase):
    """Runs the released 1.0.0 and this lfg through the same steps, each in its own sandbox with the same
    stand-ins, and compares everything they did: exit codes, words on screen, password prompts, every Mac command
    in order, the loops' command lines and the files left behind. For someone who hasn't opted in (no terminal,
    or a terminal and 'No' already answered), the two must match exactly."""

    @classmethod
    def setUpClass(cls):
        here = os.path.dirname(os.path.abspath(__file__))
        r = subprocess.run(['git', '-C', here, 'show', BASE + ':lfg'], capture_output=True, text=True)
        if r.returncode:
            raise unittest.SkipTest('no git history here to compare with lfg 1.0.0')
        cls.tmp = tempfile.mkdtemp(prefix='lfgbase-')
        with open(cls.tmp + '/lfg_base', 'w') as f:
            f.write(r.stdout)
        cls.base = load('lfg_base', cls.tmp + '/lfg_base')
        assert cls.base.VERSION == '1.0.0'

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def transcript(self, mod, steps, tty=False, answered=None):
        d = os.path.realpath(tempfile.mkdtemp(prefix='lfgtwin-'))
        os.makedirs(d + '/bin')
        os.makedirs(d + '/sudoers.d')
        for name, body in STUBS.items():
            with open('%s/bin/%s' % (d, name), 'w') as f:
                f.write(body)
            os.chmod('%s/bin/%s' % (d, name), 0o755)
        b, state = d + '/bin/', d + '/state'
        values = dict(SUDO=b + 'sudo', PMSET=b + 'pmset', SYSADMINCTL=b + 'sysadminctl', CAFFEINATE=b + 'caffeinate',
                      OSASCRIPT=b + 'osascript', PS=b + 'ps', ID=b + 'id', CURL=b + 'curl', VISUDO=b + 'visudo',
                      CHOWN=b + 'chown', SUDOERS_D=d + '/sudoers.d', ROOT=ME, TICK=1, STATE=state,
                      DEADLINE=state + '/until', LOG=state + '/log', STARTED=state + '/started',
                      LOCK_BEFORE=state + '/lock-before', ASKED=state + '/setup-asked', SHARE=d + '/share',
                      HOME=d + '/home')
        values = {k: v for k, v in values.items() if hasattr(mod, k)}  # 1.0.0 has no setup paths
        answers, asked = [], []

        def fake_ask(prompt):
            asked.append(prompt)
            a = answers.pop(0) if answers else PW
            if a == 'NO_TERMINAL':
                raise mod.NoTerminal
            if isinstance(a, type) and issubclass(a, BaseException):
                raise a
            return a

        patches = [mock.patch.multiple(mod, **values), mock.patch.dict(os.environ, {'LFG_TEST_DIR': d, 'NO_COLOR': '1'}),
                   mock.patch.object(mod, 'ask', side_effect=fake_ask),
                   mock.patch.object(mod, 'Menu', side_effect=OSError('no terminal in tests'))]
        if hasattr(mod, 'can_ask'):
            patches.append(mock.patch.object(mod, 'can_ask', return_value=tty))
        for p in patches:
            p.start()
        try:
            for name in SANDBOXED:
                if hasattr(mod, name) and not getattr(mod, name).startswith(d + '/'):
                    raise SystemExit('refusing to run: %s is not the sandbox stand-in' % name)
            os.makedirs(state)
            if answered:
                with open(state + '/setup-asked', 'w') as f:
                    f.write(answered)
            for name, text in (('sleepdisabled', '0'), ('lock', 'immediate')):
                with open('%s/%s' % (d, name), 'w') as f:
                    f.write(text)
            return self.play(mod, d, steps, answers, asked)
        finally:
            table = subprocess.run(['/bin/ps', '-axww', '-o', 'pid=,command='], capture_output=True, text=True).stdout
            for line in table.splitlines():
                pid, _, cmd = line.strip().partition(' ')
                if d + '/' in cmd and int(pid) != os.getpid():
                    try:
                        os.kill(int(pid), signal.SIGKILL)
                    except OSError:
                        pass
            for p in reversed(patches):
                p.stop()
            shutil.rmtree(d, ignore_errors=True)

    # Commands lfg itself runs, in order. The loops' own (lid sleep back on at the end, the relock window,
    # caffeinate) run on their own clock: they are compared as a whole at the end instead.
    MAIN = ('sudo', 'sysadminctl', 'visudo', 'chown', 'curl')

    def play(self, mod, d, steps, answers, asked):
        def norm(text):
            text = text.replace(d, '<D>')
            text = re.sub(r'\b\d{10}\b', '<T>', text)
            text = re.sub(r'\b\d{1,2}:\d\d( tomorrow| [A-Z][a-z]{2})?', '<clock>', text)
            text = re.sub(r'\b\d+h \d\dm\b|\b\d+m\b', '<span>', text)
            text = re.sub(r'-w \d+', '-w <pid>', text)
            return re.sub(r'\d+% done', '<n>% done', text)

        def read(name):
            try:
                with open('%s/%s' % (d, name)) as f:
                    return f.read().strip()
            except OSError:
                return '-'

        def calls():
            text = read('calls.log')
            return [] if text == '-' else text.splitlines()

        def settle(secs=30):
            """Loops of a run that is over are let finish, so what is left over doesn't depend on timing."""
            stop = time.time() + secs
            while time.time() < stop:
                s = mod.status()
                if s['kind'] == 'on' or not (s['admin'] or s['awake']):
                    break
                time.sleep(0.2)
            time.sleep(0.3)

        out, seen = [], 0
        for step in steps:
            kind, arg = step[0], step[1:]
            if kind == 'write':
                with open('%s/%s' % (d, arg[0]), 'w') as f:
                    f.write(arg[1])
                continue
            if kind == 'answers':
                answers[:] = list(arg[0])
                continue
            if kind == 'install':  # what Shared.make_install sets up, for uninstall
                os.makedirs(mod.SHARE)
                open(mod.SHARE + '/lfg', 'w').close()
                os.makedirs(d + '/onpath')
                os.symlink(mod.SHARE + '/lfg', d + '/onpath/lfg')
                os.environ['PATH'] = d + '/onpath:' + os.environ.get('PATH', '')
                continue
            if kind == 'wait-end':
                stop = time.time() + 30
                while time.time() < stop and mod.status()['kind'] == 'on':
                    time.sleep(0.2)
                settle()
                continue
            buf = io.StringIO()
            with redirect_stdout(buf):
                if kind == 'pick':
                    with mock.patch.object(mod, 'pick', side_effect=lambda s: mod.choices(s)[arg[0]]):
                        rc = mod.main([])
                else:
                    rc = mod.main(list(arg))
            settle()
            now = calls()
            new, seen = now[seen:], len(now)
            s = mod.status()
            until = read('state/until')
            left = round((int(until) - time.time()) / 60) if until.isdigit() and int(until) > 0 else until
            out.append(dict(
                step=' '.join(map(str, step)), rc=rc, said=norm(buf.getvalue()).splitlines(),
                prompts=list(asked), mac=[norm(c) for c in new if c.startswith(self.MAIN)],
                loops=sorted(norm(p['cmd']) for p in s['admin'] + s['awake']), kind=s['kind'],
                files=dict(sleepdisabled=read('sleepdisabled'), lock=read('lock'), before=read('state/lock-before'),
                           until_minutes_left=left, share=os.path.exists(d + '/share'),
                           onpath=os.path.lexists(d + '/onpath/lfg'), sudoers=os.listdir(d + '/sudoers.d'))))
            asked.clear()
        everything = [norm(c) for c in calls()]
        return out + [dict(all_mac=sorted(c for c in everything if not c.startswith('caffeinate')),
                           caffeinate=sorted({c for c in everything if c.startswith('caffeinate')}))]

    def same(self, steps):
        want = self.transcript(self.base, steps)
        for tty, answered in ((False, None), (True, 'no 2026-11-28')):
            got = self.transcript(lfg, steps, tty=tty, answered=answered)
            for a, b in zip(want, got):
                self.assertEqual(a, b, 'differs from lfg 1.0.0 (terminal=%s, answered=%s)' % (tty, answered))
            self.assertEqual(len(want), len(got))

    def test_start_extend_list_status_off_uninstall(self):
        self.same([('lfg', '2'), ('lfg', '+1'), ('lfg', 'status'), ('lfg', '1'), ('pick', 0), ('pick', 4),
                   ('lfg', '3'), ('lfg', 'off'), ('lfg', 'status'), ('lfg', 'off'), ('lfg', '+2'),
                   ('install',), ('lfg', 'uninstall')])

    def test_turn_off_from_the_list(self):
        self.same([('lfg',), ('pick', 3), ('lfg', 'status')])

    def test_a_natural_end_and_the_relock_window(self):
        self.same([('write', 'lock', '300'), ('lfg', '0.0015'), ('wait-end',), ('lfg', 'status'),
                   ('lfg', 'off')])

    def test_passwords_no_terminal_and_ctrl_c(self):
        self.same([('answers', ['nope', 'nope', 'nope']), ('lfg', '2'),
                   ('answers', ['nope', PW]), ('lfg', '2'), ('lfg', 'off'),
                   ('answers', ['NO_TERMINAL']), ('lfg', '2'),
                   ('answers', [KeyboardInterrupt]), ('lfg', '2'), ('lfg', 'status')])

    def test_standard_account_stuck_sleep_and_a_left_off_lock(self):
        self.same([('write', 'groups', 'staff everyone localaccounts'), ('answers', ['whatever']), ('lfg', '2'),
                   ('write', 'groups', 'staff everyone admin'),
                   ('write', 'sleepdisabled', '1'), ('lfg', 'status'), ('lfg', '1'), ('lfg', 'off'),
                   ('write', 'lock', 'off'), ('write', 'state/lock-before', '300'), ('lfg', 'status'),
                   ('install',), ('lfg', 'uninstall'), ('lfg', 'off'), ('lfg', 'status')])


class Pieces(unittest.TestCase):
    def test_parse(self):
        P = lfg.parse
        self.assertEqual(P([]), ('start', None))
        self.assertEqual(P(['6']), ('start', 6.0))
        self.assertEqual(P(['+2']), ('add', 2.0))
        self.assertEqual(P(['0']), ('off', None))
        self.assertEqual(P(['off']), ('off', None))
        self.assertEqual(P(['status']), ('status', None))
        self.assertEqual(P(['--version']), ('version', None))
        self.assertEqual(P(['update']), ('update', None))
        self.assertEqual(P(['uninstall']), ('uninstall', None))
        for bad in (['-1'], ['73'], ['x'], ['nan'], ['inf'], ['+0']):
            self.assertEqual(P(bad)[0], 'bad', bad)

    def test_ps_time(self):
        self.assertEqual(lfg.seconds('09:34'), 574)
        self.assertEqual(lfg.seconds('02:00:01'), 7201)
        self.assertEqual(lfg.seconds('1-02:00:01'), 93601)

    def test_keys(self):
        self.assertEqual(lfg.press(4, 0, 'down'), (1, None))
        self.assertEqual(lfg.press(4, 0, 'up'), (3, None))
        self.assertEqual(lfg.press(4, 2, 'enter'), (2, 'go'))
        self.assertEqual(lfg.press(4, 0, '3'), (2, 'go'))
        self.assertEqual(lfg.press(4, 0, '9'), (0, None))
        self.assertEqual(lfg.press(4, 1, 'esc'), (1, 'quit'))

    def test_custom_typing(self):
        T = lfg.type_key
        self.assertEqual(T('', '2'), ('2', None))
        self.assertEqual(T('2', '.'), ('2.', None))
        self.assertEqual(T('2.', '5'), ('2.5', None))
        self.assertEqual(T('2.5', '\x7f'), ('2.', None))
        self.assertEqual(T('2', 'x'), ('2', None))
        self.assertEqual(T('2', 'q'), ('2', None))  # q types nothing here; it does not leave
        self.assertEqual(T('12345', '6'), ('12345', None))
        self.assertEqual(T('2', 'enter'), ('2', 'go'))
        self.assertEqual(T('2', 'esc'), ('2', 'back'))
        now = 1_800_000_000
        s = dict(now=now, until=now + 3600)
        self.assertEqual(lfg.custom_hours(s, '4'), (4.0, ''))
        self.assertEqual(lfg.custom_hours(s, ''), (None, ''))
        self.assertIn('more than 0', lfg.custom_hours(s, '0')[1])
        self.assertIn('at most 71', lfg.custom_hours(s, '80')[1])
        self.assertIn('number', lfg.custom_hours(s, '1.2.3')[1])

    def test_list_shows_where_the_run_stands(self):
        now = 1_800_000_000
        s = dict(now=now, until=now + 13 * 3600 + 18 * 60, started=now - 6000, kind='on')
        text = lambda lines: '\n'.join(''.join(t for t, _ in l) for l in lines)
        f = text(lfg.frame(s, lfg.choices(s), 0, 80))
        self.assertIn('Started', f)
        self.assertIn('1h 40m ago', f)
        self.assertIn('13h 18m left', f)
        self.assertIn('Custom…', f)
        self.assertEqual([r[0] for r in lfg.choices(s)], ['add', 'add', 'custom', 'off', 'leave'])
        f = text(lfg.frame(s, lfg.choices(s), 2, 80, typing='4'))
        self.assertIn('Add 4▏ hours', f)
        self.assertIn('% done', f)
        self.assertIn('until ' + lfg.clock(s['until'] + 4 * 3600, now), f)
        self.assertTrue(all(len(l) < 80 for l in f.splitlines()))

    def test_clock(self):
        import datetime
        now = datetime.datetime(2026, 11, 22, 21, 0).timestamp()
        self.assertEqual(lfg.clock(now + 3600, now), '22:00')
        self.assertEqual(lfg.clock(now + 15 * 3600, now), '12:00 tomorrow')
        self.assertEqual(lfg.clock(now + 40 * 3600, now), '13:00 Tue')


if __name__ == '__main__':
    unittest.main(verbosity=2)
