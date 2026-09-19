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


def path_for(log_dir):
    return os.path.join(log_dir, "signal-history.csv")


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


def render(rows, days_requested):
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
    bars = "".join(
        '<div class="hr"><div class="hbar" style="height:%.1f%%"></div>'
        '<div class="hlab">%02d</div><div class="htip">%02d:00 — %.0f%% down</div></div>'
        % (min(100.0, profile[h]), h, h, profile[h]) for h in range(24))

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
        bars=bars,
        headline=headline,
        span="%d day%s" % (len(days), "" if len(days) == 1 else "s"),
        generated=time.strftime("%a %d %b %H:%M"),
    )


_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>RouterOps — Signal History</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; padding:28px 32px; background:#141414; color:#E8E8E8;
         font:14px/1.5 "Segoe UI",system-ui,sans-serif; }
  h1 { font-size:19px; font-weight:600; margin:0 0 2px; }
  .sub { color:#8A8A8A; font-size:12px; margin-bottom:22px; }
  .row { display:flex; align-items:center; gap:12px; margin-bottom:5px; }
  .day { width:96px; flex:none; font-size:12px; color:#B8B8B8;
         text-align:right; font-variant-numeric:tabular-nums; }
  .strip { flex:1; height:34px; border-radius:3px; background:#242424;
           display:block; }
  .note { width:190px; flex:none; font-size:11.5px;
          font-variant-numeric:tabular-nums; }
  .note.good { color:#4C7A4F; }
  .note.bad  { color:#D98A88; }
  .axis { display:flex; gap:12px; margin:10px 0 26px; }
  .axis .day { width:96px; }
  .ticks { flex:1; display:flex; justify-content:space-between;
           font-size:11px; color:#6E6E6E; font-variant-numeric:tabular-nums; }
  .axis .note { width:190px; }
  h2 { font-size:14px; font-weight:600; margin:28px 0 4px; }
  .headline { color:#C9C9C9; font-size:12.5px; margin-bottom:14px; }
  .hours { display:flex; gap:3px; align-items:flex-end; height:92px;
           padding-left:108px; }
  .hr { flex:1; display:flex; flex-direction:column; justify-content:flex-end;
        align-items:center; height:100%; position:relative; }
  .hbar { width:100%; background:linear-gradient(#E53935,#8E1B1B);
          border-radius:2px 2px 0 0; min-height:2px; }
  .hlab { font-size:10px; color:#6E6E6E; margin-top:4px;
          font-variant-numeric:tabular-nums; }
  .htip { position:absolute; bottom:100%; left:50%; transform:translateX(-50%);
          background:#000; border:1px solid #333; padding:3px 7px;
          border-radius:3px; font-size:11px; white-space:nowrap;
          opacity:0; pointer-events:none; transition:opacity .12s; }
  .hr:hover .htip { opacity:1; }
  .legend { display:flex; gap:16px; margin-top:26px; font-size:11.5px;
            color:#9A9A9A; flex-wrap:wrap; }
  .legend span { display:flex; align-items:center; gap:6px; }
  .legend i { width:11px; height:11px; border-radius:2px; display:block; }
  .empty { color:#8A8A8A; }
</style></head><body>
  <h1>Signal History</h1>
  <div class="sub">Last $span · one line per day · generated $generated</div>
  $strips
  <div class="axis"><div class="day"></div>
    <div class="ticks"><span>00:00</span><span>03</span><span>06</span>
      <span>09</span><span>12</span><span>15</span><span>18</span>
      <span>21</span><span>24:00</span></div>
    <div class="note"></div></div>

  <h2>When it tends to go</h2>
  <div class="headline">$headline</div>
  <div class="hours">$bars</div>

  <div class="legend">
    <span><i style="background:#4CAF50"></i>connected</span>
    <span><i style="background:#FFB300"></i>weak</span>
    <span><i style="background:#E53935"></i>signal fine, no internet</span>
    <span><i style="background:#8E1B1B"></i>no service</span>
    <span><i style="background:#6A1B9A"></i>router unreachable</span>
    <span><i style="background:#3A3A3A"></i>paused</span>
    <span><i style="background:#242424"></i>not watched</span>
  </div>
</body></html>
"""
