# RouterOps

System tray app to control your router — open dashboard or reboot, from the tray.

## Setup

```
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set your credentials:

```
ROUTER_USERNAME=user
ROUTER_PASSWORD=your_password
```

## Build

```
pyinstaller --name RouterOps main.py --icon=icon.png --noconsole --onefile
```

###OR 

```
pyinstaller RouterOps.spec --clean
```

> Requires Chrome. Selenium Manager handles the driver automatically.
