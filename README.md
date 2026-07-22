# ZeroPiFrame - DIY Photoframe for Raspberry Pi Zero 2 W

A DIY digital picture frame running on a **Raspberry Pi Zero 2 W**. Images and videos are synced from either **Nextcloud (WebDAV)** or **Immich (REST API)** into a local cache, and a slideshow displays them on the connected screen via the Linux framebuffer — no desktop environment or X11 required.

This is a modernized replacement for the abandoned [photOS](https://github.com/avanc/photOS) project, which was based on Buildroot and is no longer maintained.

## Features

- Slideshow with multiple transitions: `none`, `fade`, `ken_burns` (default), `slide_left/right/up/down`, `wipe_left`, `zoom_in`, `dissolve`
- Photo and video support (H.264 hardware-decoded via v4l2m2m), with EXIF auto-rotation
- Sync from Nextcloud (WebDAV) or Immich (REST API)
- Web UI (Flask, port 8080) to configure sources, slideshow behavior, and display schedule — no SSH needed for day-to-day use
- Runs headless as systemd services; recovers automatically after reboot or crash
- Works fully offline once media is cached; sync silently skips when the server is unreachable

## Tech stack

| Component      | Technology                                           |
|----------------|-------------------------------------------------------|
| OS             | Raspberry Pi OS Lite 64-bit (Trixie / Debian 13)      |
| Display        | pygame 2.x with `SDL_VIDEODRIVER=kmsdrm`              |
| Image render   | Pillow (EXIF rotation, resize, transitions)           |
| Video player   | mpv (`--vo=drm --hwdec=v4l2m2m`)                      |
| Nextcloud sync | `webdavclient3` (WebDAV PROPFIND + ETag)              |
| Immich sync    | Immich REST API (`/api/assets`, `/api/albums`)        |
| Web UI         | Flask on port 8080                                    |
| Services       | systemd (3 services + 1 timer)                        |

## Repository structure

```
├── src/
│   ├── slideshow.py          # Slideshow service (pygame, transitions, video)
│   ├── sync.py                # Sync service (Nextcloud WebDAV + Immich API)
│   ├── webui.py               # Flask web UI (port 8080)
│   ├── templates/             # Web UI HTML templates
│   └── static/style.css       # Dark theme, mobile-first
├── config.yaml.example        # Documented config template
├── requirements.txt           # Python dependencies
├── install.sh                 # One-shot setup script
└── Konzept_Digitaler_Bilderrahmen.md   # Architecture documentation (German)
```

## Installation

```bash
# Flash Raspberry Pi OS Lite 64-bit (Trixie), set hostname=zeropiframe, enable SSH
ssh frame@zeropiframe.local

# Copy this repo to the Pi, then:
sudo bash install.sh
# install.sh auto-detects the user from SUDO_USER (defaults to "frame")
# or explicitly: sudo bash install.sh frame
```

After install, open `http://zeropiframe.local:8080` to configure your photo source and slideshow settings.

### What gets installed

```
/opt/zeropiframe/              # App files (deployed by install.sh)
├── venv/                     # Python virtualenv (--system-site-packages)
├── slideshow.py / sync.py / webui.py
├── templates/ / static/
└── config.yaml               # Live config, written by the web UI (not tracked in git)

/var/lib/zeropiframe/cache/    # Downloaded images + videos
/var/log/zeropiframe-sync.log  # Sync log
```

| systemd unit                    | Purpose                                    |
|----------------------------------|---------------------------------------------|
| `zeropiframe-slideshow.service`  | Runs the slideshow continuously on boot     |
| `zeropiframe-sync.timer`        | Triggers sync 30s after boot, then hourly   |
| `zeropiframe-sync.service`      | One-shot sync run (called by the timer)     |
| `zeropiframe-webui.service`     | Flask web UI on :8080                       |

## Configuration

Copy `config.yaml.example` for the full schema (source credentials, sync interval, cache limits, slideshow transition/timing, display on/off schedule). The web UI writes `config.yaml` directly and sends `SIGHUP` to the slideshow process so changes apply at the next image boundary without a restart.

## Known limitations

- `mpv --vo=drm` needs exclusive DRM access, so there's a brief (~1s) black screen at video start/end while pygame hands off and reinitializes
- H.265/HEVC is not hardware-accelerated on the Zero 2 W (software decode only — may be slow for high-res video)
- WebDAV recursive folder scans use PROPFIND with Depth:1 per folder, which can be slow on deep Nextcloud hierarchies
- The web UI has no authentication — intended for a trusted home network only

## Roadmap

- Sync from Nextcloud and Immich simultaneously
- Immich album picker (dropdown instead of typing album names)
- Sync progress indicator in the web UI (SSE)
- Portrait/landscape auto-detection
- OTA updates (`git pull` + service restart via a web UI button)

## License

Not yet decided — add a `LICENSE` file before making this repository public if you want to set terms for reuse.
