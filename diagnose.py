"""Turning LTE numbers into the one answer worth having: whose problem is this?

The tower this link depends on has unreliable backup power, so the interesting
question is never "what is my RSRP"; it is "is waiting going to fix this, or
should I touch something". Those are different failures and they look identical
from the taskbar today, which is why the same guesses get repeated every time.

Four states worth telling apart, and what each one means to do:

  BACKHAUL   radio link is healthy, nothing behind it answers. The tower is up
             and talking to us; what is behind the tower is not. Rebooting the
             router cannot help and costs two minutes. Wait.
  NOSERVICE  the modem is not attached, or attached with unusable signal. The
             tower is down or has dropped to a power level we cannot use.
             Also: wait, but this is the one that usually ends in a cell change.
  DEGRADED   attached and usable, but the margin is thin. Things will be slow
             and some requests will fail while others succeed.
  ROUTER     the router itself is not answering on the LAN. The only state where
             the existing Reboot Huawei is the right move.

Thresholds below are the ordinary LTE engineering bands. Two of the numbers are
doing most of the work, and they mean different things:

  RSRP  how much of our tower's signal reaches us: a coverage/distance measure.
  SINR  how much of it is intelligible over noise and interference: a quality
        measure. Throughput tracks SINR far more closely than RSRP.

That distinction is the whole diagnostic. Strong RSRP with poor SINR is not a
weak signal; it is a contended or interfered one, and no amount of rebooting
changes it. The live sample this was built against (RSRP -83 dBm, SINR 8 dB) is
exactly that shape: comfortable coverage, mediocre quality.
"""

# (floor, label, bar count); first row whose floor the value meets, best first.
_RSRP_BANDS = (
    (-80,  "excellent", 4),
    (-90,  "good",      3),
    (-100, "fair",      2),
    (-110, "poor",      1),
)
_SINR_BANDS = (
    (20, "excellent", 4),
    (13, "good",      3),
    (5,  "fair",      2),
    (0,  "poor",      1),
)


def _grade(value, bands):
    if value is None:
        return ("unknown", 0)
    for floor, label, bars in bands:
        if value >= floor:
            return (label, bars)
    return ("unusable", 0)


# States, worst first: the order the tray uses to decide what to show.
STARTING  = "starting"
ROUTER    = "router"
NOSESSION = "nosession"
NOLOGIN   = "nologin"
PAUSED    = "paused"
NOSERVICE = "noservice"
BACKHAUL  = "backhaul"
DEGRADED  = "degraded"
OK        = "ok"

# Whether each state means the internet is usable right now.
USABLE = {OK, DEGRADED}

# These two are not verdicts about the link; they are the absence of one. We
# were not looking, so nothing can be concluded about what happened while we
# weren't. Counting them as "down" would announce an outage and a recovery every
# time someone opens the router portal, and every time the app starts, and would
# fold that time into the outage durations that are meant to be evidence.
INDETERMINATE = {STARTING, PAUSED, NOSESSION}


class Assessment:
    """One verdict about one sample.

    Three lengths, because three places need it and they are not the same size:

      headline  the state, in a few words. Heads the tray menu.
      hint      what to do about it, in one short line, and only when there is
                something to do. A menu is a list of things to click; a
                paragraph of explanation at the top of one pushes the actual
                items down the screen and gets skipped anyway.
      detail    the numbers, for the hover tooltip where there is room.
    """

    def __init__(self, state, headline, hint, bars, sample=None, detail=""):
        self.state    = state
        self.headline = headline
        self.hint     = hint
        self.bars     = bars          # 0-4, drives the tray icon
        self.sample   = sample or {}
        self.detail   = detail

    @property
    def usable(self):
        return self.state in USABLE


def _cell_of(sample):
    """The cell we are camped on, or None when we are not camped on one.

    The modem reports eNB 0 and EARFCN 0 while it is detached, and sometimes
    N/A in all three fields instead. Either is the absence of a cell, not a
    different one, so reading it as a handover turns every service drop into a
    spurious pair of "moved to a different cell" lines (one on the way out
    and one on the way back) and buries the real handover in the noise.
    Observed exactly that during a 53-minute outage: eight cell-change lines,
    of which one was a genuine move; and the N/A form on 2026-09-22, four
    lines of "eNB 830762→N/A" and back in five minutes. PCI 0 is a real PCI,
    so only N/A rules that one out.
    """
    enb = (sample.get("enb") or "").strip()
    pci = (sample.get("pci") or "").strip()
    earfcn = (sample.get("earfcn") or "").strip()
    if not (enb and pci and earfcn) or "N/A" in (enb, pci, earfcn) \
            or enb == "0" or earfcn == "0":
        return None
    return (enb, pci, earfcn)


def assess(sample, internet, fault=None):
    """Classify one poll.

    `sample` is lte.parse() output, or None if we came back with nothing, in
    which case `fault` says why, because the reasons are not interchangeable and
    collapsing them is how an indicator ends up lying. Losing the session is not
    evidence about the router at all; saying "unreachable, reboot it" when the
    box is answering perfectly well is precisely the wrong-guess this is meant to
    put a stop to.

    `internet` is the reachability probe result, or None where it was not run.
    Comparing against the previous verdict is transitions()' job, not this one's.
    """
    if sample is None:
        if fault == "session":
            # Someone else logged in, or the firmware dropped us. The router is
            # fine and so, most likely, is the link; we simply cannot see it
            # this minute. Indeterminate, not an outage.
            return Assessment(NOSESSION, "Lost the router session",
                              "Nothing wrong; this clears itself", 0)
        if fault == "login":
            return Assessment(NOLOGIN, "Login refused by the router",
                              "Check the credentials in .env", 0)
        return Assessment(ROUTER, "Router not answering",
                          "Reboot the router; this is the case for it", 0)

    status = (sample.get("status") or "").strip()
    rsrp, sinr = sample.get("rsrp"), sample.get("sinr")
    rsrp_label, rsrp_bars = _grade(rsrp, _RSRP_BANDS)
    sinr_label, sinr_bars = _grade(sinr, _SINR_BANDS)

    # Coverage sets the ceiling and quality pulls it down, but only so far:
    # taking the plain minimum reads a perfectly ordinary link (good RSRP, fair
    # SINR, which is this link on a normal day) as two bars out of four, and an
    # indicator that sits at "weak" all day is one nobody looks at again.
    # Allowing SINR one notch of slack keeps the warning colours for links that
    # have actually degraded, while a genuinely interfered one (strong RSRP,
    # poor SINR) still drops to two bars and says so.
    bars = min(rsrp_bars, sinr_bars + 1)

    attached = status.upper().startswith("LTE")
    detail = "RSRP %s dBm (%s) · SINR %s dB (%s)" % (
        rsrp if rsrp is not None else "?", rsrp_label,
        sinr if sinr is not None else "?", sinr_label,
    )
    short = "RSRP %s · SINR %s" % (
        rsrp if rsrp is not None else "?", sinr if sinr is not None else "?")

    if not attached or rsrp_bars == 0 or sinr_bars == 0:
        return Assessment(
            NOSERVICE,
            "No LTE service" if attached else "Not attached (%s)" % (status or "unknown"),
            "Tower is down; rebooting won't help",
            0, sample, detail,
        )

    # Attached with usable signal. Whether that is worth anything depends
    # entirely on what is behind the tower, which only the probe can see.
    if internet is False:
        return Assessment(
            BACKHAUL,
            "Signal fine, no internet",
            "The tower's problem; rebooting won't help",
            bars, sample, detail,
        )

    if bars <= 2:
        return Assessment(
            DEGRADED,
            "Weak: %s" % short,
            # Strong coverage with poor quality is interference, not distance,
            # and that is worth saying because it looks like a weak signal and
            # is the one people reboot over.
            "Interference, not distance" if rsrp_bars >= 3 and sinr_bars <= 2
            else "Slow; some requests will fail",
            bars, sample, detail,
        )

    return Assessment(OK, "Connected: %s" % short, "", bars, sample, detail)


# ── change detection ──────────────────────────────────────────────────────────

# These are the events worth interrupting someone for. Everything else is the
# numbers wobbling, which is what radio numbers do, and saying so every 30
# seconds would train them to ignore the thing entirely.

def transitions(now, prev, now_sample, prev_sample):
    """What changed between two assessments. Returns a list of (kind, text).

    `prev` is the last verdict that was a verdict, not whatever the previous
    tick happened to be: the tray skips the gaps (paused, session lost) when it
    chooses what to compare against, so a change that straddles a gap is
    still seen.
    """
    events = []
    if prev is None:
        return events

    # Coming back is the entry worth having: it is the one that ends the
    # waiting, and with the duration beside it "it's been dropping all evening"
    # stops being a feeling and becomes something to hold the provider to.
    #
    # Crossing into or out of a gap in observation says nothing either way, so
    # it is not reported as one.
    if now.state in INDETERMINATE or prev.state in INDETERMINATE:
        pass
    elif now.usable and not prev.usable:
        events.append(("restored", "Internet is back. %s" % now.headline))
    elif prev.usable and not now.usable:
        events.append(("lost", now.headline))
    elif now.state != prev.state:
        events.append(("changed", now.headline))

    if not (now_sample and prev_sample):
        return events

    # Cell change: the smoking gun nobody watches for. When the near tower drops
    # off, the modem re-camps on a further one: still "connected", still four
    # bars, but a different cell with a worse path. That is what makes one
    # request succeed and the next one fail, and without this it is invisible.
    old_cell, new_cell = _cell_of(prev_sample), _cell_of(now_sample)
    if old_cell and new_cell and old_cell != new_cell:
        events.append((
            "cell",
            "Moved to a different cell: eNB %s→%s, PCI %s→%s, EARFCN %s→%s" % (
                old_cell[0], new_cell[0], old_cell[1], new_cell[1],
                old_cell[2], new_cell[2],
            ),
        ))

    # Uptime going backwards means the link dropped and re-established between
    # two polls, a round trip we would otherwise never see, because both
    # samples either side of it can look perfectly healthy.
    old_up, new_up = prev_sample.get("uptime"), now_sample.get("uptime")
    if old_up is not None and new_up is not None and new_up < old_up:
        events.append(("reattach", "Link re-established (was up %s)" % human_duration(old_up)))

    return events


def human_duration(seconds):
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm %02ds" % (seconds // 60, seconds % 60)
    if seconds < 86400:
        return "%dh %02dm" % (seconds // 3600, (seconds % 3600) // 60)
    return "%dd %dh" % (seconds // 86400, (seconds % 86400) // 3600)


def tooltip(assessment, sample, down_since=None):
    """The hover text. Capped by Windows at 127 characters plus a terminator, so
    the ordering matters more than the completeness; the verdict has to survive
    truncation even when the detail does not."""
    lines = [assessment.headline]
    if assessment.detail and assessment.detail not in assessment.headline:
        lines.append(assessment.detail)
    if sample:
        cell = sample.get("pci")
        band = sample.get("band")
        if band:
            lines.append("Band %s · PCI %s · %s" % (band, cell, sample.get("bandwidth") or "?"))
        up = sample.get("uptime")
        if up is not None:
            lines.append("Up %s" % human_duration(up))
    if down_since is not None:
        lines.append("Down for %s" % human_duration(down_since))
    text = "\n".join(lines)
    return text[:127]
