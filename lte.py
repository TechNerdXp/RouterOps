"""Browserless telemetry for the Huawei LTE CPE B2368-66.

The router has no HiLink/JSON API — /api/* is a 404 here — but its CGI UI is
plain form-encoded HTTP, so a session can be held and read without a browser at
all. That matters for more than tidiness: the firmware's missing Content-Type
header (the document.write bandage in main.py) is a *browser* problem. Chrome
sees no type, honours X-Content-Type-Options: nosniff, and renders the markup as
text. Raw HTTP never looks at the header, so nothing needs recovering. Removing
the browser removes the bug with it.

Everything here is stdlib. No requests, no selenium; one less moving part in the
resident process, and none of the import weight in the frozen exe.

Measured against the live router, 2026-09-19 (firmware B2368_V100R001C00SPC169):
one poll is HTTP 200, ~0.40 s, ~4.75 KB. mini_httpd answers HTTP/1.0 and closes
the socket, so there is no keep-alive to reuse — each poll pays its own TCP+TLS
handshake, and that cost is already inside the 0.40 s.
"""

import base64
import html as _html
import http.client
import re
import socket
import ssl
import urllib.parse

HOST = "192.168.1.1"

# The router presents a self-signed certificate for its own LAN address. There
# is no CA that could vouch for 192.168.1.1 and no name to match, so verifying
# is not a thing that can succeed — the same call the existing Selenium path
# makes with --ignore-certificate-errors.
_TLS = ssl._create_unverified_context()

_CONNECT_TIMEOUT = 8     # router is one hop away; it answers or it doesn't
_LOGIN_TIMEOUT   = 45    # login.cgi is slow — the UI itself warns about ~15 s


class RouterUnreachable(Exception):
    """No answer at all: cable out, router rebooting, or LAN down."""


class Evicted(Exception):
    """Our session was taken over by another login.

    The router keeps exactly one admin session. A second login silently
    replaces the first, and the loser's next request comes back as a ~362-byte
    stub redirecting to /logout.cgi instead of ~4.7 KB of data. That makes
    eviction cheap to detect and impossible to confuse with a real reading.
    """


class LoginRefused(Exception):
    """Credentials rejected, or the router would not open a session."""


# ── wire helpers ──────────────────────────────────────────────────────────────

def _request(host, method, path, body=None, cookie=None, timeout=_CONNECT_TIMEOUT):
    """One request/response against mini_httpd. Returns (status, headers, text)."""
    headers = {}
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Content-Length"] = str(len(body))
    if cookie:
        headers["Cookie"] = cookie
    conn = http.client.HTTPSConnection(host, 443, timeout=timeout, context=_TLS)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, resp.getheaders(), raw.decode("utf-8", "replace")
    except (OSError, http.client.HTTPException) as exc:
        raise RouterUnreachable(str(exc)) from exc
    finally:
        conn.close()


def _session_cookie(headers):
    for name, value in headers:
        if name.lower() == "set-cookie" and value.startswith("session="):
            return value.split(";", 1)[0]
    return None


# The evicted-session body is a tiny JS stub. Size alone would do, but matching
# the redirect target as well keeps it from ever mistaking a short real page.
def _is_evicted(text):
    return "logout.cgi" in text and len(text) < 1500


# ── parsing ───────────────────────────────────────────────────────────────────

_TD  = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


def _cells(page):
    """Every table cell's visible text, in document order."""
    out = []
    for cell in _TD.findall(page):
        text = _html.unescape(_TAG.sub(" ", cell))
        out.append(" ".join(text.split()))
    return out


def _num(text):
    """First signed number in a string, as int or float. None if there is none."""
    if not text:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        return None
    raw = m.group(0)
    return float(raw) if "." in raw else int(raw)


_UPTIME_UNITS = (("Day", 86400), ("Hour", 3600), ("Minute", 60), ("Second", 1))


def _uptime_seconds(text):
    """'0 Day(s), 1 Hour(s),38 Minute(s),22 Second(s)' -> 5902.

    Worth having as a number rather than a label: this counter resetting is an
    unambiguous re-attach marker — the link went away and came back — which is
    exactly the event a tower losing power produces.
    """
    if not text:
        return None
    total = 0
    found = False
    for unit, mult in _UPTIME_UNITS:
        m = re.search(r"(\d+)\s*" + unit, text, re.I)
        if m:
            total += int(m.group(1)) * mult
            found = True
    return total if found else None


# label as it appears in the page -> (key, converter)
_FIELDS = {
    "Status":                 ("status",      str),
    "Connection Up Time":     ("uptime",      _uptime_seconds),
    "Signal Strength":        ("rssi",        _num),
    "SINR":                   ("sinr",        _num),
    "RSRP":                   ("rsrp",        _num),
    "RSRQ":                   ("rsrq",        _num),
    "Frequency Band":         ("band",        str),
    "DL EARFCN":              ("earfcn",      str),
    "Duplexing Mode":         ("duplex",      str),
    "BandWidth":              ("bandwidth",   str),
    "RANK":                   ("rank",        str),
    "Global Cell ID":         ("global_cell", str),
    "Physical Cell ID":       ("pci",         str),
    "eNB ID [DEC]":           ("enb",         str),
    "Cell ID [DEC]":          ("cell",        str),
    "ECGI":                   ("ecgi",        str),
    "UL Packet Rate":         ("ul_kbps",     _num),
    "DL Packet Rate":         ("dl_kbps",     _num),
    "CQI":                    ("cqi",         str),
    "Data Roaming Status":    ("roaming",     str),
    "Service Provider":       ("provider",    str),
    "CA Activation Status":   ("ca",          str),
}


def parse(page):
    """Pull the LTE fields out of lteStatus.cgi's HTML.

    The page lays out two label/value pairs per row, so a positional row parser
    would have to know that shape. Reading every cell in order and taking the
    one after each known label does not, which is why it survives the GET (full
    page) and the POST (refresh fragment) alike — the fragment drops IMEI/IMSI
    and a few rows, and nothing here depends on their being present.
    """
    cells = _cells(page)
    out = {}
    for i, cell in enumerate(cells):
        field = _FIELDS.get(cell)
        if field is None or i + 1 >= len(cells):
            continue
        key, conv = field
        if key in out:
            continue  # first occurrence wins; these labels are unique in practice
        value = cells[i + 1].strip()
        out[key] = value if conv is str else conv(value)
    return out


# ── session ───────────────────────────────────────────────────────────────────

class LteSession:
    """One admin session on the router, held open across polls.

    Held open because logging in costs seconds and the router tolerates only one
    session at a time — so the session is a resource to be owned deliberately
    and given up politely, not re-established on every read.
    """

    def __init__(self, username, password, host=HOST):
        self._host   = host
        self._user   = username
        self._pass   = password
        self._cookie = None
        # The cookie of a session we have been thrown out of. Kept, not
        # discarded, because it is the only thing that can free the slot it is
        # still occupying — see login().
        self._stale  = None

    @property
    def live(self):
        return self._cookie is not None

    def login(self):
        """Open a session. Evicts whoever currently holds one — by design, the
        caller decides when that is allowed.

        First, give back any session we hold or have been thrown out of. This
        is not tidiness, it is the whole difference between working and not:
        the router issues a cookie for a login even when its single slot is
        still taken, so the login *appears* to succeed and then every request
        made with that cookie comes back as the logout stub. Measured against
        the live router: three plain re-logins in a row all returned 362 bytes;
        one /logout.cgi with the dead cookie, then the same login, returned
        4,752 bytes of data. Dropping the dead cookie without spending it is
        what left the slot occupied until the firmware timed it out minutes
        later.
        """
        self._release()

        status, headers, page = _request(self._host, "GET", "/login.cgi")
        if status != 200:
            raise LoginRefused("login page returned HTTP %s" % status)

        # authToken is a per-fetch CSRF value; a stale one is refused, so every
        # login attempt has to re-scrape it rather than cache one.
        m = re.search(r'name="authToken"[^>]*value="([^"]+)"', page)
        if not m:
            raise LoginRefused("no authToken on the login page")

        form = urllib.parse.urlencode({
            "languageChange":   "0",
            "languageSelected": "ENG",
            "Multilingual":     "",
            "authToken":        m.group(1),
            "mtenCurrent_Min":  "",
            "mtenCurrent_Sec":  "",
            "accountLock":      "",
            "UserName":         self._user,
            "password":         self._pass,
            "hiddenPassword":   base64.b64encode(self._pass.encode()).decode(),
            "submitValue":      "1",
        })
        pre = _session_cookie(headers)
        status, headers, body = _request(
            self._host, "POST", "/login.cgi", body=form, cookie=pre,
            timeout=_LOGIN_TIMEOUT,
        )
        cookie = _session_cookie(headers) or pre
        # A good login answers with a redirect stub to indexMain.cgi. Being sent
        # anywhere else — or handed the login form back — means refused.
        if status != 200 or not cookie or "indexMain" not in body:
            raise LoginRefused("router did not open a session (HTTP %s)" % status)
        self._cookie = cookie

    def poll(self):
        """Read current LTE telemetry. Raises Evicted if someone took the session."""
        if not self._cookie:
            raise Evicted("no session")
        status, _headers, page = _request(
            self._host, "POST", "/lteStatus.cgi",
            body="act=Apply&interval=30", cookie=self._cookie,
        )
        if status != 200:
            raise RouterUnreachable("lteStatus.cgi returned HTTP %s" % status)
        if _is_evicted(page):
            # Keep the cookie as stale rather than dropping it: it is the key
            # to the slot this dead session still holds, and login() spends it.
            self._stale, self._cookie = self._cookie, None
            raise Evicted("session taken over by another login")
        return parse(page)

    def logout(self):
        """Release the single session slot so someone else can have it."""
        self._release()

    def _release(self):
        """Hand back whichever session we are still on the hook for.

        Best effort on purpose: this runs on the way out, before every login,
        and while yielding to a foreground task that is already waiting. A
        failure to log out politely must never delay or break any of those.
        """
        cookie = self._cookie or self._stale
        self._cookie = self._stale = None
        if not cookie:
            return
        try:
            _request(self._host, "GET", "/logout.cgi", cookie=cookie, timeout=5)
        except RouterUnreachable:
            pass


# ── is there actually internet? ───────────────────────────────────────────────

# The router can be attached to a healthy cell and still have nothing behind it:
# when the tower is running on failing backup power, the radio link stays up
# while its backhaul does not. No field on lteStatus.cgi can see past the tower,
# so the only way to tell that apart from a working link is to try to reach
# something beyond it.
#
# A TCP handshake to a public resolver is the cheapest honest probe: one SYN, no
# DNS lookup to confound it (the address is a literal), no payload, and nothing
# anyone could call abusive at one attempt per 30 s.
_PROBE_HOSTS = (("1.1.1.1", 53), ("8.8.8.8", 53))


def internet_reachable(timeout=3.0):
    """True if anything beyond the router answers. The second host is tried only
    if the first is silent, so one provider having a bad day is not an outage."""
    for host, port in _PROBE_HOSTS:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False
