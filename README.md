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

**Left-click** opens Signal History — it costs nothing, needs no router session
and works while the line is down, which is when you want it. **Right-click** is
the menu: the verdict in two short lines, then Open Router Portal, the device
tasks, Refresh now, Pause and Start with Windows. Hover for the numbers. Those
tasks come from the same table that builds the Explorer menu and the Jump List,
so all three stay in step.

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

Asking for it again does not open a second copy: the page is rebuilt from the
CSV and the window already on screen is raised, re-reading the file as it comes
back to the front. One window, however many times it is clicked — and the one
you get is always current, not whenever you first opened it.

Underneath it is a plain CSV at `%LOCALAPPDATA%\RouterOps\signal-history.csv`,
one row per sample, pruned to 30 days (~150 KB/day). Each minute takes the
*worst* state seen in it, so a 40-second drop still shows up instead of being
averaged away.

Directly under the strips, on the same axis and the same width, is the
hour-of-day profile across the whole window — which hours the link was
unusable, and which one is worst. It shares the strips' scale on purpose: a red
patch at 20:00 on Tuesday sits straight above the bar that says how usual 20:00
is. That is the part you can plan around: if the tower's backup gives out
around eight most evenings, this says so in one line instead of a feeling.

### The health number, and whether the line is worth it

The page opens with a single **network health** figure, 0–100, and one line
under it that says whether the line is worth its money. Two independent things
decide that, and both are printed beside the number — a score with its workings
hidden is one nobody can act on:

| | |
|---|---|
| **availability** | share of watched minutes the link was usable (weighted 0.8) |
| **speed** | average of the speed checks against the 15 Mbps bar — hitting it scores 75, not 100 (weighted 0.2) |

Availability carries most of the weight because the two inputs are not equally
trustworthy. Availability comes from a sample every 30 s around the clock;
speed comes from a handful of checks run whenever someone felt like running
one. A small, self-selected series should not be able to swing the number much.

### Speed checks

Every Speed Check records what fast.com settles on. They are irregular by nature
— you run one when you want one — so they are kept in their own CSV and shown,
at the foot of the page, as a run of readings against the **15 Mbps** bar: the
point where calls stop hurting and ordinary use stops waiting on the network.
The verdict on them is in the health line at the top; the foot is just the
numbers behind it.

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

Against a person with the portal open — who holds no mutex — repeated evictions
back off, doubling to a four-minute ceiling, rather than fighting for the slot.

One firmware quirk is worth knowing, because it is not guessable. The router
hands out a cookie for a login **even when its one slot is still occupied**, so
the login appears to succeed and every request on it returns the logout stub.
Measured: three plain re-logins in a row all came back 362 bytes; one
`/logout.cgi` with the dead cookie, then the same login, returned 4,752 bytes of
data. So the evicted cookie is kept and spent on the way into the next login.
Without that, every drop cost minutes of blindness until the firmware timed the
ghost session out; with it, a drop costs one 30-second sample.

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
