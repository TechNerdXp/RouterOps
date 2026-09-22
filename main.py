"""RouterOps' entry point: what each command-line task is, where it shows up in
the shell, and which process may run it.

The tasks that drive Chrome live in browser.py and are imported only when one
runs, so the tray, which stays up for weeks and drives no browser, never
loads Selenium.
"""

import ctypes
import logging
import os
import sys
import winreg
from ctypes import wintypes
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

load_dotenv(os.path.join(
    getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))),
    ".env",
))

# ── logging ───────────────────────────────────────────────────────────────────
# The frozen exe runs --noconsole, so the log file is the only place failures
# can surface; keep it outside the (read-only) bundle dir.

LOG_DIR  = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                        "RouterOps")
LOG_PATH = os.path.join(LOG_DIR, "routerops.log")

log = logging.getLogger("routerops")
log.setLevel(logging.INFO)
try:
    os.makedirs(LOG_DIR, exist_ok=True)
    _fh = RotatingFileHandler(LOG_PATH, maxBytes=262144, backupCount=1,
                              encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_fh)
except OSError:
    log.addHandler(logging.NullHandler())
if not getattr(sys, "frozen", False):
    log.addHandler(logging.StreamHandler())

# ── device + menu definitions ─────────────────────────────────────────────────
# (label, registry_key, [(task_label, cli_arg, separator_before)])
DEVICES = [
    (
        "Huawei LTE CPE B2368-66",
        "RouterOpsHuawei",
        [
            ("Reboot Huawei",  "--reboot-huawei", False),
            ("Guest Mode On",  "--guest-on",      True),
            ("Guest Mode Off", "--guest-off",     False),
        ],
    ),
    (
        "TP-Link TL-WR844N",
        "RouterOpsTplink",
        [
            ("Reboot TP-Link", "--reboot-tplink", False),
        ],
    ),
]

# standalone: not tied to any single device
UTILITIES = [
    ("Signal Monitor", "--tray"),
    ("Network Report", "--history"),
    ("Speed Check",    "--speed-check"),
]

AUMID = "TechNerdXp.RouterOps"
HKCU  = winreg.HKEY_CURRENT_USER


def _reg_delete_tree(hkey, path):
    try:
        with winreg.OpenKey(hkey, path, 0, winreg.KEY_ALL_ACCESS) as key:
            while True:
                try:
                    _reg_delete_tree(hkey, path + "\\" + winreg.EnumKey(key, 0))
                except OSError:
                    break
        winreg.DeleteKey(hkey, path)
    except Exception:
        pass


# ── registry / jump list registration ────────────────────────────────────────

def register_context_menu():
    if not getattr(sys, "frozen", False):
        return
    exe = sys.executable
    exefile_shell = r"Software\Classes\exefile\shell"

    # Clean up old keys from previous versions
    for old in ["RouterOpsDevice", "RouterOpsReboot", "RouterOpsSpeedCheck",
                "RouterOpsEnableTV", "RouterOpsDisableTV",
                "RouterOpsEnableGujjar", "RouterOpsDisableGujjar"]:
        _reg_delete_tree(HKCU, f"{exefile_shell}\\{old}")

    try:
        for dev_label, reg_key, tasks in DEVICES:
            device_path = f"{exefile_shell}\\{reg_key}"
            shell_path  = f"{device_path}\\shell"

            # Wipe the verbs before rewriting them. They are named by position
            # ("1_speed_check"), so inserting or removing a task renumbers the
            # rest and the old keys are left behind as duplicate menu entries;
            # writing over the top only ever adds. Regenerating from empty is
            # the only version that can also delete.
            _reg_delete_tree(HKCU, shell_path)

            with winreg.CreateKey(HKCU, device_path) as k:
                winreg.SetValueEx(k, "MUIVerb",     0, winreg.REG_SZ, dev_label)
                winreg.SetValueEx(k, "SubCommands", 0, winreg.REG_SZ, "")
                winreg.SetValueEx(k, "AppliesTo",   0, winreg.REG_SZ,
                                  'System.FileName:="RouterOps.exe"')

            for i, (task_label, arg, sep_before) in enumerate(tasks):
                verb = f"{i + 1}_{arg.lstrip('-').replace('-', '_')}"
                with winreg.CreateKey(HKCU, f"{shell_path}\\{verb}") as k:
                    winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, task_label)
                    if sep_before:
                        winreg.SetValueEx(k, "CommandFlags", 0, winreg.REG_DWORD, 0x20)
                with winreg.CreateKey(HKCU, f"{shell_path}\\{verb}\\command") as k:
                    winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{exe}" {arg}')

        # Standalone utilities: flat entries outside device submenus
        util_path = f"{exefile_shell}\\RouterOpsUtils"
        _reg_delete_tree(HKCU, f"{util_path}\\shell")   # same reason as above
        with winreg.CreateKey(HKCU, util_path) as k:
            winreg.SetValueEx(k, "MUIVerb",     0, winreg.REG_SZ, "Network")
            winreg.SetValueEx(k, "SubCommands", 0, winreg.REG_SZ, "")
            winreg.SetValueEx(k, "AppliesTo",   0, winreg.REG_SZ,
                              'System.FileName:="RouterOps.exe"')
        for i, (task_label, arg) in enumerate(UTILITIES):
            verb = f"{i + 1}_{arg.lstrip('-').replace('-', '_')}"
            with winreg.CreateKey(HKCU, f"{util_path}\\shell\\{verb}") as k:
                winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, task_label)
            with winreg.CreateKey(HKCU, f"{util_path}\\shell\\{verb}\\command") as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{exe}" {arg}')
    except Exception:
        pass


def _stamp_pinned_shortcut():
    import glob
    from win32com.shell import shell
    from win32com.propsys import propsys, pscon
    import pythoncom

    taskbar = os.path.join(
        os.environ.get("APPDATA", ""),
        r"Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar",
    )
    for lnk in glob.glob(os.path.join(taskbar, "*.lnk")):
        try:
            link = pythoncom.CoCreateInstance(
                shell.CLSID_ShellLink, None,
                pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink,
            )
            pf = link.QueryInterface(pythoncom.IID_IPersistFile)
            pf.Load(lnk, 2)
            path, _ = link.GetPath(shell.SLGP_RAWPATH)
            if os.path.basename(path).lower() == "routerops.exe":
                store = link.QueryInterface(propsys.IID_IPropertyStore)
                store.SetValue(pscon.PKEY_AppUserModel_ID,
                               propsys.PROPVARIANTType(AUMID))
                store.Commit()
                pf.Save(lnk, True)
                break
        except Exception:
            pass


def register_jump_list():
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(AUMID)
    _stamp_pinned_shortcut()

    if not getattr(sys, "frozen", False):
        return

    exe = sys.executable

    from win32com.shell import shell
    from win32com.propsys import propsys, pscon
    import pythoncom

    def make_link(title, arg):
        link = pythoncom.CoCreateInstance(
            shell.CLSID_ShellLink, None,
            pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink,
        )
        link.SetPath(exe)
        link.SetArguments(arg)
        link.SetIconLocation(exe, 0)
        store = link.QueryInterface(propsys.IID_IPropertyStore)
        store.SetValue(pscon.PKEY_Title, propsys.PROPVARIANTType(title))
        store.Commit()
        return link

    def make_separator():
        link = pythoncom.CoCreateInstance(
            shell.CLSID_ShellLink, None,
            pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink,
        )
        store = link.QueryInterface(propsys.IID_IPropertyStore)
        pk = propsys.PSGetPropertyKeyFromName("System.AppUserModel.IsDestListSeparator")
        store.SetValue(pk, propsys.PROPVARIANTType(True, pythoncom.VT_BOOL))
        store.Commit()
        return link

    def make_collection(items):
        coll = pythoncom.CoCreateInstance(
            shell.CLSID_EnumerableObjectCollection, None,
            pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IObjectCollection,
        )
        for item in items:
            coll.AddObject(item)
        return coll

    try:
        cdl = pythoncom.CoCreateInstance(
            shell.CLSID_DestinationList, None,
            pythoncom.CLSCTX_INPROC_SERVER, shell.IID_ICustomDestinationList,
        )
        cdl.SetAppID(AUMID)
        try:
            cdl.DeleteList(AUMID)
        except Exception:
            pass
        cdl.BeginList()

        items = []
        for i, (dev_label, _reg_key, tasks) in enumerate(DEVICES):
            if i > 0:
                items.append(make_separator())
            for task_label, arg, _sep in tasks:
                items.append(make_link(task_label, arg))
        if UTILITIES:
            items.append(make_separator())
            for task_label, arg in UTILITIES:
                items.append(make_link(task_label, arg))

        cdl.AddUserTasks(make_collection(items))
        cdl.CommitList()
    except Exception:
        pass


# ── failure handling ──────────────────────────────────────────────────────────

def _alert(text):
    # Every _alert is terminal for this process, but the dialog blocks until
    # dismissed, so drop the single-instance claim first so an unattended error
    # box on screen never keeps the next launch out.
    if _GUARD is not None:
        _GUARD.release()
    try:  # error icon, topmost, foreground; the exe has no console
        ctypes.windll.user32.MessageBoxW(None, text, "RouterOps",
                                         0x10 | 0x1000 | 0x10000)
    except Exception:
        pass


# ── single-instance guard ─────────────────────────────────────────────────────
# A faulty mouse double-fires one taskbar/jump-list click, and the router flows
# need ~20s before anything is visible, so a second "nothing happened" click is
# easy to make. Either way two processes would drive their own Chrome into the
# same router session and interleave.
#
# A duplicate launch just disappears: no dialog, no second window, only a log
# line. Nothing is wrong from the user's side: the task they asked for is
# already on its way, so the extra click should cost them nothing, not even an
# OK button.
#
# The claim is a *named kernel object*, deliberately not a lock file: Windows
# destroys it the moment the last handle closes (normal exit, crash, or Task
# Manager kill alike), so there is no stale lock that can ever wedge the app
# shut, and nothing to clean up by hand.

_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
_KERNEL32.CreateMutexW.restype  = wintypes.HANDLE
_KERNEL32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
_KERNEL32.CloseHandle.argtypes  = [wintypes.HANDLE]

_ERROR_ALREADY_EXISTS = 183

_GUARD = None  # set once the running instance owns its task


def _create_named(name):
    """Create/open a session-local named mutex → (handle, existed_before)."""
    h = _KERNEL32.CreateMutexW(None, False, "Local\\" + name)
    return h, ctypes.get_last_error() == _ERROR_ALREADY_EXISTS


class _InstanceGuard:
    def __init__(self, task, label):
        self._name   = "RouterOps." + task
        self._label  = label
        self._handle = None

    def claim(self):
        """True if this process may run the task; False if another instance owns it."""
        try:
            handle, taken = _create_named(self._name)
        except Exception:  # the guard must never be why a task can't run
            return True
        if not handle:
            return True
        if taken:
            _KERNEL32.CloseHandle(handle)  # must never outlive the owner
            log.info("%s: duplicate launch ignored", self._label)
            return False
        self._handle = handle
        return True

    def release(self):
        handle, self._handle = self._handle, None
        if handle:
            _KERNEL32.CloseHandle(handle)


# ── windows that outlive the process that opened them ─────────────────────────
# The instance guard above dies with the process, which is the right lifetime
# for a task but the wrong one for the Network Report window: that window is
# handed to Chrome and this process exits seconds later, so by the time the user
# clicks Network Report again there is nothing left holding a claim and a second
# window opens beside the first. The window itself is the only thing that
# outlives the launch, so the window is what gets asked.
#
# Matched on its exact title rather than anything sturdier because there is
# nothing sturdier to match on: the browser is not ours, its process is shared
# with every other Chrome window, and the one thing we do control is what the
# page calls itself. history.PAGE_TITLE is that name, kept in one place.

_SW_RESTORE = 9

# Declared rather than left to ctypes' defaults: a bare call passes handles as
# 32-bit ints, which raises on any handle above 0x7FFFFFFF. That failure would
# land inside the guards below and turn a click into a silent no-op, the one
# outcome worse than the second window this replaces.
_U32 = ctypes.WinDLL("user32", use_last_error=True)
_ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_U32.EnumWindows.argtypes             = [_ENUMPROC, wintypes.LPARAM]
_U32.IsWindowVisible.argtypes         = [wintypes.HWND]
_U32.GetWindowTextLengthW.argtypes    = [wintypes.HWND]
_U32.GetWindowTextW.argtypes          = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_U32.IsIconic.argtypes                = [wintypes.HWND]
_U32.ShowWindow.argtypes              = [wintypes.HWND, ctypes.c_int]
_U32.GetForegroundWindow.restype      = wintypes.HWND
_U32.SetForegroundWindow.argtypes     = [wintypes.HWND]
_U32.BringWindowToTop.argtypes        = [wintypes.HWND]
_U32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, wintypes.LPDWORD]
_U32.GetWindowThreadProcessId.restype = wintypes.DWORD
_U32.AttachThreadInput.argtypes       = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]


def _find_window(title):
    """HWND of the visible top-level window with exactly this title, or None."""
    found = []

    @_ENUMPROC
    def visit(hwnd, _lparam):
        if not _U32.IsWindowVisible(hwnd):
            return True
        length = _U32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _U32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value != title:
            return True
        found.append(hwnd)
        return False  # stop at the first

    try:
        _U32.EnumWindows(visit, 0)
    except Exception:  # never let looking for a window stop us opening one
        return None
    return found[0] if found else None


def _raise_window(hwnd):
    """Bring someone else's window to the front, and un-minimise it first.

    SetForegroundWindow on its own is not enough here. Windows only grants the
    foreground to a process that already has it or that received the last input
    event, and this process has neither: the click landed on the tray icon or
    the Jump List, and we were started by it. The call then fails silently, the
    window stays where it was, and the click looks like it did nothing, which
    is the complaint this whole path exists to fix. Borrowing the foreground
    thread's input queue for the length of the call is the documented way round
    it, and it is released immediately.
    """
    attached, other, me = False, 0, 0
    try:
        if _U32.IsIconic(hwnd):
            _U32.ShowWindow(hwnd, _SW_RESTORE)
        foreground = _U32.GetForegroundWindow()
        if foreground:
            other = _U32.GetWindowThreadProcessId(foreground, None)
            me = _KERNEL32.GetCurrentThreadId()
            if other and other != me:
                attached = bool(_U32.AttachThreadInput(other, me, True))
        _U32.SetForegroundWindow(hwnd)
        _U32.BringWindowToTop(hwnd)
    except Exception:
        pass
    finally:
        if attached:
            try:
                _U32.AttachThreadInput(other, me, False)
            except Exception:
                pass


# ── signal monitor ────────────────────────────────────────────────────────────

def signal_monitor():
    """The resident tray readout, RouterOps' only long-lived task.

    Imported here rather than at module scope so that every other flow, which
    exits in seconds, does not pay for loading it. It brings in no third-party
    package: the router speaks plain HTTP once a browser is out of the way, and
    the tray is ctypes against the same Win32 surface the rest of this file uses.
    """
    import tray
    user, password = os.getenv("ROUTER_USERNAME"), os.getenv("ROUTER_PASSWORD")
    if not user or not password:
        _alert("Signal Monitor needs ROUTER_USERNAME and ROUTER_PASSWORD in .env.")
        return
    log.info("signal monitor: starting")
    import history
    tray.run_tray(user, password, log, LOG_PATH, _tray_menu(),
                  history.path_for(LOG_DIR))
    log.info("signal monitor: exited")


def _report_geometry(width=1240):
    """(width, height, left, top) for the report window: the full height of
    the work area (the screen minus the taskbar), centred horizontally."""
    rect = wintypes.RECT()
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        ok = ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
    except Exception:
        ok = False
    if not ok:
        return width, 1000, 40, 0
    wa_w, wa_h = rect.right - rect.left, rect.bottom - rect.top
    width = min(width, wa_w)
    return width, wa_h, rect.left + max(0, (wa_w - width) // 2), rect.top


def network_report(days=7):
    """Open what the link has been doing as one page, in an app-mode window.

    Reads only the local CSV. It never touches the router, so it needs no
    session and no router claim, and it works perfectly well while the line is
    down, which is exactly when someone would want to look at it.
    """
    import history
    rows   = history.load(history.path_for(LOG_DIR), days)
    speeds = history.load_speed(history.speed_path_for(LOG_DIR), days)
    out    = os.path.join(LOG_DIR, "signal-history.html")
    try:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(history.render(rows, days, speeds))
    except OSError as exc:
        _alert(f"Could not write the history page:\n\n{exc}")
        return
    log.info("network report: %d samples, %d speed checks, over %d days",
             len(rows), len(speeds), days)

    # One window, however many times it is asked for. A second copy of a page
    # built from a local file is never a second thing to look at, and the file
    # was just rewritten above, so the window already on screen, raised and
    # re-read, is the page that was asked for. The page reloads itself when it
    # comes back to the front, which is what makes the raise show tonight's
    # data rather than whenever the window was first opened.
    open_already = _find_window(history.PAGE_TITLE)
    if open_already:
        _raise_window(open_already)
        log.info("network report: already open; raised that window")
        return

    # Opened detached, and then this process is done. The window clock exists
    # for windows pointed at the router: a driven flow still going six minutes
    # in is stuck, and a leftover login page should not sit in the taskbar. A
    # week of your own history is neither: it is a page to read for as long as
    # reading it takes, and closing it mid-sentence would be the bug.
    #
    # Staying behind to watch it would also mean _WindowClock.hold(), which
    # asks chromedriver whether the window still exists once a second, around
    # 360 round trips per window, to supervise a local file that needs no
    # supervision. Detaching removes the clock, the loop and the driver
    # process together.
    # As tall as the screen allows, so seven day strips, the hour profile and
    # the speed checks are all in the frame at once: a report you have to
    # scroll is one you read half of.
    width, height, left, top = _report_geometry()
    url = "file:///" + out.replace("\\", "/")
    _browser().open_window(url, width, height, left, top)
    log.info("network report: window opened; leaving it to the reader")


# ── entry point ───────────────────────────────────────────────────────────────

def _browser():
    """The Chrome-driving tasks, loaded on first use; see the module docstring."""
    import browser
    return browser


def _task_alert(text):
    """How a browser task reports its final failure: with the log beside it."""
    _alert(f"{text}\n\nLog: {LOG_PATH}")


def speed_check():
    import history
    _browser().speed_check(history.speed_path_for(LOG_DIR))


def _tray_menu():
    """The operations the tray icon offers, as groups separated by a rule.

    Derived from DEVICES/UTILITIES rather than restated, so the tray icon's
    menu, the Explorer context menu and the taskbar Jump List are three
    renderings of one list. Add a device above and it appears in all three;
    restating it here is how they would quietly drift apart instead.

    Flat with separators, the way the Jump List already groups them; the task
    labels carry their own device name, which is what they were renamed for.
    """
    groups = [[("Open Router Portal", None)]]
    for _dev_label, _reg_key, tasks in DEVICES:
        groups.append([(label, arg) for label, arg, _sep in tasks])
    # Everything except the monitor itself, which is what is showing the menu.
    utilities = [(label, arg) for label, arg in UTILITIES if arg != "--tray"]
    if utilities:
        groups.append(utilities)
    return groups


def _task_label(arg):
    """Human name for an arg, taken from the menus the user actually clicked."""
    for _dev, _key, tasks in DEVICES:
        for label, task_arg, _sep in tasks:
            if task_arg == arg:
                return label
    for label, task_arg in UTILITIES:
        if task_arg == arg:
            return label
    return "Open Router"


def main():
    global _GUARD

    dispatch = {
        "--reboot-huawei": lambda: _browser().reboot_huawei(_task_alert),
        "--guest-on":      lambda: _browser().guest_mode(True, _task_alert),
        "--guest-off":     lambda: _browser().guest_mode(False, _task_alert),
        "--reboot-tplink": lambda: _browser().reboot_tplink(_task_alert),
        "--speed-check":   speed_check,
        "--tray":          signal_monitor,
        "--history":       network_report,
    }
    arg = next((a for a in dispatch if a in sys.argv), None)

    # Guarded per task, not app-wide: a stray second click on Reboot Huawei is
    # noise, but Speed Check while a reboot runs is a real second intention.
    guard = _InstanceGuard(arg.lstrip("-") if arg else "open-router",
                           _task_label(arg))
    if not guard.claim():
        return
    _GUARD = guard
    try:
        register_context_menu()
        register_jump_list()
        if arg:
            dispatch[arg]()
        else:
            _browser().open_router(_task_alert)
    finally:
        guard.release()


if __name__ == "__main__":
    main()
