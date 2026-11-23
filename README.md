# lfg

Keep your Mac awake with the lid shut for a few hours, then let it go back to normal by itself. Useful when a long download, a render or an AI agent should keep working while you close the laptop and walk away.

```
curl -fsSL https://raw.githubusercontent.com/udayanwalvekar/lfg/main/lfg.sh | bash
```

That's the whole install. You can also download [`lfg.sh`](lfg.sh) and run `bash lfg.sh`.

**Needs:** a Mac, an admin account on it (you type your Mac password to start and to stop), and Python 3.9 or newer (it comes with Apple's Command Line Tools: `xcode-select --install`).

The installer puts the program in `~/.local/share/lfg` and the `lfg` command in a folder already on your PATH. It changes no Mac settings. (Only if no suitable folder is on your PATH does it add `~/.local/bin` to it, in `~/.zshrc`, or `~/.bash_profile` if your shell is bash.)

## Commands

| Type | What happens |
|---|---|
| `lfg` | On for 3 hours. Asks your Mac password once. |
| `lfg 6` | On for 6 hours from now. Never cuts a longer run short. |
| `lfg` (while on) | Shows when it started, when it ends and how long is left, then lets you add 3 hours, 6 hours or any number you type, turn it off, or leave it. |
| `lfg +2` | Adds 2 hours to the current end. No password while it's on. |
| `lfg off` | Off now: sleep and your screen lock come back. Asks your password. |
| `lfg status` | Says whether it is on and until when. Changes nothing. |
| `lfg update` | Gets the newest lfg. A run that is on keeps going. |
| `lfg uninstall` | Removes lfg. If a run is on or left something off, it asks you to run `lfg off` first. |

## What it does

While it's on, the Mac doesn't sleep when you close the lid, stays on Wi-Fi, and doesn't lock its screen. When the time is up, lid sleep comes back by itself and a Terminal window asks for your password to put the screen lock back the way you had it (if you keep it off, there's no window and it stays off). If that window doesn't show up, run `lfg off`.

- **Anyone who opens the lid gets in without a password while it's on.** Keep the laptop with you, or turn lfg off before you leave it somewhere.
- Don't put the Mac in a bag while it's on: a closed laptop that stays awake gets hot.
- Adding time needs no password, so an agent can run `lfg +3` while you're away. That also means any program on your Mac can push the end time out, up to 72 hours ahead.
- If the Mac restarts during a run, lid sleep stays off until you run `lfg off`. `lfg status` tells you when that's the case.
- Every step is written to `~/.lfg/log`. Your password never is.

## Remove it

```
lfg off
lfg uninstall
```

This removes the program, the `lfg` command and `~/.lfg`.

## Licence

MIT
