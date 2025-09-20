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
import threading
from urllib.parse import urlparse, parse_qs
import unicodedata
from zoneinfo import ZoneInfo 

from flask import Flask, render_template, request, jsonify, send_file, abort, session, redirect
from flask_socketio import SocketIO
import pam
import psutil
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
import yt_dlp

from datetime import datetime
import glob

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

# ==================== App ====================
app = Flask(__name__, static_folder="static")
app.config['SECRET_KEY'] = SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_MB * 1024 * 1024
socketio = SocketIO(app, cors_allowed_origins='*')

DRIVE_ENABLED = True

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
def load_service_map():
    mapping = {}
    for item in SERVICE_UNITS_ENV.split(','):
        item = item.strip()
        if not item:
            continue
        if ':' in item:
            k, v = item.split(':', 1)
            mapping[k.strip()] = v.strip()
    return mapping

SERVICE_MAP = load_service_map()

def allowed_file(name: str) -> bool:
    return '.' in name and name.rsplit('.',1)[1].lower() in ALLOWED_EXT

def check_password(user, pw) -> bool:
    p = pam.pam()
    return p.authenticate(user, pw, service=PAM_SERVICE)

def _systemctl_cmd(*args, quiet=False):
    base = []
    if USE_NSENTER:
        base = ['nsenter','-t','1','-m','-p','-i','-u','-n','--']
    cmd = base + ['systemctl'] + (['--quiet'] if quiet else []) + list(args)
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def get_service_status(unit: str) -> bool:
    p = _systemctl_cmd('is-active', unit, quiet=True)
    return p.returncode == 0

def get_system_stats():
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory().percent
    return {
        'cpu_percent': float(cpu),
        'memory_percent': float(mem),
        'temps': _get_temps(),
        'net': _get_net(),
    }

def auth_required_json(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get('logged_in'):
            return jsonify({'ok': False, 'error': 'Unauthorized', 'code': 401}), 401
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

_net_last = {"ts": None, "rx": {}, "tx": {}}

def _get_temps():
    out = []
    try:
        temps = psutil.sensors_temperatures(fahrenheit=False) or {}
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
    now = time.time()
    stats = psutil.net_if_stats() or {}
    addrs = psutil.net_if_addrs() or {}
    io = psutil.net_io_counters(pernic=True) or {}

    def _skip(name):
        n = name.lower()
        return n.startswith(("lo", "docker", "veth", "br-", "virbr", "vmnet", "zt"))

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
ARIA2_RPC_URL = os.getenv('ARIA2_RPC_URL', 'http://host.docker.internal:6800/jsonrpc')
ARIA2_RPC_SECRET = os.getenv('ARIA2_RPC_SECRET', '')
DOWNLOAD_ROOT = Path(os.getenv('DOWNLOAD_ROOT', '/mnt/drive')).resolve()

# ==================== Aria2 RPC ====================
def _aria2_call(method, params=None):
    payload = {"jsonrpc": "2.0", "id": "koala", "method": method, "params": params or []}
    if ARIA2_RPC_SECRET:
        payload["params"].insert(0, f"token:{ARIA2_RPC_SECRET}")
    data = json.dumps(payload).encode()
    req = urllib.request.Request(ARIA2_RPC_URL, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())

# ==================== yt-dlp helper ====================
YTDL_TASKS = {}

def normalize_filename_hook(d):
    if d['status'] == 'finished':
        filepath = d.get('filename')
        if not filepath or not os.path.exists(filepath):
            return
        directory, filename = os.path.split(filepath)
        normalized_filename = unicodedata.normalize('NFC', filename)
        if filename != normalized_filename:
            try:
                os.rename(filepath, os.path.join(directory, normalized_filename))
            except OSError:
                pass
def run_youtubedl(url, dest_path, audio_only, task_id, db_id):
    
    playlist_id = get_playlist_id(url)
    download_url = f'https://www.youtube.com/playlist?list={playlist_id}' if playlist_id else url
    
    output_template = str(dest_path / ('%(playlist_title)s/%(playlist_index)s - %(title)s.%(ext)s' if playlist_id else '%(title)s.%(ext)s'))

    ydl_opts = {
        'outtmpl': output_template,
        'noplaylist': playlist_id is None,
        'postprocessor_hooks': [normalize_filename_hook],
        'ignoreerrors': True,
        # No progress hook for now to ensure stability
    }

    if audio_only:
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}]

    try:
        # Get title first for the database
        with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': True}) as ydl:
            info = ydl.extract_info(download_url, download=False)
            title = info.get('title', 'YouTube Content')
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("UPDATE youtubedl_history SET name=? WHERE id=?", (title, db_id))
                conn.commit()

        # Perform download
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([download_url])
        
        # Mark as complete in the database
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE youtubedl_history SET completed_at=? WHERE id=?", (int(time.time()), db_id))
            conn.commit()
        
        # Mark as finished for the UI task list
        if task_id in YTDL_TASKS:
            YTDL_TASKS[task_id]['status'] = 'finished'

    except Exception as e:
        print(f"YTDL Error for task {task_id}: {e}")
        if task_id in YTDL_TASKS:
            YTDL_TASKS[task_id]['status'] = 'error'


def get_playlist_id(url):
    try:
        query = parse_qs(urlparse(url).query)
        if 'list' in query:
            playlist_id = query['list'][0]
            if playlist_id.startswith(('PL', 'OL', 'UU', 'FL')):
                return playlist_id
    except Exception:
        return None
    return None

def run_youtubedl(url, dest_path, audio_only, task_id, db_id):
    
    playlist_id = get_playlist_id(url)
    download_url = f'https://www.youtube.com/playlist?list={playlist_id}' if playlist_id else url
    
    output_template = str(dest_path / ('%(playlist_title)s/%(playlist_index)s - %(title)s.%(ext)s' if playlist_id else '%(title)s.%(ext)s'))

    ydl_opts = {
        'outtmpl': output_template,
        'noplaylist': playlist_id is None,
        'postprocessor_hooks': [normalize_filename_hook],
        'ignoreerrors': True,
        # No progress hook for now to ensure stability
    }

    if audio_only:
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}]

    try:
        # Get title first for the database
        with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': True}) as ydl:
            info = ydl.extract_info(download_url, download=False)
            title = info.get('title', 'YouTube Content')
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("UPDATE youtubedl_history SET name=? WHERE id=?", (title, db_id))
                conn.commit()

        # Perform download
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([download_url])
        
        # Mark as complete in the database
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE youtubedl_history SET completed_at=? WHERE id=?", (int(time.time()), db_id))
            conn.commit()
        
        # Mark as finished for the UI task list
        if task_id in YTDL_TASKS:
            YTDL_TASKS[task_id]['status'] = 'finished'

    except Exception as e:
        print(f"YTDL Error for task {task_id}: {e}")
        if task_id in YTDL_TASKS:
            YTDL_TASKS[task_id]['status'] = 'error'

# ==================== Background tasks ====================
def _infer_name(files, bt):
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
    if not rows:
        return
    with sqlite3.connect(DB_PATH) as conn:
        for t in rows:
            gid   = t.get("gid")
            bt    = t.get("bittorrent") or {}
            files = t.get("files") or []
            name  = _infer_name(files, bt)
            total = int(t.get("totalLength") or 0)
            dest = "/"
            try:
                if files:
                    p0 = Path(files[0]["path"])
                    if str(p0).startswith(str(DOWNLOAD_ROOT)):
                        dest = "/" + str(p0.parent.relative_to(DOWNLOAD_ROOT)).strip("/")
            except Exception:
                pass

            ts = int(time.time())
            cur = conn.execute("SELECT 1 FROM torrent_history WHERE gid=? LIMIT 1", (gid,))
            if not cur.fetchone():
                conn.execute(
                    """INSERT INTO torrent_history(name, gid, dest, size_bytes, added_at, completed_at)
                       VALUES (?,?,?,?,?,?)""",
                    (name, gid, dest, total, ts, ts)
                )
            _aria2_call("aria2.removeDownloadResult", [gid]) # Purge from aria2's memory
        conn.commit()

def _stats_task():
    while True:
        socketio.emit('system_stats', get_system_stats())
        socketio.sleep(2)

def _torrent_task():
    while True:
        try:
            fields = ["gid","status","totalLength","completedLength","downloadSpeed","files","bittorrent"]

            active  = _aria2_call("aria2.tellActive",   [fields]).get("result", [])
            waiting = _aria2_call("aria2.tellWaiting",  [0, 100, fields]).get("result", [])
            stopped = _aria2_call("aria2.tellStopped",  [0, 100, fields]).get("result", [])

            def enrich(row):
                row = dict(row)
                row["name"] = _infer_name(row.get("files") or [], row.get("bittorrent") or {})
                total = int(row.get("totalLength") or 0)
                row["isMetadata"] = (total == 0)
                return row

            progress = [enrich(r) for r in (active + waiting) if r.get("status") in ("active","waiting","paused")]
            completed_rows = [enrich(r) for r in stopped if r.get("status") == "complete"]
            if completed_rows:
                _record_history(completed_rows)

            socketio.emit("torrent_status", {"progress": progress})
        except Exception:
            pass
        socketio.sleep(2)

def _ytdl_task():
    while True:
        # Emit active tasks to the UI
        active_tasks = {k: v for k, v in YTDL_TASKS.items() if v.get('status') not in ['finished', 'error']}
        socketio.emit("ytdl_status", {"progress": list(active_tasks.values())})
        
        # Clean up finished/errored tasks from memory
        finished_ids = [task_id for task_id, data in YTDL_TASKS.items() if data.get('status') in ['finished', 'error']]
        
        if finished_ids:
            socketio.sleep(5) # Give UI a moment to show 100% before it disappears
            for task_id in finished_ids:
                if task_id in YTDL_TASKS:
                    del YTDL_TASKS[task_id]

        socketio.sleep(2)

_started = False
@app.before_request
def _start_tasks_once():
    global _started
    if not _started:
        socketio.start_background_task(_stats_task)
        socketio.start_background_task(_torrent_task)
        socketio.start_background_task(_ytdl_task)
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS youtubedl_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT,
                dest TEXT NOT NULL,
                audio_only INTEGER,
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

# ==================== Daily Notes App ====================

NOTES_DIR = 'daily_notes'
KST = ZoneInfo("Asia/Seoul")

def get_today_note_path(date_obj=None):
    """Gets the file path for a given or today's note in KST."""
    if date_obj is None:
        date_obj = datetime.now(KST)
    
    today_str = date_obj.strftime('%Y-%m-%d')
    filename = f"note_{today_str}.txt"
    # Make sure this path is inside a mounted volume if using Docker
    return os.path.join(NOTES_DIR, filename)

@app.route('/notes')
@auth_required_json
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
    
    today_str = datetime.now(KST).strftime('%Y-%m-%d')
    
    note_files = glob.glob(os.path.join(NOTES_DIR, 'note_*.txt'))
    
    # Extract YYYY-MM-DD from 'daily_notes/note_YYYY-MM-DD.txt'
    dates = [os.path.basename(f)[5:-4] for f in note_files]
    
    dates.add(today_str)
    # Sort descending (newest first)
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
    except (ValueError, FileNotFoundError):
        return jsonify({'error': 'Invalid date or note not found'}), 404

@app.route('/notes/api/save/<string:date_str>', methods=['POST'])
@auth_required_json
def notes_save(date_str):
    """Saves the content for a specific note."""
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
        filename = f"note_{date_str}.txt"
        note_path = os.path.join(NOTES_DIR, filename)
        
        data = request.get_json()
        if data is None:
            return jsonify({'status': 'error', 'message': 'Invalid data'}), 400
            
        os.makedirs(NOTES_DIR, exist_ok=True)
        with open(note_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
            
        return jsonify({'status': 'success', 'message': f'Note for {date_str} saved!'})
    except ValueError:
        return jsonify({'error': 'Invalid date format'}), 400

# ==================== Admin ====================
@app.get('/admin')
def admin_page():
    return render_template('admin.html', app_name=APP_NAME)

@app.post('/admin/services/toggle')
@auth_required_json
def toggle_service():
    data = request.get_json(force=True, silent=True) or {}
    friendly = data.get('service')
    desired = bool(data.get('state'))
    if friendly == 'drive':
        global DRIVE_ENABLED
        DRIVE_ENABLED = desired
        return jsonify({'ok': True, 'service': 'drive', 'status': DRIVE_ENABLED})
    unit = SERVICE_MAP.get(friendly)
    if not unit:
        return jsonify({'ok': False, 'error': f"Unknown service '{friendly}'"}), 400
    action = 'start' if desired else 'stop'
    p = _systemctl_cmd(action, unit)
    if p.returncode != 0:
        msg = (p.stderr or p.stdout).decode(errors='ignore').strip() or f'Failed to {action} {friendly}'
        return jsonify({'ok': False, 'error': msg}), 500
    return jsonify({'ok': True, 'service': friendly, 'status': get_service_status(unit)})

@app.get('/admin/services/list')
@auth_required_json
def list_services():
    services = {k: get_service_status(v) for k, v in SERVICE_MAP.items()}
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
    audio_only = bool(data.get('audio_only'))
    if not link:
        abort(400, 'Missing link')

    dpath = _safe_join_download(dest)
    dpath.mkdir(parents=True, exist_ok=True)
    
    task_id = secrets.token_hex(8)

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO youtubedl_history(name, url, dest, audio_only, added_at)
               VALUES (?,?,?,?,?)""",
            ("Fetching title...", link, dest, 1 if audio_only else 0, int(time.time()))
        )
        conn.commit()
        db_id = cursor.lastrowid

    thread = threading.Thread(target=run_youtubedl, args=(link, dpath, audio_only, task_id, db_id))
    thread.start()
    return jsonify({'ok': True, 'result': {'message': 'Video download started in the background.'}})

@app.get('/admin/youtubedl/history')
@auth_required_json
def ytdl_history():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT id, name, url, dest, audio_only, added_at, completed_at
            FROM youtubedl_history ORDER BY COALESCE(completed_at, added_at) DESC LIMIT 500
        """).fetchall()
    out = []
    for r in rows:
        out.append({'id': r[0], 'name': r[1], 'url': r[2], 'dest': r[3],
                    'audio_only': bool(r[4]), 'added_at': r[5], 'completed_at': r[6]})
    return jsonify({'ok': True, 'history': out})

@app.post('/admin/youtubedl/history/delete')
@auth_required_json
def ytdl_history_delete():
    data = request.get_json(force=True, silent=True) or {}
    hid = data.get('id')
    if hid is None:
        abort(400, 'Missing history id')
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM youtubedl_history WHERE id=?", (hid,))
        conn.commit()
    return jsonify({'ok': True})

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
        return send_file(target, as_attachment=True, download_name=target.name)

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

# ==================== JSON error formatting ====================
@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(410)
@app.errorhandler(413)
@app.errorhandler(500)
def json_errors(err):
    code = getattr(err, 'code', 500)
    return jsonify({'ok': False, 'error': getattr(err, 'description', str(err)), 'code': code}), code

# ==================== Main ====================
if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)