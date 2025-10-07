# oled-sys-monitor

![Example running on Android phone](https://github.com/user-attachments/assets/3ed88066-e760-40f2-aefa-eb8b31787eeb)

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
<b>HTTPS</b> <i>(recommended for Android wake lock)</i>
</summary>
Chrome on Android requires a secure context for `navigator.wakeLock` .

- Generate or obtain a certificate and key (self‑signed is fine for LAN testing)
- Run the server with HTTPS:

```bash
python monitor_server.py --host 0.0.0.0 --port 8443 --cert cert.pem --key key.pem
```

- On your phone, open `https://<your-ip>:8443/` and accept the certificate warning if prompted.

## Beware: certificate generation requires extra tools and can be a lot of work compared to simply downloading a third party app to keep your screen awake.

### Certificate generation

**Step 1**: Install Chocolatey:
- Open elevated powershell;
- Paste `Set-ExecutionPolicy AllSigned` and type `y` when prompted. This will enable execution of _signed_ scripts, the default behaviour is `Restricted`. Feel free to change it back once you're done. You can check the current value with `Get-ExecutionPolicy`. This is necessary to enable installing Chocolatey from the web.
- Paste this command, wait for the installation to finish and you're good to go:<br>
```bash
Set-ExecutionPolicy Bypass -Scope Process -Force; [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072; iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
```

**Step 2**: Install mkcert:
- Paste into powershell: `choco install mkcert -y` (optional Firefox trust: `choco install nss -y`)

**Step 3**: Generate certificates:
- Set your local IP below and paste into powershell in the project directory:<br>
`mkcert -key-file key.pem -cert-file cert.pem YOUR_IP localhost`

**Step 4**: Run the server:
- Paste into powershell or cmd:
```bash
python monitor_server.py --host 0.0.0.0 --port 8443 --http-port 8000 --cert cert.pem --key key.pem --open-firewall
```
This will run the server with HTTP on port 8000 and HTTPS on port 8443. Use HTTPS and bypass the Chrome warning for screen keep awake functionality. Keeping the page in fullscreen will request a screen wake lock and keep the display on; the lock is released automatically if you leave fullscreen or the tab is hidden.
</details>