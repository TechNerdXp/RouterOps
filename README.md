# RouterOps

Router control from the Windows shell — reboot, guest mode, speed check — plus a
resident tray readout of the 4G link.

## Signal Monitor

`RouterOps.exe --tray` puts a live 4G indicator in the notification area. Colour
says whether the internet works; bars say how good the radio is, and the
combination is the point:

| Icon | Means | What to do |
|---|---|---|
| Green bars | Connected, healthy | — |
| Amber bars | Attached but thin — slow, some requests failing | Nothing to fix; it is the link |
| **Red bars, still tall** | **Signal is fine, nothing beyond the tower answers** | **Wait. Rebooting cannot help — it is the tower's backhaul or its power** |
| Red, no bars | Not attached, or signal unusable | Wait; watch for the cell ID to change |
| Red bar only | The router itself is not answering on the LAN | This is the one case where Reboot Huawei is right |
| Red bar only | The router refused the login | Check `.env`; if those are right, reboot |
| Grey | Paused, another task has the router, or the session was taken | Nothing — it clears itself |

Click it — either button — for the menu: the verdict in two short lines, then
Open Router Portal, the device tasks, Refresh now, Pause, Start with Windows and
Signal History. Hover for the numbers. Those tasks come from the same table that
builds the Explorer menu and the Jump List, so all three stay in step.

It never pops up a notification — the colour is the notification. Changes worth
keeping still go to the log: the link dropping, coming back, re-attaching, and
moving to a different cell tower. That last one matters most and nothing else
surfaces it: when `eNB`/`PCI`/`EARFCN` change you have been handed to a
different tower, which is what makes one request succeed and the next one fail.

## Signal History

`RouterOps.exe --history` (also in the Network submenu, the Jump List, and the
tray's own menu) opens the last 7 days as one page in an app-mode window, the
same way the router portal opens — **one line per day**, midnight to midnight,
coloured by the same scheme as the icon.

Underneath it is a plain CSV at `%LOCALAPPDATA%\RouterOps\signal-history.csv`,
one row per sample, pruned to 30 days (~150 KB/day). Each minute takes the
*worst* state seen in it, so a 40-second drop still shows up instead of being
averaged away.

Below the strips is an hour-of-day profile across the whole window — which hours
the link was unusable, and which one is worst. That is the part you can plan
around: if the tower's backup gives out around eight most evenings, this says so
in one line instead of a feeling.

The view reads only the CSV. It never touches the router, so it needs no session
and works fine while the line is down — which is when you'd want it.

### Cost

Samples every 30 s: one HTTPS request, ~0.40 s and ~4.75 KB, measured against
the live router. Between samples both threads are blocked in the kernel —
`GetMessageW` for the UI, `WaitForMultipleObjects` for the poller — so an idle
monitor is not scheduled at all. There is no spin loop anywhere in it.

Sampling on a timer is how the metric works, not a workaround: RSRP and SINR
are continuously varying radio measurements with no change event to subscribe
to, and the router's own web UI reads the same page on a 5-second timer.

### Sharing one session

The Huawei B2368-66 keeps **exactly one** admin session — a second login
silently evicts the first. Opening the router always wins: any task that logs in
claims a named mutex for its whole flow, and the monitor will not log back in
while that is held. It drops its session, shows grey, and picks up afterwards.
Losing the readout for a minute is fine; being the reason a reboot fails is not.

## Setup

```
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set your credentials:

```
ROUTER_USERNAME=user
ROUTER_PASSWORD=your_password
```

The monitor needs nothing beyond the standard library — it talks to the router
over plain HTTP and draws its icon with ctypes. Selenium and Chrome are still
required for the browser-driven tasks (reboot, guest mode, portal, speed check).

## Build

```
pyinstaller --name RouterOps main.py --icon=icon.png --noconsole --onefile
```

###OR 

```
pyinstaller RouterOps.spec --clean
```

> Requires Chrome for the browser-driven tasks. Selenium Manager is bypassed in
> favour of the cached ChromeDriver.

## Layout

| File | What |
|---|---|
| `main.py` | Shell integration (context menu, Jump List), Selenium flows, dispatch |
| `lte.py` | Browserless router session and `lteStatus.cgi` parsing — stdlib only |
| `diagnose.py` | Turning the numbers into a verdict and detecting what changed |
| `tray.py` | The notification-area icon, its menu, and the sampling loop |
| `history.py` | The sample log and the 7-day page built from it |
