import os
import subprocess
import sqlite3
import secrets
import time
import mimetypes
import shutil
from pathlib import Path
from functools import wraps

from flask import Flask, render_template, request, jsonify, send_file, abort, session
from flask_socketio import SocketIO
import pam
import psutil
from werkzeug.utils import secure_filename

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

DRIVE_ENABLED = True  # toggle controlled by admin

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
    return {'cpu_percent': float(cpu), 'memory_percent': float(mem)}

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

# ==================== Background stats ====================
def _stats_task():
    while True:
        socketio.emit('system_stats', get_system_stats())
        socketio.sleep(2)

_started = False
@app.before_request
def _start_stats_once():
    global _started
    if not _started:
        socketio.start_background_task(_stats_task)
        _started = True

# ==================== Share DB ====================
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
        conn.commit()
_db_init()

# ==================== Auth endpoints ====================
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

# ==================== Admin endpoints ====================
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

# ==================== Drive page & APIs ====================
@app.get('/')
def drive_root():
    if not DRIVE_ENABLED:
        return "<h1 style='text-align:center;margin-top:20%'>🚫 Drive is disabled by admin</h1>", 503
    return render_template('index.html', app_name=APP_NAME)

# ==================== Drive page & APIs (auth) ====================
@app.get('/')
def drive_page():
    # Root serves the Drive UI. Login overlay if not authenticated.
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
