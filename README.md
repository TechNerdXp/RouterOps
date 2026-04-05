# Router Settings

This script allows you to control your router settings using the command line.

## Requirements
- Python
- Selenium
- Pystray
- PIL

## How to use
1. Clone this repository to your local machine.
2. Install the required libraries by running `pip install -r requirements.txt`
3. Replace `http://192.168.1.1` with the IP address of your router in `main.py`
4. Replace `"user"` and `"LTE@Endusr"` with your router's username and password respectively in `main.py`
5. Replace the path of the icon images in the script to the correct path on your machine
6. Run the script by running `python main.py`

## Features
- Dashboard: Open your router's dashboard in a new browser window.
- Reboot: Reboots your router.
- Reboot Headless: Reboots your router without opening a new browser window.

## Creating an exe
You can use the pyinstaller library to create an exe from the script.

```
pyinstaller --name router_settings main.py --icon=icon-active.png --noconsole --onefile
```

Please replace "icon-active.png" with the path of your icon file and "router_settings" with your desired exe name.

Please make sure you have changed the IP address and the login credentials before creating the exe.

Please note that the script is using chrome driver, you need to have chrome installed on your machine.
Please note that this script is specific to the Huawei 2368 4G router and will need to be updated according to the IP panel and specific requirements of your router.
