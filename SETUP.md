# Setup Guide

## 1. Flash the SD card

Raspberry Pi Imager → **Raspberry Pi OS Lite (64-bit)**, Trixie/Bookworm-based.

In the Imager's settings (gear icon) before writing:
- Hostname: `photoframe`
- Enable SSH, set a password (or add your SSH key)
- Configure WiFi (SSID + password)

## 2. First boot

```bash
ssh frame@photoframe.local
```

(replace `frame` with whatever username you set in the Imager)

## 3. Copy the project to the Pi

From your machine:

```bash
scp -r "DIY Pictureframe for RPI Zero W" frame@photoframe.local:~/photoframe-src
```

## 4. Run the installer

On the Pi:

```bash
cd ~/photoframe-src
sudo bash install.sh
# or, if your Pi username differs from what SUDO_USER resolves to:
sudo bash install.sh <your-username>
```

This installs all packages (mpv, pygame, iw, etc.), sets up the Python venv,
writes systemd services, tweaks `/boot/firmware/config.txt` (KMS driver,
HDMI hotplug, no overscan/splash), bumps swap to 512MB, and disables WiFi
power-save. Takes a few minutes.

## 5. Configure your photo source

Open `http://photoframe.local:8080` from any browser on the same network.

- **Sources** tab: pick Nextcloud or Immich, enter URL/credentials, hit
  "Test connection" before saving.
  - Nextcloud WebDAV URL format: `https://cloud.example.com/remote.php/dav/files/USERNAME/Fotos/`
  - Immich: server URL + API key (Immich → Account Settings → API Keys)
- **Slideshow** tab: transition, timing, shuffle, fit mode.
- **Display** tab: HDMI on/off schedule, sync interval, cache size limit.

Saving triggers `SIGHUP` to the slideshow automatically — no restart needed.

## 6. Trigger the first sync

Either wait (sync runs 30s after boot, then hourly), or click **"Sync now"**
on the status page. Watch progress with:

```bash
sudo journalctl -u photoframe-sync -f
```

## 7. Verify

```bash
sudo systemctl status photoframe-slideshow
sudo systemctl status photoframe-webui
sudo systemctl status photoframe-sync.timer
```

If the screen stays black: check `sudo journalctl -u photoframe-slideshow -e`
first — most likely cause is `vc4-kms-v3d` not active yet (needs a reboot
after install.sh adds it to config.txt), so **reboot once** after the first
install:

```bash
sudo reboot
```

## Reference

- Web UI: `http://photoframe.local:8080`
- Config file: `/opt/photoframe/config.yaml`
- Cache: `/var/lib/photoframe/cache/`
- Sync log: `/var/log/photoframe-sync.log`
- Config.txt backup (made by installer): `/boot/firmware/config.txt.photoframe.bak`

See `HANDOFF.md` for architecture/design notes and `README.md` for the
project overview.
