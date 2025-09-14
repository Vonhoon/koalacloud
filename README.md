# Koala Cloud

Koala Cloud is a self-hosted control panel + web drive:

- **Web Drive** (at `/`)  
  - Browse files under your mounted storage.  
  - Multi-file upload with drag & drop + queue + progress bars.  
  - Right-click context menu: Open/Download, Share, Properties, Move, Copy, Delete.  
  - Tokenized share links (`/s/<token>`), optionally with expiry, public to anyone.  

- **Admin Panel** (at `/admin`)  
  - PAM login (uses your system username/password).  
  - Toggle system services (via `systemctl` + `nsenter`).  
  - Live CPU & Memory stats (via Socket.IO).  
  - Storage usage bars for configured mount points.  

---

## Features

- Single Flask app with Socket.IO realtime updates.
- SQLite used to store share tokens.
- `nsenter` integration so container can control host services without DBus headaches.
- Configurable mounts and services list.
- Cloudflared tunnel for secure, public HTTPS access.

---

## Requirements

- Linux server with Docker.
- Cloudflared tunnel configured with your domain (`drive.koalarepublic.top`).
- Host must run `systemd` (for service toggles).
- Volumes mounted under `/mnt/drive` (adjust if different).

---

## Local Testing (optional)

You can run Koala Cloud **directly on your machine** before building a container:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 app.py
