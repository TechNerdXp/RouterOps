"""The record of what the link has been doing, and the page that shows it.

The tray answers "what is happening now". This answers the question that comes
after a bad week: *when* does it go, and is there a pattern worth planning
around. With the tower's power failing on its own schedule, that pattern is the
difference between guessing and knowing not to start anything at eight o'clock.

One line per sample, appended to a CSV in %LOCALAPPDATA%\\RouterOps. At a sample
every 30 s that is 2,880 lines a day, around 150 KB, small enough to keep a
month of and cheap enough to append to without thinking about it. CSV rather
than a database because it is greppable, survives a half-written last line, and
needs nothing to read it.

The page is generated as one self-contained file and opened the same way the
router portal is: an app-mode window with no browser furniture around it.
"""

import os
import string
import time

import diagnose

FIELDS = ("ts", "state", "bars", "rsrp", "sinr", "rsrq", "rssi",
          "enb", "pci", "earfcn", "uptime", "dl", "ul")

KEEP_DAYS = 30
DAY = 86400

# What a speed check has to reach for the line to be worth having. Chosen as
# the point where calls stop being painful and ordinary use stops waiting on
# the network. Below it the connection still works, it just costs you time.
HEALTHY_MBPS = 15.0

# How much of the health score availability decides; the rest is speed. See
# health_score() for why the continuous series gets most of the say.
AVAILABILITY_WEIGHT = 0.8

SPEED_FIELDS = ("ts", "mbps")

# The window's title, which is also its identity: main.py looks for a window
# with exactly this name before opening another one. Change it here or not at
# all; a literal in the template and a literal in the matcher would drift
# apart, and the symptom would be two windows again.
PAGE_TITLE = "RouterOps · Network Report"


def path_for(log_dir):
    return os.path.join(log_dir, "signal-history.csv")


def speed_path_for(log_dir):
    return os.path.join(log_dir, "speed-history.csv")


# ── speed checks ──────────────────────────────────────────────────────────────
# One line per check, kept beside the signal log rather than inside it: these
# arrive when someone runs a speed test, not on a cadence, and mixing an
# irregular series into a regular one makes both harder to read.

def record_speed(csv_path, mbps, now=None):
    """Append one speed-check result. Never raises."""
    try:
        new = not os.path.exists(csv_path)
        with open(csv_path, "a", encoding="utf-8", newline="") as fh:
            if new:
                fh.write(",".join(SPEED_FIELDS) + "\n")
            fh.write("%d,%.2f\n" % (int(now if now is not None else time.time()), mbps))
    except OSError:
        pass


def load_speed(csv_path, days, now=None):
    """(timestamp, mbps) pairs within the window, oldest first."""
    cutoff = int(now if now is not None else time.time()) - days * DAY
    out = []
    try:
        with open(csv_path, encoding="utf-8") as fh:
            fh.readline()
            for line in fh:
                parts = line.rstrip("\n").split(",")
                if len(parts) != 2:
                    continue
                try:
                    ts, mbps = int(parts[0]), float(parts[1])
                except ValueError:
                    continue
                if ts >= cutoff:
                    out.append((ts, mbps))
    except OSError:
        return []
    return out


def speed_summary(rows):
    """What the recent checks say about the line, or None if there are none.

    The average is over the window being displayed rather than all time: a good
    month in the spring says nothing about whether calls will work this evening.
    """
    if not rows:
        return None
    values = [m for _ts, m in rows]
    healthy = [m for m in values if m >= HEALTHY_MBPS]
    return {
        "count":   len(values),
        "latest":  values[-1],
        "average": sum(values) / len(values),
        "best":    max(values),
        "worst":   min(values),
        "healthy_share": 100.0 * len(healthy) / len(values),
        "verdict": ("healthy" if sum(values) / len(values) >= HEALTHY_MBPS
                    else "questionable"),
    }


# ── writing ───────────────────────────────────────────────────────────────────

def record(csv_path, state, sample, bars, now=None):
    """Append one sample. Never raises: losing a row must not stop monitoring."""
    sample = sample or {}
    row = [
        int(now if now is not None else time.time()),
        state,
        bars,
        sample.get("rsrp", ""), sample.get("sinr", ""),
        sample.get("rsrq", ""), sample.get("rssi", ""),
        sample.get("enb", ""), sample.get("pci", ""), sample.get("earfcn", ""),
        sample.get("uptime", ""), sample.get("dl_kbps", ""), sample.get("ul_kbps", ""),
    ]
    try:
        new = not os.path.exists(csv_path)
        with open(csv_path, "a", encoding="utf-8", newline="") as fh:
            if new:
                fh.write(",".join(FIELDS) + "\n")
            fh.write(",".join("" if v is None else str(v) for v in row) + "\n")
    except OSError:
        pass


def prune(csv_path, keep_days=KEEP_DAYS, now=None):
    """Drop rows older than the window. Rewrites in one pass, cheaply."""
    cutoff = int(now if now is not None else time.time()) - keep_days * DAY
    try:
        if not os.path.exists(csv_path):
            return
        with open(csv_path, encoding="utf-8") as fh:
            lines = fh.readlines()
        if not lines:
            return
        head, body = lines[0], lines[1:]
        kept = [ln for ln in body if _ts_of(ln) >= cutoff]
        if len(kept) == len(body):
            return
        tmp = csv_path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(head)
            fh.writelines(kept)
        os.replace(tmp, csv_path)
    except OSError:
        pass


def _ts_of(line):
    try:
        return int(line.split(",", 1)[0])
    except (ValueError, IndexError):
        return 0


# ── reading ───────────────────────────────────────────────────────────────────

def load(csv_path, days, now=None):
    """Rows within the window, oldest first, as dicts."""
    cutoff = int(now if now is not None else time.time()) - days * DAY
    rows = []
    try:
        with open(csv_path, encoding="utf-8") as fh:
            header = fh.readline().strip().split(",")
            for line in fh:
                parts = line.rstrip("\n").split(",")
                # A process killed mid-write leaves one short line; skip it
                # rather than let it take the whole view down.
                if len(parts) != len(header):
                    continue
                try:
                    ts = int(parts[0])
                except ValueError:
                    continue
                if ts < cutoff:
                    continue
                rows.append(dict(zip(header, parts)))
    except OSError:
        return []
    return rows


# ── the page ──────────────────────────────────────────────────────────────────

# The tray icon's colours, so the strip and the icon agree. The one difference
# is deliberate: the icon has one red for every kind of outage, and the page,
# which has room for a legend, gives no-service and the router their own.
_FILL = {
    diagnose.OK:        "#4CAF50",
    diagnose.DEGRADED:  "#FFB300",
    diagnose.BACKHAUL:  "#E53935",
    diagnose.NOSERVICE: "#8E1B1B",
    diagnose.ROUTER:    "#6A1B9A",
    diagnose.NOLOGIN:   "#6A1B9A",
    diagnose.NOSESSION: "#3A3A3A",
    diagnose.PAUSED:    "#3A3A3A",
    diagnose.STARTING:  "#3A3A3A",
}
_NODATA = "#202226"

# Worst-first. A minute holding sixty seconds of "fine" and one of "gone" is a
# minute the link went down, so the bucket takes the worst thing in it; an
# average would erase exactly the short drops worth seeing.
_SEVERITY = [diagnose.ROUTER, diagnose.NOLOGIN, diagnose.NOSERVICE,
             diagnose.BACKHAUL, diagnose.DEGRADED, diagnose.OK,
             diagnose.NOSESSION, diagnose.PAUSED, diagnose.STARTING]
_RANK = {state: i for i, state in enumerate(_SEVERITY)}

MINUTES = 1440

# Height of the hour-profile strip in viewBox units. Taller than a day strip
# because it is a bar chart, not a colour band: the height is the reading.
HOUR_H = 72

# Height of a day strip in viewBox units.
STRIP_H = 28


def _day_buckets(rows, day_start):
    """One state per minute of this day, or None where nothing was recorded."""
    buckets = [None] * MINUTES
    for row in rows:
        minute = (int(row["ts"]) - day_start) // 60
        if not 0 <= minute < MINUTES:
            continue
        state = row["state"]
        current = buckets[minute]
        if current is None or _RANK.get(state, 99) < _RANK.get(current, 99):
            buckets[minute] = state
    return buckets


def _runs(buckets):
    """Collapse equal neighbours into (start, length, state).

    A quiet day is ~1440 identical minutes; drawing it as 1440 rectangles is
    wasteful when it is one. Run-length encoding keeps the page small no matter
    how long the window gets.
    """
    out = []
    start, current = 0, buckets[0]
    for i in range(1, MINUTES):
        if buckets[i] != current:
            out.append((start, i - start, current))
            start, current = i, buckets[i]
    out.append((start, MINUTES - start, current))
    return out


def _local_midnight(ts):
    t = time.localtime(ts)
    return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)))


def summarise(rows, days_wanted=None):
    """Per-day totals plus the hour-of-day profile, both from the same pass.

    `days_wanted` keeps the most recent N calendar days. Seven days back from
    now starts partway through an eighth date, and trimming that day off the
    strips afterwards still left it in the hour profile and the worst hour,
    which then described data the page was not showing.
    """
    if not rows:
        return [], [0.0] * 24

    by_day = {}
    for row in rows:
        by_day.setdefault(_local_midnight(int(row["ts"])), []).append(row)

    hour_bad = [0] * 24
    hour_all = [0] * 24
    days = []
    starts = sorted(by_day)
    if days_wanted:
        starts = starts[-days_wanted:]
    for day_start in starts:
        day_rows = by_day[day_start]
        buckets = _day_buckets(day_rows, day_start)

        down = sum(1 for b in buckets if b is not None and b not in diagnose.USABLE
                   and b not in diagnose.INDETERMINATE)
        seen = sum(1 for b in buckets if b is not None)

        longest, run = 0, 0
        for b in buckets:
            if b is not None and b not in diagnose.USABLE and b not in diagnose.INDETERMINATE:
                run += 1
                longest = max(longest, run)
            else:
                run = 0

        for minute, b in enumerate(buckets):
            if b is None or b in diagnose.INDETERMINATE:
                continue
            hour_all[minute // 60] += 1
            if b not in diagnose.USABLE:
                hour_bad[minute // 60] += 1

        days.append({
            "start": day_start,
            "label": time.strftime("%a %d %b", time.localtime(day_start)),
            "runs": _runs(buckets),
            "down_minutes": down,
            "seen_minutes": seen,
            "longest": longest,
        })

    profile = [(100.0 * hour_bad[h] / hour_all[h]) if hour_all[h] else 0.0
               for h in range(24)]
    return days, profile


def _fmt_minutes(n):
    if n <= 0:
        return "none"
    if n < 60:
        return "%dm" % n
    return "%dh %02dm" % (n // 60, n % 60)


def render(rows, days_requested, speed_rows=None):
    days, profile = summarise(rows, days_requested)
    speed_rows = speed_rows or []

    strips = []
    for day in days:                      # oldest at the top, so it reads downwards
        rects = []
        for start, length, state in day["runs"]:
            fill = _NODATA if state is None else _FILL.get(state, _NODATA)
            title = "%02d:%02d to %02d:%02d  %s" % (
                start // 60, start % 60,
                (start + length) // 60 % 24, (start + length) % 60,
                state or "no data")
            rects.append(
                '<rect x="%d" y="0" width="%d" height="%d" fill="%s">'
                '<title>%s</title></rect>' % (start, length, STRIP_H, fill, title))
        if day["down_minutes"]:
            note = ('down <b>%s</b> · longest <b>%s</b>'
                    % (_fmt_minutes(day["down_minutes"]), _fmt_minutes(day["longest"])))
        else:
            note = '<span class="good">no outage</span>'
        strips.append(
            '<div class="g"><div class="lab">%s</div>'
            '<svg class="strip" viewBox="0 0 1440 %d" preserveAspectRatio="none">%s</svg>'
            '<div class="note">%s</div></div>'
            % (day["label"], STRIP_H, "".join(rects), note))

    worst = max(range(24), key=lambda h: profile[h]) if any(profile) else None

    # The hour profile is drawn on the same 1440-unit scale as the day strips
    # and sits in the same grid, so hour 08 of the profile is directly under
    # 08:00 of every strip above it. Same axis, same edges: the eye reads
    # straight down from a red patch to the bar that says how usual it is.
    hour_rects = []
    for h in range(24):
        pct = min(100.0, profile[h])
        height = max(1.0, HOUR_H * pct / 100.0)
        hour_rects.append(
            '<rect x="%d" y="%.1f" width="56" height="%.1f" fill="%s">'
            '<title>%02d:00 to %02d:00  unusable %.0f%% of the time watched</title></rect>'
            % (h * 60 + 2, HOUR_H - height, height,
               "#E53935" if pct else "#2A2C31", h, (h + 1) % 24, profile[h]))
    hour_note = ("" if worst is None else
                 'worst <b>%02d:00</b> · <b>%.0f%%</b> down' % (worst, profile[worst]))
    hour_row = (
        '<div class="g hours"><div class="lab">when it goes</div>'
        '<svg class="strip" viewBox="0 0 1440 %d" preserveAspectRatio="none">%s</svg>'
        '<div class="note">%s</div></div>'
        % (HOUR_H, "".join(hour_rects), hour_note))

    if days:
        strips_block = ("".join(strips) + hour_row + _AXIS + _LEGEND)
    else:
        strips_block = ('<p class="empty">No samples recorded yet. Leave the Signal '
                        'Monitor running and check back.</p>')

    # string.Template, not %-formatting: the stylesheet below is full of
    # literal percent signs (height:100%) and every one of them would have to
    # be doubled to survive. $-placeholders collide with nothing in CSS.
    return string.Template(_TEMPLATE).substitute(
        stats=_stats_block(days, speed_rows, worst, profile),
        strips=strips_block,
        speed=_speed_block(speed_rows),
        span=("last %d day%s · " % (len(days), "" if len(days) == 1 else "s")
              if days else ""),
        generated=time.strftime("%a %d %b %H:%M"),
        healthy="%g" % HEALTHY_MBPS,
        title=PAGE_TITLE,
    )


def health_score(days, speed_rows):
    """One number for "is this connection any good", 0-100, or None.

    Two things decide whether a line is worth its money, and they fail
    independently: it can be fast and keep dropping, or rock solid and too slow
    to hold a call. So the score is both, and both are shown beside it; a
    single figure with its workings hidden is a figure nobody trusts or can act
    on.

      availability  share of the watched minutes the link was actually usable
      speed         average of the speed checks against the HEALTHY_MBPS bar,
                    capped at 100 so one very fast day cannot pay for a week of
                    outages

    Weighted heavily toward availability, for two reasons. A connection that is
    not there is worth nothing regardless of how fast it is when it returns.
    And the two inputs are not equally trustworthy: availability comes from a
    sample every 30 s around the clock, speed from a handful of checks run
    whenever someone felt like it, and a small, self-selected series should not
    be able to swing the number much.
    """
    watched = sum(d["seen_minutes"] for d in days)
    if not watched and not speed_rows:
        return None

    availability = (100.0 * sum(d["seen_minutes"] - d["down_minutes"] for d in days)
                    / watched) if watched else None

    # Hitting HEALTHY_MBPS scores 75, not 100. Meeting the bar means calls
    # work and nothing waits on the network. That is *good*, and a line with
    # real headroom above it deserves to score higher than one scraping past.
    summary = speed_summary(speed_rows)
    speed = (min(100.0, 75.0 * summary["average"] / HEALTHY_MBPS)
             if summary else None)

    if availability is None:
        score = speed
    elif speed is None:
        score = availability
    else:
        score = AVAILABILITY_WEIGHT * availability + (1 - AVAILABILITY_WEIGHT) * speed
    return {
        "score": round(score),
        "availability": availability,
        "speed": speed,
        "mbps": summary["average"] if summary else None,
        "checks": summary["count"] if summary else 0,
        "healthy_share": summary["healthy_share"] if summary else None,
        "verdict": summary["verdict"] if summary else None,
        "watched_minutes": watched,
    }


def _tile(label, value, caption):
    return ('<div class="tile"><div class="tlabel">%s</div>'
            '<div class="tvalue">%s</div><div class="tcap">%s</div></div>'
            % (label, value, caption))


def _stats_block(days, speed_rows, worst, profile):
    """The row that answers "is this line any good": the score, the two inputs
    it is made of, the hour to plan around, and the verdict on the speed."""
    h = health_score(days, speed_rows)
    if h is None:
        return ""
    score = h["score"]
    band = "good" if score >= 75 else ("warn" if score >= 50 else "bad")

    tiles = [_tile("Network health",
                   '<span class="%s">%d</span><small>/ 100</small>' % (band, score),
                   "availability counts %.0f%%, speed %.0f%%"
                   % (100 * AVAILABILITY_WEIGHT, 100 * (1 - AVAILABILITY_WEIGHT)))]

    if h["availability"] is not None:
        tiles.append(_tile("Availability", "%.1f<small class=\"pct\">%%</small>" % h["availability"],
                           "up, of %s watched" % _fmt_minutes(h["watched_minutes"])))
    else:
        tiles.append(_tile("Availability", '<span class="dim">none</span>',
                           "nothing watched yet"))

    if h["mbps"] is not None:
        tiles.append(_tile("Speed", "%.1f<small>Mbps</small>" % h["mbps"],
                           "average of %d check%s · %.0f%% reached %g Mbps"
                           % (h["checks"], "" if h["checks"] == 1 else "s",
                              h["healthy_share"], HEALTHY_MBPS)))
    else:
        tiles.append(_tile("Speed", '<span class="dim">none</span>',
                           "no speed checks yet"))

    if worst is not None:
        tiles.append(_tile("Worst hour", "%02d:00" % worst,
                           "unusable %.0f%% of the time watched" % profile[worst]))
    else:
        tiles.append(_tile("Worst hour", '<span class="dim">none</span>',
                           "not enough data yet"))

    if h["verdict"] == "healthy":
        verdict = ('<b class="good">Worth it.</b> Fast enough for calls and '
                   'ordinary use.')
    elif h["verdict"] == "questionable":
        verdict = ('<b class="bad">Questionable.</b> Below %g Mbps on average; '
                   'calls and loading will suffer.' % HEALTHY_MBPS)
    else:
        verdict = "Run a Speed Check to find out whether the line is worth it."
    return ('<section class="card"><div class="tiles">%s</div>'
            '<div class="verdict">%s</div></section>' % ("".join(tiles), verdict))


def _speed_block(rows):
    """The speed-check readings. The verdict on them lives in the stat row at
    the top; this is just the run of numbers behind it."""
    summary = speed_summary(rows)
    if not summary:
        return ('<p class="empty">No speed checks recorded yet. Run Speed Check '
                'and the results collect here.</p>')

    shown = rows[-40:]
    # The bars are scaled to the tallest reading, so the line marking the bar
    # has to sit at the same scale or it is decoration pretending to be a
    # measurement.
    top = max(summary["best"], HEALTHY_MBPS)
    mark = 100.0 * HEALTHY_MBPS / top
    bars = "".join(
        '<div class="sp"><div class="spbar %s" style="height:%.1f%%"></div>'
        '<div class="tip">%s · %.1f Mbps</div></div>'
        % ("good" if m >= HEALTHY_MBPS else "bad",
           min(100.0, 100.0 * m / top),
           time.strftime("%d %b %H:%M", time.localtime(ts)), m)
        for ts, m in shown)
    first = time.strftime("%d %b %H:%M", time.localtime(shown[0][0]))
    last = time.strftime("%d %b %H:%M", time.localtime(shown[-1][0]))
    ticks = ("<span>%s</span><span>%s</span>" % (first, last) if len(shown) > 1
             else "<span>%s</span>" % first)

    return ('<div class="g"><div class="lab">fast.com</div>'
            '<div class="speeds"><div class="threshold" style="bottom:%.1f%%"></div>'
            '%s</div>'
            '<div class="note">latest <b>%.1f</b> Mbps</div></div>'
            '<div class="g axis"><div class="lab"></div><div class="ticks">%s</div>'
            '<div class="note"></div></div>'
            '<div class="g"><div class="lab"></div><div class="foot">%d check%s · '
            'best %.1f · worst %.1f Mbps · run on demand, not on a schedule</div>'
            '<div class="note"></div></div>'
            % (mark, bars, summary["latest"], ticks,
               summary["count"], "" if summary["count"] == 1 else "s",
               summary["best"], summary["worst"]))


_AXIS = """<div class="g axis"><div class="lab"></div>
    <div class="ticks"><span>00:00</span><span>03</span><span>06</span>
      <span>09</span><span>12</span><span>15</span><span>18</span>
      <span>21</span><span>24:00</span></div>
    <div class="note"></div></div>"""

_LEGEND = """<div class="g"><div class="lab"></div><div class="legend">
    <span><i style="background:#4CAF50"></i>connected</span>
    <span><i style="background:#FFB300"></i>weak</span>
    <span><i style="background:#E53935"></i>signal fine, no internet</span>
    <span><i style="background:#8E1B1B"></i>no service</span>
    <span><i style="background:#6A1B9A"></i>router unreachable</span>
    <span><i style="background:#3A3A3A"></i>paused</span>
    <span><i style="background:#202226;border:1px solid #33363C"></i>not watched</span>
  </div><div class="note"></div></div>"""

# One type scale for the whole page: 22 for the title, 26 for a tile value,
# 13 for anything read as a sentence, 12 for labels and notes, 11 for axis
# ticks. Every chart row is the same three-column grid, so labels, strips,
# bars and notes line up top to bottom whatever section they are in.
_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>$title</title>
<style>
  :root { color-scheme:dark;
          --bg:#111214; --card:#18191C; --line:#27292E;
          --ink:#E7E8EB; --ink2:#A3A7AE; --ink3:#6E727A;
          --label:104px; --note:200px; --gap:14px; }
  * { box-sizing:border-box; }
  body { margin:0; padding:22px 32px 24px; background:var(--bg); color:var(--ink);
         font:13px/1.5 "Segoe UI Variable Text","Segoe UI",system-ui,sans-serif; }
  .page { max-width:1160px; margin:0 auto; }
  header { display:flex; align-items:baseline; justify-content:space-between;
           margin:0 0 14px; }
  h1 { font-size:22px; font-weight:600; margin:0; letter-spacing:-.01em; }
  .meta { font-size:12px; color:var(--ink3); font-variant-numeric:tabular-nums; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:8px;
          padding:16px 20px; margin-bottom:12px; }
  h2 { display:flex; justify-content:space-between; align-items:baseline;
       font-size:13px; font-weight:600; margin:0 0 14px; }
  h2 span { font-size:12px; font-weight:400; color:var(--ink3); }

  .tiles { display:grid; grid-template-columns:repeat(4,1fr); }
  .tile { padding:0 20px; border-left:1px solid var(--line); }
  .tile:first-child { padding-left:0; border-left:0; }
  .tlabel { font-size:12px; color:var(--ink2); }
  .tvalue { font-size:26px; font-weight:600; line-height:1.2; margin-top:2px; }
  .tvalue small { font-size:13px; font-weight:400; color:var(--ink2); margin-left:5px; }
  .tvalue small.pct { margin-left:1px; }
  .tcap { font-size:12px; color:var(--ink3); margin-top:2px; font-variant-numeric:tabular-nums; }
  .dim { color:var(--ink3); font-weight:400; }
  .verdict { margin-top:16px; padding-top:14px; border-top:1px solid var(--line);
             color:var(--ink2); }
  .verdict b { font-weight:600; }
  .good { color:#6FCF75; }
  .warn { color:#F2C15C; }
  .bad  { color:#F08A86; }

  .g { display:grid; grid-template-columns:var(--label) 1fr var(--note);
       column-gap:var(--gap); align-items:center; }
  .g + .g { margin-top:5px; }
  .lab { font-size:12px; color:var(--ink2); text-align:right; white-space:nowrap;
         font-variant-numeric:tabular-nums; }
  .note { font-size:12px; color:var(--ink3); white-space:nowrap;
          font-variant-numeric:tabular-nums; }
  .note b { color:var(--ink2); font-weight:600; }
  .strip { display:block; width:100%; height:28px; border-radius:4px; background:#202226; }
  .hours { margin-top:16px !important; }
  .hours .strip { height:72px; border-radius:0; background:transparent;
                  border-bottom:1px solid var(--line); }
  .hours rect:hover { fill:#FF7A75; }
  .ticks { display:flex; justify-content:space-between; font-size:11px;
           color:var(--ink3); font-variant-numeric:tabular-nums; }
  .g.axis { margin-top:6px; }
  .legend { display:flex; flex-wrap:wrap; gap:6px 18px; font-size:12px;
            color:var(--ink2); margin-top:12px; padding-top:14px;
            border-top:1px solid var(--line); }
  .legend span { display:flex; align-items:center; gap:6px; }
  .legend i { width:10px; height:10px; border-radius:2px; display:block; }

  .speeds { display:flex; gap:6px; align-items:flex-end; height:72px;
            position:relative; border-bottom:1px solid var(--line); }
  .threshold { position:absolute; left:0; right:0; border-top:1px dashed #5A5E66;
               pointer-events:none; }
  .sp { flex:1; max-width:18px; height:100%; display:flex; flex-direction:column;
        justify-content:flex-end; position:relative; }
  .spbar { width:100%; border-radius:3px 3px 0 0; min-height:2px; }
  .spbar.good { background:#4CAF50; }
  .spbar.bad  { background:#E53935; }
  .tip { position:absolute; bottom:100%; left:50%; transform:translateX(-50%);
         margin-bottom:6px; background:#0B0C0E; border:1px solid var(--line);
         padding:3px 8px; border-radius:4px; font-size:12px; white-space:nowrap;
         color:var(--ink); opacity:0; pointer-events:none; transition:opacity .12s;
         z-index:2; }
  .sp:hover .tip { opacity:1; }
  .sp:hover .spbar { filter:brightness(1.2); }
  .foot { font-size:12px; color:var(--ink3); font-variant-numeric:tabular-nums; }
  .empty { color:var(--ink3); margin:0; }
</style></head><body><div class="page">
  <header><h1>Network Report</h1>
    <div class="meta">${span}generated $generated</div></header>
  $stats
  <section class="card">
    <h2>Day by day <span>midnight to midnight · each minute shows the worst state seen in it</span></h2>
    $strips
  </section>
  <section class="card">
    <h2>Speed checks <span>fast.com download, against the $healthy Mbps bar</span></h2>
    $speed
  </section>
</div>
<script>
  // Clicking Network Report again rewrites this file and raises this window
  // rather than opening a second one, so the window has to re-read the file to
  // show what was just written. Coming back to it is the moment that matters:
  // a page generated at 2am and still on screen at noon is worse than no page,
  // because it looks current. The delay keeps an ordinary alt-tab from
  // reloading a page the reader is in the middle of.
  var loadedAt = Date.now();
  addEventListener("focus", function () {
    if (Date.now() - loadedAt > 30000) location.reload();
  });
</script>
</body></html>
"""
