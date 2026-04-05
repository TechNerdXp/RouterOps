import os
import pystray
from dotenv import load_dotenv
from PIL import Image

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains

load_dotenv()

app_name = "Router Settings"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def router_controller(task="dashboard", headless=False):
    icon.icon = imageActive
    options = webdriver.ChromeOptions()
    options.add_argument("--ignore-certificate-errors")
    if headless:
        options.add_argument("--headless=new")
    else:
        options.add_argument("--app=http://192.168.1.1/login.cgi")
        options.add_argument("--window-size=1248,768")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
    driver = webdriver.Chrome(options=options)
    try:
        driver.get("http://192.168.1.1/login.cgi")

        wait = WebDriverWait(driver, 10)
        username_field = wait.until(EC.visibility_of_element_located((By.ID, "username")))
        password_field = wait.until(EC.visibility_of_element_located((By.ID, "userpassword")))
        username_field.send_keys(os.getenv("ROUTER_USERNAME"))
        password_field.send_keys(os.getenv("ROUTER_PASSWORD"))

        login_button = wait.until(EC.element_to_be_clickable((By.XPATH, "//input[@value='Login']")))
        login_button.click()

        if task == "reboot":
            maintenance_element = driver.find_element(By.ID, "MT")
            actions = ActionChains(driver)
            actions.move_to_element(maintenance_element).perform()

            reboot_link = driver.find_element(By.XPATH, "//a[contains(text(),'Reboot')]")
            reboot_link.click()

            wait.until(EC.frame_to_be_available_and_switch_to_it("mainFrame"))
            reboot_button = driver.find_element(By.NAME, "sysSubmit")
            reboot_button.click()

            driver.switch_to.default_content()

            ok_button = wait.until(EC.presence_of_element_located((By.XPATH, '//button[text()="OK"]')))
            ok_button.click()

        if not headless:
            current_handles = driver.window_handles
            WebDriverWait(driver, 60 * 60).until(lambda d: current_handles != d.window_handles)

        driver.quit()
        icon.icon = image
    except Exception as e:
        print(f"An error occurred: {e}")
        driver.quit()
        icon.icon = image


def on_quit():
    icon.stop()


image = Image.open(os.path.join(BASE_DIR, "icon.png"))
imageActive = Image.open(os.path.join(BASE_DIR, "icon-active.png"))
icon = pystray.Icon(app_name, image, app_name)
icon.menu = pystray.Menu(
    pystray.MenuItem("Dashboard", lambda: router_controller("dashboard"), default=True),
    pystray.MenuItem("Reboot", lambda: router_controller("reboot", headless=True)),
    pystray.MenuItem("Quit", on_quit),
)
icon.run()

# pyinstaller --name router_settings main.py --icon=icon-active.png --noconsole --onefile
