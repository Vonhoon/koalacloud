import os
import subprocess
import sqlite3
import secrets
import time
import mimetypes
import shutil
import json
import urllib.request
from pathlib import Path
from functools import wraps
from datetime import datetime
import glob
from zoneinfo import ZoneInfo
import logging
from logging.handlers import RotatingFileHandler
import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, jsonify, send_file, abort, session, redirect, url_for
from flask_socketio import SocketIO
import pam
import psutil
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
# --- NEW IMPORTS ---
import threading
import yt_dlp
# -------------------

# ==================== Config ====================
APP_NAME = os.getenv('APP_NAME', 'Koala Cloud')
SECRET_KEY = os.getenv('SECRET_KEY', 'change-me')

USE_NSENTER = os.getenv('USE_NSENTER', '1') == '1'
SERVICE_UNITS_ENV = os.getenv(
    'SERVICE_UNITS',
    'jellyfin:jellyfin.service,cloudflared:cloudflared.service,'
    'minecraft:minecraft-bedrock.service,smbd:smbd.service'
)
PAM_SERVICE = os.getenv('PAM_SERVICE', 'login')

STORAGE_ROOT = Path(os.getenv('STORAGE_ROOT', './storage')).resolve()
DEFAULT_UPLOAD_DIR = Path(os.getenv('DEFAULT_UPLOAD_DIR', './storage/Video')).resolve()
DB_PATH = Path(os.getenv('DB_PATH', './share.db')).resolve()
MAX_UPLOAD_MB = int(os.getenv('MAX_UPLOAD_MB', '51200'))

DASH_MOUNTS = [Path(p) for p in os.getenv('DASH_MOUNTS', './storage').split(',')]

ALLOWED_EXT = {'txt','pdf','png','jpg','jpeg','gif','mp4','mkv','avi','zip','rar','7z','srt','ass'}

LOG_PATH = DB_PATH.parent / 'koalacloud.log'

# ==================== App ====================
app = Flask(__name__, static_folder="static")
app.config['SECRET_KEY'] = SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_MB * 1024 * 1024
socketio = SocketIO(app, cors_allowed_origins='*')

DRIVE_ENABLED = True

log_formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]')
log_handler = RotatingFileHandler(LOG_PATH, maxBytes=1024*1024, backupCount=1) # 1MB log file
log_handler.setFormatter(log_formatter)
log_handler.setLevel(logging.INFO)

app.logger.addHandler(log_handler)
app.logger.setLevel(logging.INFO)

app.logger.info('Koala Cloud starting up...')

# trust Cloudflare's proxy headers
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

@app.before_request
def force_https_on_public_domain():
    host = request.headers.get('Host', '')
    # only force on your public domain so local http://serverip:5000 still works
    if host.endswith('koalarepublic.top'):
        proto = request.headers.get('X-Forwarded-Proto', 'http')
        if proto != 'https':
            return redirect(request.url.replace('http://', 'https://', 1), code=301)

# ==================== Helpers ====================
# ==================== Helpers ====================
def load_service_map():
    mapping = {}
    for item in SERVICE_UNITS_ENV.split(','):
        item = item.strip()
        if not item:
            continue
        parts = item.split(':', 2)
        if len(parts) == 3:
            name, stype, value = parts
            mapping[name.strip()] = {'type': stype.strip(), 'value': value.strip()}
        elif len(parts) == 2: # old format for backward compatibility
            name, value = parts
            mapping[name.strip()] = {'type': 'systemd', 'value': value.strip()}
    return mapping

SERVICE_MAP = load_service_map()

def allowed_file(name: str) -> bool:
    return '.' in name and name.rsplit('.',1)[1].lower() in ALLOWED_EXT

def check_password(user, pw) -> bool:
    p = pam.pam()
    return p.authenticate(user, pw, service=PAM_SERVICE)

# In app.py, find and replace this function
def _run_host_cmd(cmd, cwd=None):
    base = []
    if USE_NSENTER:
        base = ['nsenter','-t','1','-m','-p','-i','-u','-n','--']
    full_cmd = base + cmd
    return subprocess.run(full_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def get_systemd_service_status(unit: str) -> bool:
    p = _run_host_cmd(['systemctl', 'is-active', '--quiet', unit])
    return p.returncode == 0

# In app.py, find and replace this function
def get_docker_service_status(path: str) -> bool:
    # This command is now a single string that the host shell will execute
    shell_cmd_on_host = f"cd {path} && docker compose ps -q"
    p = _run_host_cmd(['sh', '-c', shell_cmd_on_host])
    return p.returncode == 0 and p.stdout.strip() != b''

def auth_required_json(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get('logged_in'):
            return jsonify({'ok': False, 'error': 'Unauthorized', 'code': 401}), 401
        return func(*args, **kwargs)
    return wrapper

# --- NEW DECORATOR FOR PAGES ---
def auth_required_page(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get('logged_in'):
            # For pages, redirect to the main page which shows the login form
            return redirect(url_for('drive_root')) 
        return func(*args, **kwargs)
    return wrapper

def _safe_join(rel_path: str) -> Path:
    rel_path = (rel_path or '').lstrip('/')
    p = (STORAGE_ROOT / rel_path).resolve()
    if not str(p).startswith(str(STORAGE_ROOT)):
        abort(400, 'Path escapes storage root')
    return p

def _safe_join_download(rel_path: str) -> Path:
    rel_path = (rel_path or '').lstrip('/')
    p = (DOWNLOAD_ROOT / rel_path).resolve()
    if not str(p).startswith(str(DOWNLOAD_ROOT)):
        abort(400, 'Path escapes download root')
    return p

# --- Temps & Network helpers ---
_net_last = {"ts": None, "rx": {}, "tx": {}}

def _get_temps():
    out = []
    try:
        temps = psutil.sensors_temperatures(fahrenheit=False) or {}
        # prefer common chips/labels
        for chip, readings in temps.items():
            for r in readings:
                label = r.label or chip
                out.append({
                    "label": label,
                    "current": float(r.current) if r.current is not None else None,
                    "high": float(r.high) if r.high is not None else None,
                    "critical": float(r.critical) if r.critical is not None else None,
                })
    except Exception:
        pass
    return out

def _get_net():
    """Return per-interface link state, ip, speed(Mbps), and rx/tx rates (bytes/s)."""
    now = time.time()
    stats = psutil.net_if_stats() or {}
    addrs = psutil.net_if_addrs() or {}
    io = psutil.net_io_counters(pernic=True) or {}

    # exclude loopback & obvious virtuals
    def _skip(name):
        n = name.lower()
        return n.startswith(("lo", "docker", "veth", "br-", "virbr", "vmnet", "zt"))

    # compute rates
    dt = (now - (_net_last["ts"] or now)) or 1.0
    rates = {}
    for name, c in io.items():
        rx_prev = _net_last["rx"].get(name, c.bytes_recv)
        tx_prev = _net_last["tx"].get(name, c.bytes_sent)
        rates[name] = {
            "rx_bps": max(0, (c.bytes_recv - rx_prev) / dt),
            "tx_bps": max(0, (c.bytes_sent - tx_prev) / dt),
            "bytes_recv": c.bytes_recv,
            "bytes_sent": c.bytes_sent,
        }
    # update cache
    _net_last["ts"] = now
    _net_last["rx"] = {n: io[n].bytes_recv for n in io}
    _net_last["tx"] = {n: io[n].bytes_sent for n in io}

    out = {}
    for name, st in stats.items():
        if _skip(name):
            continue
        ip = None
        if name in addrs:
            for a in addrs[name]:
                if getattr(a, "family", None).__class__.__name__ == "AddressFamily":
                    # psutil >= 5.9
                    fam = a.family.name
                else:
                    fam = str(a.family)
                if "AF_INET" in fam:
                    ip = a.address
                    break
        rate = rates.get(name, {})
        out[name] = {
            "isup": bool(st.isup),
            "speed_mbps": int(st.speed) if st.speed else None,
            "ip": ip,
            "rx_bps": rate.get("rx_bps", 0.0),
            "tx_bps": rate.get("tx_bps", 0.0),
        }
    return out


# ==================== Aria2 config ====================
ARIA2_RPC_URL    = os.getenv('ARIA2_RPC_URL', 'http://host.docker.internal:6800/jsonrpc')
ARIA2_RPC_SECRET = os.getenv('ARIA2_RPC_SECRET', '')  # matches rpc-secret in aria2.conf
DOWNLOAD_ROOT    = Path(os.getenv('DOWNLOAD_ROOT', '/mnt/drive')).resolve()

# ==================== Aria2 RPC ====================
def _aria2_call(method, params=None):
    payload = {"jsonrpc": "2.0", "id": "koala", "method": method, "params": params or []}
    if ARIA2_RPC_SECRET:
        payload["params"].insert(0, f"token:{ARIA2_RPC_SECRET}")
    data = json.dumps(payload).encode()
    req = urllib.request.Request(ARIA2_RPC_URL, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())
# ==================== Static files ====================
@app.route('/manifest.json')
def manifest():
    return send_file('static/manifest.json', mimetype='application/manifest+json')
# ==================== yt-dlp helper ====================
def run_youtubedl(url, dest_path, audio_only=True):
    try:
        if audio_only:
            ydl_opts = {
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'outtmpl': f'{dest_path}/%(title)s.%(ext)s',
                'noplaylist': True,
            }
        else:
            ydl_opts = {
                'format': 'bestvideo[height<=1080]+bestaudio/best',
                'outtmpl': f'{dest_path}/%(title)s.%(ext)s',
                'noplaylist': True,
            }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        print(f"yt-dlp: Successfully downloaded {url}")
    except Exception as e:
        print(f"yt-dlp: Error downloading {url}: {e}")

# ==================== Background tasks ====================
def _infer_name(files, bt):
    """Return a nice display name for a torrent:
       1) bittorrent.info.name when present
       2) common parent folder of files
       3) first file name
    """
    try:
        if bt and isinstance(bt, dict):
            info = bt.get("info") or {}
            n = info.get("name")
            if n:
                return n
    except Exception:
        pass
    try:
        if files:
            parts = [Path(f.get("path", "")).parts for f in files if f.get("path")]
            if parts:
                common = []
                for tup in zip(*parts):
                    if all(seg == tup[0] for seg in tup):
                        common.append(tup[0])
                    else:
                        break
                if common:
                    p = Path(*common)
                    return p.name or Path(files[0].get("path", "")).name
            return Path(files[0].get("path", "")).name
    except Exception:
        pass
    return "(unknown)"

def _record_history(rows):
    """Insert completed torrents into torrent_history once."""
    if not rows:
        return
    with sqlite3.connect(DB_PATH) as conn:
        for t in rows:
            gid   = t.get("gid")
            bt    = t.get("bittorrent") or {}
            files = t.get("files") or []
            name  = _infer_name(files, bt)
            total = int(t.get("totalLength") or 0)

            # derive destination (parent dir of first file under DOWNLOAD_ROOT)
            dest = "/"
            try:
                if files:
                    p0 = Path(files[0]["path"])
                    if str(p0).startswith(str(DOWNLOAD_ROOT)):
                        dest = "/" + str(p0.parent.relative_to(DOWNLOAD_ROOT)).strip("/")
            except Exception:
                pass

            ts = int(time.time())
            # avoid duplicates by gid
            cur = conn.execute("SELECT 1 FROM torrent_history WHERE gid=? LIMIT 1", (gid,))
            if not cur.fetchone():
                conn.execute(
                    """INSERT INTO torrent_history(name, gid, dest, size_bytes, added_at, completed_at)
                       VALUES (?,?,?,?,?,?)""",
                    (name, gid, dest, total, ts, ts)
                )
        conn.commit()

def get_system_stats():
    try:
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory().percent
        return {
            'cpu_percent': float(cpu),
            'memory_percent': float(mem),
            'temps': _get_temps(),
            'net': _get_net(),
        }
    except Exception as e:
        app.logger.error(f"Could not retrieve base system stats (CPU/Mem): {e}")
        # Return empty stats on failure so the socket doesn't crash
        return {
            'cpu_percent': 0,
            'memory_percent': 0,
            'temps': [],
            'net': {},
        }

def _stats_task():
    app.logger.info("Starting system stats background task.")
    while True:
        try:
            stats = get_system_stats()
            socketio.emit('system_stats', stats)
        except Exception as e:
            app.logger.error(f"Error in stats task: {e}", exc_info=True)
        socketio.sleep(2)

def _torrent_task():
    # Emit only "in-progress" items (active + waiting/metadata).
    # Completed items are written to history and not shown in progress.
    while True:
        try:
            # ask aria2 for richer fields incl. bittorrent (for proper names)
            fields = ["gid","status","totalLength","completedLength","downloadSpeed","files","bittorrent"]

            active  = _aria2_call("aria2.tellActive",   [fields]).get("result", [])
            waiting = _aria2_call("aria2.tellWaiting",  [0, 100, fields]).get("result", [])
            stopped = _aria2_call("aria2.tellStopped",  [0, 100, fields]).get("result", [])

            def enrich(row):
                row = dict(row)
                row["name"] = _infer_name(row.get("files") or [], row.get("bittorrent") or {})
                total = int(row.get("totalLength") or 0)
                row["isMetadata"] = (total == 0)   # show “Fetching metadata…” in UI when true
                return row

            # progress list = active + waiting/paused (NOT including 'complete' or 'error')
            progress = [enrich(r) for r in (active + waiting) if r.get("status") in ("active","waiting","paused")]

            # completed -> to history (once) and do not include in progress
            completed_rows = [enrich(r) for r in stopped if r.get("status") == "complete"]
            if completed_rows:
                _record_history(completed_rows)

            socketio.emit("torrent_status", {"progress": progress})
        except Exception:
            # don’t crash the loop on transient RPC errors
            pass
        socketio.sleep(2)

_started = False
@app.before_request
def _start_tasks_once():
    global _started
    if not _started:
        socketio.start_background_task(_stats_task)
        socketio.start_background_task(_torrent_task)
        _started = True

# ==================== DB ====================
def _db_init():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shares (
                token TEXT PRIMARY KEY,
                target_path TEXT NOT NULL,
                is_dir INTEGER NOT NULL,
                expires_at INTEGER,
                created_at INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS torrent_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                gid TEXT,
                dest TEXT NOT NULL,
                size_bytes INTEGER,
                added_at INTEGER NOT NULL,
                completed_at INTEGER
            )
        """)
        conn.commit()
_db_init()

# ==================== Auth ====================
@app.post('/auth/login')
def auth_login():
    data = request.get_json(force=True, silent=True) or {}
    user = data.get('username')
    pw = data.get('password')
    if not user or not pw:
        return jsonify({'ok': False, 'error': 'Missing credentials', 'code': 400}), 400
    if not check_password(user, pw):
        return jsonify({'ok': False, 'error': 'Invalid credentials', 'code': 401}), 401
    session['logged_in'] = True
    session['user'] = user
    return jsonify({'ok': True})

@app.post('/auth/logout')
def auth_logout():
    session.clear()
    return jsonify({'ok': True})

@app.get('/auth/status')
def auth_status():
    return jsonify({'ok': True, 'logged_in': bool(session.get('logged_in')), 'user': session.get('user')})

# ==================== Admin ====================
@app.get('/admin')
def admin_page():
    return render_template('admin.html', app_name=APP_NAME)

@app.get('/admin/logs')
@auth_required_json
def get_logs():
    try:
        with open(LOG_PATH, 'r') as f:
            lines = f.readlines()
        last_100_lines = lines[-100:]
        return jsonify({'ok': True, 'logs': "".join(last_100_lines)})
    except FileNotFoundError:
        return jsonify({'ok': True, 'logs': 'Log file not found.'})
    except Exception as e:
        app.logger.error(f"Error reading log file: {e}")
        return jsonify({'ok': False, 'error': 'Could not read log file.'}), 500

@app.post('/admin/services/toggle')
@auth_required_json
def toggle_service():
    data = request.get_json(force=True, silent=True) or {}
    friendly_name = data.get('service')
    desired_state = bool(data.get('state'))

    if friendly_name == 'drive':
        global DRIVE_ENABLED
        DRIVE_ENABLED = desired_state
        return jsonify({'ok': True, 'service': 'drive', 'status': DRIVE_ENABLED})

    service_details = SERVICE_MAP.get(friendly_name)
    if not service_details:
        return jsonify({'ok': False, 'error': f"Unknown service '{friendly_name}'"}), 400

    service_type = service_details['type']
    service_value = service_details['value']

    if service_type == 'systemd':
        action = 'start' if desired_state else 'stop'
        p = _run_host_cmd(['systemctl', action, service_value])
        if p.returncode != 0:
            msg = (p.stderr or p.stdout).decode(errors='ignore').strip() or f'Failed to {action} {friendly_name}'
            return jsonify({'ok': False, 'error': msg}), 500
        return jsonify({'ok': True, 'service': friendly_name, 'status': get_systemd_service_status(service_value)})

    # In app.py, find the toggle_service function and replace the 'elif service_type == 'docker':' block
    elif service_type == 'docker':
        action_cmd_str = 'up -d' if desired_state else 'down'
        # Construct the full shell command to be executed on the host
        shell_cmd_on_host = f"cd {service_value} && docker compose {action_cmd_str}"
        
        # We pass this complete command to the host's shell
        p = _run_host_cmd(['sh', '-c', shell_cmd_on_host])

        if p.returncode != 0:
            msg = (p.stderr or p.stdout).decode(errors='ignore').strip() or f'Failed to run docker compose for {friendly_name}'
            return jsonify({'ok': False, 'error': msg}), 500
        
        time.sleep(2)
        return jsonify({'ok': True, 'service': friendly_name, 'status': get_docker_service_status(service_value)})
    return jsonify({'ok': False, 'error': f"Unsupported service type '{service_type}'"}), 400

@app.get('/admin/services/list')
@auth_required_json
def list_services():
    services = {}
    for name, details in SERVICE_MAP.items():
        if details.get('type') == 'systemd':
            services[name] = get_systemd_service_status(details['value'])
        elif details.get('type') == 'docker':
            services[name] = get_docker_service_status(details['value'])
        else: # Handle old format
             services[name] = get_systemd_service_status(details)

    services['drive'] = DRIVE_ENABLED
    return jsonify({'ok': True, 'services': services})

@app.get('/admin/storage')
@auth_required_json
def storage_api():
    out = []
    for m in DASH_MOUNTS:
        try:
            u = psutil.disk_usage(str(m))
            out.append({'mountpoint': str(m), 'total': u.total, 'used': u.used, 'percent': u.percent})
        except Exception:
            continue
    return jsonify({'ok': True, 'storage': out})
# ==================== Help Page ====================
@app.get('/help')
def help_page():
    return render_template('help.html', app_name=APP_NAME)

# ==================== Drive page ====================
@app.get('/')
def drive_root():
    if not DRIVE_ENABLED:
        return "<h1 style='text-align:center;margin-top:20%'>🚫 Drive is disabled by admin</h1>", 503
    return render_template('index.html', app_name=APP_NAME)
@app.get('/api/list')
@auth_required_json
def api_list():
    rel = request.args.get('path', '').strip()
    p = _safe_join(rel)
    if not p.exists():
        abort(404)
    if p.is_file():
        st = p.stat()
        return jsonify({'ok': True, 'type': 'file', 'name': p.name,
                        'path': str(p.relative_to(STORAGE_ROOT)), 'size': st.st_size, 'mtime': int(st.st_mtime)})
    items = []
    for c in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        try:
            st = c.stat()
            items.append({'name': c.name, 'path': str(c.relative_to(STORAGE_ROOT)),
                          'type': 'file' if c.is_file() else 'dir',
                          'size': st.st_size, 'mtime': int(st.st_mtime)})
        except PermissionError:
            continue
    return jsonify({'ok': True, 'type': 'dir', 'path': '/' if p == STORAGE_ROOT else str(p.relative_to(STORAGE_ROOT)), 'items': items})

@app.get('/api/download')
@auth_required_json
def api_download():
    rel = request.args.get('path', '')
    p = _safe_join(rel)
    if not p.exists() or not p.is_file():
        abort(404)
    return send_file(p, as_attachment=True, download_name=p.name)

@app.post('/api/upload')
@auth_required_json
def api_upload():
    rel = request.args.get('path', '').strip() or str(DEFAULT_UPLOAD_DIR.relative_to(STORAGE_ROOT))
    target_dir = _safe_join(rel)
    target_dir.mkdir(parents=True, exist_ok=True)
    if 'file' not in request.files:
        abort(400, 'No file part')
    f = request.files['file']
    if not f or f.filename == '':
        abort(400, 'No selected file')
    if not allowed_file(f.filename):
        abort(400, 'File type not allowed')
    name = secure_filename(f.filename)
    dest = target_dir / name
    f.save(dest)
    return jsonify({'ok': True, 'saved_as': str(dest.relative_to(STORAGE_ROOT))})

@app.post('/api/mkdir')
@auth_required_json
def api_mkdir():
    parent = request.json.get('parent', '')
    name = secure_filename(request.json.get('name', '')).strip()
    if not name:
        abort(400, 'Missing name')
    p = _safe_join(parent)
    (p / name).mkdir(parents=False, exist_ok=False)
    return jsonify({'ok': True})

@app.post('/api/delete')
@auth_required_json
def api_delete():
    rel = request.json.get('path', '')
    p = _safe_join(rel)
    if not p.exists():
        abort(404)
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()
    return jsonify({'ok': True})

@app.post('/api/move')
@auth_required_json
def api_move():
    src = _safe_join(request.json.get('src', ''))
    dst = _safe_join(request.json.get('dst', ''))
    if not src.exists():
        abort(404, 'src missing')
    shutil.move(str(src), str(dst))
    return jsonify({'ok': True})

@app.post('/api/copy')
@auth_required_json
def api_copy():
    src = _safe_join(request.json.get('src', ''))
    dst = _safe_join(request.json.get('dst', ''))
    if not src.exists():
        abort(404, 'src missing')
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    return jsonify({'ok': True})

@app.get('/api/properties')
@auth_required_json
def api_properties():
    rel = request.args.get('path','')
    p = _safe_join(rel)
    if not p.exists():
        abort(404)
    st = p.stat()
    kind = 'file' if p.is_file() else 'dir'
    mime = mimetypes.guess_type(p.name)[0] if p.is_file() else 'inode/directory'
    return jsonify({'ok': True, 'type': kind, 'name': p.name,
                    'path': str(p.relative_to(STORAGE_ROOT)), 'size': st.st_size,
                    'mtime': int(st.st_mtime), 'mime': mime})

# ==================== Torrent APIs ====================
@app.post('/admin/torrents/add')
@auth_required_json
def torrents_add():
    data = request.get_json(force=True, silent=True) or {}
    magnet = (data.get('link') or '').strip()
    dest   = (data.get('dest') or '').strip()
    if not magnet:
        abort(400, 'Missing magnet link')
    dpath = _safe_join_download(dest)
    dpath.mkdir(parents=True, exist_ok=True)
    r = _aria2_call("aria2.addUri", [[magnet], {"dir": dpath.as_posix()}])
    return jsonify({'ok': True, 'result': r})

@app.post('/admin/torrents/remove')
@auth_required_json
def torrents_remove():
    data = request.get_json(force=True, silent=True) or {}
    gid = data.get('gid')
    if not gid:
        abort(400, 'Missing gid')
    try:
        _aria2_call("aria2.remove", [gid])
    finally:
        # remove from result list as well (does not delete files)
        try:
            _aria2_call("aria2.removeDownloadResult", [gid])
        except Exception:
            pass
    return jsonify({'ok': True})

@app.get('/admin/torrents/history')
@auth_required_json
def torrents_history():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT id, name, gid, dest, size_bytes, added_at, completed_at
            FROM torrent_history ORDER BY COALESCE(completed_at, added_at) DESC LIMIT 500
        """).fetchall()
    out = []
    for r in rows:
        out.append({'id': r[0], 'name': r[1], 'gid': r[2], 'dest': r[3],
                    'size_bytes': r[4], 'added_at': r[5], 'completed_at': r[6]})
    return jsonify({'ok': True, 'history': out})

@app.post('/admin/torrents/history/delete')
@auth_required_json
def torrents_history_delete():
    data = request.get_json(force=True, silent=True) or {}
    hid = data.get('id')
    if hid is None:
        abort(400, 'Missing history id')
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM torrent_history WHERE id=?", (hid,))
        conn.commit()
    return jsonify({'ok': True})

@app.get('/admin/torrents/browse')
@auth_required_json
def torrents_browse():
    rel = request.args.get('path','').strip()
    p = _safe_join_download(rel)
    if not p.exists():
        abort(404)
    if p.is_file():
        abort(400, 'Not a directory')

    items = []
    for c in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        if c.is_dir():
            items.append({'name': c.name, 'path': str(c.relative_to(DOWNLOAD_ROOT))})

    parent_rel = '' if p == DOWNLOAD_ROOT else str(p.parent.relative_to(DOWNLOAD_ROOT))
    return jsonify({
        'ok': True,
        'cwd': '/' if p == DOWNLOAD_ROOT else str(p.relative_to(DOWNLOAD_ROOT)),
        'parent': parent_rel,
        'root_abs': str(DOWNLOAD_ROOT),
        'dirs': items
    })

@app.post('/admin/torrents/mkdir')
@auth_required_json
def torrents_mkdir():
    parent = request.json.get('parent','').strip()
    name = secure_filename(request.json.get('name','')).strip()
    if not name:
        abort(400, 'Missing folder name')
    base = _safe_join_download(parent)
    (base / name).mkdir(parents=False, exist_ok=False)
    return jsonify({'ok': True})

@app.post('/admin/youtubedl/add')
@auth_required_json
def youtubedl_add():
    data = request.get_json(force=True, silent=True) or {}
    link = (data.get('link') or '').strip()
    dest = (data.get('dest') or '').strip()
    audio_only = bool(data.get('audio_only', False))
    if not link:
        abort(400, 'Missing link')

    dpath = _safe_join_download(dest)
    dpath.mkdir(parents=True, exist_ok=True)
    
    thread = threading.Thread(target=run_youtubedl, args=(link, dpath, audio_only))
    thread.start()
    return jsonify({'ok': True, 'result': {'message': f'Video download started in the background.{audio_only=}'}})


# ==================== Public share endpoints (no auth) ====================
@app.post('/api/share')
@auth_required_json
def api_share():
    rel = request.json.get('path', '').strip()
    hours = float(request.json.get('expires_hours', 0))
    target = _safe_join(rel)
    if not target.exists():
        abort(404)
    token = secrets.token_urlsafe(16)
    is_dir = 1 if target.is_dir() else 0
    expires_at = int(time.time() + hours * 3600) if hours > 0 else None
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            'INSERT INTO shares(token, target_path, is_dir, expires_at, created_at) VALUES (?,?,?,?,?)',
            (token, str(target), is_dir, expires_at, int(time.time()))
        )
        conn.commit()
    return jsonify({'ok': True, 'token': token, 'url': f'/s/{token}'})

@app.get('/s/<token>')
def shared_entry(token):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute('SELECT token, target_path, is_dir, expires_at FROM shares WHERE token=?', (token,)).fetchone()
    if not row:
        abort(404)
    _, target_path, is_dir, expires_at = row
    if expires_at and time.time() > expires_at:
        abort(410, description='Link expired')
    target = Path(target_path)
    if not target.exists():
        abort(404)

    if not is_dir:
        # single-file share -> download
        return send_file(target, as_attachment=True, download_name=target.name)

    # folder share: allow browsing and downloading child files within the folder
    child = request.args.get('p', '').strip()
    current = (target / child) if child else target
    current = current.resolve()
    if not str(current).startswith(str(target.resolve())):
        abort(400)
    if current.is_file():
        return send_file(current, as_attachment=True, download_name=current.name)

    items = []
    for ch in sorted(current.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        st = ch.stat()
        items.append({'name': ch.name, 'type': 'file' if ch.is_file() else 'dir',
                      'size': st.st_size, 'mtime': int(st.st_mtime)})
    rel = str(current.relative_to(target)) if current != target else ''
    parent_q = '' if rel == '' else f"?p={Path(rel).parent.as_posix()}"
    html = [f"<h3>Shared folder: /{target.name}{('/'+rel) if rel else ''}</h3>"]
    if rel:
        html.append(f'<p><a href="/s/{token}{parent_q}">⬅️ Up</a></p>')
    html.append('<ul>')
    for it in items:
        if it['type'] == 'dir':
            html.append(f'<li>📁 <a href="/s/{token}?p={(Path(rel)/it["name"]).as_posix()}">{it["name"]}</a></li>')
        else:
            href = f'/s/{token}?p={(Path(rel)/it["name"]).as_posix()}'
            html.append(f'<li>📄 <a href="{href}">{it["name"]}</a> — <a href="{href}">download</a></li>')
    html.append('</ul>')
    return "\n".join(html)

# ==================== Multi-Note App (Upgraded & Fixed) ====================
NOTES_DIR = '/data/daily_notes'
KST = ZoneInfo("Asia/Seoul") 

@app.route('/notes')
@auth_required_page
def notes_app_shell():
    """
    Serves the main HTML shell of the notes app.
    The app itself will be rendered by JavaScript.
    """
    return render_template('notes.html')

@app.route('/notes/api/list')
@auth_required_json
def notes_list():
    """Returns a sorted list of all available note dates."""
    os.makedirs(NOTES_DIR, exist_ok=True)
    
    # Also add today's KST date to the list if it doesn't exist yet
    today_str = datetime.now(KST).strftime('%Y-%m-%d')
    
    note_files = glob.glob(os.path.join(NOTES_DIR, 'note_*.txt'))
    
    # Extract YYYY-MM-DD from 'daily_notes/note_YYYY-MM-DD.txt'
    dates = {os.path.basename(f)[5:-4] for f in note_files}
    dates.add(today_str) # Use a set to automatically handle duplicates
    
    sorted_dates = sorted(list(dates), reverse=True)
    
    return jsonify(sorted_dates)

@app.route('/notes/api/get/<string:date_str>')
@auth_required_json
def notes_get(date_str):
    """Returns the content of a specific note file."""
    try:
        # Validate date format to prevent directory traversal
        datetime.strptime(date_str, '%Y-%m-%d')
        filename = f"note_{date_str}.txt"
        note_path = os.path.join(NOTES_DIR, filename)
        
        if os.path.exists(note_path):
            with open(note_path, 'r', encoding='utf-8') as f:
                content = json.load(f)
            return jsonify(content)
        else:
            # If note for that day doesn't exist, return a default empty structure
            return jsonify([{'task': '', 'status': 'to-do'}])
    except (ValueError, FileNotFoundError, json.JSONDecodeError):
        # Return default structure on any error (e.g., empty file)
        return jsonify([{'task': '', 'status': 'to-do'}])

@app.route('/notes/api/save/<string:date_str>', methods=['POST'])
@auth_required_json
def notes_save_api(date_str): # Renamed to avoid conflict with function `save`
    """Saves the content for a specific note."""
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
        filename = f"note_{date_str}.txt"
        note_path = os.path.join(NOTES_DIR, filename)
        
        data = request.get_json()
        if data is None:
            return jsonify({'status': 'error', 'message': 'Invalid data'}), 200
            
        os.makedirs(NOTES_DIR, exist_ok=True)
        with open(note_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
            
        return jsonify({'status': 'success', 'message': f'Note for {date_str} saved!'})
    except ValueError:
        return jsonify({'error': 'Invalid date format'}), 200
@app.route('/health')
def health_check():
    """Health check endpoint."""
    return jsonify({'status': 'ok'})
# ==================== JSON error formatting ====================
@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(410)
@app.errorhandler(413)
@app.errorhandler(500)
def json_errors(err):
    code = getattr(err, 'code', 500)
    app.logger.error(f'HTTP Error {code}: {getattr(err, "description", str(err))} - URL: {request.url}')
    return jsonify({'ok': False, 'error': getattr(err, 'description', str(err)), 'code': code}), code

# ==================== Main ====================
if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)

