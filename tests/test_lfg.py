#!/usr/bin/env python3 -I
"""lfg tests. Everything that would touch the Mac (sudo, pmset, sysadminctl, caffeinate, osascript, ps, kill)
(and id) is a stand-in script in a throwaway folder that only writes to a log. The tests refuse to run if any of them
resolves to the real program. Run: python3 -I tests/test_lfg.py"""
import io, os, pwd, re, shutil, signal, subprocess, sys, tempfile, time, unittest, warnings
warnings.simplefilter('ignore', ResourceWarning)  # the background loops are meant to outlive lfg
from contextlib import redirect_stdout
from importlib.machinery import SourceFileLoader
from unittest import mock

lfg = SourceFileLoader('lfg', os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lfg')).load_module()
ME = pwd.getpwuid(os.getuid()).pw_name
PW = 'testpw'

STUBS = {
    'sudo': r'''#!/bin/bash
D="$LFG_TEST_DIR"
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
  if [ "$(cat "$D/lock" 2>/dev/null)" = off ]; then echo "x sysadminctl[1:2] screenLock is off" >&2
  else echo "x sysadminctl[1:2] screenLock delay is immediate" >&2; fi
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
    # Never kills anything real: only logs.
    'kill': r'''#!/bin/bash
echo "kill $*" >> "$LFG_TEST_DIR/calls.log"
exit 0
''',
    # The groups this user is in: an admin unless the test says otherwise.
    'id': r'''#!/bin/bash
cat "$LFG_TEST_DIR/groups" 2>/dev/null || echo "staff everyone admin"
''',
}


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
            OSASCRIPT=b + 'osascript', PS=b + 'ps', KILL=b + 'kill', ID=b + 'id', ROOT=ME, TICK=1,
            STATE=state, DEADLINE=state + '/until', LOG=state + '/log', STARTED=state + '/started',
            SHARE=self.d + '/share', HOME=self.d + '/home')]
        for p in self.patches:
            p.start()
        # Fail closed: nothing below may run if a real program would be used.
        for name in ('SUDO', 'PMSET', 'SYSADMINCTL', 'CAFFEINATE', 'OSASCRIPT', 'PS', 'KILL', 'ID', 'STATE', 'DEADLINE',
                     'LOG', 'STARTED', 'SHARE', 'HOME'):
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
        self.lfg('0.002')  # ~7 s
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

    def test_a_standard_account_is_told_and_never_asked(self):
        self.write('groups', 'staff everyone localaccounts')
        self.assertEqual(self.lfg('2'), 1)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.calls(), [])
        self.assertIn('needs an admin account', self.out)

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

    def test_update_refuses_what_is_not_lfg(self):
        class Fake:
            def __init__(self, body): self.body = body
            def read(self): return self.body
        with mock.patch.object(lfg.urllib.request, 'urlopen', return_value=Fake(b'<html>404</html>')), \
                mock.patch.object(lfg.subprocess, 'run') as ran:
            self.assertEqual(lfg.update([]), 1)
            ran.assert_not_called()
        same = b"#!/usr/bin/env bash\nLFG_PROGRAM_END\nVERSION = '%s'\n" % lfg.VERSION.encode()
        with mock.patch.object(lfg.urllib.request, 'urlopen', return_value=Fake(same)), \
                mock.patch.object(lfg.subprocess, 'run') as ran, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(lfg.update([]), 0)
            ran.assert_not_called()
        self.assertIn('newest', out.getvalue())


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
