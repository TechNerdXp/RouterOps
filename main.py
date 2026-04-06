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
        ("1_reboot",    "Reboot Router",  "--reboot",    False),
        ("2_gueston",   "Guest Mode On",  "--guest-on",  True),
        ("3_guestoff",  "Guest Mode Off", "--guest-off", False),
        ("4_speedcheck","Speed Check",    "--speed-check",True),
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

        _reg_delete_tree(HKCU, f"{exefile_shell}\\RouterOpsSpeedCheck")
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
            pf.Load(lnk, 2)  # STGM_READWRITE
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
            pythoncom.CLSCTX_INPROC_SERVER,
            shell.IID_IShellLink,
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
            pythoncom.CLSCTX_INPROC_SERVER,
            shell.IID_IShellLink,
        )
        store = link.QueryInterface(propsys.IID_IPropertyStore)
        pk = propsys.PSGetPropertyKeyFromName(
            "System.AppUserModel.IsDestListSeparator"
        )
        store.SetValue(pk, propsys.PROPVARIANTType(True, pythoncom.VT_BOOL))
        store.Commit()
        return link

    def make_collection(items):
        coll = pythoncom.CoCreateInstance(
            shell.CLSID_EnumerableObjectCollection, None,
            pythoncom.CLSCTX_INPROC_SERVER,
            shell.IID_IObjectCollection,
        )
        for item in items:
            coll.AddObject(item)
        return coll

    try:
        cdl = pythoncom.CoCreateInstance(
            shell.CLSID_DestinationList, None,
            pythoncom.CLSCTX_INPROC_SERVER,
            shell.IID_ICustomDestinationList,
        )
        cdl.SetAppID(AUMID)
        try:
            cdl.DeleteList(AUMID)
        except Exception:
            pass
        cdl.BeginList()

        items = [
            make_link("Reboot Router",  "--reboot"),
            make_separator(),
            make_link("Guest Mode On",  "--guest-on"),
            make_link("Guest Mode Off", "--guest-off"),
            make_separator(),
            make_link("Speed Check",    "--speed-check"),
        ]
        cdl.AddUserTasks(make_collection(items))
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


def _do_toggle_dera_tv_pcp(driver, wait, enable: bool):
    sec = wait.until(EC.presence_of_element_located((By.ID, "Sec")))
    ActionChains(driver).move_to_element(sec).perform()
    wait.until(EC.element_to_be_clickable((By.ID, "Sec-ParentalControl"))).click()
    wait.until(EC.frame_to_be_available_and_switch_to_it("mainFrame"))
    wait.until(EC.element_to_be_clickable((By.ID, "editBtn1"))).click()
    driver.switch_to.default_content()
    enable_cb = wait.until(EC.presence_of_element_located((By.ID, "enableck")))
    if enable_cb.is_selected() != enable:
        enable_cb.click()
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



def speed_check():
    options = webdriver.ChromeOptions()
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--app=https://fast.com")
    options.add_argument("--window-size=1200,700")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    driver = webdriver.Chrome(options=options)
    try:
        time.sleep(60)
    finally:
        safe_quit(driver)


def guest_mode(enable: bool):
    """One login: Gujjar WiFi on + TV PCP off (or reverse)."""
    driver = hidden_driver()
    try:
        wait = WebDriverWait(driver, 15)
        _login(driver, wait)
        _do_toggle_gujjar_wifi(driver, wait, enable)
        _do_toggle_dera_tv_pcp(driver, wait, not enable)
    except Exception:
        pass
    finally:
        safe_quit(driver)


def main():
    register_context_menu()
    register_jump_list()
    if "--reboot" in sys.argv:
        reboot_router()
    elif "--guest-on" in sys.argv:
        guest_mode(True)
    elif "--guest-off" in sys.argv:
        guest_mode(False)
    elif "--speed-check" in sys.argv:
        speed_check()
    else:
        open_router()


if __name__ == "__main__":
    main()

# pyinstaller --name RouterOps main.py --noconsole --onefile --clean
