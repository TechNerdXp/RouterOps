import contextlib
import ctypes
import logging
import os
import sys
import threading
import time
import winreg
from ctypes import wintypes
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains

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

# Last step announced via _mark(); failure messages quote it so the log tells
# exactly where a flow died.
_CURRENT_STEP = "starting"


def _mark(step):
    global _CURRENT_STEP
    _CURRENT_STEP = step
    log.info(step)

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

# standalone — not tied to any single device
UTILITIES = [
    ("Signal Monitor", "--tray"),
    ("Signal History", "--history"),
    ("Speed Check",    "--speed-check"),
]

AUMID = "TechNerdXp.RouterOps"
HKCU  = winreg.HKEY_CURRENT_USER


# ── helpers ───────────────────────────────────────────────────────────────────

def safe_quit(driver):
    try:
        driver.quit()
    except Exception:
        pass


# ── window lifetime ───────────────────────────────────────────────────────────
# Every window RouterOps opens is on the same clock. None of them is meant to be
# watched to the end: a speed test settles in under a minute, the router page is
# opened to check one setting, and a driven flow still going six minutes in is
# stuck, not busy. Left to themselves the leftovers stack up in the taskbar until
# the next click adds another, so the app closes its own windows.

WINDOW_TTL = 6 * 60  # seconds any one RouterOps window may stay open


def _window_gone(driver):
    """True once the user has closed the window out from under us."""
    try:
        return not driver.window_handles
    except WebDriverException:
        return True


class _WindowClock:
    """The six-minute cap on one browser window, enforced from a side thread.

    The clock starts when the window does and runs on its own, so the limit
    holds whether the main thread is driving the page, blocked on an error
    message box, or just waiting for the user to finish reading. When it fires
    it quits the driver, which fails whatever command is in flight and drops a
    driven flow into its normal error path.
    """

    def __init__(self, driver, ttl=WINDOW_TTL):
        self._driver   = driver
        self._ttl      = ttl
        self._finished = threading.Event()
        self.expired   = False
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        if self._finished.wait(self._ttl):
            return  # the task closed the window first
        self.expired = True
        log.info("window reached the %d-minute limit — closing it", self._ttl // 60)
        safe_quit(self._driver)

    def hold(self):
        """Block while the window is the user's: until they close it, or time runs out."""
        while not self.expired and not _window_gone(self._driver):
            time.sleep(1)
        if not self.expired:
            log.info("window closed by hand")

    def stop(self):
        self._finished.set()


def _chrome_options(app_url=None):
    opts = webdriver.ChromeOptions()
    if app_url:
        opts.add_argument(f"--app={app_url}")
    opts.add_argument("--ignore-certificate-errors")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-sync")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-client-side-phishing-detection")
    opts.add_argument("--disable-default-apps")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    return opts


def _find_chromedriver():
    """Return the most-recently-used cached ChromeDriver, bypassing Selenium Manager."""
    import glob
    cache = os.path.join(os.path.expanduser("~"), ".cache", "selenium", "chromedriver")
    hits = glob.glob(os.path.join(cache, "**", "chromedriver.exe"), recursive=True)
    return max(hits, key=os.path.getmtime) if hits else None


def _driver(url, size="1248,768"):
    from selenium.webdriver.chrome.service import Service
    opts = _chrome_options(url)
    opts.add_argument(f"--window-size={size}")
    cd = _find_chromedriver()
    return webdriver.Chrome(options=opts, service=Service(cd) if cd else None)


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
            # rest and the old keys are left behind as duplicate menu entries —
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

        # Standalone utilities — flat entries outside device submenus
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


# ── failure handling + firmware workaround ───────────────────────────────────

# mini_httpd on the B2368-66 omits the Content-Type header on some CGI
# responses (indexMain.cgi among them) while also sending
# X-Content-Type-Options: nosniff, so Chrome renders the dashboard HTML as
# plain text and no DOM ever exists. The markup itself arrives intact —
# re-injecting it via document.write on the same origin yields a fully
# working page, since scripts/frames are static files served with correct
# types. Confirmed live against the router on 2026-08-03.
_REWRITE_JS = """
if (document.contentType === 'text/plain') {
    const raw = document.body.firstChild ? document.body.firstChild.textContent
                                         : document.body.textContent;
    if (raw && raw.trimStart().startsWith('<')) {
        document.open();
        document.write(raw);
        document.close();
        return true;
    }
}
return false;
"""


def _ensure_html(driver, wait):
    """Recover a page the router served without Content-Type (no-op otherwise)."""
    wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
    if driver.execute_script(_REWRITE_JS):
        log.info("recovered Content-Type-less page (router firmware bug)")
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")


def _enter_main_frame(driver, wait):
    wait.until(EC.frame_to_be_available_and_switch_to_it("mainFrame"))
    _ensure_html(driver, wait)


def _alert(text):
    # Every _alert is terminal for this process, but the dialog blocks until
    # dismissed — drop the single-instance claim first so an unattended error
    # box on screen never keeps the next launch out.
    if _GUARD is not None:
        _GUARD.release()
    try:  # error icon, topmost, foreground — the exe has no console
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
# A duplicate launch just disappears — no dialog, no second window, only a log
# line. Nothing is wrong from the user's side: the task they asked for is
# already on its way, so the extra click should cost them nothing, not even an
# OK button.
#
# The claim is a *named kernel object*, deliberately not a lock file: Windows
# destroys it the moment the last handle closes — normal exit, crash, or Task
# Manager kill alike — so there is no stale lock that can ever wedge the app
# shut, and nothing to clean up by hand.

_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
_KERNEL32.CreateMutexW.restype  = wintypes.HANDLE
_KERNEL32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
_KERNEL32.CloseHandle.argtypes  = [wintypes.HANDLE]
_KERNEL32.ReleaseMutex.argtypes = [wintypes.HANDLE]
_KERNEL32.WaitForSingleObject.restype  = wintypes.DWORD
_KERNEL32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

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


# ── claiming the router ───────────────────────────────────────────────────────
# The B2368-66 keeps exactly one admin session: a second login silently evicts
# the first. Once the tray monitor is resident it is holding that session most
# of the time, so every flow here that logs in has to say so.
#
# The rule is settled and one-sided — getting into the router wins, the readout
# yields. So this is not a negotiation: the claim waits a moment for an in-flight
# sample to finish (one is ~0.4 s and the monitor only holds the mutex while it
# runs), then proceeds regardless. Holding it is what matters, because that is
# what stops the monitor logging back in and evicting a reboot halfway through.
#
# A mutex rather than a flag or a lock file, for the reason _InstanceGuard gives:
# Windows releases it when the owning process dies, however it dies.

_ROUTER_MUTEX = "Local\\RouterOps.Router"


class _RouterClaim:
    """Hold the router for the duration of a flow that logs into it."""

    def __init__(self, wait_seconds=5):
        self._wait = wait_seconds
        self._handle = None
        self._owned = False

    def __enter__(self):
        try:
            self._handle = _KERNEL32.CreateMutexW(None, False, _ROUTER_MUTEX)
            if self._handle:
                self._owned = _KERNEL32.WaitForSingleObject(
                    self._handle, self._wait * 1000) == 0  # WAIT_OBJECT_0
                if not self._owned:
                    log.info("router claim timed out — proceeding anyway")
        except Exception:  # the claim must never be why a task cannot run
            pass
        return self

    def __exit__(self, *_exc):
        try:
            if self._owned:
                _KERNEL32.ReleaseMutex(self._handle)
            if self._handle:
                _KERNEL32.CloseHandle(self._handle)
        except Exception:
            pass
        self._handle, self._owned = None, False
        return False


def _classify(driver, exc):
    """Best-effort split: router problem vs transient hiccup vs script problem."""
    try:
        url = driver.current_url
        net_error = driver.find_elements(By.ID, "main-frame-error")
        plain = driver.execute_script("return document.contentType") == "text/plain"
    except WebDriverException:
        return ("script", f"browser/driver died mid-task ({exc.__class__.__name__})")
    if net_error:
        return ("router", f"Chrome shows a network error page at {url} — "
                          "router unreachable or its web server is down")
    if plain:
        return ("router", f"page at {url} came without Content-Type and could not "
                          "be recovered — firmware acting up; power-cycle the router")
    if isinstance(exc, TimeoutException):
        if "login.cgi" in url:
            return ("router", "login not accepted — still on the login page "
                              "(wrong credentials, or the router refused the session)")
        return ("transient", f"timed out waiting at {url} — router slow or its UI changed")
    if isinstance(exc, (StaleElementReferenceException,
                        ElementClickInterceptedException,
                        ElementNotInteractableException)):
        return ("transient", f"{exc.__class__.__name__} at {url} — "
                             "page re-rendered mid-action")
    if isinstance(exc, NoSuchElementException):
        return ("script", f"element not found at {url} — selector/firmware mismatch")
    return ("script", f"{exc.__class__.__name__}: {exc}")


def _run_task(name, url, body, attempts=2, timeout=25, claims_router=True):
    """Run body(driver, wait) with a fresh browser + session per attempt,
    retrying once; classify and surface the final failure.

    The router claim spans every attempt rather than each one: releasing it
    between tries would let the monitor log back in and evict the retry.
    """
    with _RouterClaim() if claims_router else contextlib.nullcontext():
        _run_attempts(name, url, body, attempts, timeout)


def _run_attempts(name, url, body, attempts, timeout):
    for attempt in range(1, attempts + 1):
        log.info("%s: attempt %d/%d", name, attempt, attempts)
        driver = _driver(url)
        clock = _WindowClock(driver)
        try:
            body(driver, WebDriverWait(driver, timeout))
            log.info("%s: done", name)
            return
        except Exception as exc:
            if clock.expired:
                # the window was pulled out from under the flow, so whatever
                # selenium raised on the way down says nothing about the router
                kind, detail = ("script", f"ran past the {WINDOW_TTL // 60}-minute "
                                          "window limit and was cut off")
            else:
                kind, detail = _classify(driver, exc)
            log.warning("%s: attempt %d/%d failed at '%s' [%s] %s",
                        name, attempt, attempts, _CURRENT_STEP, kind, detail)
            if attempt == attempts:
                log.error("%s: giving up", name)
                _alert(f"{name} failed at: {_CURRENT_STEP}\n\n[{kind}] {detail}\n\n"
                       f"Log: {LOG_PATH}")
        finally:
            clock.stop()
            safe_quit(driver)
        time.sleep(2)  # let mini_httpd settle before the retry


# ── Huawei LTE CPE B2368-66 ───────────────────────────────────────────────────

def _huawei_login(driver, wait):
    _mark("huawei: loading login page")
    driver.get("http://192.168.1.1/login.cgi")
    _mark("huawei: filling login form")
    wait.until(EC.visibility_of_element_located((By.ID, "username"))).send_keys(
        os.getenv("ROUTER_USERNAME")
    )
    wait.until(EC.visibility_of_element_located((By.ID, "userpassword"))).send_keys(
        os.getenv("ROUTER_PASSWORD")
    )
    _mark("huawei: submitting login (router takes ~15s to answer)")
    wait.until(EC.element_to_be_clickable((By.XPATH, "//input[@value='Login']"))).click()
    # the router redirects http->https on load, so url_changes() against the
    # http URL would pass before login even happens; wait to leave login.cgi
    wait.until(lambda d: "login.cgi" not in d.current_url)
    _mark("huawei: waiting for dashboard")
    _ensure_html(driver, wait)
    wait.until(EC.presence_of_element_located((By.ID, "MT")))


def open_router():
    from selenium.webdriver.chrome.service import Service
    # The claim covers the whole life of the window, not just the login. While
    # someone is reading the router's own pages the session is theirs, and a
    # monitor sample that logged back in would throw them out of the portal they
    # are standing in — the one thing this app must never do.
    with _RouterClaim():
        opts = _chrome_options("http://192.168.1.1/login.cgi")
        opts.add_argument("--window-size=1248,768")
        cd = _find_chromedriver()
        driver = webdriver.Chrome(options=opts, service=Service(cd) if cd else None)
        # deliberately not detached any more: this window outlives its task by
        # design, so something has to stay behind to close it at the limit
        clock = _WindowClock(driver)
        try:
            _huawei_login(driver, WebDriverWait(driver, 25))
            log.info("open-router: logged in")
        except Exception as exc:
            kind, detail = _classify(driver, exc)
            log.warning("open-router: failed at '%s' [%s] %s", _CURRENT_STEP, kind, detail)
            # the window stays up so the state is inspectable — on the same clock
            _alert(f"Open router failed at: {_CURRENT_STEP}\n\n[{kind}] {detail}\n\n"
                   f"Log: {LOG_PATH}")
        clock.hold()
        clock.stop()
        safe_quit(driver)


def reboot_huawei():
    def body(driver, wait):
        _huawei_login(driver, wait)
        _mark("huawei: opening maintenance menu")
        ActionChains(driver).move_to_element(
            wait.until(EC.presence_of_element_located((By.ID, "MT")))
        ).perform()
        _mark("huawei: clicking Reboot")
        wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//a[contains(text(),'Reboot')]")
        )).click()
        _mark("huawei: waiting for reboot page")
        _enter_main_frame(driver, wait)
        wait.until(EC.element_to_be_clickable((By.NAME, "sysSubmit"))).click()
        driver.switch_to.default_content()
        _mark("huawei: confirming reboot")
        wait.until(EC.presence_of_element_located((By.XPATH, '//button[text()="OK"]'))).click()
        time.sleep(1.5)

    _run_task("Reboot Huawei", "http://192.168.1.1/login.cgi", body)


def _do_toggle_dera_tv_pcp(driver, wait, enable: bool):
    _mark("huawei: opening parental control")
    sec = wait.until(EC.presence_of_element_located((By.ID, "Sec")))
    ActionChains(driver).move_to_element(sec).perform()
    wait.until(EC.element_to_be_clickable((By.ID, "Sec-ParentalControl"))).click()
    _enter_main_frame(driver, wait)
    wait.until(EC.element_to_be_clickable((By.ID, "editBtn1"))).click()
    driver.switch_to.default_content()
    _mark("huawei: toggling TV parental control")
    cb = wait.until(EC.presence_of_element_located((By.ID, "enableck")))
    if cb.is_selected() != enable:
        cb.click()
    _mark("huawei: applying parental control change")
    wait.until(EC.element_to_be_clickable(
        (By.XPATH, '//button[normalize-space()="Apply"]')
    )).click()
    time.sleep(1.5)


def _do_toggle_gujjar_wifi(driver, wait, enable: bool):
    _mark("huawei: opening WLAN settings")
    net = wait.until(EC.presence_of_element_located((By.ID, "Net")))
    ActionChains(driver).move_to_element(net).perform()
    wait.until(EC.element_to_be_clickable((By.ID, "Net-WLAN"))).click()
    _enter_main_frame(driver, wait)
    wait.until(EC.element_to_be_clickable((By.ID, "t1"))).click()
    time.sleep(1)
    wait.until(EC.element_to_be_clickable(
        (By.XPATH, '//a[@class="edit"][@value="2"]')
    )).click()
    driver.switch_to.default_content()
    _mark("huawei: toggling guest wifi")
    cb = wait.until(EC.presence_of_element_located((By.ID, "wlanEnable")))
    if cb.is_selected() != enable:
        cb.click()
    _mark("huawei: applying wifi change")
    wait.until(EC.element_to_be_clickable(
        (By.XPATH, '//button[normalize-space()="Apply"]')
    )).click()
    time.sleep(1.5)


def guest_mode(enable: bool):
    def body(driver, wait):
        _huawei_login(driver, wait)
        # both toggles set an absolute state, so a retry that redoes the
        # first toggle is harmless
        _do_toggle_gujjar_wifi(driver, wait, enable)
        _do_toggle_dera_tv_pcp(driver, wait, not enable)

    _run_task("Guest Mode On" if enable else "Guest Mode Off",
              "http://192.168.1.1/login.cgi", body)


# ── TP-Link TL-WR844N ─────────────────────────────────────────────────────────

def _tplink_login(driver, wait):
    _mark("tplink: loading login page")
    driver.get("http://tplinkwifi.net")
    _mark("tplink: submitting password")
    wait.until(EC.visibility_of_element_located(
        (By.CSS_SELECTOR, "input[type='password']")
    )).send_keys(os.getenv("ROUTER_PASSWORD"))
    wait.until(EC.element_to_be_clickable((By.ID, "local-login-button"))).click()
    _mark("tplink: waiting for dashboard")
    wait.until(EC.url_contains("#"))
    wait.until(lambda d: d.execute_script("return document.readyState") == "complete")


def reboot_tplink():
    def body(driver, wait):
        _tplink_login(driver, wait)
        _mark("tplink: opening reboot page")
        driver.get("http://tplinkwifi.net/#reboot")
        wait.until(EC.element_to_be_clickable((By.ID, "reboot-button"))).click()
        _mark("tplink: confirming reboot")
        wait.until(EC.element_to_be_clickable((By.ID, "reboot-confirm-msg-btn-ok"))).click()
        time.sleep(1.5)

    _run_task("Reboot TP-Link", "http://tplinkwifi.net", body, timeout=15,
              claims_router=False)


# ── speed check ───────────────────────────────────────────────────────────────

# ── signal monitor ────────────────────────────────────────────────────────────

def signal_monitor():
    """The resident tray readout — RouterOps' only long-lived task.

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


def signal_history(days=7):
    """Open what the link has been doing as one page, in an app-mode window.

    Reads only the local CSV — it never touches the router, so it needs no
    session and no _RouterClaim, and it works perfectly well while the line is
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
    log.info("signal history: %d samples, %d speed checks, over %d days",
             len(rows), len(speeds), days)

    # Opened detached, and then this process is done. The window clock exists
    # for windows pointed at the router — a driven flow still going six minutes
    # in is stuck, and a leftover login page should not sit in the taskbar. A
    # week of your own history is neither: it is a page to read for as long as
    # reading it takes, and closing it mid-sentence would be the bug.
    #
    # Staying behind to watch it would also mean _WindowClock.hold(), which
    # asks chromedriver whether the window still exists once a second — around
    # 360 round trips per window, to supervise a local file that needs no
    # supervision. Detaching removes the clock, the loop and the driver
    # process together.
    opts = _chrome_options("file:///" + out.replace("\\", "/"))
    opts.add_argument("--window-size=1320,880")
    opts.add_experimental_option("detach", True)
    from selenium.webdriver.chrome.service import Service
    cd = _find_chromedriver()
    driver = webdriver.Chrome(options=opts, service=Service(cd) if cd else None)
    log.info("signal history: window opened; leaving it to the reader")


def _read_fast_com(driver, timeout=120):
    """The download figure fast.com settles on, in Mbps. None if it never does.

    Waiting for the 'succeeded' class rather than just reading #speed-value
    matters: until the test finishes that element is a live counter climbing
    towards the real number, so reading it early records a figure from halfway
    up the ramp and quietly libels the connection.
    """
    WebDriverWait(driver, timeout).until(
        lambda d: "succeeded" in (d.find_element(
            By.ID, "speed-progress-indicator").get_attribute("class") or "")
    )
    value = driver.find_element(By.ID, "speed-value").text.strip()
    units = driver.find_element(By.ID, "speed-units").text.strip().lower()
    mbps = float(value)
    if units.startswith("kbps"):
        mbps /= 1000.0
    elif units.startswith("gbps"):
        mbps *= 1000.0
    return mbps


def speed_check():
    # fast.com settles in well under a minute; the rest of the window's life
    # belongs to the user, up to the shared limit
    driver = _driver("https://fast.com", size="1200,700")
    clock = _WindowClock(driver)
    try:
        # Keep the figure. One speed test answers "is it slow right now"; a
        # run of them answers "is this line worth what we pay for it", and
        # that is the question that comes up after a bad week.
        try:
            import history
            mbps = _read_fast_com(driver)
            history.record_speed(history.speed_path_for(LOG_DIR), mbps)
            log.info("speed check: %.1f Mbps (%s)", mbps,
                     "healthy" if mbps >= history.HEALTHY_MBPS else "below par")
        except Exception as exc:
            # Never let bookkeeping spoil the thing the user actually clicked:
            # the window is already open and showing them the answer.
            log.info("speed check: could not read the result (%s)",
                     exc.__class__.__name__)
        clock.hold()
    finally:
        clock.stop()
        safe_quit(driver)


# ── entry point ───────────────────────────────────────────────────────────────

def _tray_menu():
    """The operations the tray icon offers, as groups separated by a rule.

    Derived from DEVICES/UTILITIES rather than restated, so the tray icon's
    menu, the Explorer context menu and the taskbar Jump List are three
    renderings of one list. Add a device above and it appears in all three;
    restating it here is how they would quietly drift apart instead.

    Flat with separators, the way the Jump List already groups them — the task
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
        "--reboot-huawei": reboot_huawei,
        "--guest-on":      lambda: guest_mode(True),
        "--guest-off":     lambda: guest_mode(False),
        "--reboot-tplink": reboot_tplink,
        "--speed-check":   speed_check,
        "--tray":          signal_monitor,
        "--history":       signal_history,
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
        (dispatch[arg] if arg else open_router)()
    finally:
        guard.release()


if __name__ == "__main__":
    main()
