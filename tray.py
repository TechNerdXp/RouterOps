"""The resident tray readout: RouterOps' first long-lived process.

Everything else in RouterOps is fire-and-exit: a context-menu click starts a
process, it drives one flow, it dies. This does not, and a process that stays up
for weeks has to answer for what it does while idle. So, plainly:

  Nothing here spins. The UI thread sits in GetMessageW, which parks the thread
  in the kernel until a message arrives; it is not scheduled and costs nothing
  while waiting. The poll thread sits in WaitForMultipleObjects with a 30-second
  timeout, which is the same kind of wait: the kernel wakes it on a timer or on
  a signal, whichever comes first. Two blocked threads and ~0.4 s of work a
  minute is the entire idle cost. A `while True: sleep(1)` would be a worse
  version of what the OS already provides properly, which is why there isn't one.

Reading the signal is a *sample*, not a poll-because-we-gave-up. RSRP and SINR
are continuously varying radio measurements with no change event to subscribe
to; the router's own web UI reads lteStatus.cgi on a 5-second timer for exactly
this reason. 30 seconds is a deliberate choice against that ceiling.

── sharing the router ────────────────────────────────────────────────────────
The B2368-66 keeps exactly ONE admin session: a second login silently evicts the
first. So a resident poller and the existing Selenium flows are in direct
competition for one slot, and the rule is settled: getting into the router wins,
the readout yields. It must never be the reason a reboot or a portal login fails.

The arbitration is a named mutex, the same idiom _InstanceGuard already uses in
main.py, and for the same reason: Windows releases it when the owning process
dies, however it dies, so there is no state that can wedge the app shut.

  A foreground task claims Local\\RouterOps.Router for its whole flow and then
  simply logs in, evicting us. That is allowed and expected.

  This thread claims the same mutex with a zero timeout before each sample. If
  it cannot, the router is someone else's right now: it drops its session and
  shows "paused" rather than a stale number, and, the important part, does
  not log back in until the mutex is free again. Losing the session is fine.
  Fighting over it mid-reboot is not.
"""

import ctypes
import os
import subprocess
import sys
import threading
import time
import winreg
from ctypes import wintypes

import diagnose
import history
import lte

user32   = ctypes.WinDLL("user32",   use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32    = ctypes.WinDLL("gdi32",    use_last_error=True)
shell32  = ctypes.WinDLL("shell32",  use_last_error=True)

LRESULT = ctypes.c_ssize_t

POLL_SECONDS = 30
RUN_KEY      = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE    = "RouterOps"


# ── Win32 surface ─────────────────────────────────────────────────────────────

WM_DESTROY, WM_COMMAND, WM_CLOSE, WM_NULL = 0x0002, 0x0111, 0x0010, 0x0000
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
WM_APP_TRAY   = 0x8001   # tray icon callbacks land here
WM_APP_SAMPLE = 0x8002   # poll thread -> UI thread: a new reading is ready

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04

MF_STRING, MF_SEPARATOR, MF_GRAYED, MF_CHECKED = 0x0000, 0x0800, 0x0001, 0x0008
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100

WAIT_OBJECT_0, WAIT_TIMEOUT = 0x0, 0x102
IDI_APPLICATION = 32512

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_byte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT), ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


# Every handle-returning and handle-taking call is declared, without exception.
# ctypes defaults an undeclared argument to C int, and a HBITMAP or HICON on
# 64-bit Windows does not fit in one, so an undeclared DeleteObject either
# raises on the spot or, worse, silently truncates the handle and frees nothing.
# That is the difference between a tray icon that runs for weeks and one that
# leaks GDI objects until the desktop stops drawing.

HGDIOBJ = wintypes.HANDLE
HMENU   = wintypes.HANDLE

user32.DefWindowProcW.restype     = LRESULT
user32.DefWindowProcW.argtypes    = [wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM]
user32.RegisterClassExW.restype   = wintypes.ATOM
user32.RegisterClassExW.argtypes  = [ctypes.POINTER(WNDCLASSEXW)]
user32.CreateWindowExW.restype    = wintypes.HWND
user32.CreateWindowExW.argtypes   = [wintypes.DWORD, wintypes.LPCWSTR,
                                     wintypes.LPCWSTR, wintypes.DWORD,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, wintypes.HWND, HMENU,
                                     wintypes.HINSTANCE, wintypes.LPVOID]
user32.PostMessageW.argtypes      = [wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM]
user32.GetMessageW.argtypes       = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                     wintypes.UINT, wintypes.UINT]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.GetSystemMetrics.argtypes  = [ctypes.c_int]

user32.GetDC.restype              = wintypes.HDC
user32.GetDC.argtypes             = [wintypes.HWND]
user32.ReleaseDC.argtypes         = [wintypes.HWND, wintypes.HDC]
user32.CreateIconIndirect.restype = wintypes.HICON
user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
user32.DestroyIcon.argtypes       = [wintypes.HICON]
user32.LoadIconW.restype          = wintypes.HICON
user32.LoadIconW.argtypes         = [wintypes.HINSTANCE, wintypes.LPCWSTR]

user32.CreatePopupMenu.restype    = HMENU
user32.AppendMenuW.argtypes       = [HMENU, wintypes.UINT, ctypes.c_size_t,
                                     wintypes.LPCWSTR]
user32.TrackPopupMenu.restype     = wintypes.BOOL
user32.TrackPopupMenu.argtypes    = [HMENU, wintypes.UINT, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                     wintypes.LPVOID]
user32.DestroyMenu.argtypes       = [HMENU]
user32.GetGuiResources.argtypes   = [wintypes.HANDLE, wintypes.DWORD]
user32.GetGuiResources.restype    = wintypes.DWORD

gdi32.CreateDIBSection.restype    = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes   = [wintypes.HDC,
                                     ctypes.POINTER(BITMAPINFOHEADER),
                                     wintypes.UINT,
                                     ctypes.POINTER(ctypes.c_void_p),
                                     wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateBitmap.restype        = wintypes.HBITMAP
gdi32.CreateBitmap.argtypes       = [ctypes.c_int, ctypes.c_int, wintypes.UINT,
                                     wintypes.UINT, wintypes.LPVOID]
gdi32.DeleteObject.argtypes       = [HGDIOBJ]

shell32.Shell_NotifyIconW.restype  = wintypes.BOOL
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                      ctypes.POINTER(NOTIFYICONDATAW)]

kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.CreateMutexW.restype     = wintypes.HANDLE
kernel32.CreateMutexW.argtypes    = [wintypes.LPVOID, wintypes.BOOL,
                                     wintypes.LPCWSTR]
kernel32.ReleaseMutex.argtypes    = [wintypes.HANDLE]
kernel32.CreateEventW.restype     = wintypes.HANDLE
kernel32.CreateEventW.argtypes    = [wintypes.LPVOID, wintypes.BOOL,
                                     wintypes.BOOL, wintypes.LPCWSTR]
kernel32.SetEvent.argtypes        = [wintypes.HANDLE]
kernel32.ResetEvent.argtypes      = [wintypes.HANDLE]
kernel32.CloseHandle.argtypes     = [wintypes.HANDLE]
kernel32.WaitForSingleObject.restype  = wintypes.DWORD
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForMultipleObjects.restype  = wintypes.DWORD
kernel32.WaitForMultipleObjects.argtypes = [wintypes.DWORD,
                                            ctypes.POINTER(wintypes.HANDLE),
                                            wintypes.BOOL, wintypes.DWORD]


# ── the icon ──────────────────────────────────────────────────────────────────

# Drawn at runtime rather than shipped as a set of .ico files, because the glyph
# carries two independent facts and a file per combination would be 20 files.
# Colour says whether the internet works; bars say how good the radio is. The
# combination is the point: red bars at full height means the signal is perfect
# and the internet is still down, which is precisely the state that tells you
# rebooting the router is a waste of two minutes.

_COLOURS = {                      # (B, G, R)
    diagnose.OK:        (0x50, 0xAF, 0x4C),   # green
    diagnose.DEGRADED:  (0x00, 0xB3, 0xFF),   # amber
    diagnose.BACKHAUL:  (0x35, 0x39, 0xE5),   # red: signal fine, internet gone
    diagnose.NOSERVICE: (0x35, 0x39, 0xE5),   # red
    diagnose.ROUTER:    (0x35, 0x39, 0xE5),   # red
    diagnose.NOLOGIN:   (0x35, 0x39, 0xE5),   # red: a real problem to fix
    diagnose.NOSESSION: (0x9E, 0x9E, 0x9E),   # grey: not looking, not broken
    diagnose.PAUSED:    (0x9E, 0x9E, 0x9E),   # grey
    diagnose.STARTING:  (0x9E, 0x9E, 0x9E),   # grey
}
_EMPTY = (0x60, 0x60, 0x60)       # unlit bar: visible on light and dark taskbars

# Opacities, which carry as much of the meaning as the colours do and were
# picked against both taskbar themes rather than by eye on one:
#   unlit  a bar that is simply not reached. It has to be legible on a dark
#          taskbar without competing with the lit bars on a light one, where a
#          mid-grey at any real weight sits as heavy as the colour beside it.
#   dead   every bar, in the state's own colour, when there is no signal at all.
#          Faint enough never to be mistaken for a reading, strong enough to
#          still read as red; too light and the worst state of the lot is the
#          one that looks most benign on a light taskbar.
_A_LIT, _A_UNLIT, _A_DEAD = 255, 75, 160


def _icon_size():
    return user32.GetSystemMetrics(49) or 16   # SM_CXSMICON, DPI-aware


PHI = 1.6180339887


# The golden ratio sets the RANGE of the bars, not the step between them.
# Compounding it per bar (3·5·8·13, consecutive Fibonacci) sounds right and
# looks wrong: over four steps it is a 4.3× spread, so the glyph reads as three
# stubs standing next to one tower. Making the shortest bar 1/φ² of the tallest
# (a 2.8× spread) and spacing the two between them evenly gives the climb the
# eye actually reads as a meter.
#
# The tallest is 7/8 of the icon, so the run has air above it instead of
# butting into the edge and reading as clipped. Everything derives from `size`,
# so a 20, 24 or 32 px icon on a high-DPI taskbar is drawn at its own scale
# rather than being a 16 px drawing stretched.
#
# Bar width and gap are not golden and do not pretend to be: at 16 px the whole
# run has 15 usable pixels, and φ between bar and gap needs 18. Pixels win.

def _bar_geometry(size):
    """Bar rectangles for an icon of this size, as (x, width, height)."""
    unit  = size / 16.0
    width = max(2, round(3 * unit))
    gap   = max(1, round(1 * unit))
    span  = 4 * width + 3 * gap
    left  = max(0, (size - span) // 2)         # centred; the old run started at 0

    tall  = max(4, round(size * 0.875))
    short = max(2, round(tall / (PHI * PHI)))
    return [(left + i * (width + gap), width,
             round(short + (tall - short) * i / 3.0))
            for i in range(4)]


def make_icon(state, bars):
    """Build an HICON of signal bars. Caller owns it and must DestroyIcon it."""
    size = _icon_size()
    lit  = _COLOURS.get(state, _EMPTY)
    px   = bytearray(size * size * 4)          # BGRA, top-down, starts transparent

    # With no bars at all the glyph would be an empty square, so the whole run
    # is drawn in the state's own colour, faint: a dim red silhouette still
    # reads as "this is bad" at a glance, and keeps one shape for every state
    # instead of swapping in a different mark for the dead ones.
    dim_is_state = bars == 0

    for i, (x0, width, height) in enumerate(_bar_geometry(size)):
        if i < bars:
            colour, alpha = lit, _A_LIT
        elif dim_is_state:
            colour, alpha = lit, _A_DEAD
        else:
            colour, alpha = _EMPTY, _A_UNLIT
        for y in range(size - height, size):
            row = y * size * 4
            for x in range(x0, min(x0 + width, size)):
                o = row + x * 4
                px[o:o + 4] = bytes((colour[0], colour[1], colour[2], alpha))

    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth, bmi.biHeight = size, -size    # negative = top-down
    bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0

    hdc  = user32.GetDC(None)
    bits = ctypes.c_void_p()
    colour_bmp = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0,
                                        ctypes.byref(bits), None, 0)
    user32.ReleaseDC(None, hdc)
    if not colour_bmp:
        return user32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))
    ctypes.memmove(bits, bytes(px), len(px))

    # The alpha channel does the masking on a 32-bpp icon, so the mask bitmap
    # only has to exist, but it does have to be deleted, like the colour one.
    mask_bmp = gdi32.CreateBitmap(size, size, 1, 1, None)
    info = ICONINFO(True, 0, 0, mask_bmp, colour_bmp)
    hicon = user32.CreateIconIndirect(ctypes.byref(info))
    gdi32.DeleteObject(colour_bmp)
    gdi32.DeleteObject(mask_bmp)
    return hicon or user32.LoadIconW(None, ctypes.c_wchar_p(IDI_APPLICATION))


# ── launching the app's other tasks ───────────────────────────────────────────

def _self_command(arg):
    """Argv to re-run RouterOps with one task arg, frozen or from source.

    `arg` is None for the router portal, which is the plain no-argument launch;
    main() falls through to open_router when it recognises no task. So None has
    to be dropped here rather than appended, or subprocess is handed a None to
    join into a command line and the portal fails to open at all.
    """
    base = [sys.executable]
    if not getattr(sys, "frozen", False):
        base.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "main.py"))
    return base + ([arg] if arg else [])


def launch(arg):
    """Start a task in its own process, exactly as a context-menu click would.

    Deliberately a separate process rather than an in-process call: those flows
    drive Chrome, take minutes, and end by exiting. Running one inside the
    resident process would put a Selenium failure in the same process as the
    tray icon, and the tray would go down with it.
    """
    try:
        subprocess.Popen(_self_command(arg), close_fds=True)
    except OSError:
        pass


# ── autostart ─────────────────────────────────────────────────────────────────

def _autostart_command():
    return '"%s" --tray' % sys.executable


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
            return True
    except OSError:
        return False


def set_autostart(on):
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            if on:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ,
                                  _autostart_command())
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)
                except OSError:
                    pass
    except OSError:
        pass


def refresh_autostart_path():
    """Re-point an existing autostart entry at wherever the exe now lives.

    The Run value is written once, when the menu item is ticked, and nothing
    rewrote it afterwards, so moving the app left an entry naming a path that
    is gone. Nothing reported that: the menu still showed the tick, because the
    value existed, and the only symptom was no tray icon after a reboot. The
    context menu and Jump List already rebuild themselves from sys.executable
    on every launch; this is the one pointer that did not.
    """
    if not getattr(sys, "frozen", False):
        return                      # from source sys.executable is python.exe
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            current, _ = winreg.QueryValueEx(k, RUN_VALUE)
    except OSError:
        return                      # not enabled; leave it that way
    if current != _autostart_command():
        set_autostart(True)


# ── the poll thread ───────────────────────────────────────────────────────────

class Monitor:
    """Owns the router session and the sampling cadence.

    Runs on its own thread because a sample is a blocking HTTP round trip of
    ~0.4 s, and a message pump that stalls for 0.4 s every 30 s is a pump that
    visibly hitches when you right-click it.
    """

    def __init__(self, username, password, hwnd, log, history_path):
        self._session = lte.LteSession(username, password)
        self._hwnd    = hwnd
        self._log     = log
        self._history = history_path
        self._lock    = threading.Lock()

        # Both are kernel objects so the wait below can be a real wait. _wake is
        # manual-reset so an explicit "Refresh now" cannot be missed between ticks.
        self._stop = kernel32.CreateEventW(None, True, False, None)
        self._wake = kernel32.CreateEventW(None, True, False, None)

        self._assessment = diagnose.Assessment(
            diagnose.STARTING, "Starting up…", "", 0)
        self._sample      = None
        self._prev_sample = None
        self._verdict     = None        # last assessment that was not a gap
        self._down_since  = None        # time.time() the link was first seen down
        self._paused      = False       # by the user, as opposed to yielding
        self._pending     = None        # a better verdict waiting to be confirmed
        self._evictions   = 0           # consecutive; drives the backoff below
        self._skip        = 0           # ticks to sit out while someone else has it
        self._thread      = threading.Thread(target=self._run, daemon=True)

    # -- state shared with the UI thread ---------------------------------------

    def snapshot(self):
        with self._lock:
            assessment, sample, paused = self._assessment, self._sample, self._paused
            down_since = self._down_since
        # Seconds down, but only while we are looking: a grey icon saying "down
        # for 20m" would be claiming something it cannot currently see.
        down = (None if down_since is None
                or assessment.state in diagnose.INDETERMINATE
                else time.time() - down_since)
        return assessment, sample, down, paused

    def _publish(self, assessment, sample):
        with self._lock:
            self._assessment = assessment
            if sample is not None:
                self._sample = sample
        # Written for every tick, including the paused ones: a gap in the record
        # is itself a fact, and the history view draws it as one rather than
        # pretending the minutes either side joined up.
        history.record(self._history, assessment.state, sample, assessment.bars)
        user32.PostMessageW(self._hwnd, WM_APP_SAMPLE, 0, 0)

    # -- lifecycle -------------------------------------------------------------

    def start(self):
        self._thread.start()

    def stop(self):
        kernel32.SetEvent(self._stop)

    def refresh_now(self):
        kernel32.SetEvent(self._wake)

    def toggle_pause(self):
        """Flip the flag and wake the poll thread, which does the rest.

        Pausing means logging out, and that is a network call of up to five
        seconds. Made from here it stalled the menu while it ran, and it used
        the session from the UI thread while the poll thread might be halfway
        through a sample on the same one. The session belongs to one thread.
        """
        with self._lock:
            self._paused = not self._paused
        self.refresh_now()

    def shutdown(self):
        self.stop()
        self._thread.join(timeout=5)
        self._session.logout()

    # -- the loop --------------------------------------------------------------

    def _run(self):
        pruned_at = 0.0
        while True:
            # On the way in and then once a day. The monitor runs for weeks at a
            # time, and pruning only at start let the file outgrow its 30 days.
            if time.time() - pruned_at >= history.DAY:
                history.prune(self._history)
                pruned_at = time.time()
            try:
                self._tick()
            except Exception as exc:              # a sample must never kill the thread
                self._log.warning("monitor tick failed: %s", exc)
            if self._sleep():
                break
        self._session.logout()

    def _sleep(self):
        """Wait out the cadence. True if we were asked to stop.

        WaitForMultipleObjects blocks in the kernel: the thread is descheduled
        until one of the events fires or the timeout elapses. This is the whole
        of the app's idle cost.
        """
        handles = (wintypes.HANDLE * 2)(self._stop, self._wake)
        result = kernel32.WaitForMultipleObjects(2, handles, False,
                                                 POLL_SECONDS * 1000)
        if result == WAIT_OBJECT_0:
            return True
        if result == WAIT_OBJECT_0 + 1:
            kernel32.ResetEvent(self._wake)
        return False

    def _tick(self):
        with self._lock:
            paused = self._paused
        if paused:
            self._session.logout()        # a no-op once nothing is held
            self._publish(diagnose.Assessment(
                diagnose.PAUSED, "Monitoring paused", "", 0), None)
            return

        # Sitting out. The router mutex only arbitrates between RouterOps' own
        # tasks; a person logging in from a browser holds no mutex, and against
        # a single-session device our next login throws them out of the page
        # they are reading. Retrying every 30 seconds would be a login war we
        # would win half of and ruin entirely, so consecutive evictions back
        # off, doubling, to a ceiling of eight ticks (four minutes). Whoever
        # has the router gets to keep it, and we come back when they are done.
        if self._skip > 0:
            self._skip -= 1
            # Still a tick of watching nothing, and recorded as one, so the
            # report shows these minutes as paused rather than as a blank.
            self._publish(self._assessment, None)
            return

        # Claim the router, or stand down. Zero timeout: if a foreground task
        # holds it we want the answer now, not to queue behind a two-minute
        # reboot. Losing here is a normal outcome, not an error.
        handle = kernel32.CreateMutexW(None, False, lte.ROUTER_MUTEX)
        if not handle:
            return
        owned = kernel32.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0
        if not owned:
            kernel32.CloseHandle(handle)
            if self._session.live:
                # Give the slot up rather than make them evict us, and never
                # show a number we can no longer stand behind.
                self._session.logout()
                self._log.info("router busy with another task; monitoring paused")
            self._publish(diagnose.Assessment(
                diagnose.PAUSED, "Router in use by another task", "", 0), None)
            return
        try:
            self._sample_once()
        finally:
            kernel32.ReleaseMutex(handle)
            kernel32.CloseHandle(handle)

    def _settle(self, candidate):
        """Adopt a new verdict once it has held, unless it is bad news.

        Radio measurements wander continuously, so a link parked on a threshold
        flips class on every sample. This one sits almost exactly on the SINR
        5 dB line between "fair" and "poor", which had it alternating between
        connected and weak every 30 seconds: the icon flickering, and a log
        filling with changes that are noise rather than events.

        Damping is one-sided on purpose. A move to a worse state is taken at
        once, because waiting another 30 seconds to admit the internet is gone
        is a worse failure than a little flicker. A move to a better one has to
        survive two samples before it counts.

        The cost is that one tick of a genuine improvement is published under
        the old verdict; the reading beside it is still the fresh one.
        """
        previous = self._assessment
        if candidate.state == previous.state:
            self._pending = None
            return candidate                      # same class, newer numbers
        if not candidate.usable or previous.state in diagnose.INDETERMINATE:
            self._pending = None
            return candidate                      # downgrades and gaps: no wait
        if self._pending == candidate.state:
            self._pending = None
            return candidate                      # held for two; it is real
        self._pending = candidate.state
        return previous

    def _read(self):
        """One reading. Returns (sample, fault); fault is None when it worked.

        A single eviction is retried once, straight away: we hold the router
        mutex, so no RouterOps task is competing, and this firmware does
        sometimes drop a session seconds after issuing it for no reason visible
        from outside. That retry is only worth making when the last read worked
        because once evictions are repeating, something really is holding the router
        and a second login per tick is just a second eviction for whoever has
        it. See _tick for the backoff that goes with this.
        """
        attempts = (1, 2) if self._evictions == 0 else (1,)
        for attempt in attempts:
            try:
                if not self._session.live:
                    self._session.login()
                return self._session.poll(), None
            except lte.Evicted:
                if attempt != attempts[-1]:
                    continue
                # Most likely a by-hand browser login. Their session, their
                # router; pick up again when they are done with it.
                self._log.info("session lost twice; waiting for the next tick")
                return None, "session"
            except lte.LoginRefused as exc:
                self._log.warning("login refused: %s", exc)
                return None, "login"
            except lte.RouterUnreachable as exc:
                self._log.info("router unreachable: %s", exc)
                return None, "unreachable"
        return None, "session"

    def _sample_once(self):
        sample, fault = self._read()

        if fault == "session":
            self._evictions += 1
            # The first loss costs nothing: the firmware drops sessions on its
            # own and the next login frees the slot and takes a fresh one, so
            # waiting would only add 30 seconds of blindness to something that
            # heals itself. It is a loss that *keeps* happening that means
            # somebody else is really using the router, and that is what backs
            # off, doubling, to a ceiling of eight ticks.
            self._skip = 0 if self._evictions == 1 else min(2 ** (self._evictions - 2), 8)
            if self._skip:
                self._log.info("router is someone else's; backing off %d tick%s",
                               self._skip, "" if self._skip == 1 else "s")
        elif fault is None:
            if self._evictions:
                self._log.info("router free again after %d eviction%s",
                               self._evictions, "" if self._evictions == 1 else "s")
            self._evictions = 0
            self._skip = 0

        # Only ask the wider internet when the router says the radio is up:
        # with no LTE attachment the answer is a foregone conclusion, and a
        # probe that cannot inform anything should not be sent.
        internet = None
        if sample and (sample.get("status") or "").upper().startswith("LTE"):
            internet = lte.internet_reachable()

        assessment = self._settle(diagnose.assess(sample, internet, fault))
        now = time.time()

        # Transitions go to the log and nowhere else. The icon's colour is the
        # notification; a popup every time the link twitches would be the thing
        # you end up turning off, and then the colour is all you had anyway.
        # The log still accumulates the evidence, so "it has been dropping every
        # twenty minutes since six" stays provable after the fact.
        #
        # Only transitions are written. At two samples a minute, a line each
        # would fill the 256 KB rotation inside a day and bury the events that
        # matter underneath the ones that don't.
        #
        # Judged against the last real verdict, not the last tick. A pause or a
        # lost session between two verdicts is a gap in watching, and comparing
        # with the gap itself swallowed the event on its far side: a reboot's
        # pause followed by "Router not answering" logged no "lost", so the
        # "restored" that ended it had nothing before it (2026-09-21 14:48).
        for kind, text in diagnose.transitions(assessment, self._verdict,
                                               sample, self._prev_sample):
            if kind == "restored" and self._down_since is not None:
                # Recorded here rather than left to the reader to subtract two
                # timestamps: how long it was out is the fact worth keeping.
                # By the clock, not by counting ticks: a tick during an outage
                # runs long while the probe waits out its timeouts, and counting
                # 30 s a tick logged a 53-minute outage as 44.
                text = "%s (down for %s)" % (
                    text, diagnose.human_duration(now - self._down_since))
            self._log.info("%s: %s", kind, text)

        if assessment.state not in diagnose.INDETERMINATE:
            self._verdict = assessment
            if assessment.usable:
                self._down_since = None
            elif self._down_since is None:
                self._down_since = now

        if sample:
            self._prev_sample = sample
        self._publish(assessment, sample)


# ── the tray window ───────────────────────────────────────────────────────────

# Menu command ids. The tray's own controls are fixed; the router tasks are
# numbered from ID_TASK_BASE as they are rendered, because what they are comes
# from main._tray_menu() and is not this module's business.
ID_REFRESH, ID_PAUSE, ID_AUTOSTART, ID_LOG = 1, 2, 3, 4
ID_EXIT = 99
ID_TASK_BASE = 100


class TrayWindow:
    """The icon, its menu, and the message pump that drives both."""

    def __init__(self, monitor_factory, log, log_path, menu_groups):
        self._log         = log
        self._log_path    = log_path
        self._menu_groups = menu_groups
        self._task_args   = {}
        self._icon     = None      # current HICON; ours, destroyed on replace
        self._added    = False
        self._monitor  = None

        # Held as an attribute because ctypes does not keep the trampoline alive
        # on its own; let it be collected and the first message into the window
        # proc jumps into freed memory.
        self._wndproc = WNDPROC(self._on_message)

        # Explorer broadcasts this when it restarts and rebuilds the taskbar.
        # Without re-adding the icon on it, the icon disappears for good the
        # first time Explorer restarts, which looks exactly like a crash.
        # Note the window below is a normal top-level window, not a message-only
        # one: message-only windows are excluded from broadcasts, so HWND_MESSAGE
        # would quietly cost us this notification and with it the recovery.
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

        self._hwnd = self._make_window()
        self._monitor = monitor_factory(self._hwnd)

    def _make_window(self):
        hinst = kernel32.GetModuleHandleW(None)
        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.lpfnWndProc = self._wndproc
        cls.hInstance = hinst
        cls.lpszClassName = "RouterOpsTray"
        if not user32.RegisterClassExW(ctypes.byref(cls)):
            err = ctypes.get_last_error()
            if err != 1410:                     # ERROR_CLASS_ALREADY_EXISTS
                raise ctypes.WinError(err)
        self._class = cls                       # keep the class (and proc) alive
        hwnd = user32.CreateWindowExW(
            0, "RouterOpsTray", "RouterOps", 0,
            0, 0, 0, 0, None, None, hinst, None,
        )
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        return hwnd                             # never shown; no WS_VISIBLE

    # -- icon ------------------------------------------------------------------

    def _nid(self, flags):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = flags
        nid.uCallbackMessage = WM_APP_TRAY
        return nid

    def _apply(self, assessment, sample, down_since):
        icon = make_icon(assessment.state, assessment.bars)
        nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        nid.hIcon = icon
        nid.szTip = diagnose.tooltip(assessment, sample, down_since)
        ok = shell32.Shell_NotifyIconW(NIM_MODIFY if self._added else NIM_ADD,
                                       ctypes.byref(nid))
        self._added = bool(ok) or self._added
        # Replace first, then destroy the old one, never the other way round,
        # and never skipped: one leaked HICON every 30 s is a GDI handle leak
        # that takes about a day to turn into a visibly broken desktop.
        old, self._icon = self._icon, icon
        if old:
            user32.DestroyIcon(old)

    def _remove_icon(self):
        if self._added:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid(0)))
            self._added = False
        if self._icon:
            user32.DestroyIcon(self._icon)
            self._icon = None

    # -- menu ------------------------------------------------------------------

    def _show_menu(self):
        assessment, _sample, _down, paused = self._monitor.snapshot()
        menu = user32.CreatePopupMenu()

        # The verdict, at the top, in at most two short lines: what it is, and
        # what to do about it when there is anything to do. This is a menu, and
        # a paragraph up here just pushes the things you came to click off the
        # bottom. The numbers live in the hover tooltip, which has room.
        user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, assessment.headline[:52])
        if assessment.hint:
            user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0,
                               "   " + assessment.hint[:52])
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)

        # The same operations the taskbar pin offers, in the same grouping,
        # from the same table; see main._tray_menu(). Ids are assigned per
        # render from ID_TASK_BASE up, so nothing here has to know what the
        # tasks are.
        self._task_args = {}
        ident = ID_TASK_BASE
        for group in self._menu_groups:
            for label, arg in group:
                self._task_args[ident] = arg
                user32.AppendMenuW(menu, MF_STRING, ident, label)
                ident += 1
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)

        user32.AppendMenuW(menu, MF_STRING, ID_REFRESH, "Refresh now")
        user32.AppendMenuW(menu, MF_STRING, ID_PAUSE,
                           "Resume monitoring" if paused else "Pause monitoring")
        user32.AppendMenuW(menu, MF_STRING | (MF_CHECKED if autostart_enabled() else 0),
                           ID_AUTOSTART, "Start with Windows")
        user32.AppendMenuW(menu, MF_STRING, ID_LOG, "Open log folder")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_EXIT, "Exit")

        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        # Without this the menu never dismisses when you click away, the
        # documented quirk of showing a popup from a window that is not the
        # foreground window. The WM_NULL afterwards is the other half of it.
        user32.SetForegroundWindow(self._hwnd)
        choice = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                       pt.x, pt.y, 0, self._hwnd, None)
        user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if choice:
            self._command(choice)

    def _command(self, ident):
        if ident in self._task_args:
            launch(self._task_args[ident])
        elif ident == ID_REFRESH:
            self._monitor.refresh_now()
        elif ident == ID_PAUSE:
            self._monitor.toggle_pause()
        elif ident == ID_AUTOSTART:
            set_autostart(not autostart_enabled())
        elif ident == ID_LOG:
            try:
                os.startfile(os.path.dirname(self._log_path))
            except OSError:
                pass
        elif ident == ID_EXIT:
            user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)

    # -- messages --------------------------------------------------------------

    def _on_message(self, hwnd, msg, wparam, lparam):
        try:
            if msg == self._taskbar_created:
                self._added = False             # the taskbar forgot us; re-add
                self._refresh_icon()
                return 0
            if msg == WM_APP_TRAY:
                # Left opens the history, right opens the menu. The history is
                # what you reach for the moment the icon changes colour, and it
                # is free: no router session, no traffic, and it works while
                # the line is down. The portal would be the obvious choice but
                # it is the rarer errand once the tray is telling you what is
                # happening, and a speed test spends real bandwidth, which is
                # too much to fire on a stray click. No double-click gesture:
                # it cannot coexist with a single-click action, because the
                # first click fires before the second arrives.
                if lparam == WM_LBUTTONUP:
                    launch("--history")
                elif lparam == WM_RBUTTONUP:
                    self._show_menu()
                return 0
            if msg == WM_APP_SAMPLE:
                self._refresh_icon()
                return 0
            if msg in (WM_CLOSE, WM_DESTROY):
                self._remove_icon()
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            # An exception raised through a ctypes callback unwinds into Windows'
            # own stack, where there is nothing to catch it. Anything that goes
            # wrong drawing an icon has to stay inside this function.
            self._log.exception("tray message %s failed", msg)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _refresh_icon(self):
        assessment, sample, down_since, _paused = self._monitor.snapshot()
        self._apply(assessment, sample, down_since)

    # -- run -------------------------------------------------------------------

    def run(self):
        self._monitor.start()
        self._refresh_icon()
        msg = wintypes.MSG()
        # GetMessageW blocks in the kernel until something arrives. This loop
        # body runs once per actual event, not continuously; while nothing is
        # happening the thread is not scheduled and the app costs nothing.
        while True:
            got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self._monitor.shutdown()
        self._remove_icon()


def run_tray(username, password, log, log_path, menu_groups, history_path):
    """Entry point: build the window, hand it a monitor, pump until Exit.

    `menu_groups` is main._tray_menu() output: the router operations, grouped,
    passed in rather than imported so this module stays a shell around whatever
    the app decides its tasks are.
    """
    refresh_autostart_path()
    window = TrayWindow(
        lambda hwnd: Monitor(username, password, hwnd, log, history_path),
        log, log_path, menu_groups)
    window.run()
