from __future__ import annotations
import hashlib, html, json, mimetypes, os, shutil, sqlite3, subprocess, threading, time, uuid, sys, stat
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlparse, parse_qs

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config as BotoConfig
from flask import Flask, jsonify, render_template, request
import keyring
import requests
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = json.loads((ROOT / "config.example.json").read_text())
SERVICE_NAME = "WiredriveWatch"

if sys.platform == "darwin":
    DATA_DIR = Path.home() / "Library" / "Application Support" / "Wiredrive Sync"
else:
    DATA_DIR = Path.home() / ".wiredrive-sync"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "watchfolder.db"

app = Flask(__name__)

@app.route("/favicon.ico")
def favicon():
    return app.send_static_file("favicon.ico")
observers: dict[str, Observer] = {}
workers_started = False
sync_worker_started = False
reconcile_lock = threading.Lock()
reconciling_mappings: set[str] = set()
remote_inventory_cache = {}
remote_inventory_cache_lock = threading.Lock()


def now():
    return datetime.now().isoformat(timespec="seconds")


def _previous_state_candidates():
    """Find older extracted builds so first 1.5.6 launch can inherit mappings/state."""
    roots=[]
    for base in (ROOT.parent, Path.home()/"Desktop", Path.home()/"Downloads"):
        try:
            if base.exists() and base not in roots:
                roots.append(base)
        except Exception:
            pass
    candidates=[]
    seen=set()
    for base in roots:
        try:
            for d in base.iterdir():
                if not d.is_dir() or d.resolve()==ROOT.resolve():
                    continue
                lname=d.name.lower()
                if "wiredrive" not in lname or ("sync" not in lname and "watch" not in lname):
                    continue
                cfg=d/"config.json"
                dbp=d/"watchfolder.db"
                if not cfg.exists() and not dbp.exists():
                    continue
                key=str(d.resolve())
                if key in seen:
                    continue
                seen.add(key)
                stamp=max(
                    cfg.stat().st_mtime if cfg.exists() else 0,
                    dbp.stat().st_mtime if dbp.exists() else 0,
                )
                candidates.append((stamp,d,cfg,dbp))
        except Exception:
            continue
    return sorted(candidates,key=lambda x:x[0],reverse=True)


def migrate_previous_state():
    """Move runtime state out of the version folder. Best effort and one-time only."""
    if CONFIG_PATH.exists() or DB_PATH.exists():
        return
    local_cfg=ROOT/"config.json"
    local_db=ROOT/"watchfolder.db"
    source=None
    if local_cfg.exists() or local_db.exists():
        source=(0,ROOT,local_cfg,local_db)
    else:
        candidates=_previous_state_candidates()
        if candidates:
            source=candidates[0]
    if not source:
        return
    _,src_dir,src_cfg,src_db=source
    try:
        if src_cfg.exists() and not CONFIG_PATH.exists():
            shutil.copy2(src_cfg,CONFIG_PATH)
        if src_db.exists() and not DB_PATH.exists():
            shutil.copy2(src_db,DB_PATH)
        (DATA_DIR/"migration.txt").write_text(f"Migrated from {src_dir}\\n{now()}\\n")
    except Exception:
        pass


migrate_previous_state()


def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
    cfg = json.loads(CONFIG_PATH.read_text())
    # gentle migration from prototype config
    if "wiredrive" not in cfg:
        cfg["wiredrive"] = DEFAULT_CONFIG["wiredrive"].copy()
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def secret_set(name: str, value: str):
    if value:
        keyring.set_password(SERVICE_NAME, name, value)
    else:
        try: keyring.delete_password(SERVICE_NAME, name)
        except Exception: pass


def secret_get(name: str) -> str:
    try: return keyring.get_password(SERVICE_NAME, name) or ""
    except Exception: return ""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        c.execute("""CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, watch_id TEXT, path TEXT, filename TEXT,
          destination TEXT, size INTEGER DEFAULT 0, status TEXT,
          progress INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0,
          message TEXT DEFAULT '', remote_asset_id TEXT DEFAULT '',
          created_at TEXT, updated_at TEXT
        )""")
        # migrate older prototype DB
        cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
        if "remote_asset_id" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN remote_asset_id TEXT DEFAULT ''")
        if "direction" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN direction TEXT DEFAULT 'upload'")
        if "remote_modified" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN remote_modified TEXT DEFAULT ''")
        c.execute("""CREATE TABLE IF NOT EXISTS sync_assets (
          watch_id TEXT NOT NULL,
          remote_asset_id TEXT NOT NULL,
          remote_filename TEXT DEFAULT '',
          remote_size INTEGER DEFAULT 0,
          remote_modified TEXT DEFAULT '',
          remote_folder_id TEXT DEFAULT '',
          local_path TEXT DEFAULT '',
          local_size INTEGER DEFAULT 0,
          local_mtime REAL DEFAULT 0,
          status TEXT DEFAULT '',
          last_sync TEXT DEFAULT '',
          PRIMARY KEY (watch_id, remote_asset_id)
        )""")
        sacols={r[1] for r in c.execute("PRAGMA table_info(sync_assets)")}
        if "remote_folder_id" not in sacols:
            c.execute("ALTER TABLE sync_assets ADD COLUMN remote_folder_id TEXT DEFAULT ''")
        jcols={r[1] for r in c.execute("PRAGMA table_info(jobs)")}
        if "remote_folder_id" not in jcols:
            c.execute("ALTER TABLE jobs ADD COLUMN remote_folder_id TEXT DEFAULT ''")
        jcols={r[1] for r in c.execute("PRAGMA table_info(jobs)")}
        for col, ddl in (
            ("bytes_transferred","INTEGER DEFAULT 0"),
            ("transfer_speed","REAL DEFAULT 0"),
            ("eta_seconds","REAL DEFAULT 0"),
            ("transfer_started_at","REAL DEFAULT 0"),
            ("priority_time","REAL DEFAULT 0")
        ):
            if col not in jcols:
                c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
        c.execute("""CREATE TABLE IF NOT EXISTS sync_folders (
          watch_id TEXT NOT NULL,
          local_relative_path TEXT NOT NULL,
          remote_folder_id TEXT NOT NULL,
          remote_folder_name TEXT DEFAULT '',
          last_sync TEXT DEFAULT '',
          PRIMARY KEY (watch_id, local_relative_path)
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_watch_path_status ON jobs(watch_id,path,status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_direction_status_priority ON jobs(direction,status,priority_time)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sync_assets_watch_path_status ON sync_assets(watch_id,local_path,status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sync_assets_watch_remote ON sync_assets(watch_id,remote_asset_id)")
        c.commit()


def priority_timestamp(value=None, fallback=None):
    """Convert local/remote modification values to an epoch timestamp for queue priority."""
    if value not in (None,""):
        try:
            n=float(value)
            # Wiredrive may provide seconds or milliseconds.
            if n > 100000000000:
                n /= 1000.0
            return n
        except (TypeError,ValueError):
            try:
                dt=datetime.fromisoformat(str(value).replace("Z","+00:00"))
                return dt.timestamp()
            except Exception:
                pass
    return float(fallback if fallback is not None else time.time())


def ignored_local_path(path: Path, root: Path | None = None):
    """True for filesystem metadata/hidden content that must never be synced."""
    p = Path(path)

    try:
        if root is not None:
            parts = p.resolve().relative_to(Path(root).expanduser().resolve()).parts
        else:
            parts = (p.name,)
    except Exception:
        parts = (p.name,)

    # AppleDouble sidecars are the primary macOS case:
    #   TAH26_COLOUR___173_D
    #   ._TAH26_COLOUR___173_D   <-- metadata sidecar, never media
    #
    # We deliberately ignore all dot-prefixed path components as well, covering
    # .DS_Store, .Spotlight-V100, .Trashes, hidden folders, etc.
    if any(part.startswith(".") and part not in (".", "..") for part in parts):
        return True

    # Finder can mark items hidden without a dot prefix.
    if sys.platform == "darwin":
        try:
            flags = getattr(p.stat(), "st_flags", 0)
            uf_hidden = getattr(stat, "UF_HIDDEN", 0)
            if uf_hidden and (flags & uf_hidden):
                return True
        except (FileNotFoundError, OSError):
            pass

    return False


def ignored_remote_asset(asset):
    name = str(asset.get("filename") or "")
    if name.startswith("."):
        return True
    rel = Path(str(asset.get("relative_dir") or ""))
    return any(part.startswith(".") and part not in (".", "..") for part in rel.parts)


def allowed(path: Path, cfg):
    exts = cfg.get("allowed_extensions", [])
    return not exts or path.suffix.lower() in [x.lower() for x in exts]


def enqueue(path: str, watch_id: str, destination: str):
    p = Path(path)
    if not p.is_file(): return
    cfg = load_config()
    watch = watch_for_job(cfg, watch_id)
    root = Path(watch["path"]).expanduser() if watch and watch.get("path") else None
    if ignored_local_path(p, root): return
    if not allowed(p, cfg): return
    try:
        pst=p.stat()
    except FileNotFoundError:
        return
    with db() as c:
        # Fast path: unchanged files already reconciled in a prior run never
        # enter the stability/upload queue again.
        synced = c.execute(
            """SELECT local_size,local_mtime,status FROM sync_assets
               WHERE watch_id=? AND local_path=? AND status IN ('downloading','complete')
               ORDER BY last_sync DESC LIMIT 1""",
            (watch_id, str(p)),
        ).fetchone()
        if synced:
            same_size=int(synced["local_size"] or 0)==int(pst.st_size)
            same_mtime=abs(float(synced["local_mtime"] or 0)-float(pst.st_mtime)) < 0.001
            if same_size and (same_mtime or synced["status"]=="downloading"):
                return
        exists = c.execute(
            "SELECT id FROM jobs WHERE watch_id=? AND path=? AND status IN ('waiting','ready','uploading','complete')",
            (watch_id, str(p)),
        ).fetchone()
        if exists: return
        jid = str(uuid.uuid4())
        c.execute("""INSERT INTO jobs
          (id,watch_id,path,filename,destination,size,status,progress,attempts,message,
           remote_asset_id,created_at,updated_at,direction,priority_time)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            jid, watch_id, str(p), p.name, destination, pst.st_size,
            "waiting", 0, 0, "Waiting for file to finish writing", "",
            now(), now(), "upload", float(pst.st_mtime)))
        c.commit()


class Handler(FileSystemEventHandler):
    def __init__(self, watch_id, destination):
        self.watch_id, self.destination = watch_id, destination
    def on_created(self, event):
        if not event.is_directory: enqueue(event.src_path, self.watch_id, self.destination)
    def on_moved(self, event):
        if not event.is_directory: enqueue(event.dest_path, self.watch_id, self.destination)


def stop_observers():
    global observers
    for ob in list(observers.values()):
        ob.stop(); ob.join(timeout=2)
    observers = {}


def start_observers():
    global observers
    stop_observers()
    cfg = load_config()
    for w in cfg.get("watch_folders", []):
        if not w.get("enabled"): continue
        direction = w.get("direction", "upload")
        if direction not in ("upload", "two_way"):
            continue
        raw = (w.get("path") or "").strip()
        if not raw: continue
        path = Path(raw).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        label = w.get("destination_label") or f"Project {w.get('project_id','?')} / Folder {w.get('folder_id','?')}"
        recursive=bool(w.get("include_subfolders", True))
        ob = Observer(); ob.schedule(Handler(w["id"], label), str(path), recursive=recursive); ob.start()
        observers[w["id"]] = ob
        start_fast_reconcile(w, label)


def file_stable(path: Path, seconds: int) -> bool:
    try:
        s1 = path.stat(); time.sleep(seconds); s2 = path.stat()
        return s1.st_size == s2.st_size and s1.st_mtime == s2.st_mtime
    except FileNotFoundError:
        return False


def md5_hex(path: Path, progress=None) -> str:
    h = hashlib.md5()
    total = max(path.stat().st_size, 1); read = 0
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk); read += len(chunk)
            if progress: progress(min(8, int(read * 8 / total)))
    return h.hexdigest()


def parse_expiration(value: str):
    if not value: return None
    try: return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception: return None


def _seconds_to_expiry(value: str):
    exp = parse_expiration(value)
    if not exp: return None
    return (exp - datetime.now(timezone.utc)).total_seconds()


def apply_upload_config(cfg, data):
    """Persist non-secret Wiredrive upload config and put temporary AWS credentials in Keychain."""
    wd = cfg.setdefault("wiredrive", {})
    for secret_name, field in (("aws_access_key", "accessKey"), ("aws_secret_key", "secretKey"), ("aws_session_token", "sessionToken")):
        if data.get(field): secret_set(secret_name, str(data[field]))
    mappings = {
        "user_id": data.get("userId", wd.get("user_id", "")),
        "client_code": data.get("clientCode", wd.get("client_code", "wsl")),
        "api_version": data.get("apiVersion", wd.get("api_version", "2006-03-01")),
        "region": data.get("region", wd.get("region", "us-west-2")),
        "version": data.get("version", wd.get("version", "latest")),
        "signature_version": data.get("signatureVersion", wd.get("signature_version", "v4")),
        "refresh_interval": int(data.get("refreshInterval", wd.get("refresh_interval", 3300000))),
        "bucket_name": data.get("bucketName", wd.get("bucket_name", "wiredrive-upload")),
        "asset_path_prefix": data.get("assetPathPrefix", wd.get("asset_path_prefix", "/st7/wiredrive/files/wiredrive.com/")),
        "part_size": int(data.get("partSize", wd.get("part_size", 5242880))),
        "queue_size": int(data.get("queueSize", wd.get("queue_size", 4))),
        "upload_max_size": int(data.get("uploadMaxSize", wd.get("upload_max_size", 5368709120))),
        "handler": data.get("handler", wd.get("handler", "S3Handler")),
        "expiration": data.get("expiration", wd.get("expiration", "")),
        "upload_hash": data.get("hash", wd.get("upload_hash", "")),
        "upload_date": data.get("date", wd.get("upload_date", "")),
        "is_library_available": bool(data.get("isLibraryAvailable", wd.get("is_library_available", True))),
        "is_project_available": bool(data.get("isProjectAvailable", wd.get("is_project_available", True))),
    }
    wd.update(mappings)
    return cfg


def wiredrive_login(cfg):
    """Create an authenticated Wiredrive session using the login flow captured in the HAR."""
    wd = cfg.get("wiredrive", {})
    base = (wd.get("site_url") or "https://wsl.wiredrive.com").rstrip("/")
    username = secret_get("wiredrive_username")
    password = secret_get("wiredrive_password")
    client_code = wd.get("client_code") or "wsl"
    if not username or not password:
        raise RuntimeError("Wiredrive login required. Use Connect to Wiredrive first.")

    sess = requests.Session()
    sess.headers.update({"User-Agent": "WiredriveWatch/0.4"})
    # Establish any cookies/CSRF state the login app expects.
    login_page = sess.get(base + "/apps/login?next=%2F", timeout=30)
    if not login_page.ok:
        raise RuntimeError(f"Wiredrive login page failed: HTTP {login_page.status_code}")

    csrf = sess.cookies.get("csrftoken") or sess.cookies.get("csrf") or ""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": base,
        "Referer": base + "/apps/login?next=%2F",
    }
    if csrf:
        headers["X-CSRFToken"] = csrf

    r = sess.post(
        base + "/auth/api-token-auth",
        json={"username": username, "client_code": client_code, "password": password},
        headers=headers,
        timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Wiredrive login failed: HTTP {r.status_code}")
    auth = r.headers.get("Authorization", "")
    if not auth:
        raise RuntimeError("Wiredrive login succeeded but did not return an Authorization token.")
    secret_set("wiredrive_jwt", auth)
    # Preserve cookies too, in case a deployment requires both JWT and session state.
    cookie = "; ".join(f"{c.name}={c.value}" for c in sess.cookies)
    if cookie:
        secret_set("wiredrive_cookie", cookie)
    return auth, cookie


def refresh_upload_config(cfg, force=False):
    """Refresh Wiredrive's temporary S3 session using an authenticated Wiredrive login."""
    wd = cfg.get("wiredrive", {})
    remaining = _seconds_to_expiry(wd.get("expiration", ""))
    have = all(secret_get(x) for x in ("aws_access_key", "aws_secret_key", "aws_session_token"))
    if not force and have and remaining is not None and remaining > 300:
        return cfg, False

    base = (wd.get("site_url") or "https://wsl.wiredrive.com").rstrip("/")
    url = base + "/?routekey=get-upload-config"
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": base + "/app/upload/",
    }

    auth = secret_get("wiredrive_jwt")
    cookie = secret_get("wiredrive_cookie")
    if auth:
        headers["Authorization"] = auth
    if cookie:
        headers["Cookie"] = cookie

    r = requests.get(url, headers=headers, timeout=30, allow_redirects=False)
    # Expired/missing Wiredrive auth commonly redirects to login or returns HTML.
    content_type = (r.headers.get("Content-Type") or "").lower()
    needs_login = r.status_code in (301, 302, 401, 403) or "text/html" in content_type
    if needs_login:
        auth, cookie = wiredrive_login(cfg)
        headers["Authorization"] = auth
        if cookie:
            headers["Cookie"] = cookie
        r = requests.get(url, headers=headers, timeout=30, allow_redirects=False)

    if not r.ok:
        raise RuntimeError(f"Wiredrive credential refresh failed: HTTP {r.status_code}")
    content_type = (r.headers.get("Content-Type") or "").lower()
    if "text/html" in content_type:
        raise RuntimeError("Wiredrive login required; upload-config returned the login page.")

    try:
        js = r.json()
    except Exception:
        raise RuntimeError("Wiredrive upload-config did not return JSON. Reconnect to Wiredrive.")

    if js.get("code") != 200 or not isinstance(js.get("data"), dict):
        raise RuntimeError(f"Wiredrive credential refresh error: {js.get('message') or 'unexpected response'}")
    data = js["data"]
    missing = [x for x in ("accessKey", "secretKey", "sessionToken", "expiration", "hash", "date", "userId") if not data.get(x)]
    if missing:
        raise RuntimeError("Wiredrive credential refresh response is missing: " + ", ".join(missing))
    apply_upload_config(cfg, data)
    save_config(cfg)
    return cfg, True

def credentials_status(cfg):
    wd = cfg.get("wiredrive", {})
    exp = wd.get("expiration", "")
    remaining = _seconds_to_expiry(exp)
    have = all(secret_get(x) for x in ("aws_access_key", "aws_secret_key", "aws_session_token"))
    if not have:
        return {"ready": False, "message": "Wiredrive upload credentials not initialized", "expiration": exp, "auto_refresh": True}
    if remaining is not None and remaining <= 0:
        return {"ready": False, "message": "Upload credentials expired; automatic refresh will run before the next upload", "expiration": exp, "auto_refresh": True}
    return {"ready": True, "message": "Wiredrive connected — credentials refresh automatically", "expiration": exp, "auto_refresh": True}


def watch_for_job(cfg, watch_id):
    for w in cfg.get("watch_folders", []):
        if w.get("id") == watch_id: return w
    return None



def _remote_cache_key(cfg, watch, folder_id):
    return (
        str(watch.get("project_id") or cfg.get("wiredrive",{}).get("project_id") or ""),
        str(folder_id or ""),
    )


def cached_remote_inventory(cfg, watch, folder_id, ttl=12):
    key=_remote_cache_key(cfg,watch,folder_id)
    now_mono=time.monotonic()
    with remote_inventory_cache_lock:
        cached=remote_inventory_cache.get(key)
        if cached and now_mono-cached[0] <= ttl:
            return cached[1]
    probe=dict(watch)
    probe["folder_id"]=str(folder_id)
    inv=fetch_remote_inventory(cfg,probe)
    with remote_inventory_cache_lock:
        remote_inventory_cache[key]=(now_mono,inv)
    return inv


def invalidate_remote_inventory(cfg, watch, folder_id):
    key=_remote_cache_key(cfg,watch,folder_id)
    with remote_inventory_cache_lock:
        remote_inventory_cache.pop(key,None)


def find_remote_equivalent(cfg, watch, folder_id, local_path):
    """Return an existing remote asset when path identity matches filename + byte size."""
    local_path=Path(local_path)
    inv=cached_remote_inventory(cfg,watch,folder_id)
    size=local_path.stat().st_size
    matches=[a for a in inv.get("assets",[]) if a.get("filename")==local_path.name and int(a.get("size") or 0)==size]
    if not matches:
        return None
    # Stable identity is asset ID; if duplicates somehow exist, use the newest/last-listed one.
    asset=matches[-1]
    asset["remote_folder_id"]=str(folder_id)
    return asset

class WiredriveUploader:
    def __init__(self, cfg): self.cfg = cfg

    def upload(self, job, progress):
        mode = self.cfg.get("uploader_mode", "wiredrive")
        path = Path(job["path"])
        watch = watch_for_job(self.cfg, job.get("watch_id"))
        root = Path(watch["path"]).expanduser() if watch and watch.get("path") else None
        if ignored_local_path(path, root):
            raise RuntimeError("Hidden/system metadata file ignored by Wiredrive Sync")
        if mode == "simulation":
            total=max(path.stat().st_size,1); sent=0; sim_start=time.monotonic()
            with path.open("rb") as f:
                for chunk in iter(lambda:f.read(4*1024*1024), b""):
                    sent += len(chunk)
                    elapsed=max(time.monotonic()-sim_start,0.001)
                    speed=sent/elapsed
                    eta=max(total-sent,0)/speed if speed>0 else 0
                    progress(min(99,int(sent*100/total)),sent,speed,eta); time.sleep(.05)
            progress(100,total,0,0); return {"message":"Simulation complete", "asset_id":""}
        if mode != "wiredrive": raise RuntimeError(f"Unknown uploader mode: {mode}")
        return self.upload_wiredrive(job, progress)

    def upload_wiredrive(self, job, progress):
        path = Path(job["path"])
        try:
            self.cfg, _ = refresh_upload_config(self.cfg)
        except Exception as refresh_error:
            # If refresh fails but the cached temporary credentials are still valid, continue with them.
            status = credentials_status(self.cfg)
            if not status["ready"]: raise RuntimeError(str(refresh_error))
        status = credentials_status(self.cfg)
        if not status["ready"]: raise RuntimeError(status["message"])
        wd = self.cfg.get("wiredrive", {})
        watch = watch_for_job(self.cfg, job["watch_id"])
        if not watch: raise RuntimeError("Watch-folder mapping no longer exists")
        project_id = str(watch.get("project_id") or wd.get("project_id") or "")
        folder_id = str(watch.get("folder_id") or wd.get("folder_id") or "")
        if not project_id or not folder_id: raise RuntimeError("Project ID and Folder ID are required")

        try:
            rel_dir=path.parent.resolve().relative_to(Path(watch["path"]).expanduser().resolve())
        except Exception:
            rel_dir=Path(".")
        if bool(watch.get("include_subfolders",True)) and str(rel_dir) not in ("","."):
            progress(1)
            folder_id=ensure_remote_relative_path(self.cfg,watch,rel_dir)

        # True-sync reconciliation: never upload a second copy when the corresponding
        # Wiredrive folder already contains the same filename and byte size.
        progress(2)
        existing=find_remote_equivalent(self.cfg,watch,folder_id,path)
        if existing:
            return {
                "message":"Already present in Wiredrive — synchronized without upload",
                "asset_id":str(existing.get("asset_id") or ""),
                "remote_asset":existing,
                "skipped":True,
                "remote_folder_id":str(folder_id),
            }

        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        size = path.stat().st_size
        progress(3)
        access = secret_get("aws_access_key"); secret = secret_get("aws_secret_key"); token = secret_get("aws_session_token")

        base = (wd.get("site_url") or "https://wsl.wiredrive.com").rstrip("/")
        create_url = base + "/?routekey=asset"
        data = {
            "action":"upload", "name":path.name, "type":mime, "operation":"create",
            "hash":wd.get("upload_hash",""),
            "date":wd.get("upload_date",""),
            "userId":str(wd.get("user_id", "")),
            "isLibraryAvailable":str(wd.get("is_library_available", True)).lower(),
            "isProjectAvailable":str(wd.get("is_project_available", True)).lower(),
            "clientCode":wd.get("client_code","wsl"), "apiVersion":wd.get("api_version","2006-03-01"),
            "region":wd.get("region","us-west-2"), "version":wd.get("version","latest"),
            "signatureVersion":wd.get("signature_version","v4"), "refreshInterval":str(wd.get("refresh_interval",3300000)),
            "bucketName":wd.get("bucket_name","wiredrive-upload"),
            "assetPathPrefix":wd.get("asset_path_prefix","/st7/wiredrive/files/wiredrive.com/"),
            "partSize":str(wd.get("part_size",5242880)), "queueSize":str(wd.get("queue_size",4)),
            "uploadMaxSize":str(wd.get("upload_max_size",5368709120)), "handler":wd.get("handler","S3Handler"),
            "accessKey":access, "secretKey":secret, "sessionToken":token, "expiration":wd.get("expiration", ""),
            "package":"project", "projectId":project_id, "folderData[targetId]":folder_id,
        }
        required_create = {
            "userId": data.get("userId"),
            "hash": data.get("hash"),
            "date": data.get("date"),
            "projectId": data.get("projectId"),
            "folderData[targetId]": data.get("folderData[targetId]"),
        }
        missing_create=[k for k,v in required_create.items() if not str(v or "").strip()]
        if missing_create:
            raise RuntimeError("Wiredrive create-session is missing upload-config fields: " + ", ".join(missing_create))

        sess = requests.Session()
        cookie = secret_get("wiredrive_cookie")
        auth = secret_get("wiredrive_jwt")
        headers = {
            "X-Requested-With":"XMLHttpRequest",
            "Referer":base+"/app/upload/",
            "Origin":base,
            "Accept":"application/json, text/javascript, */*; q=0.01",
        }
        if cookie: headers["Cookie"] = cookie
        if auth: headers["Authorization"] = auth
        r = sess.post(create_url, data=data, headers=headers, timeout=60)
        if not r.ok:
            server_msg=re.sub(r"<[^>]+>", " ", r.text or "")
            server_msg=" ".join(server_msg.split())[:220]
            raise RuntimeError(f"Wiredrive create-session failed: HTTP {r.status_code}" + (f" — {server_msg}" if server_msg else ""))
        try: js = r.json()
        except Exception: raise RuntimeError(f"Wiredrive create-session returned invalid JSON: {r.text[:240]}")
        if js.get("code") != 200: raise RuntimeError(f"Wiredrive create-session error: {js.get('message') or js}")
        info = js.get("data") or {}
        upload_path = info.get("uploadUrl"); object_key = info.get("objectKey"); finalize_token = info.get("token"); asset_id = info.get("assetId")
        if not all([upload_path, object_key, finalize_token]): raise RuntimeError("Wiredrive create-session response is missing upload fields")

        key = "/".join([wd.get("asset_path_prefix","/st7/wiredrive/files/wiredrive.com/").strip("/"), upload_path.strip("/"), object_key])
        bucket = wd.get("bucket_name","wiredrive-upload")
        boto = boto3.client("s3", region_name=wd.get("region","us-west-2"), aws_access_key_id=access,
            aws_secret_access_key=secret, aws_session_token=token,
            config=BotoConfig(signature_version="s3v4", s3={"use_accelerate_endpoint": True}))
        transfer_cfg = TransferConfig(multipart_threshold=int(wd.get("part_size",5242880)), multipart_chunksize=int(wd.get("part_size",5242880)), max_concurrency=int(wd.get("queue_size",4)), use_threads=True)
        sent = {"n":0}; plock = threading.Lock(); transfer_start=time.monotonic()
        def cb(n):
            with plock:
                sent["n"] += n
                elapsed=max(time.monotonic()-transfer_start,0.001)
                speed=sent["n"]/elapsed
                remaining=max(size-sent["n"],0)
                eta=(remaining/speed) if speed>0 else 0
                pct=min(97, 8 + int(sent["n"] * 89 / max(size,1)))
                progress(pct,sent["n"],speed,eta)
        boto.upload_file(str(path), bucket, key, ExtraArgs={"ContentType":mime}, Callback=cb, Config=transfer_cfg)

        finalize_url = base + "/?routekey=upload-asset-node&token=" + requests.utils.quote(str(finalize_token), safe="")
        finalize = {"folderId":folder_id, "_content_type":mime, "_name":path.name, "_path":upload_path,
                    "_key":object_key, "_crc32":"", "_md5":"", "_size":str(size)}
        fr = sess.post(finalize_url, data=finalize, headers=headers, timeout=60)
        if not fr.ok: raise RuntimeError(f"Wiredrive finalize failed: HTTP {fr.status_code} {fr.text[:240]}")
        try: fjs = fr.json()
        except Exception: raise RuntimeError(f"Wiredrive finalize returned invalid JSON: {fr.text[:240]}")
        if fjs.get("code") != 200: raise RuntimeError(f"Wiredrive finalize error: {fjs.get('message') or fjs}")
        progress(100,size,0,0)
        final_asset_id=str((fjs.get("data") or {}).get("assetId") or asset_id or "")
        invalidate_remote_inventory(self.cfg,watch,folder_id)
        return {
            "message":"Uploaded to Wiredrive",
            "asset_id":final_asset_id,
            "remote_asset":{
                "asset_id":final_asset_id,
                "filename":path.name,
                "size":size,
                "modified":"",
                "remote_folder_id":str(folder_id),
            },
            "remote_folder_id":str(folder_id),
            "skipped":False,
        }



def wiredrive_headers(cfg, referer=None):
    wd=cfg.get("wiredrive",{})
    base=(wd.get("site_url") or "https://wsl.wiredrive.com").rstrip("/")
    headers={
        "User-Agent":"WiredriveSync/1.0",
        "Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer":referer or base + "/projects/",
    }
    auth=secret_get("wiredrive_jwt")
    cookie=secret_get("wiredrive_cookie")
    if auth: headers["Authorization"]=auth
    if cookie: headers["Cookie"]=cookie
    return headers


def _extract_js_object(page, marker="oRawData="):
    pos=page.find(marker)
    if pos < 0:
        raise RuntimeError("Wiredrive folder page did not contain oRawData inventory.")
    i=pos+len(marker)
    while i<len(page) and page[i].isspace(): i+=1
    if i>=len(page) or page[i] != "{":
        raise RuntimeError("Wiredrive oRawData inventory was not a JSON object.")
    start=i; depth=0; in_string=False; escape=False
    for j in range(i,len(page)):
        ch=page[j]
        if in_string:
            if escape: escape=False
            elif ch=="\\": escape=True
            elif ch=='"': in_string=False
            continue
        if ch=='"': in_string=True
        elif ch=="{": depth+=1
        elif ch=="}":
            depth-=1
            if depth==0:
                return json.loads(page[start:j+1])
    raise RuntimeError("Wiredrive oRawData inventory was incomplete.")



def _extract_js_json(page, var_name):
    """Extract a JSON-compatible JS variable embedded in Wiredrive's legacy project page."""
    patterns=(f"var {var_name}=", f"var {var_name} =", f"{var_name}=")
    pos=-1; marker=None
    for candidate in patterns:
        pos=page.find(candidate)
        if pos >= 0:
            marker=candidate; break
    if pos < 0:
        raise RuntimeError(f"Wiredrive page did not contain {var_name}.")
    i=pos+len(marker)
    while i<len(page) and page[i].isspace(): i+=1
    if i>=len(page) or page[i] not in "[{":
        raise RuntimeError(f"Wiredrive {var_name} was not JSON-compatible data.")
    opener=page[i]; closer='}' if opener=='{' else ']'
    start=i; depth=0; in_string=False; escape=False
    for j in range(i,len(page)):
        ch=page[j]
        if in_string:
            if escape: escape=False
            elif ch=='\\': escape=True
            elif ch=='"': in_string=False
            continue
        if ch=='"': in_string=True
        elif ch==opener: depth+=1
        elif ch==closer:
            depth-=1
            if depth==0:
                return json.loads(page[start:j+1])
    raise RuntimeError(f"Wiredrive {var_name} data was incomplete.")


def _wiredrive_get_page(cfg, url, referer=None):
    base=(cfg.get('wiredrive',{}).get('site_url') or 'https://wsl.wiredrive.com').rstrip('/')
    headers=wiredrive_headers(cfg, referer=referer or base+'/projects/')
    r=requests.get(url,headers=headers,timeout=45,allow_redirects=False)
    login_html='Wiredrive Login' in (r.text[:2500] if r.text else '')
    if r.status_code in (301,302,401,403) or login_html:
        wiredrive_login(cfg)
        headers=wiredrive_headers(cfg, referer=referer or base+'/projects/')
        r=requests.get(url,headers=headers,timeout=45,allow_redirects=False)
    if not r.ok:
        raise RuntimeError(f"Wiredrive browser request failed: HTTP {r.status_code}")
    return r.text


def fetch_wiredrive_projects(cfg):
    """Return projects visible to the authenticated Wiredrive user."""
    base=(cfg.get('wiredrive',{}).get('site_url') or 'https://wsl.wiredrive.com').rstrip('/')
    pages=[base+'/projects/']
    seed=''
    watches=cfg.get('watch_folders') or []
    if watches: seed=str(watches[0].get('project_id') or '')
    seed=seed or str(cfg.get('wiredrive',{}).get('project_id') or '')
    if seed: pages.append(f'{base}/projects/folder/{seed}/{seed}/')
    last_error=None
    for url in pages:
        try:
            page=_wiredrive_get_page(cfg,url)
            raw=_extract_js_json(page,'projectList')
            projects=[]
            for p in raw if isinstance(raw,list) else []:
                pid=str(p.get('node_id') or p.get('id') or '')
                name=str(p.get('node') or p.get('name') or '').strip()
                if pid and name:
                    projects.append({'id':pid,'name':name,'migrated':bool(p.get('migrated',False))})
            if projects:
                projects.sort(key=lambda x:x['name'].lower())
                return projects
        except Exception as e:
            last_error=e
    raise RuntimeError(f"Could not load Wiredrive project list: {last_error or 'projectList not found'}")


def fetch_wiredrive_folder_tree(cfg, project_id):
    project_id=str(project_id or '').strip()
    if not project_id: raise RuntimeError('Project ID is required.')
    base=(cfg.get('wiredrive',{}).get('site_url') or 'https://wsl.wiredrive.com').rstrip('/')
    url=f'{base}/projects/folder/{project_id}/{project_id}/'
    page=_wiredrive_get_page(cfg,url)
    tree=_extract_js_json(page,'oTreeContent')
    try: project_list=_extract_js_json(page,'projectList')
    except Exception: project_list=[]
    project_name=next((str(p.get('node') or '') for p in project_list if str(p.get('node_id') or '')==project_id),'')
    if not project_name:
        m=re.search(r'id="projectlable"[^>]*>(.*?)</a>',page,re.S|re.I)
        project_name=re.sub('<[^>]+>','',m.group(1)).strip() if m else f'Project {project_id}'
    folders=[]
    for f in (tree.get('folders') or [] if isinstance(tree,dict) else []):
        fid=str(f.get('id') or f.get('node_id') or '')
        parent=str(f.get('parent') or project_id)
        name=str(f.get('node') or f.get('name') or '').strip()
        if fid and name:
            folders.append({'id':fid,'parent':parent,'name':name,'count':int(f.get('count') or 0)})
    return {'project_id':project_id,'project_name':project_name,'root_id':project_id,'folders':folders,'source_url':url}


def _folder_maps(tree):
    folders=tree.get("folders") or []
    by_id={str(f["id"]):f for f in folders}
    children={}
    for f in folders:
        children.setdefault(str(f.get("parent") or tree.get("root_id") or ""),[]).append(f)
    for arr in children.values():
        arr.sort(key=lambda x:x.get("name","").lower())
    return by_id,children


def _relative_remote_folders(tree, selected_root_id):
    """Return selected root + descendants as (folder_id, relative_path, name)."""
    selected_root_id=str(selected_root_id)
    by_id,children=_folder_maps(tree)
    rows=[(selected_root_id, Path("."), by_id.get(selected_root_id,{}).get("name") or tree.get("project_name",""))]
    stack=[(selected_root_id,Path("."))]
    seen={selected_root_id}
    while stack:
        parent,rel=stack.pop()
        for f in reversed(children.get(str(parent),[])):
            fid=str(f["id"])
            if fid in seen: continue
            seen.add(fid)
            child_rel=rel/f["name"]
            rows.append((fid,child_rel,f["name"]))
            stack.append((fid,child_rel))
    return rows


def _record_folder_mapping(watch_id, rel_path, remote_folder_id, remote_name=""):
    rel="" if str(rel_path) in ("",".") else str(rel_path)
    with db() as c:
        c.execute("""INSERT INTO sync_folders
          (watch_id,local_relative_path,remote_folder_id,remote_folder_name,last_sync)
          VALUES (?,?,?,?,?)
          ON CONFLICT(watch_id,local_relative_path) DO UPDATE SET
            remote_folder_id=excluded.remote_folder_id,
            remote_folder_name=excluded.remote_folder_name,
            last_sync=excluded.last_sync
        """,(watch_id,rel,str(remote_folder_id),remote_name,now()))
        c.commit()


def create_remote_folder(cfg, project_id, parent_folder_id, folder_name):
    """Create one Wiredrive project folder using the legacy endpoint used by Wiredrive's UI."""
    project_id=str(project_id); parent_folder_id=str(parent_folder_id)
    folder_name=str(folder_name).strip()
    if not folder_name: raise RuntimeError("Cannot create a blank Wiredrive folder name.")
    base=(cfg.get("wiredrive",{}).get("site_url") or "https://wsl.wiredrive.com").rstrip("/")
    url=f"{base}/services/projects/json/?action=createFolder&project={project_id}"
    headers=wiredrive_headers(cfg,referer=f"{base}/projects/folder/{project_id}/{parent_folder_id}/")
    headers.update({
        "X-Requested-With":"XMLHttpRequest",
        "Origin":base,
        "Content-Type":"application/x-www-form-urlencoded; charset=UTF-8",
        "Accept":"application/json, text/javascript, */*; q=0.01",
    })
    data={"folderName":folder_name,"folderlist":parent_folder_id}
    r=requests.post(url,data=data,headers=headers,timeout=45,allow_redirects=False)
    if r.status_code in (301,302,401,403) or "Wiredrive Login" in (r.text[:2000] if r.text else ""):
        wiredrive_login(cfg)
        headers=wiredrive_headers(cfg,referer=f"{base}/projects/folder/{project_id}/{parent_folder_id}/")
        headers.update({
            "X-Requested-With":"XMLHttpRequest","Origin":base,
            "Content-Type":"application/x-www-form-urlencoded; charset=UTF-8",
            "Accept":"application/json, text/javascript, */*; q=0.01",
        })
        r=requests.post(url,data=data,headers=headers,timeout=45,allow_redirects=False)
    if not r.ok:
        msg=re.sub(r"<[^>]+>"," ",r.text or "")
        raise RuntimeError(f"Wiredrive folder creation failed: HTTP {r.status_code} — {' '.join(msg.split())[:180]}")
    try:
        js=r.json()
    except Exception:
        raise RuntimeError(f"Wiredrive folder creation returned invalid JSON: {(r.text or '')[:180]}")
    folder=js.get("folder") or (js.get("data") or {}).get("folder") or {}
    fid=str(folder.get("id") or folder.get("node_id") or folder.get("folderId") or "")
    if fid:
        return {"id":fid,"name":folder.get("node") or folder.get("name") or folder_name,"parent":str(folder.get("parent") or parent_folder_id)}
    # Some deployments wrap the success response differently. Confirm by reloading the tree.
    tree=fetch_wiredrive_folder_tree(cfg,project_id)
    matches=[f for f in tree.get("folders",[]) if str(f.get("parent"))==parent_folder_id and f.get("name")==folder_name]
    if matches:
        return matches[-1]
    raise RuntimeError(f"Wiredrive reported folder creation success but the new folder '{folder_name}' could not be found.")


def _known_remote_folder_id(watch_id, relative_dir):
    rel="" if str(relative_dir) in ("",".") else str(relative_dir)
    with db() as c:
        row=c.execute(
            "SELECT remote_folder_id FROM sync_folders WHERE watch_id=? AND local_relative_path=?",
            (watch_id,rel)
        ).fetchone()
    return str(row["remote_folder_id"]) if row and row["remote_folder_id"] else ""


def ensure_remote_relative_path(cfg, watch, relative_dir):
    """Return remote folder ID corresponding to a local relative directory, creating missing folders."""
    relative_dir=Path(relative_dir)
    known=_known_remote_folder_id(watch["id"],relative_dir)
    if known:
        return known
    project_id=str(watch.get("project_id") or cfg.get("wiredrive",{}).get("project_id") or "")
    root_id=str(watch.get("folder_id") or cfg.get("wiredrive",{}).get("folder_id") or "")
    if not project_id or not root_id: raise RuntimeError("Project ID and Folder ID are required.")
    if str(relative_dir) in ("","."):
        _record_folder_mapping(watch["id"],"",root_id,watch.get("destination_label",""))
        return root_id

    tree=fetch_wiredrive_folder_tree(cfg,project_id)
    by_id,children=_folder_maps(tree)
    parent=root_id
    current_rel=Path(".")
    create_missing=bool(watch.get("create_missing_folders",True))
    for part in relative_dir.parts:
        if part in ("","."): continue
        current_rel=current_rel/part
        match=next((f for f in children.get(str(parent),[]) if f.get("name")==part),None)
        if match:
            parent=str(match["id"])
        else:
            if not create_missing:
                raise RuntimeError(f"Remote folder does not exist: {current_rel}")
            created=create_remote_folder(cfg,project_id,parent,part)
            parent=str(created["id"])
            children.setdefault(str(created.get("parent") or ""),[]).append(created)
        _record_folder_mapping(watch["id"],current_rel,parent,part)
    return parent


def fetch_recursive_inventory(cfg, watch, create_local_dirs=True):
    """Read the selected Wiredrive folder and optionally every subfolder beneath it."""
    project_id=str(watch.get("project_id") or cfg.get("wiredrive",{}).get("project_id") or "")
    root_id=str(watch.get("folder_id") or cfg.get("wiredrive",{}).get("folder_id") or "")
    if not project_id or not root_id:
        raise RuntimeError("Project ID and Folder ID are required for download sync.")
    recursive=bool(watch.get("include_subfolders",True))
    if not recursive:
        inv=fetch_remote_inventory(cfg,watch)
        for a in inv["assets"]:
            a["relative_dir"]=""
            a["remote_folder_id"]=root_id
        inv["folder_count"]=1
        return inv

    tree=fetch_wiredrive_folder_tree(cfg,project_id)
    folders=_relative_remote_folders(tree,root_id)
    assets=[]
    for fid,rel,name in folders:
        child=dict(watch); child["folder_id"]=fid
        inv=fetch_remote_inventory(cfg,child)
        rel_str="" if str(rel)=="." else str(rel)
        for a in inv["assets"]:
            a["relative_dir"]=rel_str
            a["remote_folder_id"]=fid
            assets.append(a)
        if create_local_dirs:
            local_root=Path(watch["path"]).expanduser()
            local_dir=local_root if not rel_str else local_root/rel_str
            local_dir.mkdir(parents=True,exist_ok=True)
        _record_folder_mapping(watch["id"],rel_str,fid,name)
    return {
        "project_id":project_id,"folder_id":root_id,
        "folder_name":watch.get("destination_label",""),
        "total":len(assets),"assets":assets,"folder_count":len(folders),
        "source_url":tree.get("source_url",""),
    }

def fetch_remote_inventory(cfg, watch):
    project_id=str(watch.get("project_id") or cfg.get("wiredrive",{}).get("project_id") or "")
    folder_id=str(watch.get("folder_id") or cfg.get("wiredrive",{}).get("folder_id") or "")
    if not project_id or not folder_id:
        raise RuntimeError("Project ID and Folder ID are required for download sync.")
    base=(cfg.get("wiredrive",{}).get("site_url") or "https://wsl.wiredrive.com").rstrip("/")
    url=f"{base}/projects/folder/{project_id}/{folder_id}/"
    headers=wiredrive_headers(cfg, referer=base+"/projects/")
    r=requests.get(url,headers=headers,timeout=45,allow_redirects=False)
    if r.status_code in (301,302,401,403) or "Wiredrive Login" in (r.text[:2000] if r.text else ""):
        auth,cookie=wiredrive_login(cfg)
        headers=wiredrive_headers(cfg, referer=base+"/projects/")
        r=requests.get(url,headers=headers,timeout=45,allow_redirects=False)
    if not r.ok:
        raise RuntimeError(f"Wiredrive folder inventory failed: HTTP {r.status_code}")
    raw=_extract_js_object(r.text)
    assets=[]
    for item in raw.get("list",[]):
        if item.get("content") != "file": continue
        media=item.get("media") or {}
        dl=media.get("download") or {}
        if not dl.get("url"): continue
        assets.append({
            "asset_id":str(item.get("fileId") or ""),
            "filename":item.get("fileName") or f"asset-{item.get('fileId')}",
            "size":int(item.get("fileSize") or dl.get("fileSize") or 0),
            "modified":str(item.get("dateModified") or ""),
            "mime_type":dl.get("mimeType") or "",
            "download_url":dl.get("url") or "",
            "renew_url":dl.get("renewUrl") or "",
            "folder_name":item.get("folderName") or "",
        })
    return {
        "project_id":project_id,
        "folder_id":folder_id,
        "folder_name":assets[0].get("folder_name","") if assets else "",
        "total":int(raw.get("total") or len(assets)),
        "assets":assets,
        "source_url":url,
    }


def _safe_local_name(name):
    name=os.path.basename(name).replace("\\x00","").strip()
    return name or "wiredrive-download"


def _asset_state(watch_id, asset_id):
    with db() as c:
        r=c.execute("SELECT * FROM sync_assets WHERE watch_id=? AND remote_asset_id=?",(watch_id,asset_id)).fetchone()
        return dict(r) if r else None


def _upsert_asset(watch_id, asset, local_path, status, local_size=0, local_mtime=0):
    with db() as c:
        c.execute("""INSERT INTO sync_assets
          (watch_id,remote_asset_id,remote_filename,remote_size,remote_modified,remote_folder_id,local_path,local_size,local_mtime,status,last_sync)
          VALUES (?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(watch_id,remote_asset_id) DO UPDATE SET
            remote_filename=excluded.remote_filename,
            remote_size=excluded.remote_size,
            remote_modified=excluded.remote_modified,
            remote_folder_id=excluded.remote_folder_id,
            local_path=excluded.local_path,
            local_size=excluded.local_size,
            local_mtime=excluded.local_mtime,
            status=excluded.status,
            last_sync=excluded.last_sync
        """,(watch_id,asset["asset_id"],asset["filename"],asset["size"],asset["modified"],
             str(asset.get("remote_folder_id") or ""),str(local_path),int(local_size or 0),
             float(local_mtime or 0),status,now()))
        c.commit()


def enqueue_download(watch, asset):
    if ignored_remote_asset(asset):
        return False
    watch_id=watch["id"]; state=_asset_state(watch_id,asset["asset_id"])
    root=Path(watch["path"]).expanduser()
    root.mkdir(parents=True,exist_ok=True)
    name=_safe_local_name(asset["filename"])
    rel=Path(asset.get("relative_dir") or "")
    final=(root/rel/name)
    final.parent.mkdir(parents=True,exist_ok=True)

    if state:
        tracked=Path(state.get("local_path") or final)
        same_remote=(str(state.get("remote_modified",""))==asset["modified"] and int(state.get("remote_size") or 0)==asset["size"])
        if same_remote and tracked.exists() and tracked.stat().st_size==asset["size"] and state.get("status")=="complete":
            return False

    # True-sync reconciliation: an exact file at the same relative path is the
    # local counterpart of this remote asset even if SQLite has never seen it.
    if final.exists() and final.is_file() and final.stat().st_size==asset["size"]:
        st=final.stat()
        _upsert_asset(watch_id,asset,final,"complete",st.st_size,st.st_mtime)
        return False

    # A same-name file with different size is a real conflict. Preserve both rather
    # than overwriting; exact matches above never get the Wiredrive suffix.
    if final.exists() and (not state or Path(state.get("local_path") or "") != final):
        stem=final.stem; suffix=final.suffix
        final=final.with_name(f"{stem} (Wiredrive {asset['asset_id']}){suffix}")

    with db() as c:
        active=c.execute("""SELECT id FROM jobs WHERE watch_id=? AND remote_asset_id=?
                            AND direction='download' AND status IN ('waiting','ready','downloading')""",
                         (watch_id,asset["asset_id"])).fetchone()
        if active: return False
        jid=str(uuid.uuid4())
        c.execute("""INSERT INTO jobs
          (id,watch_id,path,filename,destination,size,status,progress,attempts,message,
           remote_asset_id,created_at,updated_at,direction,remote_modified,remote_folder_id,priority_time)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (jid,watch_id,str(final),name,watch.get("destination_label") or "Wiredrive",
           asset["size"],"ready",0,0,"Queued for download",asset["asset_id"],now(),now(),"download",asset["modified"],
           str(asset.get("remote_folder_id") or watch.get("folder_id") or ""),
           priority_timestamp(asset.get("modified"))))
        c.commit()
    # Persist target before download so filesystem watcher knows not to re-upload it.
    _upsert_asset(watch_id,asset,final,"downloading")
    return True


def _local_sync_state_matches(watch_id, p):
    try:
        st=p.stat()
    except FileNotFoundError:
        return False
    with db() as c:
        row=c.execute(
            """SELECT local_size,local_mtime FROM sync_assets
               WHERE watch_id=? AND local_path=? AND status='complete'
               ORDER BY last_sync DESC LIMIT 1""",
            (watch_id,str(p))
        ).fetchone()
    if not row:
        return False
    return (
        int(row["local_size"] or 0)==int(st.st_size)
        and abs(float(row["local_mtime"] or 0)-float(st.st_mtime)) < 0.001
    )


def _mark_existing_job_reconciled(watch_id, p, asset):
    with db() as c:
        rows=c.execute(
            """SELECT id FROM jobs WHERE watch_id=? AND path=? AND direction='upload'
               AND status IN ('waiting','ready','failed')""",
            (watch_id,str(p))
        ).fetchall()
        for row in rows:
            c.execute(
                """UPDATE jobs SET status='complete',progress=100,bytes_transferred=size,
                   transfer_speed=0,eta_seconds=0,remote_asset_id=?,message=?,updated_at=?
                   WHERE id=?""",
                (
                    str(asset.get("asset_id") or ""),
                    "Already present in Wiredrive — fast reconciled",
                    now(),
                    row["id"],
                )
            )
        c.commit()


def reconcile_upload_mapping(watch, destination):
    """Bulk reconcile upload-capable mappings before queueing local files."""
    wid=watch["id"]
    try:
        cfg=load_config()
        current=watch_for_job(cfg,wid)
        if (
            not current
            or not current.get("enabled")
            or current.get("direction","upload") not in ("upload","two_way")
        ):
            return

        root=Path(current["path"]).expanduser()
        recursive=bool(current.get("include_subfolders",True))
        files=root.rglob("*") if recursive else root.iterdir()
        candidates=[]

        for p in files:
            if (
                not p.is_file()
                or ignored_local_path(p, root)
                or p.name.endswith(".wdpartial")
                or not allowed(p,cfg)
            ):
                continue

            # On later upgrades this is the dominant fast path: no remote polling.
            if _local_sync_state_matches(wid,p):
                continue
            candidates.append(p)

        if not candidates:
            return

        # Newest local media should never wait behind a deep historical reconciliation.
        candidates.sort(
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True
        )

        # Prime the queue with the newest files immediately. The normal upload path still
        # performs its duplicate check, so already-uploaded files in this hot set are safe.
        hot_count=min(12,len(candidates))
        for p in candidates[:hot_count]:
            enqueue(str(p),wid,destination)

        # Then reconcile the whole historical tree in bulk.
        try:
            inv=fetch_recursive_inventory(cfg,current,create_local_dirs=False)
        except Exception:
            # Safe fallback: unknown files still use the established upload flow.
            for p in candidates:
                enqueue(str(p),wid,destination)
            return

        remote_index={}
        for asset in inv.get("assets",[]):
            rel=str(asset.get("relative_dir") or "")
            key=(rel,asset.get("filename") or "",int(asset.get("size") or 0))
            remote_index.setdefault(key,[]).append(asset)

        for p in candidates:
            try:
                rel=p.parent.resolve().relative_to(root.resolve())
                rel_str="" if str(rel)=="." else str(rel)
                st=p.stat()
            except (FileNotFoundError,ValueError):
                continue

            matches=remote_index.get((rel_str,p.name,int(st.st_size))) or []
            if matches:
                asset=matches[-1]
                _upsert_asset(wid,asset,p,"complete",st.st_size,st.st_mtime)
                _mark_existing_job_reconciled(wid,p,asset)
            else:
                enqueue(str(p),wid,destination)
    finally:
        with reconcile_lock:
            reconciling_mappings.discard(wid)


def start_fast_reconcile(watch, destination):
    wid=watch.get("id")
    if not wid:
        return
    with reconcile_lock:
        if wid in reconciling_mappings:
            return
        reconciling_mappings.add(wid)
    threading.Thread(
        target=reconcile_upload_mapping,
        args=(dict(watch),destination),
        daemon=True,
        name=f"wiredrive-reconcile-{wid}",
    ).start()


def scan_remote_once(cfg=None):
    cfg=cfg or load_config()
    summary=[]
    for w in cfg.get("watch_folders",[]):
        if not w.get("enabled") or w.get("direction","upload") not in ("download","two_way"):
            continue
        inv=fetch_recursive_inventory(cfg,w)
        queued=0
        asset_map={a["asset_id"]:a for a in inv["assets"]}
        newest_assets=sorted(
            inv["assets"],
            key=lambda a: priority_timestamp(a.get("modified"),0),
            reverse=True
        )
        for asset in newest_assets:
            if enqueue_download(w,asset): queued+=1
        summary.append({"watch_id":w["id"],"folder_name":inv["folder_name"],"remote_total":len(inv["assets"]),"queued":queued})
    return summary


def download_asset(cfg, job, progress):
    watch=watch_for_job(cfg,job["watch_id"])
    if not watch: raise RuntimeError("Sync mapping no longer exists.")
    lookup_watch=dict(watch)
    if job.get("remote_folder_id"):
        lookup_watch["folder_id"]=job["remote_folder_id"]
    inv=fetch_remote_inventory(cfg,lookup_watch)
    asset=next((a for a in inv["assets"] if a["asset_id"]==str(job["remote_asset_id"])),None)
    if asset:
        asset["remote_folder_id"]=str(job.get("remote_folder_id") or lookup_watch.get("folder_id") or "")
        try:
            asset["relative_dir"]=str(Path(job["path"]).expanduser().parent.relative_to(Path(watch["path"]).expanduser()))
        except Exception:
            asset["relative_dir"]=""
    if not asset: raise RuntimeError(f"Remote asset {job['remote_asset_id']} is no longer present in the Wiredrive folder.")

    final=Path(job["path"]).expanduser()
    final.parent.mkdir(parents=True,exist_ok=True)
    partial=Path(str(final)+".wdpartial")
    expected=int(asset["size"] or 0)
    offset=partial.stat().st_size if partial.exists() else 0
    if expected and offset>expected:
        partial.unlink(missing_ok=True); offset=0

    headers={"User-Agent":"WiredriveSync/1.0"}
    if offset>0: headers["Range"]=f"bytes={offset}-"
    with requests.get(asset["download_url"],headers=headers,stream=True,timeout=(30,120),allow_redirects=True) as r:
        if offset>0 and r.status_code==200:
            # Server ignored range; safely restart.
            partial.unlink(missing_ok=True); offset=0
        elif offset>0 and r.status_code!=206:
            raise RuntimeError(f"Wiredrive resume failed: HTTP {r.status_code}")
        elif offset==0 and not r.ok:
            raise RuntimeError(f"Wiredrive download failed: HTTP {r.status_code}")

        mode="ab" if offset>0 and r.status_code==206 else "wb"
        written=offset if mode=="ab" else 0
        session_start_bytes=written
        transfer_start=time.monotonic()
        with partial.open(mode) as f:
            for chunk in r.iter_content(chunk_size=4*1024*1024):
                if not chunk: continue
                f.write(chunk); written+=len(chunk)
                elapsed=max(time.monotonic()-transfer_start,0.001)
                session_bytes=max(written-session_start_bytes,0)
                speed=session_bytes/elapsed
                remaining=max(expected-written,0) if expected else 0
                eta=(remaining/speed) if speed>0 and expected else 0
                pct=min(99,int(written*100/expected)) if expected else 0
                progress(pct,written,speed,eta)

    actual=partial.stat().st_size
    if expected and actual!=expected:
        raise RuntimeError(f"Download incomplete: received {actual} of {expected} bytes. It will resume on retry.")

    st=partial.stat()
    # State first: prevents the filesystem move event from becoming an upload loop.
    _upsert_asset(job["watch_id"],asset,final,"downloading",actual,st.st_mtime)
    os.replace(partial,final)
    st=final.stat()
    _upsert_asset(job["watch_id"],asset,final,"complete",st.st_size,st.st_mtime)
    progress(100,actual,0,0)
    return {"message":f"Downloaded from Wiredrive · Asset {asset['asset_id']}","asset_id":asset["asset_id"]}


def download_worker():
    while True:
        cfg=load_config()
        with db() as c:
            row=c.execute("""SELECT * FROM jobs WHERE direction='download'
                             AND status IN ('ready','failed') AND attempts < 8
                             ORDER BY priority_time DESC, created_at DESC LIMIT 1""").fetchone()
        if not row:
            time.sleep(1); continue
        job=dict(row)
        if job["status"]=="failed": time.sleep(min(15,2+job["attempts"]*2))
        update_job(job["id"],status="downloading",attempts=job["attempts"]+1,message="Downloading from Wiredrive",
                   transfer_started_at=time.time(),transfer_speed=0,eta_seconds=0)
        try:
            result=download_asset(cfg,get_job(job["id"]),lambda pct,*t:update_transfer(job["id"],pct,*t))
            update_job(job["id"],status="complete",progress=100,transfer_speed=0,eta_seconds=0,
                       bytes_transferred=int(job.get("size") or 0),message=result["message"])
        except Exception as e:
            update_job(job["id"],status="failed",transfer_speed=0,eta_seconds=0,message=str(e))


def remote_poll_worker():
    next_scan=0
    while True:
        cfg=load_config()
        interval=max(15,int(cfg.get("remote_check_seconds",15)))
        if time.time()>=next_scan:
            try: scan_remote_once(cfg)
            except Exception as e:
                # Expose poll failure as service state without creating endless queue jobs.
                cfg["_last_remote_error"]=str(e)
                cfg["_last_remote_check"]=now()
                save_config(cfg)
            else:
                cfg.pop("_last_remote_error",None)
                cfg["_last_remote_check"]=now()
                save_config(cfg)
            next_scan=time.time()+interval
        time.sleep(2)


def ensure_sync_workers():
    global sync_worker_started
    if not sync_worker_started:
        threading.Thread(target=download_worker,daemon=True).start()
        threading.Thread(target=remote_poll_worker,daemon=True).start()
        sync_worker_started=True


def worker():
    while True:
        cfg=load_config(); stability=int(cfg.get("stability_seconds",5))
        with db() as c:
            row=c.execute("SELECT * FROM jobs WHERE direction='upload' AND status IN ('waiting','ready','failed') AND attempts < 5 ORDER BY priority_time DESC, created_at DESC LIMIT 1").fetchone()
        if not row: time.sleep(1); continue
        job=dict(row); p=Path(job["path"])
        watch = watch_for_job(cfg, job["watch_id"])
        root = Path(watch["path"]).expanduser() if watch and watch.get("path") else None
        if ignored_local_path(p, root):
            with db() as c:
                c.execute("DELETE FROM jobs WHERE id=?", (job["id"],))
                c.commit()
            continue
        if not p.exists(): update_job(job["id"],status="failed",message="Source file no longer exists"); time.sleep(1); continue
        if job["status"]=="failed": time.sleep(min(10,2+job["attempts"]*2))
        if job["status"]=="waiting":
            update_job(job["id"],message=f"Checking file stability for {stability}s")
            if not file_stable(p,stability): update_job(job["id"],message="File still changing; will check again"); continue
            update_job(job["id"],status="ready",message="Ready to upload")
        fresh=get_job(job["id"]); update_job(job["id"],status="uploading",attempts=fresh["attempts"]+1,
                                            message="Preparing upload",transfer_started_at=time.time(),
                                            bytes_transferred=0,transfer_speed=0,eta_seconds=0)
        try:
            result=WiredriveUploader(cfg).upload(get_job(job["id"]),lambda pct,*t:update_transfer(job["id"],pct,*t))
            remote_asset_id=result.get("asset_id", "")
            final_bytes=int(p.stat().st_size if p.exists() else fresh.get("size") or 0)
            update_job(job["id"],status="complete",progress=100,bytes_transferred=final_bytes,
                       transfer_speed=0,eta_seconds=0,message=result.get("message","Complete"),
                       remote_asset_id=remote_asset_id)
            # Record successful or reconciled uploads before the remote poll can see
            # them. This prevents our own upload from being downloaded back again.
            if remote_asset_id and p.exists():
                st=p.stat()
                remote_asset=result.get("remote_asset") or {
                    "asset_id":str(remote_asset_id),"filename":p.name,"size":st.st_size,
                    "modified":"","remote_folder_id":str(result.get("remote_folder_id") or "")
                }
                _upsert_asset(job["watch_id"],remote_asset,p,"complete",st.st_size,st.st_mtime)
            if cfg.get("move_completed_to"):
                dest=Path(cfg["move_completed_to"]).expanduser(); dest.mkdir(parents=True,exist_ok=True); shutil.move(str(p),str(dest/p.name))
            elif cfg.get("delete_after_upload"): p.unlink(missing_ok=True)
        except Exception as e:
            update_job(job["id"],status="failed",transfer_speed=0,eta_seconds=0,message=str(e))


def update_job(jid, **fields):
    if not fields: return
    fields["updated_at"]=now(); sql=",".join([f"{k}=?" for k in fields])
    with db() as c: c.execute(f"UPDATE jobs SET {sql} WHERE id=?",(*fields.values(),jid)); c.commit()


def update_transfer(jid, pct, bytes_transferred=None, speed=None, eta=None):
    fields={"progress":max(0,min(100,int(pct or 0)))}
    if bytes_transferred is not None: fields["bytes_transferred"]=int(bytes_transferred)
    if speed is not None: fields["transfer_speed"]=float(max(0,speed))
    if eta is not None: fields["eta_seconds"]=float(max(0,eta))
    update_job(jid,**fields)


def get_job(jid):
    with db() as c:
        r=c.execute("SELECT * FROM jobs WHERE id=?",(jid,)).fetchone(); return dict(r) if r else None


def purge_ignored_upload_jobs():
    cfg = load_config()
    watches = {w.get("id"): w for w in cfg.get("watch_folders", [])}
    with db() as c:
        rows = c.execute(
            """SELECT id,watch_id,path FROM jobs
               WHERE direction='upload'
               AND status IN ('waiting','ready','failed','uploading')"""
        ).fetchall()
        removed = 0
        for row in rows:
            watch = watches.get(row["watch_id"])
            root = Path(watch["path"]).expanduser() if watch and watch.get("path") else None
            if ignored_local_path(Path(row["path"]), root):
                c.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
                removed += 1
        if removed:
            c.commit()
    return removed


def ensure_worker():
    global workers_started
    if not workers_started:
        purge_ignored_upload_jobs()
        threading.Thread(target=worker,daemon=True).start()
        workers_started=True


def _har_response_json(entry):
    if not entry: return None
    content = entry.get("response", {}).get("content", {})
    text = content.get("text", "") or ""
    if content.get("encoding") == "base64":
        import base64
        text = base64.b64decode(text).decode("utf-8", errors="replace")
    try: return json.loads(text)
    except Exception: return None


def import_har_payload(raw: bytes):
    har=json.loads(raw.decode("utf-8")); entries=har.get("log",{}).get("entries",[])
    create=None; upload_config_entry=None
    for e in entries:
        req=e.get("request",{}); url=req.get("url","")
        if req.get("method")=="GET" and "routekey=get-upload-config" in url: upload_config_entry=e
        if req.get("method")=="POST" and "routekey=asset" in url: create=e
    if not create: raise ValueError("No Wiredrive routekey=asset request found in this HAR")
    req=create["request"]; body=req.get("postData",{}).get("text","")
    vals=dict(parse_qsl(body, keep_blank_values=True))
    site=urlparse(req.get("url","")).scheme+"://"+urlparse(req.get("url","")).netloc
    cookie=""
    for h in req.get("headers",[]):
        if h.get("name","").lower()=="cookie": cookie=h.get("value","")
    if cookie: secret_set("wiredrive_cookie", cookie)

    cfg=load_config(); wd=cfg.setdefault("wiredrive",{}); wd["site_url"]=site
    # Prefer the dedicated browser configuration endpoint captured in the newer HAR.
    config_js=_har_response_json(upload_config_entry)
    config_data=(config_js or {}).get("data") if isinstance(config_js, dict) and config_js.get("code")==200 else None
    if isinstance(config_data, dict):
        apply_upload_config(cfg, config_data)
    else:
        required=["userId","clientCode","region","bucketName","assetPathPrefix","accessKey","secretKey","sessionToken","expiration"]
        missing=[x for x in required if not vals.get(x)]
        if missing: raise ValueError("HAR upload request is missing: "+", ".join(missing))
        apply_upload_config(cfg, vals)

    project_id=vals.get("projectId", ""); folder_id=vals.get("folderData[targetId]", "")
    if not project_id or not folder_id: raise ValueError("HAR does not contain the Wiredrive project/folder destination mapping")
    wd.update({"project_id":project_id,"folder_id":folder_id})
    if not cfg.get("watch_folders"):
        cfg["watch_folders"]=[{"id":"primary","name":"Wiredrive Watch","path":"~/Wiredrive Watch","destination_label":"Imported Wiredrive folder","project_id":project_id,"folder_id":folder_id,"enabled":True}]
    else:
        for w in cfg["watch_folders"]:
            if not w.get("project_id"): w["project_id"]=project_id
            if not w.get("folder_id"): w["folder_id"]=folder_id
    save_config(cfg); start_observers()
    return {"project_id":project_id,"folder_id":folder_id,"expiration":wd.get("expiration", ""),"site_url":site,"cookie_captured":bool(cookie),"auto_refresh_endpoint":bool(upload_config_entry)}


@app.route("/")
def index(): return render_template("index.html")

@app.route("/api/state")
def state():
    cfg=load_config()
    with db() as c:
        jobs=[dict(x) for x in c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 300")]
        sync_assets=[dict(x) for x in c.execute("SELECT * FROM sync_assets ORDER BY last_sync DESC LIMIT 300")]
        synced_total_row=c.execute("SELECT COUNT(*) AS total FROM sync_assets WHERE status='complete'").fetchone()
        synced_total=int((synced_total_row["total"] if synced_total_row else 0) or 0)
        mapping_stats={}
        for w in cfg.get("watch_folders",[]):
            wid=w.get("id","")
            row=c.execute("""SELECT
                SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END) AS complete_count,
                SUM(CASE WHEN status IN ('waiting','ready','uploading','downloading') THEN 1 ELSE 0 END) AS active_count,
                MAX(last_sync) AS last_sync
                FROM sync_assets WHERE watch_id=?""",(wid,)).fetchone()
            jobs_row=c.execute("""SELECT
                SUM(CASE WHEN status='uploading' THEN 1 ELSE 0 END) AS uploading,
                SUM(CASE WHEN status='downloading' THEN 1 ELSE 0 END) AS downloading,
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                MAX(updated_at) AS last_job
                FROM jobs WHERE watch_id=?""",(wid,)).fetchone()
            p=Path((w.get("path") or "")).expanduser() if w.get("path") else None
            mapping_stats[wid]={
                "synced":int((row["complete_count"] if row else 0) or 0),
                "active":int((row["active_count"] if row else 0) or 0),
                "last_sync":(row["last_sync"] if row else "") or (jobs_row["last_job"] if jobs_row else "") or "",
                "uploading":int((jobs_row["uploading"] if jobs_row else 0) or 0),
                "downloading":int((jobs_row["downloading"] if jobs_row else 0) or 0),
                "failed":int((jobs_row["failed"] if jobs_row else 0) or 0),
                "local_exists":bool(p and p.exists()),
            }
    return jsonify({
        "config":cfg,"jobs":jobs,"sync_assets":sync_assets,"synced_total":synced_total,
        "watching":list(observers.keys()),"credentials":credentials_status(cfg),
        "account":secret_get("wiredrive_username"),"mapping_stats":mapping_stats,
        "server_time":now(),
    })

@app.route('/api/wiredrive/projects', methods=['GET'])
def api_wiredrive_projects():
    try:
        cfg=load_config()
        return jsonify({'ok':True,'projects':fetch_wiredrive_projects(cfg)})
    except Exception as e:
        return jsonify({'ok':False,'error':str(e)}),400


@app.route('/api/wiredrive/folders', methods=['GET'])
def api_wiredrive_folders():
    try:
        project_id=(request.args.get('project_id') or '').strip()
        cfg=load_config()
        return jsonify({'ok':True,**fetch_wiredrive_folder_tree(cfg,project_id)})
    except Exception as e:
        return jsonify({'ok':False,'error':str(e)}),400


@app.route("/api/browse-folder", methods=["POST"])
def browse_folder():
    """Open macOS Standard Additions folder chooser and return a POSIX path."""
    if os.uname().sysname != "Darwin":
        return jsonify({"ok": False, "error": "Native folder browsing is currently available on macOS only."}), 400

    script = """
try
    activate
    set chosenFolder to choose folder with prompt "Choose a Wiredrive watch folder" default location (path to home folder)
    return POSIX path of chosenFolder
on error errMsg number errNum
    if errNum is -128 then
        return "__WIREDRIVE_CANCELLED__"
    else
        return "__WIREDRIVE_ERROR__" & errNum & ":" & errMsg
    end if
end try
"""
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        )
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()

        if stdout == "__WIREDRIVE_CANCELLED__":
            return jsonify({"ok": False, "cancelled": True})

        if stdout.startswith("__WIREDRIVE_ERROR__"):
            return jsonify({
                "ok": False,
                "error": "macOS folder picker failed: " + stdout.replace("__WIREDRIVE_ERROR__", "", 1)
            }), 400

        if result.returncode != 0:
            return jsonify({
                "ok": False,
                "error": "macOS folder picker failed: " + (stderr or f"osascript exited {result.returncode}")
            }), 400

        selected = stdout
        if not selected:
            return jsonify({"ok": False, "error": "macOS folder picker returned no folder."}), 400

        if selected.endswith("/") and selected != "/":
            selected = selected[:-1]

        return jsonify({"ok": True, "path": selected})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Folder picker timed out after 120 seconds."}), 408
    except Exception as e:
        return jsonify({"ok": False, "error": f"Folder picker exception: {e}"}), 400



def _clean_mapping(data, mapping_id=None):
    data=data or {}
    path=(data.get("path") or "").strip()
    if not path: raise ValueError("Choose a local sync folder.")
    project_id=str(data.get("project_id") or "").strip()
    folder_id=str(data.get("folder_id") or "").strip()
    if not project_id or not folder_id:
        raise ValueError("Choose a Wiredrive destination folder.")
    direction=str(data.get("direction") or "two_way")
    if direction not in ("upload","download","two_way"):
        raise ValueError("Invalid sync direction.")
    resolved=Path(path).expanduser()
    try: resolved.mkdir(parents=True,exist_ok=True)
    except Exception as e: raise ValueError(f"Cannot access local sync folder: {e}")
    return {
        "id":mapping_id or str(data.get("id") or f"map-{uuid.uuid4().hex[:10]}"),
        "name":(data.get("name") or resolved.name or "Wiredrive Mapping").strip(),
        "path":path,
        "destination_label":(data.get("destination_label") or "Wiredrive Folder").strip(),
        "project_id":project_id,
        "folder_id":folder_id,
        "enabled":bool(data.get("enabled",True)),
        "direction":direction,
        "include_subfolders":bool(data.get("include_subfolders",True)),
        "create_missing_folders":bool(data.get("create_missing_folders",True)),
    }


@app.route("/api/mappings",methods=["POST"])
def create_mapping():
    try:
        cfg=load_config(); mapping=_clean_mapping(request.get_json(force=True) or {})
        if any(w.get("id")==mapping["id"] for w in cfg.get("watch_folders",[])):
            mapping["id"]=f"map-{uuid.uuid4().hex[:10]}"
        cfg.setdefault("watch_folders",[]).append(mapping)
        save_config(cfg); start_observers()
        return jsonify({"ok":True,"mapping":mapping,"config":cfg})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),400


@app.route("/api/mappings/<mapping_id>",methods=["PUT"])
def update_mapping(mapping_id):
    try:
        cfg=load_config(); watches=cfg.get("watch_folders",[])
        idx=next((i for i,w in enumerate(watches) if w.get("id")==mapping_id),None)
        if idx is None: return jsonify({"ok":False,"error":"Mapping not found"}),404
        mapping=_clean_mapping(request.get_json(force=True) or {},mapping_id)
        watches[idx]=mapping; cfg["watch_folders"]=watches
        save_config(cfg); start_observers()
        return jsonify({"ok":True,"mapping":mapping,"config":cfg})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),400


@app.route("/api/mappings/<mapping_id>/toggle",methods=["POST"])
def toggle_mapping(mapping_id):
    cfg=load_config(); w=watch_for_job(cfg,mapping_id)
    if not w: return jsonify({"ok":False,"error":"Mapping not found"}),404
    data=request.get_json(silent=True) or {}
    w["enabled"]=bool(data.get("enabled",not w.get("enabled",True)))
    save_config(cfg); start_observers()
    return jsonify({"ok":True,"enabled":w["enabled"]})


@app.route("/api/mappings/<mapping_id>",methods=["DELETE"])
def delete_mapping(mapping_id):
    cfg=load_config(); watches=cfg.get("watch_folders",[])
    if not any(w.get("id")==mapping_id for w in watches):
        return jsonify({"ok":False,"error":"Mapping not found"}),404
    with db() as c:
        active=c.execute("SELECT COUNT(*) AS n FROM jobs WHERE watch_id=? AND status IN ('uploading','downloading')",(mapping_id,)).fetchone()["n"]
        if active:
            return jsonify({"ok":False,"error":"This mapping has an active transfer. Wait for it to finish before deleting."}),409
        c.execute("DELETE FROM jobs WHERE watch_id=?",(mapping_id,))
        c.execute("DELETE FROM sync_assets WHERE watch_id=?",(mapping_id,))
        c.execute("DELETE FROM sync_folders WHERE watch_id=?",(mapping_id,))
        c.commit()
    cfg["watch_folders"]=[w for w in watches if w.get("id")!=mapping_id]
    save_config(cfg); start_observers()
    return jsonify({"ok":True})


@app.route("/api/mappings/<mapping_id>/sync",methods=["POST"])
def sync_mapping(mapping_id):
    try:
        cfg=load_config(); w=watch_for_job(cfg,mapping_id)
        if not w: return jsonify({"ok":False,"error":"Mapping not found"}),404
        # Local scan is handled by observer restart; remote scan is limited to this mapping.
        start_observers(); summary=[]
        if w.get("enabled") and w.get("direction","upload") in ("download","two_way"):
            inv=fetch_recursive_inventory(cfg,w); queued=0
            for asset in sorted(
                inv.get("assets",[]),
                key=lambda a: priority_timestamp(a.get("modified"),0),
                reverse=True
            ):
                if enqueue_download(w,asset): queued+=1
            summary={"watch_id":mapping_id,"remote_total":len(inv.get("assets",[])),"queued":queued}
        return jsonify({"ok":True,"summary":summary})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),400


@app.route("/api/settings",methods=["POST"])
def save_settings():
    try:
        cfg=load_config(); data=request.get_json(force=True) or {}
        cfg["stability_seconds"]=max(2,int(data.get("stability_seconds",cfg.get("stability_seconds",5))))
        cfg["remote_check_seconds"]=max(15,int(data.get("remote_check_seconds",cfg.get("remote_check_seconds",15))))
        mode=str(data.get("uploader_mode",cfg.get("uploader_mode","wiredrive")))
        if mode not in ("wiredrive","simulation"): raise ValueError("Invalid uploader mode")
        cfg["uploader_mode"]=mode
        save_config(cfg); start_observers()
        return jsonify({"ok":True,"config":cfg})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),400


@app.route("/api/config",methods=["POST"])
def config_save():
    try:
        incoming=request.get_json(force=True) or {}
        watches=incoming.get("watch_folders") or []
        if not watches:
            return jsonify({"ok":False,"error":"At least one watch folder is required."}),400

        for w in watches:
            raw=(w.get("path") or "").strip()
            if not raw:
                return jsonify({"ok":False,"error":"Choose or enter a local watch-folder path."}),400
            # Store the user's path as entered, but verify that it can be created/accessed.
            resolved=Path(raw).expanduser()
            try:
                resolved.mkdir(parents=True,exist_ok=True)
            except Exception as e:
                return jsonify({"ok":False,"error":f"Cannot access watch folder: {e}"}),400

        # secret values are never accepted through this route
        save_config(incoming)
        # Read it back so the UI can confirm persistence before restarting observers.
        saved=load_config()
        start_observers()
        return jsonify({"ok":True,"config":saved})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),400

@app.route("/api/import-har",methods=["POST"])
def import_har():
    f=request.files.get("har")
    if not f: return jsonify({"ok":False,"error":"Choose a HAR file first"}),400
    try: result=import_har_payload(f.read()); return jsonify({"ok":True,"imported":result})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),400

@app.route("/api/wiredrive/connect", methods=["POST"])
def wiredrive_connect():
    try:
        data = request.get_json(force=True) or {}
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        if not username or not password:
            return jsonify({"error": "Username and password are required"}), 400
        secret_set("wiredrive_username", username)
        secret_set("wiredrive_password", password)
        cfg = load_config()
        auth, cookie = wiredrive_login(cfg)
        cfg, _ = refresh_upload_config(cfg, force=True)
        return jsonify({"ok": True, "credentials": credentials_status(cfg)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/wiredrive/disconnect", methods=["POST"])
def wiredrive_disconnect():
    for name in ("wiredrive_username","wiredrive_password","wiredrive_jwt","wiredrive_cookie",
                 "aws_access_key","aws_secret_key","aws_session_token"):
        secret_set(name, "")
    cfg = load_config()
    cfg.setdefault("wiredrive", {})["expiration"] = ""
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/refresh-credentials",methods=["POST"])
def refresh_credentials():
    try:
        cfg=load_config(); cfg, changed=refresh_upload_config(cfg, force=True)
        return jsonify({"ok":True,"refreshed":changed,"credentials":credentials_status(cfg)})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),400

@app.route("/api/jobs/<jid>/retry",methods=["POST"])
def retry(jid): update_job(jid,status="ready",attempts=0,progress=0,message="Queued for retry"); return jsonify({"ok":True})

@app.route("/api/jobs/<jid>",methods=["DELETE"])
def delete_job(jid):
    with db() as c: c.execute("DELETE FROM jobs WHERE id=?",(jid,)); c.commit()
    return jsonify({"ok":True})

@app.route("/api/remote-inventory",methods=["GET"])
def remote_inventory():
    try:
        cfg=load_config()
        wid=request.args.get("watch_id","primary")
        watch=watch_for_job(cfg,wid)
        if not watch: return jsonify({"ok":False,"error":"Watch mapping not found"}),404
        inv=fetch_recursive_inventory(cfg,watch)
        # URLs are intentionally withheld from the browser UI.
        public={**inv,"assets":[{k:v for k,v in a.items() if k not in ("download_url","renew_url")} for a in inv["assets"]]}
        return jsonify({"ok":True,"inventory":public})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),400

@app.route("/api/sync-now",methods=["POST"])
def sync_now():
    try:
        start_observers()
        summary=scan_remote_once(load_config())
        return jsonify({"ok":True,"summary":summary})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}),400

@app.route("/api/scan",methods=["POST"])
def scan():
    start_observers()
    try: summary=scan_remote_once(load_config())
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),400
    return jsonify({"ok":True,"remote":summary})

if __name__=="__main__":
    init_db(); start_observers(); ensure_worker(); ensure_sync_workers(); app.run(host="127.0.0.1",port=8765,debug=False,threaded=True)
