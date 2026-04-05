import os
import sys
import winreg
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains

load_dotenv()


def register_context_menu():
    """Register all right-click verbs on this exe (HKCU, no admin needed)."""
    if not getattr(sys, "frozen", False):
        return
    exe = sys.executable
    shell = r"Software\Classes\Applications\RouterOps.exe\shell"
    verbs = {
        "reboot":     ("Reboot Router",        f'"{exe}" --reboot'),
        "enabletv":   ("Enable Dera TV PCP",   f'"{exe}" --enable-tv'),
        "disabletv":  ("Disable Dera TV PCP",  f'"{exe}" --disable-tv'),
    }
    try:
        for verb, (label, cmd) in verbs.items():
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{shell}\\{verb}") as k:
                winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, label)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{shell}\\{verb}\\command") as k:
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
        driver.service.stop()  # stop chromedriver process only; Chrome window stays open


def reboot_router():
    options = webdriver.ChromeOptions()
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--headless=new")
    driver = webdriver.Chrome(options=options)
    try:
        wait = WebDriverWait(driver, 10)
        _login(driver, wait)
        ActionChains(driver).move_to_element(driver.find_element(By.ID, "MT")).perform()
        driver.find_element(By.XPATH, "//a[contains(text(),'Reboot')]").click()
        wait.until(EC.frame_to_be_available_and_switch_to_it("mainFrame"))
        driver.find_element(By.NAME, "sysSubmit").click()
        driver.switch_to.default_content()
        wait.until(EC.presence_of_element_located((By.XPATH, '//button[text()="OK"]'))).click()
    finally:
        driver.quit()


def toggle_dera_tv_pcp(enable: bool):
    options = webdriver.ChromeOptions()
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--headless=new")
    driver = webdriver.Chrome(options=options)
    try:
        wait = WebDriverWait(driver, 10)
        _login(driver, wait)

        # Security > Parental Control
        ActionChains(driver).move_to_element(driver.find_element(By.ID, "Sec")).perform()
        wait.until(EC.element_to_be_clickable((By.ID, "Sec-ParentalControl"))).click()

        # Content is inside mainFrame
        wait.until(EC.frame_to_be_available_and_switch_to_it("mainFrame"))

        # Edit the TV record (first entry)
        wait.until(EC.element_to_be_clickable((By.ID, "editBtn1"))).click()

        # Toggle enable checkbox to the desired state
        enable_cb = wait.until(EC.presence_of_element_located((By.ID, "Enable")))
        if enable_cb.is_selected() != enable:
            enable_cb.click()

        # Apply
        wait.until(EC.element_to_be_clickable(
            (By.XPATH, '//button[normalize-space()="Apply"]')
        )).click()

    finally:
        driver.quit()


def main():
    register_context_menu()
    if "--reboot" in sys.argv:
        reboot_router()
    elif "--enable-tv" in sys.argv:
        toggle_dera_tv_pcp(True)
    elif "--disable-tv" in sys.argv:
        toggle_dera_tv_pcp(False)
    else:
        open_router()


if __name__ == "__main__":
    main()

# pyinstaller --name RouterOps main.py --noconsole --onefile --clean
