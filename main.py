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
    """Headful Chrome minimized — stable for interactions, invisible to user."""
    options = webdriver.ChromeOptions()
    options.add_argument("--ignore-certificate-errors")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    driver = webdriver.Chrome(options=options)
    driver.minimize_window()
    return driver


def register_context_menu():
    """Register right-click verbs for RouterOps.exe (HKCU, no admin needed).

    Uses exefile\\shell (the correct path for exe right-click menus) with an
    AppliesTo filter so the items only appear on RouterOps.exe, not every exe.
    """
    if not getattr(sys, "frozen", False):
        return
    exe = sys.executable
    root = r"Software\Classes\exefile\shell"
    verbs = {
        "RouterOpsReboot":          ("Reboot Router",         f'"{exe}" --reboot'),
        "RouterOpsEnableTV":        ("Enable Dera TV PCP",    f'"{exe}" --enable-tv'),
        "RouterOpsDisableTV":       ("Disable Dera TV PCP",   f'"{exe}" --disable-tv'),
        "RouterOpsEnableGujjar":    ("Enable Gujjar WiFi",    f'"{exe}" --enable-gujjar'),
        "RouterOpsDisableGujjar":   ("Disable Gujjar WiFi",   f'"{exe}" --disable-gujjar'),
    }
    try:
        for verb, (label, cmd) in verbs.items():
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{root}\\{verb}") as k:
                winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, label)
                winreg.SetValueEx(k, "AppliesTo", 0, winreg.REG_SZ, 'System.FileName:="RouterOps.exe"')
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{root}\\{verb}\\command") as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, cmd)
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

    except Exception:
        log(f"toggle_gujjar_wifi ERROR:\n{traceback.format_exc()}")
    finally:
        safe_quit(driver)


def main():
    register_context_menu()
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
    else:
        open_router()


if __name__ == "__main__":
    main()

# pyinstaller --name RouterOps main.py --noconsole --onefile --clean
