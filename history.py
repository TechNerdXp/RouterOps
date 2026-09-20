"""The record of what the link has been doing, and the page that shows it.

The tray answers "what is happening now". This answers the question that comes
after a bad week: *when* does it go, and is there a pattern worth planning
around. With the tower's power failing on its own schedule, that pattern is the
difference between guessing and knowing not to start anything at eight o'clock.

One line per sample, appended to a CSV in %LOCALAPPDATA%\\RouterOps. At a sample
every 30 s that is 2,880 lines a day, around 150 KB — small enough to keep a
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
# the network — below it the connection still works, it just costs you time.
HEALTHY_MBPS = 15.0

# How much of the health score availability decides; the rest is speed. See
# health_score() for why the continuous series gets most of the say.
AVAILABILITY_WEIGHT = 0.8

SPEED_FIELDS = ("ts", "mbps")

# The window's title, which is also its identity: main.py looks for a window
# with exactly this name before opening another one. Change it here or not at
# all — a literal in the template and a literal in the matcher would drift
# apart, and the symptom would be two windows again.
PAGE_TITLE = "RouterOps — Signal History"


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
    """Append one sample. Never raises — losing a row must not stop monitoring."""
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

# Same colours the tray icon uses, so the strip and the icon agree.
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
_NODATA = "#242424"

# Worst-first. A minute holding sixty seconds of "fine" and one of "gone" is a
# minute the link went down, so the bucket takes the worst thing in it — an
# average would erase exactly the short drops worth seeing.
_SEVERITY = [diagnose.ROUTER, diagnose.NOLOGIN, diagnose.NOSERVICE,
             diagnose.BACKHAUL, diagnose.DEGRADED, diagnose.OK,
             diagnose.NOSESSION, diagnose.PAUSED, diagnose.STARTING]
_RANK = {state: i for i, state in enumerate(_SEVERITY)}

MINUTES = 1440

# Height of the hour-profile strip in viewBox units. Taller than a day strip
# because it is a bar chart, not a colour band: the height is the reading.
HOUR_H = 72


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


def summarise(rows):
    """Per-day totals plus the hour-of-day profile, both from the same pass."""
    if not rows:
        return [], [0.0] * 24

    by_day = {}
    for row in rows:
        by_day.setdefault(_local_midnight(int(row["ts"])), []).append(row)

    hour_bad = [0] * 24
    hour_all = [0] * 24
    days = []
    for day_start in sorted(by_day):
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
    days, profile = summarise(rows)
    days = days[-days_requested:] if days_requested else days

    strips = []
    for day in reversed(days):            # newest at the top
        rects = []
        for start, length, state in day["runs"]:
            fill = _NODATA if state is None else _FILL.get(state, _NODATA)
            title = "%02d:%02d–%02d:%02d  %s" % (
                start // 60, start % 60,
                (start + length) // 60 % 24, (start + length) % 60,
                state or "no data")
            rects.append(
                '<rect x="%d" y="0" width="%d" height="34" fill="%s">'
                '<title>%s</title></rect>' % (start, length, fill, title))
        note = ("no outage" if not day["down_minutes"]
                else "down %s · longest %s" % (_fmt_minutes(day["down_minutes"]),
                                               _fmt_minutes(day["longest"])))
        strips.append(
            '<div class="row"><div class="day">%s</div>'
            '<svg class="strip" viewBox="0 0 1440 34" preserveAspectRatio="none">%s</svg>'
            '<div class="note %s">%s</div></div>'
            % (day["label"], "".join(rects),
               "bad" if day["down_minutes"] else "good", note))

    worst = max(range(24), key=lambda h: profile[h]) if any(profile) else None

    # The hour profile is drawn on the same 1440-unit scale as the day strips
    # and sits in the same row grid, so hour 08 of the profile is directly
    # under 08:00 of every strip above it. Same axis, same edges — the eye
    # reads straight down from a red patch to the bar that says how usual it is.
    hour_rects = []
    for h in range(24):
        pct = min(100.0, profile[h])
        height = max(1.0, HOUR_H * pct / 100.0)
        hour_rects.append(
            '<rect x="%d" y="%.1f" width="58" height="%.1f" fill="%s" rx="1">'
            '<title>%02d:00–%02d:00  unusable %.0f%% of the time watched</title></rect>'
            % (h * 60 + 1, HOUR_H - height, height,
               "#E53935" if pct else "#2E2E2E", h, (h + 1) % 24, profile[h]))
    hour_note = ("" if worst is None else
                 "worst %02d:00 · %.0f%% down" % (worst, profile[worst]))
    hour_row = (
        '<div class="row hours"><div class="day">when it goes</div>'
        '<svg class="strip hourstrip" viewBox="0 0 1440 %d" preserveAspectRatio="none">%s</svg>'
        '<div class="note %s">%s</div></div>'
        % (HOUR_H, "".join(hour_rects), "bad" if hour_note else "", hour_note))

    speed  = _speed_block(speed_rows or [])
    health = _health_block(days, speed_rows or [])

    headline = ("Not enough data yet." if worst is None or not any(profile) else
                "Worst hour is %02d:00 — the link was unusable %.0f%% of the time "
                "it was watched in that hour." % (worst, profile[worst]))

    # string.Template, not %-formatting: the stylesheet below is full of
    # literal percent signs (height:100%) and every one of them would have to
    # be doubled to survive. $-placeholders collide with nothing in CSS.
    return string.Template(_TEMPLATE).substitute(
        strips="".join(strips) or
               '<p class="empty">No samples recorded yet. Leave the Signal '
               'Monitor running and check back.</p>',
        hour_row=hour_row if days else "",
        headline=headline,
        speed=speed,
        health=health,
        span="%d day%s" % (len(days), "" if len(days) == 1 else "s"),
        generated=time.strftime("%a %d %b %H:%M"),
        title=PAGE_TITLE,
    )


def health_score(days, speed_rows):
    """One number for "is this connection any good", 0-100, or None.

    Two things decide whether a line is worth its money, and they fail
    independently: it can be fast and keep dropping, or rock solid and too slow
    to hold a call. So the score is both, and both are shown beside it — a
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
    whenever someone felt like it — a small, self-selected series should not
    be able to swing the number much.
    """
    watched = sum(d["seen_minutes"] for d in days)
    if not watched and not speed_rows:
        return None

    availability = (100.0 * sum(d["seen_minutes"] - d["down_minutes"] for d in days)
                    / watched) if watched else None

    # Hitting HEALTHY_MBPS scores 75, not 100. Meeting the bar means calls
    # work and nothing waits on the network — that is *good*, and a line with
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


def _health_block(days, speed_rows):
    """The one block that answers "is this line any good": the score, its two
    inputs, and the verdict on the speed — which used to be a section of its
    own further down, asking the same question in different words."""
    h = health_score(days, speed_rows)
    if h is None:
        return ""
    score = h["score"]
    band = "good" if score >= 75 else ("warn" if score >= 50 else "bad")
    parts = []
    if h["availability"] is not None:
        parts.append("up <b>%.1f%%</b> of the %s watched" % (
            h["availability"], _fmt_minutes(h["watched_minutes"])))
    if h["mbps"] is not None:
        parts.append("<b>%.1f Mbps</b> average over %d check%s, %.0f%% of them "
                     "reached %g Mbps" % (
                         h["mbps"], h["checks"], "" if h["checks"] == 1 else "s",
                         h["healthy_share"], HEALTHY_MBPS))
    else:
        parts.append("no speed checks yet")

    if h["verdict"] == "healthy":
        verdict = '<span class="good">Worth it</span> — fast enough for calls and ordinary use.'
    elif h["verdict"] == "questionable":
        verdict = ('<span class="bad">Questionable</span> — below %g Mbps on average; '
                   'calls and loading will suffer.' % HEALTHY_MBPS)
    else:
        verdict = "Run a Speed Check to find out whether the line is worth it."
    return ('<div class="health"><div class="score %s">%d</div>'
            '<div class="hmeta"><div class="hlabel">Network health · is the line worth it?</div>'
            '<div class="hparts">%s</div>'
            '<div class="hverdict">%s</div></div></div>'
            % (band, score, " · ".join(parts), verdict))


def _speed_block(rows):
    """The speed-check readings. The verdict on them lives in the health block
    at the top; this is just the run of numbers behind it."""
    summary = speed_summary(rows)
    if not summary:
        return ('<div class="headline">No speed checks recorded yet. Run '
                'Speed Check and the results collect here.</div>')

    # The bars are scaled to the tallest reading, so the line marking the bar
    # has to sit at the same scale or it is decoration pretending to be a
    # measurement.
    top = max(summary["best"], HEALTHY_MBPS)
    mark = 100.0 * HEALTHY_MBPS / top
    bars = "".join(
        '<div class="sp"><div class="spbar %s" style="height:%.1f%%"></div>'
        '<div class="sptip">%s — %.1f Mbps</div></div>'
        % ("good" if m >= HEALTHY_MBPS else "bad",
           min(100.0, 100.0 * m / top),
           time.strftime("%d %b %H:%M", time.localtime(ts)), m)
        for ts, m in rows[-40:])

    return ('<div class="speeds"><div class="threshold" style="bottom:%.1f%%">'
            '</div>%s</div>'
            '<div class="spfoot">latest %.1f · best %.1f · worst %.1f Mbps '
            '· dashed line is %g · run when someone asked for one, not on a schedule</div>'
            % (mark, bars, summary["latest"], summary["best"],
               summary["worst"], HEALTHY_MBPS))


_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>$title</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; padding:22px 32px; background:#141414; color:#E8E8E8;
         font:14px/1.5 "Segoe UI",system-ui,sans-serif; }
  h1 { font-size:19px; font-weight:600; margin:0 0 2px; }
  .sub { color:#8A8A8A; font-size:12px; margin-bottom:14px; }
  .row { display:flex; align-items:center; gap:12px; margin-bottom:5px; }
  .day { width:96px; flex:none; font-size:12px; color:#B8B8B8;
         text-align:right; font-variant-numeric:tabular-nums; }
  .strip { flex:1; height:34px; border-radius:3px; background:#242424;
           display:block; }
  .note { width:190px; flex:none; font-size:11.5px;
          font-variant-numeric:tabular-nums; }
  .note.good { color:#4C7A4F; }
  .note.bad  { color:#D98A88; }
  .axis { display:flex; gap:12px; margin:8px 0 10px; }
  .axis .day { width:96px; }
  .ticks { flex:1; display:flex; justify-content:space-between;
           font-size:11px; color:#6E6E6E; font-variant-numeric:tabular-nums; }
  .axis .note { width:190px; }
  h2 { font-size:14px; font-weight:600; margin:28px 0 4px; }
  .headline { color:#C9C9C9; font-size:12.5px; margin-bottom:10px; }
  .row.hours { margin-top:10px; }
  .hourstrip { height:72px; background:transparent; }
  .hourstrip rect:hover { fill:#FF6F6A; }
  .row.hours .day { color:#8A8A8A; }
  .health { display:flex; align-items:center; gap:16px; margin:10px 0 14px; }
  .score { font-size:34px; font-weight:600; line-height:1; min-width:72px;
           text-align:center; padding:12px 10px; border-radius:8px;
           font-variant-numeric:tabular-nums; }
  .score.good { background:#1E3A20; color:#7FD184; }
  .score.warn { background:#3D3216; color:#F0C060; }
  .score.bad  { background:#3B1B1B; color:#EE8E8B; }
  .hlabel { font-size:13px; font-weight:600; color:#DCDCDC; }
  .hparts { font-size:12px; color:#8A8A8A; margin-top:2px; }
  .hparts b { color:#C9C9C9; font-weight:600; }
  .hverdict { font-size:12.5px; color:#C9C9C9; margin-top:6px; }
  .hverdict span { font-weight:600; }
  .good { color:#6FBF73; }
  .bad  { color:#E87B78; }
  h2.minor { font-size:13px; color:#B8B8B8; margin-top:22px; }
  .speeds { display:flex; gap:4px; align-items:flex-end; height:48px;
            padding:0 202px 0 108px; position:relative; margin-top:12px; }
  .threshold { position:absolute; left:108px; right:202px; bottom:0; border-top:1px
               dashed #5A5A5A; pointer-events:none; }
  .sp { flex:1; max-width:22px; display:flex; flex-direction:column;
        justify-content:flex-end; height:100%; position:relative; }
  .spbar { width:100%; border-radius:2px 2px 0 0; min-height:2px; }
  .spbar.good { background:linear-gradient(#4CAF50,#357A38); }
  .spbar.bad  { background:linear-gradient(#E53935,#8E1B1B); }
  .sptip { position:absolute; bottom:100%; left:50%; transform:translateX(-50%);
           background:#000; border:1px solid #333; padding:3px 7px;
           border-radius:3px; font-size:11px; white-space:nowrap; opacity:0;
           pointer-events:none; transition:opacity .12s; z-index:2; }
  .sp:hover .sptip { opacity:1; }
  .spfoot { padding-left:108px; font-size:11px; color:#6E6E6E; margin-top:6px;
            font-variant-numeric:tabular-nums; }
  .legend { display:flex; gap:16px; margin-top:12px; font-size:11.5px;
            color:#9A9A9A; flex-wrap:wrap; }
  .legend span { display:flex; align-items:center; gap:6px; }
  .legend i { width:11px; height:11px; border-radius:2px; display:block; }
  .empty { color:#8A8A8A; }
</style></head><body>
  <h1>Signal History</h1>
  $health
  <div class="sub">Last $span · one line per day · generated $generated</div>
  $strips
  $hour_row
  <div class="axis"><div class="day"></div>
    <div class="ticks"><span>00:00</span><span>03</span><span>06</span>
      <span>09</span><span>12</span><span>15</span><span>18</span>
      <span>21</span><span>24:00</span></div>
    <div class="note"></div></div>
  <div class="headline">$headline</div>

  <div class="legend">
    <span><i style="background:#4CAF50"></i>connected</span>
    <span><i style="background:#FFB300"></i>weak</span>
    <span><i style="background:#E53935"></i>signal fine, no internet</span>
    <span><i style="background:#8E1B1B"></i>no service</span>
    <span><i style="background:#6A1B9A"></i>router unreachable</span>
    <span><i style="background:#3A3A3A"></i>paused</span>
    <span><i style="background:#242424"></i>not watched</span>
  </div>

  <h2 class="minor">Speed checks</h2>
  $speed
<script>
  // Clicking Signal History again rewrites this file and raises this window
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
