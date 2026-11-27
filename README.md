# lfg

**LFG = let's fucking go.** Type `lfg` and shut the lid: your Mac keeps working for as long as you said, then it can sleep again.

I wanted my laptop to stay awake with the lid shut while things kept running: a long download, a render, an AI agent working through the night. I tried `caffeinate`, I tried apps, and none of them did the whole job. So I made my own.

```
curl -fsSL https://raw.githubusercontent.com/udayanwalvekar/lfg/main/lfg.sh | bash
```

That's the whole install. You can also download [`lfg.sh`](lfg.sh) and run `bash lfg.sh`.

**Needs:** a Mac, an admin account on it (you type your Mac password to start and to stop, or only once if you run `lfg setup`), and Python 3.9 or newer (it comes with Apple's Command Line Tools: `xcode-select --install`).

The installer puts the program in `~/.local/share/lfg` and the `lfg` command in a folder already on your PATH. It changes no Mac settings. (Only if no suitable folder is on your PATH does it add `~/.local/bin` to it, in `~/.zshrc`, or `~/.bash_profile` if your shell is bash.)

## How to use it

**Just type `lfg` and press Enter.** Type your Mac password once and it's on for 3 hours.

<img src="docs/start.png" alt="Typing lfg: it asks for the Mac password once, then says it is on until 22:17 with 3 hours left" width="700">

**Want a set number of hours?** Write the hours: `lfg 6` is 6 hours, `lfg 1.5` is an hour and a half.

**Already on?** Type `lfg` again. It shows when it started, when it ends and how much time is left, then lets you add 3 hours, 6 hours or any number you type, turn it off, or leave it. Adding time needs no password.

<img src="docs/picker.png" alt="lfg while on: started, ends, time left, a progress bar, and the choices Add 3 hours, Add 6 hours, Custom, Turn it off now, Leave it as it is" width="700">

Pick Custom and type the hours: the new end time shows as you type.

<img src="docs/custom.png" alt="Custom: typing 2.5 hours shows the new end time as you type" width="700">

**Done early?** `lfg off`.

**While it's on, your screen lock is off: anyone who opens the lid gets in without a password.** Keep the laptop with you. (Not if you say yes to `lfg setup`, below.)

**Don't want to type your password every time?** Type `lfg setup` and say yes. You type your password one last time, and that's it: starting, stopping and adding time never ask again. The first time you type `lfg` in Terminal, it asks you this once.

<img src="docs/setup.png" alt="lfg setup: Never ask for your password again? It explains the rule it adds, then Yes, never ask again or No, keep asking" width="700">

I made this for my WhatsApp assistant, which runs on my Mac. It doesn't need my screen unlocked. It just needs the Mac awake with the lid shut, and it can't type my password.

What saying yes changes:

- lfg adds one rule to your Mac. It lets your account turn lid sleep off and back on without a password. That's all it allows.
- Any program on your Mac could then keep it awake. Nothing more.
- lfg stops turning off your screen lock. The Mac keeps working with the lid shut, and locks like it normally does.
- Changed your mind? `lfg setup --undo` takes the rule out. It asks your password once.

Say no, and lfg works exactly like it always has.

## lfg vs caffeinate

`caffeinate` is the command that comes with every Mac. lfg uses it inside, and adds what it doesn't do:

| | `caffeinate` | `lfg` |
|---|---|---|
| Lid shut | The Mac still goes to sleep (unless it's on power with an external display) | The Mac stays awake |
| Screen lock | Not touched | Off while it runs, then back the way you had it. Not touched after `lfg setup` |
| How long | Stops when you close its Terminal window | Keeps going after you close the window; add time whenever you like |
| At the end | Stops keeping the Mac awake | Lid sleep comes back by itself; a Terminal window asks for your password to put the screen lock back |
| Password | None | Once to start, once to stop: keeping the Mac awake with the lid shut is a system setting, so it needs your admin password. Or once ever, with `lfg setup` |

## Commands

| Type | What happens |
|---|---|
| `lfg` | On for 3 hours. Asks your Mac password once. |
| `lfg 6` | On for 6 hours from now. Never cuts a longer run short. |
| `lfg` (while on) | Shows when it started, when it ends and how long is left, then lets you add 3 hours, 6 hours or any number you type, turn it off, or leave it. |
| `lfg +2` | Adds 2 hours to the current end. No password while it's on. |
| `lfg off` | Off now: sleep and your screen lock come back. Asks your password. |
| `lfg status` | Says whether it is on and until when. Changes nothing. |
| `lfg setup` | Never asks for your password again. Asks it one last time to add the rule. |
| `lfg setup --undo` | Takes the rule out. lfg asks for your password again. |
| `lfg update` | Gets the newest lfg. A run that is on keeps going. |
| `lfg uninstall` | Removes lfg, and the `lfg setup` rule if you added it (asks your password for that). If a run is on or left something off, it asks you to run `lfg off` first. |

## What it does

While it's on, the Mac doesn't sleep when you close the lid, stays on Wi-Fi, and doesn't lock its screen. When the time is up, lid sleep comes back by itself and a Terminal window asks for your password to put the screen lock back the way you had it (if you keep it off, there's no window and it stays off). If that window doesn't show up, run `lfg off`.

- **Anyone who opens the lid gets in without a password while it's on.** Keep the laptop with you, or turn lfg off before you leave it somewhere.
- Don't put the Mac in a bag while it's on: a closed laptop that stays awake gets hot.
- Adding time needs no password, so an agent can run `lfg +3` while you're away. That also means any program on your Mac can push the end time out, up to 72 hours ahead.
- After `lfg setup`, an agent can also start it (`lfg 6`) and stop it (`lfg off`), with no password and no Terminal. The rule is the file `/etc/sudoers.d/lfg-` plus your user id. It allows exactly `pmset -a disablesleep 1` and `pmset -a disablesleep 0`, and lfg checks it with `visudo` before putting it in place.
- If the Mac restarts during a run, lid sleep stays off until you run `lfg off`. `lfg status` tells you when that's the case.
- Every step is written to `~/.lfg/log`. Your password never is.

## Remove it

```
lfg off
lfg uninstall
```

This removes the program, the `lfg` command and `~/.lfg`, and the `lfg setup` rule if you added it.

## Licence

MIT
