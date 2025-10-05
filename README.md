# oled-sys-monitor
Small python script utility for Windows that uses LibreHardwareMonitor and Django to serve system sensor variables such as CPU temperature through a local webpage on port 8000.

Background is pitch black (`#000000`). As such, usage on an OLED phone is recommended.

# Usage
- Get LibreHardwareMonitor release here: https://github.com/LibreHardwareMonitor/LibreHardwareMonitor
- Extract everything to a folder;
- Put `monitor_server.py` and `config.ini` into this folder;
- Run `python monitor_server.py`;
- On Windows, administrator rights are required for proper readings. The script already asks for elevation, no need to run python as admin beforehand.
- Get your machine's local IP using `ipconfig`;
- Navigate to this IP on port 8000 on any browser (OLED phone recommended);
- Enable fullscreen mode.

Refresh rate and sensor variable ordering can be set directly in `config.ini` or through the webpage itself.<br>
Screen will be kept awake through embedded HTML/JS.<br>
Tested on a Windows 10 machine with an AMD CPU and NVidia VGA on Chrome under both Windows 10 and Android.

<details><summary>
## HTTPS (recommended for Android wake lock)
</summary>
Chrome on Android requires a secure context for `navigator.wakeLock`.

- Generate or obtain a certificate and key (self‑signed is fine for LAN testing).
- Run the server with HTTPS:

```bash
python monitor_server.py --host 0.0.0.0 --port 8443 --cert cert.pem --key key.pem
```

- On your phone, open `https://<your-ip>:8443/` and accept the certificate warning if prompted.

Keeping the page in fullscreen will request a screen wake lock and keep the display on; the lock is released automatically if you leave fullscreen or the tab is hidden.
</details>