import ctypes
import os
import sys
import time
import winreg
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains

load_dotenv(os.path.join(
    getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))),
    ".env",
))

# ── device + menu definitions ─────────────────────────────────────────────────
# (label, registry_key, [(task_label, cli_arg, separator_before)])
DEVICES = [
    (
        "Huawei LTE CPE B2368-66",
        "RouterOpsHuawei",
        [
            ("Reboot Router",  "--reboot-huawei", False),
            ("Guest Mode On",  "--guest-on",      True),
            ("Guest Mode Off", "--guest-off",     False),
            ("Speed Check",    "--speed-check",   True),
        ],
    ),
    (
        "TP-Link TL-WR844N",
        "RouterOpsTplink",
        [
            ("Reboot Router",  "--reboot-tplink", False),
        ],
    ),
]

AUMID = "TechNerdXp.RouterOps"
HKCU  = winreg.HKEY_CURRENT_USER


# ── helpers ───────────────────────────────────────────────────────────────────

def safe_quit(driver):
    try:
        driver.quit()
    except Exception:
        pass


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
    """Return cached ChromeDriver path to bypass Selenium Manager network check."""
    import glob
    cache = os.path.join(os.path.expanduser("~"), ".cache", "selenium", "chromedriver")
    hits = glob.glob(os.path.join(cache, "**", "chromedriver.exe"), recursive=True)
    return max(hits) if hits else None


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

            with winreg.CreateKey(HKCU, device_path) as k:
                winreg.SetValueEx(k, "MUIVerb",     0, winreg.REG_SZ,   dev_label)
                winreg.SetValueEx(k, "SubCommands", 0, winreg.REG_SZ,   "")
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
            for j, (task_label, arg, _sep) in enumerate(tasks):
                items.append(make_link(task_label, arg))
                # separator between task groups within a device (where sep_before is set)
                # already handled by visual grouping across devices

        cdl.AddUserTasks(make_collection(items))
        cdl.CommitList()
    except Exception:
        pass


# ── Huawei LTE CPE B2368-66 ───────────────────────────────────────────────────

def _huawei_login(driver, wait):
    driver.get("http://192.168.1.1/login.cgi")
    wait.until(EC.visibility_of_element_located((By.ID, "username"))).send_keys(
        os.getenv("ROUTER_USERNAME")
    )
    wait.until(EC.visibility_of_element_located((By.ID, "userpassword"))).send_keys(
        os.getenv("ROUTER_PASSWORD")
    )
    wait.until(EC.element_to_be_clickable((By.XPATH, "//input[@value='Login']"))).click()
    wait.until(EC.url_changes("http://192.168.1.1/login.cgi"))


def open_router():
    from selenium.webdriver.chrome.service import Service
    opts = _chrome_options("http://192.168.1.1/login.cgi")
    opts.add_argument("--window-size=1248,768")
    opts.add_experimental_option("detach", True)
    cd = _find_chromedriver()
    driver = webdriver.Chrome(options=opts, service=Service(cd) if cd else None)
    try:
        _huawei_login(driver, WebDriverWait(driver, 10))
    finally:
        driver.service.stop()


def reboot_huawei():
    driver = _driver("http://192.168.1.1/login.cgi")
    try:
        wait = WebDriverWait(driver, 10)
        _huawei_login(driver, wait)
        ActionChains(driver).move_to_element(
            wait.until(EC.presence_of_element_located((By.ID, "MT")))
        ).perform()
        driver.find_element(By.XPATH, "//a[contains(text(),'Reboot')]").click()
        wait.until(EC.frame_to_be_available_and_switch_to_it("mainFrame"))
        driver.find_element(By.NAME, "sysSubmit").click()
        driver.switch_to.default_content()
        wait.until(EC.presence_of_element_located((By.XPATH, '//button[text()="OK"]'))).click()
        time.sleep(1.5)
    finally:
        safe_quit(driver)


def _do_toggle_dera_tv_pcp(driver, wait, enable: bool):
    sec = wait.until(EC.presence_of_element_located((By.ID, "Sec")))
    ActionChains(driver).move_to_element(sec).perform()
    wait.until(EC.element_to_be_clickable((By.ID, "Sec-ParentalControl"))).click()
    wait.until(EC.frame_to_be_available_and_switch_to_it("mainFrame"))
    wait.until(EC.element_to_be_clickable((By.ID, "editBtn1"))).click()
    driver.switch_to.default_content()
    cb = wait.until(EC.presence_of_element_located((By.ID, "enableck")))
    if cb.is_selected() != enable:
        cb.click()
    wait.until(EC.element_to_be_clickable(
        (By.XPATH, '//button[normalize-space()="Apply"]')
    )).click()
    time.sleep(1.5)


def _do_toggle_gujjar_wifi(driver, wait, enable: bool):
    net = wait.until(EC.presence_of_element_located((By.ID, "Net")))
    ActionChains(driver).move_to_element(net).perform()
    wait.until(EC.element_to_be_clickable((By.ID, "Net-WLAN"))).click()
    wait.until(EC.frame_to_be_available_and_switch_to_it("mainFrame"))
    wait.until(EC.element_to_be_clickable((By.ID, "t1"))).click()
    time.sleep(1)
    wait.until(EC.element_to_be_clickable(
        (By.XPATH, '//a[@class="edit"][@value="2"]')
    )).click()
    driver.switch_to.default_content()
    cb = wait.until(EC.presence_of_element_located((By.ID, "wlanEnable")))
    if cb.is_selected() != enable:
        cb.click()
    wait.until(EC.element_to_be_clickable(
        (By.XPATH, '//button[normalize-space()="Apply"]')
    )).click()
    time.sleep(1.5)


def guest_mode(enable: bool):
    driver = _driver("http://192.168.1.1/login.cgi")
    try:
        wait = WebDriverWait(driver, 15)
        _huawei_login(driver, wait)
        _do_toggle_gujjar_wifi(driver, wait, enable)
        _do_toggle_dera_tv_pcp(driver, wait, not enable)
    except Exception:
        pass
    finally:
        safe_quit(driver)


# ── TP-Link TL-WR844N ─────────────────────────────────────────────────────────

def _tplink_login(driver, wait):
    driver.get("http://tplinkwifi.net")
    wait.until(EC.visibility_of_element_located((By.ID, "password"))).send_keys(
        os.getenv("ROUTER_PASSWORD")
    )
    wait.until(EC.element_to_be_clickable((By.ID, "login-btn"))).click()
    wait.until(EC.url_contains("#"))


def reboot_tplink():
    driver = _driver("http://tplinkwifi.net")
    try:
        wait = WebDriverWait(driver, 15)
        _tplink_login(driver, wait)
        driver.get("http://tplinkwifi.net/#reboot")
        wait.until(EC.element_to_be_clickable(
            (By.XPATH, '//button[normalize-space()="REBOOT"]')
        )).click()
        time.sleep(1.5)
    finally:
        safe_quit(driver)


# ── speed check ───────────────────────────────────────────────────────────────

def speed_check():
    driver = _driver("https://fast.com", size="1200,700")
    try:
        time.sleep(60)
    finally:
        safe_quit(driver)


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    register_context_menu()
    register_jump_list()

    dispatch = {
        "--reboot-huawei": reboot_huawei,
        "--guest-on":      lambda: guest_mode(True),
        "--guest-off":     lambda: guest_mode(False),
        "--reboot-tplink": reboot_tplink,
        "--speed-check":   speed_check,
    }
    for arg, fn in dispatch.items():
        if arg in sys.argv:
            fn()
            return
    open_router()


if __name__ == "__main__":
    main()
