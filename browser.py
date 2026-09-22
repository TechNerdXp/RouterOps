"""Everything RouterOps does by driving Chrome: the router's own web UI, the
TP-Link's, fast.com, and the app-mode window the Network Report opens in.

Apart from main.py so that Selenium is loaded only by the processes that drive
a browser. The tray is the one process that stays up for weeks and it drives
none; with Selenium imported at the top of main.py it carried ~9 MB of it all
the same. main.py imports this module inside the tasks that need it.

Nothing here shows a dialog of its own. The router tasks take an `alert`
callable from main.py for their final failure, because main.py owns the
single-instance claim that has to be dropped before a dialog blocks.
"""

import contextlib
import ctypes
import glob
import logging
import os
import threading
import time
from ctypes import wintypes

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains

import history
import lte

log = logging.getLogger("routerops")   # configured by main.py

ROUTER_URL = "http://%s/login.cgi" % lte.HOST

# Last step announced via _mark(); failure messages quote it so the log tells
# exactly where a flow died.
_CURRENT_STEP = "starting"


def _mark(step):
    global _CURRENT_STEP
    _CURRENT_STEP = step
    log.info(step)


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
        log.info("window reached the %d-minute limit; closing it", self._ttl // 60)
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
    cache = os.path.join(os.path.expanduser("~"), ".cache", "selenium", "chromedriver")
    hits = glob.glob(os.path.join(cache, "**", "chromedriver.exe"), recursive=True)
    return max(hits, key=os.path.getmtime) if hits else None


def _driver(url, size="1248,768", position=None, detach=False):
    """Chrome in app mode on `url`. Every window RouterOps opens comes from here."""
    opts = _chrome_options(url)
    opts.add_argument(f"--window-size={size}")
    if position:
        opts.add_argument(f"--window-position={position}")
    if detach:
        opts.add_experimental_option("detach", True)
    cd = _find_chromedriver()
    return webdriver.Chrome(options=opts, service=Service(cd) if cd else None)


def open_window(url, width, height, left, top):
    """Open a page in its own app-mode window and leave it with the user.

    Detached, so the window outlives this process with no driver and no clock
    watching it. That is only right for local pages; see main.network_report
    for why, and _WindowClock for what router windows get instead.
    """
    _driver(url, size="%d,%d" % (width, height),
            position="%d,%d" % (left, top), detach=True)


# ── firmware workaround ───────────────────────────────────────────────────────

# mini_httpd on the B2368-66 omits the Content-Type header on some CGI
# responses (indexMain.cgi among them) while also sending
# X-Content-Type-Options: nosniff, so Chrome renders the dashboard HTML as
# plain text and no DOM ever exists. The markup itself arrives intact;
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


# ── claiming the router ───────────────────────────────────────────────────────
# The B2368-66 keeps exactly one admin session: a second login silently evicts
# the first. Once the tray monitor is resident it is holding that session most
# of the time, so every flow here that logs in has to say so.
#
# The rule is settled and one-sided: getting into the router wins, the readout
# yields. So this is not a negotiation: the claim waits a moment for an in-flight
# sample to finish (one is ~0.4 s and the monitor only holds the mutex while it
# runs), then proceeds regardless. Holding it is what matters, because that is
# what stops the monitor logging back in and evicting a reboot halfway through.
#
# A mutex rather than a flag or a lock file, for the reason main._InstanceGuard
# gives: Windows releases it when the owning process dies, however it dies.

_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
_KERNEL32.CreateMutexW.restype  = wintypes.HANDLE
_KERNEL32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
_KERNEL32.CloseHandle.argtypes  = [wintypes.HANDLE]
_KERNEL32.ReleaseMutex.argtypes = [wintypes.HANDLE]
_KERNEL32.WaitForSingleObject.restype  = wintypes.DWORD
_KERNEL32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]


class _RouterClaim:
    """Hold the router for the duration of a flow that logs into it."""

    def __init__(self, wait_seconds=5):
        self._wait = wait_seconds
        self._handle = None
        self._owned = False

    def __enter__(self):
        try:
            self._handle = _KERNEL32.CreateMutexW(None, False, lte.ROUTER_MUTEX)
            if self._handle:
                self._owned = _KERNEL32.WaitForSingleObject(
                    self._handle, self._wait * 1000) == 0  # WAIT_OBJECT_0
                if not self._owned:
                    log.info("router claim timed out; proceeding anyway")
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
        return ("router", f"Chrome shows a network error page at {url}: "
                          "router unreachable or its web server is down")
    if plain:
        return ("router", f"page at {url} came without Content-Type and could not "
                          "be recovered. Firmware acting up; power-cycle the router")
    if isinstance(exc, TimeoutException):
        if "login.cgi" in url:
            return ("router", "login not accepted, still on the login page "
                              "(wrong credentials, or the router refused the session)")
        return ("transient", f"timed out waiting at {url}; router slow or its UI changed")
    if isinstance(exc, (StaleElementReferenceException,
                        ElementClickInterceptedException,
                        ElementNotInteractableException)):
        return ("transient", f"{exc.__class__.__name__} at {url}: "
                             "page re-rendered mid-action")
    if isinstance(exc, NoSuchElementException):
        return ("script", f"element not found at {url}: selector/firmware mismatch")
    return ("script", f"{exc.__class__.__name__}: {exc}")


def _run_task(name, url, body, alert, attempts=2, timeout=25, claims_router=True):
    """Run body(driver, wait) with a fresh browser + session per attempt,
    retrying once; classify the final failure and hand it to `alert`.

    The router claim spans every attempt rather than each one: releasing it
    between tries would let the monitor log back in and evict the retry.
    """
    with _RouterClaim() if claims_router else contextlib.nullcontext():
        _run_attempts(name, url, body, alert, attempts, timeout)


def _run_attempts(name, url, body, alert, attempts, timeout):
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
                alert(f"{name} failed at: {_CURRENT_STEP}\n\n[{kind}] {detail}")
        finally:
            clock.stop()
            safe_quit(driver)
        time.sleep(2)  # let mini_httpd settle before the retry


# ── Huawei LTE CPE B2368-66 ───────────────────────────────────────────────────

def _huawei_login(driver, wait):
    _mark("huawei: loading login page")
    driver.get(ROUTER_URL)
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


def open_router(alert):
    # The claim covers the whole life of the window, not just the login. While
    # someone is reading the router's own pages the session is theirs, and a
    # monitor sample that logged back in would throw them out of the portal they
    # are standing in, the one thing this app must never do.
    with _RouterClaim():
        driver = _driver(ROUTER_URL)
        # deliberately not detached any more: this window outlives its task by
        # design, so something has to stay behind to close it at the limit
        clock = _WindowClock(driver)
        try:
            _huawei_login(driver, WebDriverWait(driver, 25))
            log.info("open-router: logged in")
        except Exception as exc:
            kind, detail = _classify(driver, exc)
            log.warning("open-router: failed at '%s' [%s] %s", _CURRENT_STEP, kind, detail)
            # the window stays up so the state is inspectable, on the same clock
            alert(f"Open router failed at: {_CURRENT_STEP}\n\n[{kind}] {detail}")
        clock.hold()
        clock.stop()
        safe_quit(driver)


def reboot_huawei(alert):
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

    _run_task("Reboot Huawei", ROUTER_URL, body, alert)


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


def guest_mode(enable: bool, alert):
    def body(driver, wait):
        _huawei_login(driver, wait)
        # both toggles set an absolute state, so a retry that redoes the
        # first toggle is harmless
        _do_toggle_gujjar_wifi(driver, wait, enable)
        _do_toggle_dera_tv_pcp(driver, wait, not enable)

    _run_task("Guest Mode On" if enable else "Guest Mode Off",
              ROUTER_URL, body, alert)


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


def reboot_tplink(alert):
    def body(driver, wait):
        _tplink_login(driver, wait)
        _mark("tplink: opening reboot page")
        driver.get("http://tplinkwifi.net/#reboot")
        wait.until(EC.element_to_be_clickable((By.ID, "reboot-button"))).click()
        _mark("tplink: confirming reboot")
        wait.until(EC.element_to_be_clickable((By.ID, "reboot-confirm-msg-btn-ok"))).click()
        time.sleep(1.5)

    _run_task("Reboot TP-Link", "http://tplinkwifi.net", body, alert, timeout=15,
              claims_router=False)


# ── speed check ───────────────────────────────────────────────────────────────

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


def speed_check(speed_csv):
    # fast.com settles in well under a minute; the rest of the window's life
    # belongs to the user, up to the shared limit
    driver = _driver("https://fast.com", size="1200,700")
    clock = _WindowClock(driver)
    try:
        # Keep the figure. One speed test answers "is it slow right now"; a
        # run of them answers "is this line worth what we pay for it", and
        # that is the question that comes up after a bad week.
        try:
            mbps = _read_fast_com(driver)
            history.record_speed(speed_csv, mbps)
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
