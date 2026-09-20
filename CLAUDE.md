# RouterOps

Router control and a resident 4G readout for one machine: a Huawei B2368-66 on a
tower with unreliable backup power. Windows-only by design — the whole point is
Explorer integration and a notification-area icon.

`README.md` is the behaviour: what each icon colour means, the health score, the
session rule, measured costs. Read it before changing anything user-facing; the
numbers in it were measured against the live router, not assumed.

## Where it lives

`D:\PowerTools\RouterOps` — moved here from `D:\Frontier` on 2026-09-19, because
it is a machine tool, not a product being pushed out.

It is **its own git repo** (`github.com/TechNerdXp/RouterOps`) nested inside the
PowerTools tree, which is the one exception to the machine playbook's "no tool
has its own `.git`". Commit here, not at `D:\PowerTools` — the parent repo sees
this folder as untracked and leaves it alone.

## Build

```
pyinstaller RouterOps.spec --clean
```

One file, no console, `.env` bundled in as data, `icon.png` required at build
time. The output is `dist\RouterOps.exe` and **that path is the installation** —
there is no installer and nothing copies it anywhere else. So:

- Moving the folder moves the app, and every pointer into it has to catch up.
- Rebuild after touching any `.py`. The exe is what the tray, the Explorer menu
  and autostart actually run; the sources are not.

## Pointers into the exe, and which ones self-heal

Everything is `HKCU`, written by the app itself from `sys.executable`:

| Pointer | Written by | Self-heals? |
|---|---|---|
| Explorer context menu (`Software\Classes\exefile\shell\RouterOps*`) | `main.register_context_menu()` | Yes — rebuilt on every launch |
| Jump List | `main.register_jump_list()` | Yes — same |
| Autostart (`...\CurrentVersion\Run`, value `RouterOps`) | `tray.set_autostart()` on menu tick | Yes, since `tray.refresh_autostart_path()` |
| Start Menu shortcut `RouterOps.lnk` | by hand | **No** — fix it by hand after a move |

The autostart entry was the trap: `autostart_enabled()` only asks whether the
value exists, so a stale path still showed a tick in the menu and the only
symptom was no tray icon after a reboot. `refresh_autostart_path()` now rewrites
it on tray start. Keep that property if you touch autostart.

## Layout

| File | What |
|---|---|
| `main.py` | Shell integration, Selenium flows, arg dispatch. The only entry point. |
| `lte.py` | Browserless router session, `lteStatus.cgi` parsing. **stdlib only** — keep it that way; it is what makes the resident process cheap. |
| `diagnose.py` | Numbers → a verdict (BACKHAUL / NOSERVICE / DEGRADED / ROUTER). |
| `tray.py` | The icon, its menu, the 30 s sampling loop. The only long-lived process. |
| `history.py` | The CSV at `%LOCALAPPDATA%\RouterOps` and the 7-day page built from it. |

## Rules that are not obvious from the code

- **One router session.** The B2368-66 keeps exactly one admin session and a
  second login silently evicts the first. Any flow that logs in claims the
  `Local\RouterOps.Router` mutex for its whole run; the monitor yields, shows
  grey, and picks up after. Getting into the router always wins — the readout is
  never the reason a reboot fails.
- **Spend the evicted cookie.** The firmware hands out a cookie even when the
  slot is taken, so a login can look fine and return logout stubs forever.
  `lte.py` calls `/logout.cgi` with the dead cookie before logging back in.
  Do not "simplify" that away.
- **Nothing spins.** Two blocked threads (`GetMessageW`, `WaitForMultipleObjects`)
  are the entire idle cost. No `while True: sleep()` anywhere.
- **No notifications.** The icon colour is the notification. Only state changes
  go to the log — and cell changes (`eNB`/`PCI`/`EARFCN`) matter most.
- `.env` holds the real router credentials and is gitignored. `*.png` is too,
  except `icon.png`, which the build needs.
