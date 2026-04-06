import ctypes
import os
import sys
import time
import traceback
import winreg
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains

load_dotenv()

LOG = os.path.join(os.path.expanduser("~"), "RouterOps.log")


def log(msg):
    with open(LOG, "a") as f:
        f.write(msg + "\n")


def safe_quit(driver):
    try:
        driver.quit()
    except Exception:
        pass


def hidden_driver():
    """Headful Chrome in app mode — no address bar/tabs, minimal footprint."""
    options = webdriver.ChromeOptions()
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--app=http://192.168.1.1/login.cgi")
    options.add_argument("--window-size=1248,768")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    return webdriver.Chrome(options=options)


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


def register_context_menu():
    """Register a grouped cascading right-click menu on RouterOps.exe (HKCU)."""
    if not getattr(sys, "frozen", False):
        return
    exe = sys.executable
    HKCU = winreg.HKEY_CURRENT_USER
    exefile_shell = r"Software\Classes\exefile\shell"

    # Remove old flat entries from previous versions
    for old in ["RouterOpsReboot", "RouterOpsEnableTV", "RouterOpsDisableTV",
                "RouterOpsEnableGujjar", "RouterOpsDisableGujjar"]:
        _reg_delete_tree(HKCU, f"{exefile_shell}\\{old}")

    # Cascading menu: device name as top-level label
    device = f"{exefile_shell}\\RouterOpsDevice"
    shell  = f"{device}\\shell"

    # (verb, label, arg, separator_before)
    items = [
        ("1_reboot",        "Reboot Router",        "--reboot",         False),
        ("2_enabletv",      "Enable Dera TV PCP",   "--enable-tv",      True),
        ("3_disabletv",     "Disable Dera TV PCP",  "--disable-tv",     False),
        ("4_enablegujjar",  "Enable Gujjar WiFi",   "--enable-gujjar",  True),
        ("5_disablegujjar", "Disable Gujjar WiFi",  "--disable-gujjar", False),
    ]

    try:
        # Device-specific cascading submenu
        with winreg.CreateKey(HKCU, device) as k:
            winreg.SetValueEx(k, "MUIVerb",     0, winreg.REG_SZ,   "Huawei LTE CPE B2368-66")
            winreg.SetValueEx(k, "SubCommands", 0, winreg.REG_SZ,   "")
            winreg.SetValueEx(k, "AppliesTo",   0, winreg.REG_SZ,   'System.FileName:="RouterOps.exe"')

        for verb, label, arg, sep_before in items:
            with winreg.CreateKey(HKCU, f"{shell}\\{verb}") as k:
                winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, label)
                if sep_before:
                    winreg.SetValueEx(k, "CommandFlags", 0, winreg.REG_DWORD, 0x20)
            with winreg.CreateKey(HKCU, f"{shell}\\{verb}\\command") as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{exe}" {arg}')

        # Flat item outside the device submenu
        speed = f"{exefile_shell}\\RouterOpsSpeedCheck"
        with winreg.CreateKey(HKCU, speed) as k:
            winreg.SetValueEx(k, "MUIVerb",   0, winreg.REG_SZ, "Speed Check")
            winreg.SetValueEx(k, "AppliesTo", 0, winreg.REG_SZ, 'System.FileName:="RouterOps.exe"')
        with winreg.CreateKey(HKCU, speed + r"\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{exe}" --speed-check')
    except Exception:
        pass


AUMID = "TechNerdXp.RouterOps"


def _stamp_pinned_shortcut():
    """Set our AUMID on the pinned taskbar shortcut so Jump List tasks show up."""
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
            pf.Load(lnk, 0)
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
    """Register taskbar Jump List tasks (right-click on pinned exe)."""
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(AUMID)

    if not getattr(sys, "frozen", False):
        return

    exe = sys.executable

    from win32com.shell import shell
    from win32com.propsys import propsys, pscon
    import pythoncom

    _stamp_pinned_shortcut()

    tasks = [
        ("Reboot Router",       "--reboot"),
        ("Enable Dera TV PCP",  "--enable-tv"),
        ("Disable Dera TV PCP", "--disable-tv"),
        ("Enable Gujjar WiFi",  "--enable-gujjar"),
        ("Disable Gujjar WiFi", "--disable-gujjar"),
        ("Speed Check",         "--speed-check"),
    ]

    try:
        cdl = pythoncom.CoCreateInstance(
            shell.CLSID_DestinationList, None,
            pythoncom.CLSCTX_INPROC_SERVER,
            shell.IID_ICustomDestinationList,
        )
        cdl.SetAppID(AUMID)
        cdl.BeginList()

        coll = pythoncom.CoCreateInstance(
            shell.CLSID_EnumerableObjectCollection, None,
            pythoncom.CLSCTX_INPROC_SERVER,
            shell.IID_IObjectCollection,
        )

        for title, arg in tasks:
            link = pythoncom.CoCreateInstance(
                shell.CLSID_ShellLink, None,
                pythoncom.CLSCTX_INPROC_SERVER,
                shell.IID_IShellLink,
            )
            link.SetPath(exe)
            link.SetArguments(arg)
            link.SetIconLocation(exe, 0)

            store = link.QueryInterface(propsys.IID_IPropertyStore)
            store.SetValue(pscon.PKEY_Title, propsys.PROPVARIANTType(title))
            store.Commit()

            coll.AddObject(link)

        cdl.AddUserTasks(coll)
        cdl.CommitList()
    except Exception:
        pass


def _login(driver, wait):
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
    options = webdriver.ChromeOptions()
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--app=http://192.168.1.1/login.cgi")
    options.add_argument("--window-size=1248,768")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("detach", True)
    driver = webdriver.Chrome(options=options)
    try:
        _login(driver, WebDriverWait(driver, 10))
    finally:
        driver.service.stop()


def reboot_router():
    driver = hidden_driver()
    try:
        wait = WebDriverWait(driver, 10)
        _login(driver, wait)
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


def toggle_dera_tv_pcp(enable: bool):
    driver = hidden_driver()
    try:
        wait = WebDriverWait(driver, 15)
        log(f"toggle_dera_tv_pcp: logging in, enable={enable}")
        _login(driver, wait)

        log("toggle_dera_tv_pcp: hovering Security menu")
        sec = wait.until(EC.presence_of_element_located((By.ID, "Sec")))
        ActionChains(driver).move_to_element(sec).perform()

        log("toggle_dera_tv_pcp: clicking Parental Control")
        wait.until(EC.element_to_be_clickable((By.ID, "Sec-ParentalControl"))).click()

        log("toggle_dera_tv_pcp: switching to mainFrame")
        wait.until(EC.frame_to_be_available_and_switch_to_it("mainFrame"))

        log("toggle_dera_tv_pcp: clicking editBtn1")
        wait.until(EC.element_to_be_clickable((By.ID, "editBtn1"))).click()

        # doEdit() opens a zyUiDialog in window.parent, not inside mainFrame
        driver.switch_to.default_content()

        log("toggle_dera_tv_pcp: finding enableck checkbox")
        enable_cb = wait.until(EC.presence_of_element_located((By.ID, "enableck")))
        log(f"toggle_dera_tv_pcp: checkbox selected={enable_cb.is_selected()}")
        if enable_cb.is_selected() != enable:
            enable_cb.click()

        # Apply is in the zyUiDialog footer, outside #parentalControl_add
        log("toggle_dera_tv_pcp: clicking Apply")
        wait.until(EC.element_to_be_clickable(
            (By.XPATH, '//button[normalize-space()="Apply"]')
        )).click()
        log("toggle_dera_tv_pcp: done")
        time.sleep(1.5)
    except Exception:
        log(f"toggle_dera_tv_pcp ERROR:\n{traceback.format_exc()}")
    finally:
        safe_quit(driver)


def toggle_gujjar_wifi(enable: bool):
    driver = hidden_driver()
    try:
        wait = WebDriverWait(driver, 15)
        log(f"toggle_gujjar_wifi: logging in, enable={enable}")
        _login(driver, wait)

        # Network Setting > Wireless
        log("toggle_gujjar_wifi: navigating to Wireless")
        net = wait.until(EC.presence_of_element_located((By.ID, "Net")))
        ActionChains(driver).move_to_element(net).perform()
        wait.until(EC.element_to_be_clickable((By.ID, "Net-WLAN"))).click()
        wait.until(EC.frame_to_be_available_and_switch_to_it("mainFrame"))

        # More AP tab
        log("toggle_gujjar_wifi: clicking More AP tab")
        wait.until(EC.element_to_be_clickable((By.ID, "t1"))).click()
        time.sleep(1)

        # Edit Gujjar (value="2")
        log("toggle_gujjar_wifi: clicking Gujjar edit")
        wait.until(EC.element_to_be_clickable(
            (By.XPATH, '//a[@class="edit"][@value="2"]')
        )).click()

        # Dialog opens in parent window
        driver.switch_to.default_content()

        log("toggle_gujjar_wifi: finding wlanEnable checkbox")
        cb = wait.until(EC.presence_of_element_located((By.ID, "wlanEnable")))
        log(f"toggle_gujjar_wifi: checkbox selected={cb.is_selected()}")
        if cb.is_selected() != enable:
            cb.click()

        log("toggle_gujjar_wifi: clicking Apply")
        wait.until(EC.element_to_be_clickable(
            (By.XPATH, '//button[normalize-space()="Apply"]')
        )).click()
        log("toggle_gujjar_wifi: done")
        time.sleep(1.5)
    except Exception:
        log(f"toggle_gujjar_wifi ERROR:\n{traceback.format_exc()}")
    finally:
        safe_quit(driver)


def speed_check():
    options = webdriver.ChromeOptions()
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--app=https://fast.com")
    options.add_argument("--window-size=1200,700")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("detach", True)
    driver = webdriver.Chrome(options=options)
    driver.service.stop()


def main():
    register_context_menu()
    register_jump_list()
    if "--reboot" in sys.argv:
        reboot_router()
    elif "--enable-tv" in sys.argv:
        toggle_dera_tv_pcp(True)
    elif "--disable-tv" in sys.argv:
        toggle_dera_tv_pcp(False)
    elif "--enable-gujjar" in sys.argv:
        toggle_gujjar_wifi(True)
    elif "--disable-gujjar" in sys.argv:
        toggle_gujjar_wifi(False)
    elif "--speed-check" in sys.argv:
        speed_check()
    else:
        open_router()


if __name__ == "__main__":
    main()

# pyinstaller --name RouterOps main.py --noconsole --onefile --clean
