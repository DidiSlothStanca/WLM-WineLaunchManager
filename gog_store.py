"""gog_store.py - modul GOG Store untuk Wine Launch Manager (WLM).

Dipisah dari launcher.py supaya launcher tetap ramping. Modul ini hanya butuh library
standar Python (urllib + tkinter). Launcher cukup memanggil open_dialog(host), dimana
'host' adalah objek berisi jembatan ke launcher (lihat open_gog_store() di launcher.py).

Tidak ada nilai GOG yang ditanam di launcher.py. Semua endpoint, client id, folder
download, dan filter OS/bahasa dibaca dari ~/wlm/gog_config.json (dibuat otomatis
dengan nilai bawaan saat pertama kali dibuka, silakan diedit).

Login: GOG tidak menyediakan login user/password lewat API (ada captcha), jadi login
dilakukan di halaman resmi GOG. Ada dua cara: (1) browser mini bawaan (gog_login_browser.py,
proses terpisah berbasis pywebview) yang mengambil kode login otomatis, atau (2) cadangan
manual: buka di browser biasa lalu tempel URL hasil redirect. Password TIDAK PERNAH lewat
launcher, yang disimpan hanya token OAuth di ~/wlm/gog_auth.json (permission 600).

Tampilan library: grid kartu cover ala Heroic (tombol "List View" untuk daftar teks biasa).
Cover diambil dari field 'image' di respons library GOG, hanya untuk kartu yang sedang terlihat
(lazy), lalu di-cache di ~/wlm/gog_covers/. Butuh Pillow (sudah dipakai launcher.py).

Dua cara download (tombol terpisah di jendela GOG Store):
  - "Download Setup"  : unduh installer offline (setup_*.exe + .bin), lalu (opsional) dijalankan
                        lewat Wine/Proton. Cara lama, cocok untuk semua game.
  - "Direct Download" : unduh FILE GAME langsung dari sistem konten GOG Galaxy (generasi 2) seperti
                        Heroic - TANPA installer. Hasilnya folder game siap main di
                        ~/wlm/gog_games/<game>/ (atau 'game_dir'), shortcut dibuat otomatis dari
                        goggame-<id>.info. Bisa dilanjutkan (resume per chunk), dan menjalankannya
                        lagi di folder yang sama hanya mengunduh file yang belum lengkap/berubah
                        ukuran. Redistributable (VC++/DirectX) TIDAK dipasang otomatis.
"""
import gzip
import hashlib
import http.client
import io
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout
from contextlib import closing
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

try:
    import install_watch
except ImportError:
    install_watch = None

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageTk
    HAVE_PIL = True
except ImportError:  # launcher.py juga butuh Pillow; ini hanya pengaman
    HAVE_PIL = False

# Nilai bawaan yang ditulis ke gog_config.json kalau file itu belum ada. Client id/secret
# dibawah adalah kredensial publik GOG Galaxy (dipakai juga oleh Heroic, Minigalaxy, dll).
DEFAULT_CONFIG = {
    "client_id": "46899977096215655",
    "client_secret": "9d85c43b1482497dbbce61f6e4aa173a433796eeae2ca8c5f6129f2dc4de46d9",
    "auth_url": "https://auth.gog.com/auth",
    "token_url": "https://auth.gog.com/token",
    "redirect_uri": "https://embed.gog.com/on_login_success?origin=client",
    "userdata_url": "https://embed.gog.com/userData.json",
    "library_url": "https://embed.gog.com/account/getFilteredProducts",
    "product_url": "https://api.gog.com/products/{id}",
    "download_dir": "",       # kosong = ~/wlm/gog_downloads (tujuan "Download Setup": installer)
    "game_dir": "",           # kosong = ~/wlm/gog_games (tujuan "Direct Download": folder game)
    "content_system_url": "https://content-system.gog.com",   # daftar build + secure link (Direct Download)
    "depot_meta_url": "https://cdn.gog.com/content-system/v2/meta",  # cadangan bila link build tak bisa dipakai
    "owned_url": "https://embed.gog.com/user/data/games",     # id game/DLC yang dimiliki (untuk DLC di Direct Download)
    "direct_threads": 4,      # jumlah chunk yang diunduh paralel pada Direct Download (1-16)
    "os": "windows",          # filter installer: windows / linux / mac, kosong = semua
    "language": "",           # filter bahasa, mis. "en"; kosong = semua bahasa (Direct Download: kosong = English)
    "user_agent": "wine-launcher-manager",
    "browser_command": "",    # perintah python untuk browser mini; kosong = python yang menjalankan launcher
    "browser_timeout": 600,   # detik; batas waktu jendela login browser mini
    "install_args": "",       # argumen tambahan untuk installer, mis. "/SILENT" (Inno Setup); kosong = installer interaktif
    "auto_shortcut": True,    # setelah install selesai, otomatis tambahkan game ke daftar launcher
    "max_downloads": 1,       # jumlah download yang boleh berjalan bersamaan (sisanya antre)
    "view": "grid",           # "grid" (kartu cover) atau "list" (daftar teks)
    "card_width": 200,        # lebar kartu/cover dalam piksel (120-400)
    "cover_ratio": 0.5625,    # tinggi/lebar cover: 0.5625 = 16:9, 1.5 = poster vertikal
    # akhiran yang dicoba (berurutan) setelah 'https:' + field image dari API; yang berhasil diingat
    "cover_suffixes": ["_product_tile_398.jpg", "_196.jpg", ".jpg", ".png"],
}

_host = None
_dialog = None
_token_lock = threading.Lock()


class GogError(Exception):
    pass


class GogAuthError(GogError):
    """Belum login / sesi sudah tidak valid - pengguna harus login ulang."""


class GogCancelled(GogError):
    """Jendela browser mini ditutup pengguna sebelum login selesai."""


class DownloadCancelled(GogError):
    """Pengguna menekan Pause (file .part dibiarkan supaya bisa dilanjutkan) atau Cancel (file
    .part dibuang). Pembedanya ada di flag job['pausing'] / job['discard']."""


# --------------------------------------------------------------------------- config/auth

def _config_path():
    return Path(_host.data_dir) / "gog_config.json"


def _auth_path():
    return Path(_host.data_dir) / "gog_auth.json"


def load_config():
    """Baca gog_config.json; isi field yang hilang dari DEFAULT_CONFIG. Tulis file contoh kalau belum ada."""
    cfg = dict(DEFAULT_CONFIG)
    path = _config_path()
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8")) or {}
            cfg.update({k: v for k, v in loaded.items() if k in DEFAULT_CONFIG and v is not None})
        except Exception as e:
            print(f"[WLM/GOG] Error reading {path}, using defaults: {e}")
    else:
        try:
            path.write_text(json.dumps(DEFAULT_CONFIG, indent=4), encoding="utf-8")
        except Exception as e:
            print(f"[WLM/GOG] Could not write {path}: {e}")
    return cfg


def load_auth():
    try:
        return json.loads(_auth_path().read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def save_auth(auth):
    path = _auth_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(auth, f, indent=2)
    os.chmod(path, 0o600)


def clear_auth():
    try:
        _auth_path().unlink()
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------- HTTP / API

def _http_json(cfg, url, token=None):
    headers = {"User-Agent": cfg["user_agent"]}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise GogAuthError("GOG rejected the session (HTTP 401). Please log in again.")
        raise GogError(f"GOG returned HTTP {e.code} for {urllib.parse.urlparse(url).netloc}.")
    except urllib.error.URLError as e:
        raise GogError(f"Could not reach GOG: {e.reason}")


def login_url(cfg):
    query = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "redirect_uri": cfg["redirect_uri"],
        "response_type": "code",
        "layout": "client2",
    })
    return f"{cfg['auth_url']}?{query}"


def extract_code(text):
    """Ambil authorization code dari URL redirect yang ditempel pengguna (atau kode mentahnya)."""
    text = (text or "").strip()
    m = re.search(r"[?&]code=([^&\s#]+)", text)
    if m:
        return urllib.parse.unquote(m.group(1))
    return text if re.fullmatch(r"[\w\-.~]+", text) else None


def _token_request(cfg, params, keep=None):
    params = dict(params, client_id=cfg["client_id"], client_secret=cfg["client_secret"])
    data = _http_json(cfg, f"{cfg['token_url']}?{urllib.parse.urlencode(params)}")
    if "access_token" not in data:
        raise GogError("GOG did not return an access token.")
    auth = dict(keep or {})
    auth.update({
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token") or auth.get("refresh_token", ""),
        "user_id": data.get("user_id", auth.get("user_id", "")),
        "expires_at": time.time() + int(data.get("expires_in", 3600)),
    })
    save_auth(auth)
    return auth


def login_with_code(cfg, code):
    auth = _token_request(cfg, {"grant_type": "authorization_code", "code": code,
                                "redirect_uri": cfg["redirect_uri"]})
    try:
        info = _http_json(cfg, cfg["userdata_url"], auth["access_token"])
        auth["username"] = info.get("username") or info.get("email") or ""
        save_auth(auth)
    except GogError:
        pass  # nama pengguna hanya kosmetik
    return auth


def _helper_python():
    """Python untuk menjalankan gog_login_browser.py. Pada build AppImage/PyInstaller,
    sys.executable adalah binari launcher itu sendiri (bukan python), jadi dipakai python3
    milik sistem - pywebview + GTK/WebKit2GTK memang hanya ada di sistem dan tidak bisa dibundel."""
    if getattr(sys, "frozen", False):
        return shutil.which("python3") or shutil.which("python") or "python3"
    return sys.executable


_BUNDLE_ENV_VARS = (
    "PYTHONHOME", "PYTHONPATH", "GI_TYPELIB_PATH", "GIO_EXTRA_MODULES", "GIO_MODULE_DIR",
    "GDK_PIXBUF_MODULE_FILE", "GDK_PIXBUF_MODULEDIR", "GTK_PATH", "GTK_EXE_PREFIX",
    "GTK_DATA_PREFIX", "GTK_IM_MODULE_FILE", "GSETTINGS_SCHEMA_DIR", "GST_PLUGIN_PATH",
    "GST_PLUGIN_SYSTEM_PATH", "GST_PLUGIN_SCANNER", "LD_PRELOAD", "QT_PLUGIN_PATH",
    "QML2_IMPORT_PATH", "WEBKIT_EXEC_PATH", "WEBKIT_INJECTED_BUNDLE_PATH", "_MEIPASS2")


def _helper_env():
    """Environment untuk browser login. Dari build AppImage/PyInstaller, variabel library/GTK milik
    bundel (GI_TYPELIB_PATH, GTK_PATH, LD_LIBRARY_PATH, dll.) ikut diwariskan ke python3 sistem dan
    membuat WebKit2GTK memuat komponen yang tidak cocok -> jendela login putih. Di sini variabel itu
    dibuang / dikembalikan. Saat dijalankan dari source, environment dibiarkan apa adanya."""
    env = dict(_host.clean_env())
    if not (getattr(sys, "frozen", False) or "APPIMAGE" in env or env.get("APPDIR")):
        return env
    roots = [r.rstrip("/") for r in (env.get("APPDIR"), getattr(sys, "_MEIPASS", None)) if r]

    def outside(path):
        return not any(path == r or path.startswith(r + "/") for r in roots)

    orig = env.pop("LD_LIBRARY_PATH_ORIG", None)      # diisi PyInstaller
    if orig is not None:
        env["LD_LIBRARY_PATH"] = orig
    for key in _BUNDLE_ENV_VARS:
        env.pop(key, None)
    for key in [k for k in env if k.startswith("_PYI")]:
        env.pop(key, None)
    for key in ("LD_LIBRARY_PATH", "PATH", "XDG_DATA_DIRS", "XDG_CONFIG_DIRS"):
        if key in env:
            kept = [p for p in env[key].split(":") if p and outside(p)]
            if kept:
                env[key] = ":".join(kept)
            else:
                env.pop(key)
    return env


def login_via_browser(cfg):
    """Buka browser mini (gog_login_browser.py, proses terpisah), tunggu kode login, lalu tukar
    jadi token. BLOCKING - panggil dari background thread."""
    custom = (cfg.get("browser_command") or "").strip()
    cmd = (shlex.split(custom) if custom else [_helper_python()])
    cmd += [str(Path(__file__).with_name("gog_login_browser.py")), login_url(cfg),
            "--watch", urllib.parse.urlparse(cfg["redirect_uri"]).path.strip("/")]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=_helper_env(),
                              timeout=int(cfg.get("browser_timeout") or 600))
    except FileNotFoundError as e:
        raise GogError(f"Could not start the browser helper: {e}")
    except subprocess.TimeoutExpired:
        raise GogError("The login window timed out.")

    result = {}
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            result = json.loads(line)
            break
        except ValueError:
            continue
    if result.get("code"):
        return login_with_code(cfg, result["code"])
    if result.get("cancelled"):
        raise GogCancelled("Login window was closed before finishing.")
    tail = (proc.stderr.strip().splitlines() or [""])[-1]
    raise GogError(result.get("error") or tail or f"Browser helper exited with code {proc.returncode}.")


def get_access_token(cfg):
    """Token yang masih berlaku; di-refresh otomatis kalau hampir kedaluwarsa."""
    with _token_lock:
        auth = load_auth()
        if not auth.get("refresh_token"):
            raise GogAuthError("Not logged in.")
        if auth.get("access_token") and auth.get("expires_at", 0) - 60 > time.time():
            return auth["access_token"]
        try:
            auth = _token_request(cfg, {"grant_type": "refresh_token",
                                        "refresh_token": auth["refresh_token"]}, keep=auth)
        except GogError:
            clear_auth()
            raise GogAuthError("Session expired. Please log in again.")
        return auth["access_token"]


def fetch_library(cfg, token):
    games, page, total = [], 1, 1
    while page <= total:
        query = urllib.parse.urlencode({"mediaType": 1, "sortBy": "title", "page": page})
        data = _http_json(cfg, f"{cfg['library_url']}?{query}", token)
        total = int(data.get("totalPages", 1))
        for p in data.get("products", []):
            works = p.get("worksOn") or {}
            platforms = [n for n in ("Windows", "Mac", "Linux") if works.get(n)]
            games.append({"id": p["id"], "title": p.get("title", str(p["id"])),
                          "platforms": ", ".join(platforms), "image": p.get("image") or ""})
        page += 1
    return games


def fetch_installers(cfg, token, product_id):
    """Daftar installer game ini, disaring sesuai cfg['os'] / cfg['language'] (kalau hasil saringan
    kosong, semua installer dikembalikan supaya pengguna tetap bisa memilih)."""
    url = cfg["product_url"].format(id=product_id) + "?expand=downloads&locale=en-US"
    installers = ((_http_json(cfg, url, token).get("downloads") or {}).get("installers")) or []
    want_os, want_lang = cfg["os"].lower(), cfg["language"].lower()
    filtered = [i for i in installers
                if (not want_os or str(i.get("os", "")).lower() == want_os)
                and (not want_lang or str(i.get("language", "")).lower() == want_lang)]
    return filtered or installers


def resolve_downlink(cfg, token, file_entry):
    """Tukar entri 'downlink' sebuah file installer jadi URL CDN sementara (berlaku singkat,
    jadi selalu di-resolve tepat sebelum file itu didownload)."""
    data = _http_json(cfg, file_entry["downlink"], token)
    url = data.get("downlink")
    if not url:
        raise GogError("GOG did not return a download link.")
    return url


# --------------------------------------------------------------------------- cover art

_cover_pref = {"suffix": None}   # akhiran URL yang terakhir berhasil (dicoba duluan berikutnya)
_COVER_MAX_BYTES = 8 * 1024 * 1024
_RESAMPLE = None


def _resample():
    global _RESAMPLE
    if _RESAMPLE is None:
        _RESAMPLE = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    return _RESAMPLE


def cover_base_url(image):
    """Field 'image' dari API (mis. '//images-2.gog.com/<hash>') -> URL https tanpa ekstensi."""
    image = (image or "").strip()
    if not image:
        return ""
    if image.startswith("//"):
        image = "https:" + image
    elif image.startswith("http://"):
        image = "https://" + image[len("http://"):]
    elif not image.startswith("https://"):
        image = "https://images.gog.com/" + image.lstrip("/")
    return re.sub(r"\.(?:jpe?g|png|webp)$", "", image)


def _cover_suffixes(cfg):
    sufs = [x for x in (cfg.get("cover_suffixes") or []) if isinstance(x, str) and x]
    pref = _cover_pref["suffix"]
    if pref in sufs:
        sufs.remove(pref)
        sufs.insert(0, pref)
    return sufs


def _download_cover_image(cfg, base):
    """Coba tiap akhiran sampai ada yang berupa gambar valid. Return PIL.Image (RGB) atau None.
    Error jaringan (offline/timeout) dilempar supaya tidak menunggu timeout berkali-kali."""
    for suffix in _cover_suffixes(cfg):
        req = urllib.request.Request(base + suffix, headers={"User-Agent": cfg["user_agent"]})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read(_COVER_MAX_BYTES + 1)
        except urllib.error.HTTPError:
            continue  # 404 dll: coba akhiran berikutnya
        if len(data) > _COVER_MAX_BYTES:
            continue
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
            img = img.convert("RGB")
        except Exception:
            continue
        _cover_pref["suffix"] = suffix
        return img
    return None


def load_cover(cfg, game, width, height):
    """Cover game sebagai PIL.Image persis (width x height) (dipotong 'cover-fit'), atau None kalau
    game tidak punya gambar / tidak ditemukan. Memakai cache ~/wlm/gog_covers/<id>.jpg.
    BLOCKING - panggil dari background thread."""
    cache_dir = Path(_host.data_dir) / "gog_covers"
    cache_path = cache_dir / f"{game['id']}.jpg"
    img = None
    if cache_path.exists():
        try:
            img = Image.open(cache_path)
            img.load()
            img = img.convert("RGB")
        except Exception:
            cache_path.unlink(missing_ok=True)  # cache rusak -> unduh ulang
            img = None
    if img is None:
        base = cover_base_url(game.get("image"))
        if not base:
            return None
        img = _download_cover_image(cfg, base)
        if img is None:
            return None
        img.thumbnail((420, 420), _resample())  # simpan kecil saja; asli bisa besar
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_name(cache_path.name + ".tmp")
            img.save(tmp, "JPEG", quality=88)
            tmp.replace(cache_path)
        except Exception as e:
            print(f"[WLM/GOG] Could not cache cover {cache_path}: {e}")
    return ImageOps.fit(img, (width, height), method=_resample())


# --------------------------------------------------------------------------- download

def _safe_name(text, fallback):
    return re.sub(r"[^\w\-. ]", "", text).strip() or str(fallback)


def _range_total(headers):
    """Ukuran total file dari 'Content-Range: bytes a-b/TOTAL' atau 'bytes */TOTAL' (None jika tak ada)."""
    m = re.search(r"/(\d+)\s*$", headers.get("Content-Range") or "")
    return int(m.group(1)) if m else None


def _download_file(cfg, url, path, expected, on_progress, _retry=True, cancel=None, on_bytes=None):
    """Download satu file ke path (dengan resume lewat file .part). on_progress(bytes_di_file_ini).
    Return ukuran akhir file (byte).

    Kelengkapan file dicek terhadap ukuran dari SERVER (Content-Length / Content-Range), BUKAN
    'expected' (field size dari metadata API GOG, yang kadang tidak akurat). 'expected' hanya
    dipakai pemanggil untuk info. Kalau koneksi putus di tengah, file .part dibiarkan supaya
    percobaan berikutnya melanjutkan (resume)."""
    part = path.with_name(path.name + ".part")
    offset = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": cfg["user_agent"]}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    try:
        resp = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30)
    except urllib.error.HTTPError as e:
        if e.code == 416:  # Range di luar ukuran file
            if offset and _range_total(e.headers) == offset:
                part.replace(path)  # .part sebenarnya sudah utuh
                return offset
            part.unlink(missing_ok=True)
            if _retry:  # .part tidak cocok dengan file di server -> ulangi dari awal
                return _download_file(cfg, url, path, expected, on_progress, _retry=False, cancel=cancel,
                                      on_bytes=on_bytes)
            raise GogError("Partial download was invalid and has been removed. Please try again.")
        raise GogError(f"Download failed (HTTP {e.code}). The link may have expired - try again.")
    except urllib.error.URLError as e:
        raise GogError(f"Could not reach the GOG download server: {e.reason}")
    with resp:
        length = resp.headers.get("Content-Length")
        length = int(length) if length and length.isdigit() else None
        if offset and resp.status == 206:
            server_total = _range_total(resp.headers) or (offset + length if length is not None else None)
        else:
            offset = 0  # server mengabaikan Range (atau tanpa resume) -> mulai dari awal
            server_total = length
        done = offset
        last = 0.0
        try:
            with open(part, "ab" if offset else "wb") as out:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise DownloadCancelled("Download cancelled.")
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if on_bytes is not None:
                        on_bytes(len(chunk))
                    if time.time() - last > 0.3:
                        on_progress(done)
                        last = time.time()
        except (OSError, http.client.HTTPException) as e:
            raise GogError(f"Connection lost while downloading {path.name} ({e}). "
                           "Click Download again to resume.")
        on_progress(done)
    size = part.stat().st_size
    if server_total is not None and size != server_total:
        raise GogError(f"Download of {path.name} is incomplete (got {size} of {server_total} bytes). "
                       "Click Download again to resume.")
    if size == 0:
        raise GogError(f"The server sent an empty file for {path.name}.")
    part.replace(path)
    return size


# --------------------------------------------------------------------------- download manager
#
# Semua download di sesi ini disimpan di _jobs (aktif + selesai), TIDAK terikat pada jendela GOG
# Store: jendela itu boleh ditutup, download tetap jalan dan daftarnya bisa dilihat lagi lewat
# tombol "Downloads". Pause menghentikan download tapi file .part dibiarkan, jadi Resume
# melanjutkan dari byte terakhir. Cancel menghentikan download DAN membuang file .part-nya
# (Resume sesudah Cancel mengulang dari awal).

_jobs = []
_job_ids = itertools.count(1)
_ACTIVE = ("queued", "running")
_STATUS_TEXT = {"queued": "Queued", "running": "Downloading", "paused": "Paused",
                "done": "Completed", "failed": "Failed", "cancelled": "Cancelled"}
_RESTARTABLE = ("failed", "cancelled", "paused")
_downloads_win = None
_jobs_loaded = False
_jobs_lock = threading.RLock()
_closing = False              # launcher sedang ditutup: worker tidak boleh menjadwalkan callback Tk lagi
_last_save = 0.0
# Field job yang disimpan ke gog_downloads.json (cfg, Event, thread, dll tidak ikut disimpan).
_SAVED_KEYS = ("id", "game_id", "title", "game", "kind", "installer", "direct", "choice", "status",
               "note", "progress", "done_bytes", "total_bytes", "error", "exe")
# SEMUA job disimpan (termasuk yang selesai/dibatalkan) - daftar Downloads tetap ada sampai
# pengguna menghapusnya sendiri (Remove / Clear Finished).
_KEPT_STATUS = ("queued", "running", "paused", "failed", "done", "cancelled")


def _jobs_path():
    return Path(_host.data_dir) / "gog_downloads.json"


def _ui_path():
    return Path(_host.data_dir) / "gog_ui.json"


def _ui_read():
    try:
        data = json.loads(_ui_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _ui_load(section):
    """Pengaturan tampilan tersimpan (dict kosong kalau belum ada / rusak)."""
    val = _ui_read().get(section)
    return val if isinstance(val, dict) else {}


def _ui_save(section, value):
    try:
        data = _ui_read()
        data[section] = value
        tmp = _ui_path().with_name("gog_ui.json.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, _ui_path())
    except OSError:
        pass


def _save_jobs():
    """Simpan download yang belum selesai ke disk, supaya tetap ada (sebagai Paused) walau launcher
    ditutup atau crash. Aman dipanggil dari thread manapun."""
    global _last_save
    if not _jobs_loaded or _host is None:
        return
    with _jobs_lock:
        _last_save = time.time()
        items = []
        for j in list(_jobs):
            if j["status"] not in _KEPT_STATUS:
                continue
            item = {k: j.get(k) for k in _SAVED_KEYS}
            item["dest"] = str(j["dest"])
            item["exe"] = str(j["exe"]) if j.get("exe") else None
            item["log"] = list(j["log"])[-300:]
            items.append(item)
        try:
            path = _jobs_path()
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps({"version": 1, "jobs": items}, indent=1, default=str),
                           encoding="utf-8")
            os.replace(tmp, path)
        except (OSError, TypeError, ValueError) as e:
            print(f"[WLM/GOG] Could not save the download list: {e}")


def _ensure_jobs_loaded():
    """Baca gog_downloads.json SEKALI per sesi. Download yang terputus saat launcher ditutup
    (queued/running) dipulihkan sebagai Paused; file .part-nya masih ada, jadi Resume melanjutkan
    dari titik terakhir."""
    global _jobs_loaded, _job_ids
    if _jobs_loaded or _host is None:
        return
    _jobs_loaded = True
    try:
        raw = json.loads(_jobs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    items = raw.get("jobs") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return
    cfg = load_config()
    top = 0
    for it in items:
        try:
            kind = it.get("kind") or "setup"
            saved = it.get("status")
            # Job yang masih bisa dilanjutkan butuh data download-nya; yang selesai/dibatalkan tidak.
            if saved not in ("done", "cancelled") and (
                    (kind == "direct" and not it.get("direct"))
                    or (kind != "direct" and not it.get("installer"))):
                continue
            interrupted = saved in ("queued", "running")
            failed = saved == "failed"
            status = saved if saved in ("done", "cancelled", "failed") else "paused"
            gid = it["game_id"]
            job = {"id": int(it["id"]), "cfg": cfg, "game_id": gid, "title": str(it["title"]),
                   "game": it.get("game") or {"id": gid, "title": str(it["title"])},
                   "kind": kind, "installer": it.get("installer"), "direct": it.get("direct"),
                   "dest": Path(it["dest"]), "choice": it.get("choice"),
                   "status": status,
                   "note": (str(it.get("note") or "") if status == "done"
                            and not str(it.get("note") or "").endswith("...") else ""),
                   "cancelling": False, "pausing": False, "discard": False,
                   "cancel": threading.Event(), "progress": float(it.get("progress") or 0),
                   "done_bytes": int(it.get("done_bytes") or 0),
                   "total_bytes": int(it.get("total_bytes") or 0), "speed": 0.0,
                   "error": it.get("error") if failed else None,
                   "exe": Path(it["exe"]) if it.get("exe") else None,
                   "log": [str(x) for x in (it.get("log") or [])]}
        except (KeyError, TypeError, ValueError):
            continue
        if interrupted:
            _job_log(job, "")
            _job_log(job, "The launcher was closed during this download - it is paused. "
                          "Click Resume to continue where it stopped.")
        _jobs.append(job)
        top = max(top, job["id"])
    if top:
        _job_ids = itertools.count(top + 1)


def _post(fn):
    """Jadwalkan fn di main thread (Tk). Diabaikan kalau launcher sedang ditutup."""
    if _closing:
        return
    try:
        _host.root.after(0, fn)
    except (RuntimeError, tk.TclError):
        pass


def shutdown_downloads(timeout=4.0):
    """Dipanggil launcher saat jendela utama ditutup: semua download yang berjalan/antre dijeda
    (file .part dipertahankan) lalu daftarnya disimpan. Saat launcher dijalankan lagi, download
    itu muncul sebagai Paused di jendela Downloads dan bisa di-Resume."""
    global _closing
    if not _jobs_loaded:
        return
    _closing = True
    running = [j for j in _jobs if j["status"] == "running"]
    for j in _jobs:
        if j["status"] == "queued":
            j["status"] = "paused"
    for j in running:
        if not j["discard"]:                    # Cancel yang sedang berjalan tetap Cancel
            j["cancelling"], j["pausing"] = True, True
        j["cancel"].set()                       # worker berhenti di chunk berikutnya
    deadline = time.time() + timeout
    for j in running:
        t = j.get("_thread")
        if t is not None:
            t.join(max(0.0, deadline - time.time()))
        if j["discard"]:
            j["status"] = "cancelled"
            _discard_partial(j)
        else:
            j["status"] = "paused"
            _job_log(j, "Paused because the launcher was closed.")
        j["cancelling"] = j["pausing"] = j["discard"] = False
        j["speed"] = 0.0
    _save_jobs()


def _job_log(job, text=""):
    job["log"].append(text)


def active_jobs():
    return [j for j in _jobs if j["status"] in _ACTIVE]


def _holds_game(job):
    """Job yang masih memegang file game-nya (aktif atau di-pause): game yang sama tidak boleh
    didaftarkan lagi selama job ini belum di-Cancel/Remove."""
    return job["status"] in _ACTIVE or job["status"] == "paused"


def _max_parallel(cfg):
    try:
        return max(1, int(cfg.get("max_downloads") or 1))
    except (TypeError, ValueError):
        return 1


def _job_progress(job, done, total, sample=True):
    """Update byte/persen sebuah job (aman dipanggil dari thread download). Kecepatan TIDAK
    dihitung disini: byte yang ditulis ke disk (terdekompresi, urut) bukan lalu lintas internet
    sebenarnya - lihat _net_add/_net_speed."""
    now = time.time()
    job["done_bytes"] = done
    job["progress"] = min(100.0, done * 100 / total) if total else 0.0
    if now - _last_save > 5:
        _save_jobs()


# --- kecepatan & ukuran download yang sebenarnya (lalu lintas jaringan) ---------------------
_net_lock = threading.Lock()


def _net_reset(job):
    with _net_lock:
        job["_net"] = deque([(time.time(), 0)])
        job["net_total"] = 0


def _net_add(job, n):
    """Catat n byte yang BARU diterima dari jaringan (dipanggil dari thread download)."""
    now = time.time()
    with _net_lock:
        s = job.get("_net")
        if s is None:
            s = job["_net"] = deque([(now, 0)])
        cum = job.get("net_total", 0) + n
        job["net_total"] = cum
        s.append((now, cum))
        while len(s) > 1 and s[1][0] <= now - 8:
            s.popleft()


def _net_speed(job, window=5.0):
    """Kecepatan unduh jaringan rata-rata ~5 detik terakhir, byte/detik (0 kalau tidak ada data)."""
    with _net_lock:
        s = job.get("_net")
        if not s:
            return 0.0
        now = time.time()
        while len(s) > 1 and s[1][0] <= now - window:
            s.popleft()
        t0, b0 = s[0]
        t1, b1 = s[-1]
        if now - t1 > 3.0 or b1 <= b0:
            return 0.0
        return (b1 - b0) / max(now - t0, 0.5)


def _dl_total(job):
    """Ukuran yang benar-benar diunduh lewat internet. Direct Download: total 'compressedSize'
    depot (sama dengan 'Download: about ...' di dialog), bukan ukuran install yang lebih besar."""
    if job.get("kind") == "direct":
        depots = (job.get("direct") or {}).get("depots") or []
        comp = sum(int(d.get("compressedSize") or 0) for d in depots)
        if comp:
            return comp
    return job["total_bytes"]


def _dl_done(job):
    total = _dl_total(job)
    if job.get("kind") == "direct" and job["total_bytes"]:
        return min(total, int(total * job["done_bytes"] / job["total_bytes"]))
    return job["done_bytes"]


def _fmt_eta(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _bar(pct, width=10):
    """Bar teks untuk kolom Progress (Treeview tidak bisa menampung widget)."""
    filled = int(round(max(0.0, min(100.0, pct)) / 100 * width))
    return "\u2588" * filled + "\u2591" * (width - filled) + f" {pct:.0f}%"


def _new_job(cfg, game, dest, choice, kind, installer=None, direct=None, total=0):
    """Buat + daftarkan job baru (mulai langsung, atau antre kalau slot penuh). Return job."""
    _ensure_jobs_loaded()
    for j in _jobs:
        if _holds_game(j) and str(j["game_id"]) == str(game["id"]):
            raise GogError(f"'{game['title']}' is already in the download list "
                           f"({_STATUS_TEXT[j['status']].lower()}).")
    job = {"id": next(_job_ids), "cfg": cfg, "game_id": game["id"], "title": game["title"],
           "game": game, "kind": kind, "installer": installer, "direct": direct,
           "dest": Path(dest), "choice": choice,
           "status": "queued", "note": "",
           "cancelling": False, "pausing": False, "discard": False,
           "cancel": threading.Event(), "progress": 0.0, "done_bytes": 0,
           "total_bytes": total, "speed": 0.0, "error": None, "exe": None, "log": []}
    _job_log(job, f"Game: {game['title']}")
    _jobs.append(job)
    _pump()
    _save_jobs()
    return job


def add_download(cfg, game, installer, dest, choice=None):
    """Daftarkan download SETUP (installer) baru. Return job."""
    total = sum(int(f.get("size", 0)) for f in installer.get("files") or [])
    return _new_job(cfg, game, dest, choice, "setup", installer=installer, total=total)


def add_direct_download(cfg, game, direct, dest, choice=None):
    """Daftarkan DIRECT download (file game langsung, tanpa installer). Return job."""
    total = sum(int(d.get("size") or 0) for d in direct["depots"])
    return _new_job(cfg, game, dest, choice, "direct", direct=direct, total=total)


def _pump():
    """Mulai job yang antre selama slot download masih tersedia. Main thread saja."""
    running = sum(1 for j in _jobs if j["status"] == "running")
    for job in _jobs:
        if job["status"] != "queued":
            continue
        if running >= _max_parallel(job["cfg"]):
            break
        job["status"], job["cancelling"], job["speed"] = "running", False, 0.0
        job["_last_t"] = job["_last_b"] = None
        _net_reset(job)
        job["_thread"] = threading.Thread(target=_download_worker, args=(job,), daemon=True)
        job["_thread"].start()
        running += 1


def pause_job(job):
    """Jeda download: koneksi ditutup, file .part dipertahankan. Resume melanjutkan dari sana."""
    if job["status"] == "queued":
        job["status"] = "paused"
        _job_log(job, "Paused before it started.")
        _save_jobs()
    elif job["status"] == "running" and not job["cancelling"]:
        job["cancelling"] = job["pausing"] = True
        job["cancel"].set()


def cancel_job(job):
    """Batalkan download dan BUANG file .part-nya. Boleh dipanggil untuk job yang antre,
    berjalan (juga yang sedang 'Pausing...'), atau sudah di-pause."""
    if job["status"] in ("queued", "paused"):
        job["status"], job["pausing"] = "cancelled", False
        _discard_partial(job)
        _job_log(job, "Cancelled. Partial files were deleted.")
        _save_jobs()
    elif job["status"] == "running" and not job["discard"]:
        job["cancelling"], job["discard"], job["pausing"] = True, True, False
        job["cancel"].set()


def resume_job(job):
    """Antrekan lagi job yang gagal/di-pause/dibatalkan; file .part (kalau ada) dilanjutkan."""
    if job["status"] not in _RESTARTABLE:
        return
    job.update(status="queued", cancelling=False, pausing=False, discard=False, error=None,
               note="", speed=0.0, cancel=threading.Event())
    _job_log(job, "")
    _job_log(job, "Resuming...")
    _pump()
    _save_jobs()


def _cancel_message(job):
    """Pesan log saat worker berhenti karena Pause atau Cancel."""
    if job.get("discard"):
        return "Cancelled. Partial files will be deleted."
    return "Paused. Partial files were kept - use Resume to continue where it stopped."


def _part_files(job):
    """File .part sebuah job (Direct Download menyimpannya di sub-folder juga)."""
    d = Path(job["dest"])
    if not d.is_dir():
        return []
    return list(d.rglob("*.part")) if job.get("kind") == "direct" else list(d.glob("*.part"))


def _has_partial(job):
    return bool(_part_files(job))


def _discard_partial(job):
    """Hapus file .part sebuah job, lalu foldernya kalau sudah kosong (installer/file yang
    sudah lengkap tidak disentuh)."""
    try:
        for part in _part_files(job):
            part.unlink(missing_ok=True)
        if job.get("kind") == "direct":       # sub-folder kosong sisa Direct Download
            for d in sorted((x for x in Path(job["dest"]).rglob("*") if x.is_dir()),
                            key=lambda x: len(x.parts), reverse=True):
                try:
                    d.rmdir()
                except OSError:
                    pass
        Path(job["dest"]).rmdir()
    except OSError:
        pass
    job["done_bytes"], job["progress"] = 0, 0.0


def remove_job(job, delete_partial=False):
    """Hapus job dari daftar (hanya yang tidak aktif). File .part ikut dihapus kalau diminta;
    installer yang sudah lengkap tidak pernah disentuh."""
    if job["status"] in _ACTIVE:
        return
    if delete_partial:
        try:
            for part in _part_files(job):
                part.unlink(missing_ok=True)
            Path(job["dest"]).rmdir()   # hanya berhasil kalau folder sudah kosong
        except OSError:
            pass
    if job in _jobs:
        _jobs.remove(job)
    _save_jobs()


def _finish_job(job, status, exe=None, err=None):
    """Dipanggil di main thread begitu worker sebuah job selesai/gagal/dibatalkan."""
    discard = job.get("discard")
    if status == "cancelled" and not discard:
        status = "paused"                  # berhenti karena Pause, bukan Cancel
    job["status"], job["cancelling"], job["speed"] = status, False, 0.0
    job["pausing"], job["discard"] = False, False
    job["exe"], job["error"] = exe, err
    title = job["title"]
    if status == "done":
        job["progress"] = 100.0
        _host.status(f"'{title}' downloaded to {job['dest']}", "success")
        if job.get("kind") == "direct":
            _after_direct(job)
        else:
            _after_download(job)
    elif status == "paused":
        _host.status(f"Download of '{title}' paused.", "warning")
    elif status == "cancelled":
        _discard_partial(job)
        _host.status(f"Download of '{title}' cancelled.", "warning")
    else:
        _host.status(f"GOG download failed: {err}", "danger")
    _pump()
    _save_jobs()


def _after_download(job):
    exe, choice = job["exe"], job["choice"]
    if not exe:
        return
    if choice is None:
        if messagebox.askyesno(
                "Run Installer?",
                f"'{job['title']}' finished downloading.\n\nRun the installer now with Wine/Proton?",
                parent=_host.root):
            _host.install_exe(str(exe))
        return
    # Runner sudah dipilih sebelum download -> langsung install, tanpa tanya lagi.
    try:
        args = shlex.split(job["cfg"].get("install_args") or "")
    except ValueError:
        args = []
    _job_log(job, "")
    _job_log(job, f"Starting installer via {choice.get('runner')} "
                  f"(prefix: {choice.get('prefix_code') or 'default'})...")
    try:
        proc = _host.install_exe(str(exe), choice=choice, installer_args=args, auto_shortcut=False)
    except Exception as e:
        _job_log(job, f"Could not start the installer: {e}")
        return
    if proc is None:
        _job_log(job, "The installer was not started.")
        return
    if (job["cfg"].get("auto_shortcut", True) and install_watch is not None
            and getattr(_host, "add_shortcut", None)):
        job["note"] = "Completed - installing..."
        threading.Thread(target=_install_watcher, args=(job, proc, time.time()), daemon=True).start()


# --------------------------------------------------------------------------- auto shortcut
#
# Deteksi game yang baru terpasang ada di install_watch.py (dipakai juga oleh install manual di
# launcher.py). Disini hanya: pantau installer, ambil cover sebagai ikon, lalu host.add_shortcut().

def _install_watcher(job, proc, started):
    """Thread: tunggu game terdeteksi terpasang (atau installer berhenti), lalu tambahkan shortcut."""
    res = install_watch.wait_for_install(proc, job["choice"], job["game_id"], started)
    icon = None
    if HAVE_PIL:
        try:
            icon = load_cover(job["cfg"], job["game"], 250, 250)
        except Exception:
            icon = None
    _host.root.after(0, lambda: _finish_install(job, res, icon))


def _rel(path, base):
    try:
        return str(Path(path).relative_to(base))
    except (ValueError, TypeError):
        return str(path)


def _finish_install(job, res, icon):
    """Main thread: buat shortcut dari hasil deteksi (atau tanya kalau tidak ketemu pasti)."""
    found, rc, drive_c = res["found"], res["rc"], res["drive_c"]
    if found is None:
        if rc != 0:
            job["note"] = "Completed - installer closed"
            _job_log(job, f"Installer exited (code {rc}); no installed game was found, so no shortcut was added.")
            return
        if res["candidates"]:
            best = res["candidates"][0]["exe"]
            if messagebox.askyesno(
                    "Add Shortcut",
                    f"'{job['title']}' was installed, but no GOG info file was found.\n\n"
                    f"Use this executable for the launcher shortcut?\n{_rel(best, drive_c)}",
                    parent=_host.root):
                found = {"exe": best, "workdir": None, "icon": None}
        if found is None:
            if messagebox.askyesno(
                    "Add Shortcut",
                    f"The installer finished, but '{job['title']}' could not be located automatically.\n\n"
                    "Select its .exe now to add a launcher shortcut?", parent=_host.root):
                start = str(drive_c) if drive_c and Path(drive_c).is_dir() else str(Path.home())
                picked = filedialog.askopenfilename(
                    parent=_host.root, title=f"Select the game executable - {job['title']}",
                    initialdir=start, filetypes=[("Executable Files", "*.exe"), ("All Files", "*.*")])
                if picked:
                    found = {"exe": Path(picked), "workdir": None, "icon": None}
        if found is None:
            job["note"] = "Completed - installed (no shortcut)"
            return
    _job_log(job, f"Game found: {found['exe']}")
    try:
        name = _host.add_shortcut(job["title"], found["exe"], job["choice"],
                                  icon_image=icon or found.get("icon"), work_dir=found.get("workdir"),
                                  source="gog", gog_id=job["game_id"])
    except Exception as e:
        job["note"] = "Completed - shortcut failed"
        _job_log(job, f"Could not add the shortcut: {e}")
        _host.status(f"Could not add shortcut for '{job['title']}': {e}", "danger")
        return
    job["note"] = "Completed - shortcut added"
    _job_log(job, f"Shortcut added to the launcher as '{name}'.")
    _host.status(f"'{name}' was added to the launcher.", "success")


def _download_worker(job):
    """Berjalan di background thread: download semua file installer sebuah job. Status akhir
    dikirim ke main thread lewat _finish_job()."""
    if job.get("kind") == "direct":
        return _direct_worker(job)
    cfg, installer, dest, cancel = job["cfg"], job["installer"], job["dest"], job["cancel"]

    def log(text=""):
        _job_log(job, text)

    def finish(status, exe=None, err=None):
        _post(lambda: _finish_job(job, status, exe, err))

    exe_path = None
    try:
        dest.mkdir(parents=True, exist_ok=True)
        files = installer.get("files") or []
        total = sum(int(f.get("size", 0)) for f in files)
        job["total_bytes"] = total
        base = 0
        log(f"Saving to: {dest}")
        log(f"{len(files)} file(s), {_host.human_size(total)} total")
        log("")
        for f in files:
            if cancel.is_set():
                raise DownloadCancelled("Download cancelled.")
            token = get_access_token(cfg)
            url = resolve_downlink(cfg, token, f)
            name = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name or f"{f.get('id')}.bin"
            path = dest / name
            size = int(f.get("size", 0))
            job["_last_t"] = None   # acuan kecepatan diulang tiap file (resume/skip melompatkan byte)
            # File final hanya ada setelah download selesai & lolos cek (rename dari .part), jadi
            # keberadaannya cukup - tidak dibandingkan ke 'size' metadata yang bisa meleset.
            if (path.exists() and path.stat().st_size > 0
                    and not path.with_name(path.name + ".part").exists()):
                log(f"Skipping {name} (already downloaded)")
                actual = path.stat().st_size
            else:
                log(f"Downloading {name} ({_host.human_size(size)})...")

                def progress(done, base=base):
                    _job_progress(job, base + done, total)
                actual = _download_file(cfg, url, path, size, progress, cancel=cancel,
                                        on_bytes=lambda n: _net_add(job, n))
                if size and actual != size:
                    log(f"  note: GOG listed {_host.human_size(size)} but the server sent "
                        f"{_host.human_size(actual)}; the server's size was used.")
                log(f"  done: {name}")
            base += actual
            _job_progress(job, base, total, sample=False)
            if path.suffix.lower() == ".exe" and exe_path is None:
                exe_path = path
        log("")
        log("Download complete.")
        finish("done", exe_path)
    except DownloadCancelled:
        log("")
        log(_cancel_message(job))
        finish("cancelled")
    except Exception as e:
        log("")
        log(f"ERROR: {e}")
        finish("failed", None, str(e))


# --------------------------------------------------------------------------- direct download
#
# Direct Download = unduh FILE GAME langsung dari sistem konten GOG Galaxy generasi 2 (tanpa
# installer). Alurnya:
#   1. daftar build  : GET {content_system_url}/products/<id>/os/<os>/builds?generation=2
#   2. manifest build: zlib-JSON dari link build -> daftar depot (bahasa, DLC, ukuran)
#   3. manifest depot: zlib-JSON -> daftar file; tiap file = urutan chunk (md5 + compressedMd5)
#   4. secure link   : GET .../products/<id>/secure_link -> URL CDN bertoken (kedaluwarsa, diperbarui)
#   5. tiap chunk    : {base}/{aa}/{bb}/{compressedMd5}, zlib -> disambung jadi file
# File kecil digabung GOG dalam satu 'smallFilesContainer' (dirujuk lewat sfcRef offset/size).
# Resume: file .part selalu berakhir di batas chunk, jadi lanjutannya mulai dari chunk berikutnya.

_UNIVERSAL_LANGS = ("*", "neutral", "")
_LOG_FILE_MIN = 64 * 1024 * 1024    # hanya file sebesar ini yang dicatat satu-satu di log


def _fmt_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _galaxy_path(h):
    return f"{h[:2]}/{h[2:4]}/{h}"


def _platform(cfg):
    p = str(cfg.get("os") or "").strip().lower() or "windows"
    return "osx" if p in ("mac", "macos") else p


def _fetch_bytes(cfg, url, token=None, timeout=30, on_bytes=None, cancel=None):
    """Unduh seluruh isi url. Kalau on_bytes/cancel diberikan, dibaca bertahap (64 KB): on_bytes(n)
    dipanggil untuk tiap potongan yang diterima (dasar kecepatan download asli) dan cancel dicek
    terus, jadi Pause/Cancel berlaku seketika - tidak menunggu satu chunk besar selesai."""
    headers = {"User-Agent": cfg["user_agent"]}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
            if on_bytes is None and cancel is None:
                return resp.read()
            parts = []
            while True:
                if cancel is not None and cancel.is_set():
                    raise DownloadCancelled("Download cancelled.")
                piece = resp.read(64 * 1024)
                if not piece:
                    break
                parts.append(piece)
                if on_bytes is not None:
                    on_bytes(len(piece))
            return b"".join(parts)
    except urllib.error.HTTPError as e:
        raise GogError(f"HTTP {e.code} from {urllib.parse.urlparse(url).netloc}")
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise GogError(f"Connection problem ({getattr(e, 'reason', e)})")


def _decode_meta(data):
    """Manifest GOG = JSON yang dikompres zlib (kadang gzip / sudah polos)."""
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    else:
        try:
            data = zlib.decompress(data)
        except zlib.error:
            pass
    try:
        return json.loads(data.decode("utf-8"))
    except ValueError:
        raise GogError("GOG sent a manifest that could not be read.")


def fetch_builds(cfg, token, product_id):
    """Build generasi 2 sebuah game (terbaru dulu). Kosong = game ini tidak punya build yang
    bisa di-direct-download (mis. hanya format lama / tidak ada build untuk OS itu)."""
    url = (f"{cfg['content_system_url'].rstrip('/')}/products/{product_id}/os/{_platform(cfg)}/builds?"
           + urllib.parse.urlencode({"generation": 2}))
    try:
        data = _http_json(cfg, url, token)
    except GogError as e:
        if "HTTP 404" in str(e):
            return []
        raise
    builds = [b for b in data.get("items") or [] if str(b.get("generation", 2)) == "2" and b.get("link")]
    builds.sort(key=lambda b: str(b.get("date_published") or ""), reverse=True)
    return builds


def _build_label(b):
    name = b.get("version_name") or f"build {b.get('build_id')}"
    date = str(b.get("date_published") or "")[:10]
    branch = b.get("branch")
    return " - ".join(x for x in (str(name), date, f"branch: {branch}" if branch else "") if x)


def fetch_build_meta(cfg, build):
    return _decode_meta(_fetch_bytes(cfg, build["link"]))


def meta_base_url(cfg, build):
    """Folder 'meta' CDN dari link build (.../meta/aa/bb/<hash>), dipakai untuk manifest depot."""
    parts = str(build.get("link") or "").rstrip("/").rsplit("/", 3)
    if len(parts) == 4 and parts[3][:2] == parts[1] and parts[3][2:4] == parts[2]:
        return parts[0]
    return cfg["depot_meta_url"].rstrip("/")


def fetch_depot(cfg, meta_base, depot):
    data = _decode_meta(_fetch_bytes(cfg, f"{meta_base}/{_galaxy_path(depot['manifest'])}"))
    return data.get("depot") or data


def fetch_owned_ids(cfg, token):
    """Id game + DLC yang dimiliki akun (agar depot DLC ikut). Gagal -> kosong (hanya game utama)."""
    try:
        return {str(x) for x in (_http_json(cfg, cfg["owned_url"], token).get("owned") or [])}
    except GogError:
        return set()


def _lang_ok(depot, language):
    if not language:
        return True
    langs = depot.get("languages") or []
    if not langs:
        return True
    want = language.lower()
    return any(str(l).lower() in _UNIVERSAL_LANGS or str(l).lower() == want for l in langs)


def select_depots(meta, language, owned, base_id):
    """Depot yang dipakai: game utama + DLC yang dimiliki, disaring bahasa (None = semua)."""
    base_id = str(meta.get("baseProductId") or base_id)
    out = []
    for d in meta.get("depots") or []:
        pid = str(d.get("productId") or base_id)
        if (pid == base_id or pid in owned) and d.get("manifest") and _lang_ok(d, language):
            out.append(d)
    return out


def meta_languages(meta):
    seen = {}
    for d in meta.get("depots") or []:
        for l in d.get("languages") or []:
            if str(l).lower() not in _UNIVERSAL_LANGS:
                seen[str(l)] = True
    return sorted(seen)


def _default_lang_index(cfg, langs):
    """Index di daftar 'All languages' + langs. Bawaan: bahasa di cfg['language'], kalau kosong English."""
    def base(x):
        return x.lower().replace("_", "-").split("-")[0]
    want = base(str(cfg.get("language") or "") or "en")
    for i, l in enumerate(langs):
        if base(l) == want:
            return i + 1
    return 0


class _SecureLinks:
    """URL CDN bertoken untuk satu product id. Kedaluwarsa, jadi diperbarui otomatis / paksa."""

    def __init__(self, cfg, product_id):
        self.cfg, self.pid = cfg, str(product_id)
        self._lock = threading.Lock()
        self._bases, self._expires = [], 0.0

    def bases(self, force=False):
        with self._lock:
            if force or not self._bases or time.time() >= self._expires:
                self._refresh()
            return list(self._bases)

    def _refresh(self):
        cfg = self.cfg
        url = (f"{cfg['content_system_url'].rstrip('/')}/products/{self.pid}/secure_link?"
               + urllib.parse.urlencode({"_version": 2, "generation": 2, "path": "/"}))
        data = _http_json(cfg, url, get_access_token(cfg))
        bases, expires = [], time.time() + 900
        for u in sorted(data.get("urls") or [], key=lambda x: x.get("priority", 0)):
            params = u.get("parameters") or {}
            base = str(u.get("url_format") or "")
            for k, v in params.items():
                base = base.replace("{" + k + "}", str(v))
            if not base or "{" in base:
                continue
            bases.append(base.rstrip("/"))
            try:
                expires = min(expires, float(params["expires_at"]) - 60)
            except (KeyError, TypeError, ValueError):
                pass
        if not bases:
            raise GogError("GOG did not return a download server for this game.")
        self._bases, self._expires = bases, expires


def _fetch_chunk(cfg, links, chunk, cancel, on_bytes=None):
    """Satu chunk: unduh, cek md5 (terkompres + hasil dekompresi), dekompres. Coba ulang dan
    ganti server/secure link kalau gagal."""
    gp = _galaxy_path(chunk["compressedMd5"])
    last = None
    for attempt in range(4):
        if cancel.is_set():
            raise DownloadCancelled("Download cancelled.")
        if attempt:
            time.sleep(min(attempt, 3))
        try:
            bases = links.bases(force=attempt >= 2)
        except GogAuthError:
            raise
        except GogError as e:
            last = e
            continue
        for base in bases:
            try:
                data = _fetch_bytes(cfg, f"{base}/{gp}", timeout=45, on_bytes=on_bytes, cancel=cancel)
                if hashlib.md5(data).hexdigest() != chunk["compressedMd5"]:
                    raise GogError("checksum mismatch (download)")
                raw = zlib.decompress(data)
                if len(raw) != int(chunk["size"]) or (
                        chunk.get("md5") and hashlib.md5(raw).hexdigest() != chunk["md5"]):
                    raise GogError("checksum mismatch (content)")
                return raw
            except DownloadCancelled:
                raise
            except (GogError, zlib.error) as e:
                last = e
    raise GogError(f"Could not download a piece of the game ({last}). Click Resume to try again.")


def _safe_rel(path):
    """Path relatif dari manifest (backslash Windows) -> 'a/b/c'. Tolak path berbahaya."""
    parts = [x for x in str(path).replace("\\", "/").split("/") if x not in ("", ".")]
    if not parts or ".." in parts or ":" in parts[0]:
        raise GogError(f"The manifest contains an unsafe path: {path}")
    return "/".join(parts)


def _ordered_map(tasks, func, threads, cancel):
    """Jalankan func(task) paralel (jendela 2*threads), hasilnya dikembalikan BERURUTAN.
    Generator: yield (task, hasil). Cancel -> DownloadCancelled."""
    pool = ThreadPoolExecutor(max_workers=threads)
    window, it = deque(), iter(tasks)
    try:
        while True:
            while len(window) < threads * 2:
                t = next(it, None)
                if t is None:
                    break
                window.append((t, pool.submit(func, t)))
            if not window:
                return
            t, fut = window.popleft()
            while True:
                if cancel.is_set():
                    raise DownloadCancelled("Download cancelled.")
                try:
                    res = fut.result(timeout=0.3)
                    break
                except FutTimeout:
                    continue
            yield t, res
    finally:
        for _, f in window:
            f.cancel()
        pool.shutdown(wait=False)


def _write_small(target, data):
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    part.write_bytes(data)
    part.replace(target)


def _run_direct(files, sfcs, dest, fetch, cancel, progress, log, threads=4):
    """Inti Direct Download (tanpa jaringan sendiri: semua lewat fetch, jadi bisa diuji).

    files : [{'path','size','chunks','pid','sfc': (offset,size)|None,'sfc_key'}]
    sfcs  : {sfc_key: (pid, [chunk, ...])}   isi 'smallFilesContainer' per depot
    fetch : fetch(pid, chunk) -> bytes hasil dekompresi & terverifikasi
    progress(done_bytes, total_bytes)"""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    total = sum(f["size"] for f in files)
    done, pending = 0, []
    for f in files:
        f["target"] = dest / f["path"]
        part = f["target"].with_name(f["target"].name + ".part")
        if f["target"].is_file() and f["target"].stat().st_size == f["size"]:
            part.unlink(missing_ok=True)      # sudah lengkap; .part usang dibuang
            done += f["size"]
        else:
            pending.append(f)
    skipped = len(files) - len(pending)
    if skipped:
        log(f"{skipped} file(s) already complete - skipped")

    need = sum(f["size"] for f in pending)
    try:
        free = shutil.disk_usage(dest).free
        if need > free:
            raise GogError(f"Not enough free disk space: {_fmt_size(need)} needed, "
                           f"{_fmt_size(free)} available in {dest}.")
    except OSError:
        pass
    progress(done, total)

    # 1) file kosong
    for f in [f for f in pending if f["size"] == 0]:
        _write_small(f["target"], b"")

    # 2) file kecil dari smallFilesContainer (dikelompokkan per depot)
    groups = {}
    for f in pending:
        if f["sfc"] and f["size"] > 0:
            groups.setdefault(f["sfc_key"], []).append(f)
    for key, group in groups.items():
        pid, chunks = sfcs[key]
        log(f"Reading the small-files package ({len(group)} file(s))...")
        blob = bytearray()
        with closing(_ordered_map([(pid, c) for c in chunks], lambda t: fetch(t[0], t[1]),
                                  threads, cancel)) as gen:
            for _, raw in gen:
                blob += raw
        for f in group:
            off, size = f["sfc"]
            data = bytes(blob[off:off + size])
            if len(data) != size:
                raise GogError(f"The small-files package is shorter than expected ({f['path']}).")
            _write_small(f["target"], data)
            done += size
        progress(done, total)
        del blob

    # 3) file biasa: chunk diunduh paralel, ditulis berurutan
    regular = [f for f in pending if not f["sfc"] and f["size"] > 0]
    for f in regular:
        part = f["target"].with_name(f["target"].name + ".part")
        f["target"].parent.mkdir(parents=True, exist_ok=True)
        start, acc = 0, 0
        if part.exists():
            have = part.stat().st_size
            for c in f["chunks"]:
                if acc + int(c["size"]) > have:
                    break
                acc += int(c["size"])
                start += 1
            if have != acc:
                with open(part, "r+b") as fh:
                    fh.truncate(acc)
            if start:
                log(f"Resuming {f['path']} from {_fmt_size(acc)}")
        f["_start"] = start
        done += acc

    def finalize(f):
        part = f["target"].with_name(f["target"].name + ".part")
        if not part.exists() or part.stat().st_size != f["size"]:
            raise GogError(f"{f['path']} came out with the wrong size.")
        part.replace(f["target"])

    def tasks():
        for fi, f in enumerate(regular):
            for ci in range(f["_start"], len(f["chunks"])):
                yield (fi, ci, f["pid"], f["chunks"][ci])

    for f in regular:                          # part yang ternyata sudah utuh
        if f["_start"] == len(f["chunks"]):
            finalize(f)
    progress(done, total)

    cur, fh = None, None
    try:
        with closing(_ordered_map(tasks(), lambda t: fetch(t[2], t[3]), threads, cancel)) as gen:
            for (fi, ci, _pid, _chunk), raw in gen:
                f = regular[fi]
                if cur != fi:
                    if fh:
                        fh.close()
                    part = f["target"].with_name(f["target"].name + ".part")
                    fh = open(part, "ab" if ci > 0 else "wb")
                    cur = fi
                    if f["size"] >= _LOG_FILE_MIN:
                        log(f"Downloading {f['path']} ({_fmt_size(f['size'])})...")
                fh.write(raw)
                done += len(raw)
                progress(done, total)
                if ci == len(f["chunks"]) - 1:
                    fh.close()
                    fh, cur = None, None
                    finalize(f)
    finally:
        if fh:
            fh.close()
    progress(total, total)


def _direct_worker(job):
    """Background thread untuk job Direct Download. Status akhir -> _finish_job() di main thread."""
    cfg, dest, cancel, d = job["cfg"], job["dest"], job["cancel"], job["direct"]

    def log(text=""):
        _job_log(job, text)

    def finish(status, err=None):
        _post(lambda: _finish_job(job, status, None, err))

    try:
        dest.mkdir(parents=True, exist_ok=True)
        base_id = str(job["game_id"])
        log("Mode: direct download (game files, no setup program)")
        log(f"Version: {_build_label(d['build'])}")
        log(f"Language: {d.get('language') or 'all languages'}")
        log(f"Saving to: {dest}")
        files, sfcs, dirs, pids = {}, {}, [], set()
        for n, depot in enumerate(d["depots"]):
            if cancel.is_set():
                raise DownloadCancelled("Download cancelled.")
            pid = str(depot.get("productId") or base_id)
            pids.add(pid)
            dm = fetch_depot(cfg, d["meta_base"], depot)
            sfc = (dm.get("smallFilesContainer") or {}).get("chunks") or []
            if sfc:
                sfcs[n] = (pid, sfc)
            for it in dm.get("items") or []:
                kind = it.get("type")
                if kind == "DepotDirectory":
                    dirs.append(_safe_rel(it.get("path", "")))
                elif kind == "DepotFile":
                    rel = _safe_rel(it.get("path", ""))
                    ref = it.get("sfcRef")
                    chunks = it.get("chunks") or []
                    size = int(ref["size"]) if ref else sum(int(c["size"]) for c in chunks)
                    files[rel.lower()] = {
                        "path": rel, "size": size, "chunks": chunks, "pid": pid,
                        "sfc": (int(ref["offset"]), int(ref["size"])) if ref else None,
                        "sfc_key": n}
        for rel in dirs:
            (dest / rel).mkdir(parents=True, exist_ok=True)
        file_list = list(files.values())
        total = sum(f["size"] for f in file_list)
        job["total_bytes"] = total
        log(f"{len(file_list)} file(s), {_host.human_size(total)} total")
        log("")
        links = {pid: _SecureLinks(cfg, pid) for pid in pids}
        try:
            threads = max(1, min(16, int(cfg.get("direct_threads") or 4)))
        except (TypeError, ValueError):
            threads = 4
        job["_last_t"] = None

        def fetch(pid, chunk):
            return _fetch_chunk(cfg, links[pid], chunk, cancel, on_bytes=lambda n: _net_add(job, n))

        def progress(done, tot):
            _job_progress(job, done, tot)

        _run_direct(file_list, sfcs, dest, fetch, cancel, progress, log, threads)
        _job_progress(job, total, total, sample=False)
        deps = d.get("dependencies") or []
        log("")
        log("Download complete.")
        if deps:
            log("Note: GOG lists these redistributables, which are NOT installed automatically: "
                + ", ".join(str(x) for x in deps))
            log("If the game complains about missing VC++/DirectX runtimes, install them in its "
                "Wine prefix (e.g. with winetricks).")
        finish("done")
    except DownloadCancelled:
        log("")
        log(_cancel_message(job))
        finish("cancelled")
    except Exception as e:
        log("")
        log(f"ERROR: {e}")
        finish("failed", str(e))


# --------------------------------------------------------------------------- direct: shortcut

def _ci_path(root, rel):
    """Path relatif (backslash, huruf besar/kecil bebas) di dalam root -> Path nyata, atau None."""
    cur = Path(root)
    for part in [x for x in str(rel).replace("\\", "/").split("/") if x not in ("", ".")]:
        nxt = cur / part
        if not nxt.exists():
            try:
                match = next((e for e in os.listdir(cur) if e.lower() == part.lower()), None)
            except OSError:
                return None
            if match is None:
                return None
            nxt = cur / match
        cur = nxt
    return cur


def _read_goggame_info(root, game_id):
    """(exe, workdir) utama dari goggame-<id>.info di folder game (playTasks), atau (None, None)."""
    root = Path(root)
    cands = [root / f"goggame-{game_id}.info"] + sorted(root.glob("goggame-*.info"))
    for info in cands:
        try:
            data = json.loads(info.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        tasks = [t for t in data.get("playTasks") or [] if t.get("path")
                 and str(t.get("type", "FileTask")).lower() == "filetask"]
        tasks.sort(key=lambda t: (not t.get("isPrimary"), str(t.get("category", "")) != "game"))
        for t in tasks:
            exe = _ci_path(root, t["path"])
            if exe and exe.is_file():
                work = _ci_path(root, t.get("workingDir") or "") if t.get("workingDir") else None
                return exe, (work if work and work.is_dir() else None)
    return None, None


def _after_direct(job, force=False):
    """Main thread: setelah Direct Download selesai -> buat shortcut di launcher (kalau runner dipilih).
    force=True (tombol Set Runner) mengabaikan config auto_shortcut."""
    choice = job["choice"]
    if choice is None or not getattr(_host, "add_shortcut", None) \
            or not (force or job["cfg"].get("auto_shortcut", True)):
        job["note"] = "Completed - game files only"
        return
    job["note"] = "Completed - adding shortcut..."

    def work():
        if not HAVE_PIL:
            return None
        try:
            return load_cover(job["cfg"], job["game"], 250, 250)
        except Exception:
            return None

    _bg(work, lambda icon, _err: _finish_direct(job, icon))


def _finish_direct(job, icon):
    root = Path(job["dest"])
    exe, work = _read_goggame_info(root, job["game_id"])
    if exe is None:
        _job_log(job, "No goggame-*.info start task was found in the game folder.")
        if messagebox.askyesno(
                "Add Shortcut",
                f"'{job['title']}' was downloaded, but its start file could not be found automatically.\n\n"
                "Select its .exe now to add a launcher shortcut?", parent=_host.root):
            picked = filedialog.askopenfilename(
                parent=_host.root, title=f"Select the game executable - {job['title']}",
                initialdir=str(root) if root.is_dir() else str(Path.home()),
                filetypes=[("Executable Files", "*.exe"), ("All Files", "*.*")])
            if picked:
                exe = Path(picked)
        if exe is None:
            job["note"] = "Completed - game files only (no shortcut)"
            return
    _job_log(job, f"Game found: {exe}")
    try:
        name = _host.add_shortcut(job["title"], exe, job["choice"], icon_image=icon,
                                  work_dir=work, source="gog", gog_id=job["game_id"])
    except Exception as e:
        job["note"] = "Completed - shortcut failed"
        _job_log(job, f"Could not add the shortcut: {e}")
        _host.status(f"Could not add shortcut for '{job['title']}': {e}", "danger")
        return
    job["note"] = "Completed - shortcut added"
    _job_log(job, f"Shortcut added to the launcher as '{name}'.")
    _host.status(f"'{name}' was added to the launcher.", "success")


# --------------------------------------------------------------------------- UI helpers

def _bg(work, done):
    """Jalankan work() di thread, lalu done(hasil, error) di main thread."""
    def run():
        try:
            res, err = work(), None
        except Exception as e:
            res, err = None, e
        _host.root.after(0, lambda: done(res, err))
    threading.Thread(target=run, daemon=True).start()


def _center_xy(win, parent, w, h):
    """Posisi (x, y) agar jendela ukuran w x h berada di TENGAH parent, tapi selalu utuh di dalam
    layar: sisakan ruang title bar di atas dan taskbar di bawah (~70px)."""
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    if parent is not None and parent.winfo_exists():
        cx = parent.winfo_rootx() + parent.winfo_width() // 2
        cy = parent.winfo_rooty() + parent.winfo_height() // 2
    else:
        cx, cy = sw // 2, sh // 2
    x = max(0, min(cx - w // 2, sw - w - 10))
    y = max(10, min(cy - h // 2, sh - h - 70))
    return x, y


def _recenter(win, parent):
    """Tengahkan ulang dialog (ukuran otomatis) memakai ukurannya SEKARANG. Dipanggil setelah isi
    dialog berubah, atau setelah window manager menggeser jendela saat ditampilkan."""
    try:
        if not win.winfo_exists():
            return
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w, h = min(win.winfo_reqwidth(), sw - 20), min(win.winfo_reqheight(), sh - 90)
        x, y = _center_xy(win, parent, w, h)
        win.geometry(f"+{x}+{y}")
    except tk.TclError:
        pass


def _place(win, parent, w=None, h=None):
    """Taruh jendela di tengah parent (dalam layar). Tanpa w/h = ukuran otomatis sesuai isi; dialog
    seperti itu dikoreksi lagi sebentar setelah tampil karena window manager sering menggesernya."""
    win.update_idletasks()
    auto = not (w and h)
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    # ukuran eksplisit (mis. ukuran hasil resize yang tersimpan) hanya dibatasi layar, bukan dipotong
    # ekstra ~90px, supaya jendela yang sengaja dibuat tinggi/lebar tidak "mengecil sendiri".
    w = min(w, sw) if w else min(win.winfo_reqwidth(), sw - 20)
    h = min(h, sh - 30) if h else min(win.winfo_reqheight(), sh - 90)
    x, y = _center_xy(win, parent, w, h)
    win.geometry(f"+{x}+{y}" if auto else f"{w}x{h}+{x}+{y}")
    if auto:
        for delay in (60, 250):
            win.after(delay, lambda: _recenter(win, parent))


def _fit(win):
    """Sesuaikan ukuran window dengan isinya SEKARANG (dipanggil tiap kali isi dialog berubah,
    mis. teks info jadi lebih panjang) supaya tombol di bawah tidak terpotong. Posisi tetap,
    tapi digeser ke atas kalau bagian bawahnya akan keluar layar."""
    try:
        if not win.winfo_exists():
            return
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w = min(win.winfo_reqwidth(), sw - 40)
        h = min(win.winfo_reqheight(), sh - 80)
        x, y = win.winfo_x(), win.winfo_y()
        x = max(0, min(x, sw - w - 10))
        y = max(0, min(y, sh - h - 60))
        win.geometry(f"{w}x{h}+{x}+{y}")
    except tk.TclError:
        pass


def _standalone(win):
    """Jadikan jendela benar-benar mandiri bagi window manager: grup sendiri (bukan satu grup dengan
    jendela launcher) dan bertipe 'normal'. Tanpa ini, mengklik jendela bisa ikut mengaktifkan/mengangkat
    jendela launcher sehingga taskbar yang autohide muncul. Harus dipanggil sebelum jendela ditampilkan."""
    for call in (lambda: win.wm_group(win),
                 lambda: win.wm_attributes("-type", "normal")):
        try:
            call()
        except tk.TclError:
            pass          # window manager / Tk tidak mendukung -> abaikan


def _new_toplevel(parent, title):
    win = tk.Toplevel(parent)
    win.withdraw()
    win.title(title)
    win.configure(bg=_host.colors()["primary"])
    return win


def _open_login_dialog(parent, cfg, on_success):
    win = _new_toplevel(parent, "Login to GOG")
    win.resizable(False, False)
    frame = ttk.Frame(win, padding=15)
    frame.pack(fill=tk.BOTH, expand=True)
    fonts = _host.fonts

    ttk.Label(frame, font=fonts["normal"], justify=tk.LEFT, wraplength=460,
              text="Your password is only ever typed on gog.com - never in this launcher.").pack(
        anchor="w", pady=(0, 10))

    ttk.Label(frame, text="Option A - built-in browser (recommended)", font=fonts["subtitle"]).pack(anchor="w")
    ttk.Label(frame, font=fonts["small"], justify=tk.LEFT, wraplength=460,
              text="A small login window opens. Sign in there; it closes by itself and the "
                   "launcher takes over. Needs the 'pywebview' package.").pack(anchor="w", pady=(2, 6))
    browser_btn = ttk.Button(frame, text="Sign In with Built-in Browser", style="Custom.TButton")
    browser_btn.pack(anchor="w", pady=(0, 12))

    ttk.Separator(frame, orient="horizontal").pack(fill=tk.X, pady=(0, 10))
    ttk.Label(frame, text="Option B - manual (any browser)", font=fonts["subtitle"]).pack(anchor="w")
    ttk.Label(frame, font=fonts["small"], justify=tk.LEFT, wraplength=460,
              text="Open the login page, sign in, then copy the full address from the address bar "
                   "(the page after login looks blank/odd) and paste it below.").pack(anchor="w", pady=(2, 6))

    url = login_url(cfg)
    url_entry = ttk.Entry(frame, font=fonts["small"], width=64)
    url_entry.insert(0, url)
    url_entry.config(state="readonly")
    url_entry.pack(fill=tk.X, pady=(0, 6))
    ttk.Button(frame, text="Open GOG Login Page", style="Custom.TButton",
               command=lambda: webbrowser.open(url)).pack(anchor="w", pady=(0, 8))

    ttk.Label(frame, text="Paste the address (or just the code) here:", font=fonts["small"]).pack(anchor="w")
    code_entry = ttk.Entry(frame, font=fonts["normal"], width=64)
    code_entry.pack(fill=tk.X, pady=(2, 8))
    msg = ttk.Label(frame, text="", font=fonts["small"], wraplength=460, justify=tk.LEFT)
    msg.pack(anchor="w", pady=(0, 8))

    btns = ttk.Frame(frame)
    btns.pack()
    sign_btn = ttk.Button(btns, text="Sign In", style="Custom.TButton", width=12)
    sign_btn.grid(row=0, column=0, padx=4)
    ttk.Button(btns, text="Cancel", style="Custom.TButton", width=12,
               command=win.destroy).grid(row=0, column=1, padx=4)

    def do_sign_in():
        code = extract_code(code_entry.get())
        if not code:
            msg.config(text="That doesn't look like a valid address or code.")
            return
        sign_btn.config(state="disabled")
        msg.config(text="Signing in...")

        def done(_res, err):
            if not win.winfo_exists():
                return
            if err:
                sign_btn.config(state="normal")
                msg.config(text=f"Login failed: {err}")
            else:
                win.destroy()
                on_success()
        _bg(lambda: login_with_code(cfg, code), done)

    def do_browser():
        browser_btn.config(state="disabled")
        sign_btn.config(state="disabled")
        msg.config(text="A login window has opened. Sign in there - it will close automatically.")

        def done(_res, err):
            if err is None:
                if win.winfo_exists():
                    win.destroy()
                if parent.winfo_exists():
                    on_success()
                return
            if not win.winfo_exists():
                return
            browser_btn.config(state="normal")
            sign_btn.config(state="normal")
            if isinstance(err, GogCancelled):
                msg.config(text=str(err))
            else:
                msg.config(text=f"Built-in browser failed: {err}\nYou can use Option B instead.")
        _bg(lambda: login_via_browser(cfg), done)

    browser_btn.config(command=do_browser)
    sign_btn.config(command=do_sign_in)
    win.bind("<Return>", lambda _e: do_sign_in())
    _place(win, parent)
    win.deiconify()
    win.transient(parent)
    win.grab_set()
    code_entry.focus_set()


def _choose_installer(parent, title, installers):
    """Dialog kecil pilih salah satu installer. Return index atau None."""
    labels = []
    for i in installers:
        lang = i.get("language_full") or i.get("language") or "?"
        labels.append(f"{i.get('os', '?')} - {lang} - v{i.get('version') or '?'} - "
                      f"{_host.human_size(int(i.get('total_size', 0)))}")
    win = _new_toplevel(parent, f"Choose Installer - {title}")
    win.resizable(False, False)
    frame = ttk.Frame(win, padding=15)
    frame.pack(fill=tk.BOTH, expand=True)
    ttk.Label(frame, text=f"Select the installer to download for '{title}':",
              font=_host.fonts["normal"]).pack(anchor="w", pady=(0, 8))
    combo = ttk.Combobox(frame, values=labels, state="readonly", width=56, font=_host.fonts["normal"])
    combo.current(0)
    combo.pack(fill=tk.X, pady=(0, 14))
    result = {"value": None}

    def ok():
        result["value"] = combo.current()
        win.destroy()

    row = ttk.Frame(frame)
    row.pack()
    ttk.Button(row, text="Download", style="Custom.TButton", width=12, command=ok).grid(row=0, column=0, padx=4)
    ttk.Button(row, text="Cancel", style="Custom.TButton", width=12, command=win.destroy).grid(row=0, column=1, padx=4)
    _place(win, parent)
    win.deiconify()
    win.transient(parent)
    win.grab_set()
    win.wait_window()
    return result["value"]


_RUNNER_NAMES = {"wine": "Wine (Vanilla)", "protonge": "Proton GE", "protoncachyos": "Proton-CachyOS"}


def _runner_summary(choice):
    name = _RUNNER_NAMES.get(choice.get("runner"), str(choice.get("runner") or "?"))
    if choice.get("proton_name"):
        name += f" - {choice['proton_name']}"
    prefix = choice.get("prefix_code")
    return f"{name}, prefix {prefix}" if prefix else f"{name}, default prefix"


def _choose_direct(parent, cfg, game, builds, owned):
    """Dialog Direct Download: pilih versi (build) + bahasa. Return dict siap pakai atau None."""
    fonts = _host.fonts
    base_id = str(game["id"])
    win = _new_toplevel(parent, f"Direct Download - {game['title']}")
    win.resizable(False, False)
    frame = ttk.Frame(win, padding=15)
    frame.pack(fill=tk.BOTH, expand=True)
    # Baris tombol dipasang PERTAMA dan menempel di bawah: pack membagi ruang sesuai urutan,
    # jadi Download/Cancel selalu terlihat, bagaimanapun isi di atasnya memanjang.
    row = ttk.Frame(frame)
    row.pack(side=tk.BOTTOM, pady=(4, 0))
    ttk.Label(frame, text=f"Download the game files of '{game['title']}' directly (no setup program).",
              font=fonts["normal"], wraplength=470, justify=tk.LEFT).pack(anchor="w", pady=(0, 10))
    ttk.Label(frame, text="Version:", font=fonts["normal"]).pack(anchor="w")
    build_combo = ttk.Combobox(frame, values=[_build_label(b) for b in builds], state="readonly",
                               width=58, font=fonts["normal"])
    build_combo.current(0)
    build_combo.pack(fill=tk.X, pady=(0, 8))
    ttk.Label(frame, text="Language:", font=fonts["normal"]).pack(anchor="w")
    lang_combo = ttk.Combobox(frame, values=[], state="disabled", width=58, font=fonts["normal"])
    lang_combo.pack(fill=tk.X, pady=(0, 8))
    result = {"value": None}
    st = {"meta": None, "req": 0, "langs": [], "cache": {}, "choice": None}

    # Runner (Wine / Proton GE / Proton-CachyOS + prefix) untuk shortcut di launcher.
    ask_runner = getattr(_host, "ask_runner", None)
    can_runner = bool(ask_runner) and _platform(cfg) == "windows"
    files_only = tk.BooleanVar(value=not can_runner)
    runner_text = tk.StringVar(value="Not selected yet")

    def pick_runner():
        c = ask_runner(parent=win, game_title=game["title"])
        try:
            win.grab_set()      # dialog runner menutup grab dialog ini
        except tk.TclError:
            pass
        if c is not None:
            st["choice"] = c
            files_only.set(False)
            on_files_only()
            runner_text.set(_runner_summary(c))
            _recenter(win, parent)

    def on_files_only():
        runner_btn.config(state="disabled" if files_only.get() else "normal")

    if can_runner:
        ttk.Label(frame, text="Runner (used for the launcher shortcut):", font=fonts["normal"]).pack(anchor="w")
        rrow = ttk.Frame(frame)
        rrow.pack(fill=tk.X, pady=(0, 4))
        runner_btn = ttk.Button(rrow, text="Choose Runner (Wine / Proton)...", style="Custom.TButton",
                                command=pick_runner)
        runner_btn.pack(side=tk.LEFT)
        ttk.Label(rrow, textvariable=runner_text, font=fonts["small"], wraplength=270,
                  justify=tk.LEFT).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Checkbutton(frame, text="Download game files only (no launcher shortcut)", variable=files_only,
                        style="Custom.TCheckbutton", command=on_files_only).pack(anchor="w", pady=(0, 8))
    else:
        runner_btn = None

    info_box = ttk.Frame(frame, height=68)      # tinggi tetap: dialog tidak membesar/bergeser saat info terisi
    info_box.pack(fill=tk.X, pady=(0, 12))
    info_box.pack_propagate(False)
    info = ttk.Label(info_box, text="", font=fonts["small"], wraplength=470, justify=tk.LEFT)
    info.pack(anchor="nw")
    runner_text.trace_add("write", lambda *_: win.after_idle(_fit, win))

    def current():
        meta = st["meta"]
        idx = lang_combo.current()
        lang = st["langs"][idx - 1] if idx > 0 else None
        return select_depots(meta, lang, owned, base_id), lang

    def refresh_info(*_):
        if not st["meta"]:
            return
        depots, _lang = current()
        if not depots:
            info.config(text="No files were found for this version/language.")
            dl_btn.config(state="disabled")
            _fit(win)
            return
        size = sum(int(d.get("size") or 0) for d in depots)
        comp = sum(int(d.get("compressedSize") or 0) for d in depots)
        text = (f"Install size: {_host.human_size(size)}    "
                f"Download: about {_host.human_size(comp or size)}")
        deps = st["meta"].get("dependencies") or []
        if deps:
            text += ("\nRedistributables (VC++/DirectX, ...) are not installed automatically: "
                     + ", ".join(str(x) for x in deps[:6]) + (" ..." if len(deps) > 6 else ""))
        info.config(text=text)
        dl_btn.config(state="normal")
        _fit(win)

    def load_meta(*_):
        idx = build_combo.current()
        st["meta"] = None
        st["req"] += 1
        req = st["req"]
        dl_btn.config(state="disabled")
        lang_combo.config(state="disabled")
        info.config(text="Reading version information...")
        _fit(win)

        def work():
            if idx not in st["cache"]:
                st["cache"][idx] = fetch_build_meta(cfg, builds[idx])
            return st["cache"][idx]

        def done(meta, err):
            if req != st["req"] or not win.winfo_exists():
                return
            if err:
                info.config(text=f"Could not read this version: {err}")
                _fit(win)
                return
            st["meta"] = meta
            st["langs"] = meta_languages(meta)
            lang_combo.config(values=["All languages"] + st["langs"],
                              state="readonly" if st["langs"] else "disabled")
            lang_combo.current(_default_lang_index(cfg, st["langs"]))
            refresh_info()
        _bg(work, done)

    def ok():
        if not st["meta"]:
            return
        depots, lang = current()
        if not depots:
            return
        choice = None
        if can_runner and not files_only.get():
            if st["choice"] is None:
                pick_runner()
            choice = st["choice"]
            if choice is None and not messagebox.askyesno(
                    "GOG Store",
                    "No runner selected.\n\nDownload the game files only (no launcher shortcut)?",
                    parent=win):
                return
        meta, build = st["meta"], builds[build_combo.current()]
        result["value"] = {
            "choice": choice,
            "build": build, "meta_base": meta_base_url(cfg, build),
            "depots": depots, "language": lang,
            "install_dir": str(meta.get("installDirectory") or ""),
            "dependencies": list(meta.get("dependencies") or [])}
        win.destroy()

    dl_btn = ttk.Button(row, text="Download", style="Custom.TButton", width=12, command=ok, state="disabled")
    dl_btn.grid(row=0, column=0, padx=4)
    ttk.Button(row, text="Cancel", style="Custom.TButton", width=12, command=win.destroy).grid(row=0, column=1, padx=4)
    build_combo.bind("<<ComboboxSelected>>", load_meta)
    lang_combo.bind("<<ComboboxSelected>>", refresh_info)
    _place(win, parent)
    win.deiconify()
    win.transient(parent)
    win.grab_set()
    load_meta()
    _fit(win)
    win.wait_window()
    return result["value"]


def _open_job_log(job, parent):
    c = _host.colors()
    win = _new_toplevel(parent, f"Log - {job['title']}")
    frame = ttk.Frame(win, padding=10)
    frame.pack(fill=tk.BOTH, expand=True)
    scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL)
    scroll.pack(side=tk.RIGHT, fill=tk.Y)
    text = tk.Text(frame, wrap=tk.WORD, state="disabled", font=("Courier", 9),
                   bg=c.get("text_background", c["secondary"]), fg=c["text"],
                   insertbackground=c["text"], yscrollcommand=scroll.set)
    text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scroll.config(command=text.yview)
    shown = {"n": 0}

    def poll():
        if not win.winfo_exists():
            return
        lines = list(job["log"])
        if len(lines) > shown["n"]:
            text.config(state="normal")
            for line in lines[shown["n"]:]:
                text.insert(tk.END, line + "\n")
            shown["n"] = len(lines)
            text.see(tk.END)
            text.config(state="disabled")
        win.after(500, poll)

    win.minsize(420, 240)
    _place(win, parent, 680, 380)
    win.deiconify()
    poll()


def open_downloads_window():
    """Jendela daftar download (aktif + selesai) dengan Pause / Resume / Cancel / Log / Remove."""
    global _downloads_win
    _ensure_jobs_loaded()
    if _downloads_win is not None and _downloads_win.winfo_exists():
        _downloads_win.deiconify()
        _downloads_win.lift()
        _downloads_win.focus_set()
        return
    fonts = _host.fonts
    win = _downloads_win = _new_toplevel(_host.root, "GOG Downloads")
    _standalone(win)
    win.minsize(480, 300)
    frame = ttk.Frame(win, padding=15)
    frame.pack(fill=tk.BOTH, expand=True)

    list_frame = ttk.Frame(frame)
    list_frame.pack(fill=tk.BOTH, expand=True)
    list_frame.rowconfigure(0, weight=1)
    list_frame.columnconfigure(0, weight=1)
    vs = ttk.Scrollbar(list_frame, orient=tk.VERTICAL)
    vs.grid(row=0, column=1, sticky="ns")
    hs = ttk.Scrollbar(list_frame, orient=tk.HORIZONTAL)
    hs.grid(row=1, column=0, sticky="ew")
    tree = ttk.Treeview(list_frame, columns=("Game", "Status", "Progress", "Size", "Speed"),
                        show="headings", selectmode="browse", height=9,
                        yscrollcommand=vs.set, xscrollcommand=hs.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vs.config(command=tree.yview)
    hs.config(command=tree.xview)
    saved_ui = _ui_load("downloads")
    # Mode kolom: OTOMATIS (bawaan) = lebar pas dengan isi + sisa lebar jendela dibagi rata supaya tabel
    # (termasuk bar progress) selalu memenuhi jendela. Menggeser garis kolom = mode manual (tersimpan).
    state = {"auto": saved_ui.get("auto", True) is not False}
    saved_cols = saved_ui.get("columns") if isinstance(saved_ui.get("columns"), dict) else {}
    COLS = tuple(tree["columns"])
    WEIGHTS = {"Game": 3, "Status": 2, "Progress": 4, "Size": 1, "Speed": 1}   # pembagian sisa lebar (bar progress paling banyak)
    CAPS = {"Game": 520, "Status": 420}      # batas lebar 'dibutuhkan' (teks lebih panjang -> scrollbar)

    from tkinter import font as tkfont

    def _font(spec):
        try:
            return tkfont.Font(root=win, font=spec)
        except tk.TclError:
            return tkfont.nametofont("TkDefaultFont")
    body_font = _font(ttk.Style().lookup("Treeview", "font") or "TkDefaultFont")
    head_font = _font("TkHeadingFont")
    cell_w = max(1, body_font.measure("\u2588"), body_font.measure("\u2591"))

    for col, w in (("Game", 230), ("Status", 170), ("Progress", 140), ("Size", 150), ("Speed", 95)):
        tree.heading(col, text="Network" if col == "Speed" else col, anchor="w")
        if not state["auto"]:
            try:
                w = max(60, min(1200, int(saved_cols.get(col, w))))
            except (TypeError, ValueError):
                pass
        tree.column(col, width=w, minwidth=60, anchor="w", stretch=False)

    def bar_chars():
        """Panjang bar progress (karakter) mengikuti lebar kolom Progress."""
        room = int(tree.column("Progress", "width")) - body_font.measure(" 100%") - 24
        return max(10, min(80, room // cell_w))

    def needed(col):
        """Lebar minimum kolom agar judul + seluruh isinya terbaca penuh."""
        w = head_font.measure(tree.heading(col, "text")) + 30
        if col == "Progress":
            w = max(w, body_font.measure(_bar(100, 10)) + 24)
        else:
            idx = COLS.index(col)
            for iid in tree.get_children():
                w = max(w, body_font.measure(str(tree.item(iid, "values")[idx])) + 24)
        return min(w, CAPS.get(col, 600))

    def auto_layout(*_):
        if not state["auto"] or not win.winfo_exists():
            return
        need = {c: needed(c) for c in COLS}
        avail = tree.winfo_width() - 2
        if avail <= 1:
            avail = sum(need.values())
        extra = max(0, avail - sum(need.values()))
        weight_sum = sum(WEIGHTS[c] for c in COLS)
        widths = {c: need[c] + extra * WEIGHTS[c] // weight_sum for c in COLS}
        if extra:
            widths["Game"] += avail - sum(widths.values())      # sisa pembulatan
        for c in COLS:
            if abs(int(tree.column(c, "width")) - widths[c]) > 1:
                tree.column(c, width=widths[c])

    drag = {"start": None}

    def on_tree_press(event):
        drag["start"] = ({c: int(tree.column(c, "width")) for c in COLS}
                         if tree.identify_region(event.x, event.y) == "separator" else None)

    def on_tree_release(event):
        before, drag["start"] = drag["start"], None
        if before and any(abs(int(tree.column(c, "width")) - w) > 1 for c, w in before.items()):
            state["auto"] = False                  # Anda mengatur lebar kolom sendiri
            update_hint()
        save_ui_soon()

    def on_tree_double(event):
        """Double-click garis pemisah kolom = kembali ke ukuran otomatis."""
        if tree.identify_region(event.x, event.y) == "separator":
            state["auto"] = True
            update_hint()
            auto_layout()
            save_ui()
            return "break"

    ui_timer = {"id": None}
    size_mem = {"v": saved_ui.get("size")}      # ukuran normal terakhir (dipakai saat maximized/minimized)

    def save_ui(*_):
        """Simpan mode + lebar kolom + ukuran jendela (ukuran tidak disimpan saat maximized/minimized)."""
        if not win.winfo_exists():
            return
        data = {"auto": state["auto"], "columns": {c: int(tree.column(c, "width")) for c in COLS}}
        if win.state() == "normal" and win.winfo_width() > 1:
            size_mem["v"] = f"{win.winfo_width()}x{win.winfo_height()}"
        if size_mem["v"]:
            data["size"] = size_mem["v"]
        _ui_save("downloads", data)

    def save_ui_soon(event=None):
        if event is not None and event.widget is not win:
            return
        if ui_timer["id"]:
            win.after_cancel(ui_timer["id"])
        ui_timer["id"] = win.after(700, save_ui)

    def on_close():
        if ui_timer["id"]:
            win.after_cancel(ui_timer["id"])
        save_ui()
        win.destroy()

    tree.bind("<ButtonPress-1>", on_tree_press, add="+")
    tree.bind("<ButtonRelease-1>", on_tree_release, add="+")
    tree.bind("<Double-1>", on_tree_double, add="+")
    tree.bind("<Configure>", auto_layout, add="+")
    win.bind("<Configure>", save_ui_soon, add="+")
    win.protocol("WM_DELETE_WINDOW", on_close)

    hint = ttk.Label(frame, text="", font=fonts["small"])
    hint.pack(anchor="w", pady=(4, 0))

    def update_hint():
        hint.config(text=("Columns fit automatically. Drag a column border to set your own width."
                          if state["auto"] else
                          "Custom column widths. Double-click a column border to go back to automatic sizing."))
    update_hint()

    pbar = ttk.Progressbar(frame, mode="determinate", maximum=100)
    pbar.pack(fill=tk.X, pady=(10, 0))
    detail = ttk.Label(frame, text="", font=fonts["small"], wraplength=900, justify=tk.LEFT)
    detail.pack(anchor="w", fill=tk.X, pady=(6, 0))
    frame.bind("<Configure>", lambda e: detail.config(wraplength=max(200, e.width - 40)), add="+")

    def selected_job():
        sel = tree.selection()
        return next((j for j in _jobs if sel and str(j["id"]) == sel[0]), None)

    def do_cancel():
        job = selected_job()
        if job and (job["status"] in _ACTIVE or job["status"] == "paused") and messagebox.askyesno(
                "Cancel Download",
                f"Cancel the download of '{job['title']}'?\n\n"
                "The partially downloaded files will be deleted. "
                "To keep them and continue later, use Pause instead.", parent=win):
            cancel_job(job)

    def do_toggle():
        """Satu tombol: sedang berjalan/antre -> Pause; paused/gagal/dibatalkan -> Resume."""
        job = selected_job()
        if not job:
            return
        if job["status"] in _ACTIVE:
            if not job["cancelling"]:
                pause_job(job)
        elif job["status"] in _RESTARTABLE:
            resume_job(job)
        on_state()                                  # label tombol langsung berganti (tanpa menunggu refresh)

    def do_log():
        job = selected_job()
        if job:
            _open_job_log(job, win)

    def do_runner():
        """Direct Download selesai: pilih / ganti runner (Wine, Proton GE, Proton-CachyOS) lalu buat
        atau perbarui shortcut-nya di launcher."""
        job = selected_job()
        ask_runner = getattr(_host, "ask_runner", None)
        if not job or job.get("kind") != "direct" or job["status"] != "done" or not ask_runner:
            return
        choice = ask_runner(parent=win, game_title=job["title"])
        if choice is None:
            return
        job["choice"] = choice
        _job_log(job, "")
        _job_log(job, f"Runner set to: {_runner_summary(choice)}")
        _after_direct(job, force=True)

    def do_folder():
        job = selected_job()
        if job:
            Path(job["dest"]).mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["xdg-open", str(job["dest"])], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=_host.clean_env())

    def do_remove():
        job = selected_job()
        if not job or job["status"] in _ACTIVE:
            return
        delete = False
        if job["status"] != "done" and _has_partial(job):
            ans = messagebox.askyesnocancel(
                "Remove Download",
                "Also delete the partial download files?\n\nYes: delete them\n"
                "No: keep them (downloading this game again resumes from them)\n"
                "Cancel: keep this entry in the list", parent=win)
            if ans is None:
                return
            delete = ans
        remove_job(job, delete)

    def do_clear():
        for job in [j for j in _jobs if j["status"] in ("done", "cancelled")]:
            remove_job(job)

    btns = ttk.Frame(frame)
    btns.pack(fill=tk.X, pady=(10, 0))
    buttons = {}
    for i, (key, label, cmd) in enumerate((
            ("toggle", "Pause", do_toggle), ("cancel", "Cancel", do_cancel),
            ("runner", "Set Runner", do_runner), ("log", "Show Log", do_log), ("folder", "Open Folder", do_folder),
            ("remove", "Remove", do_remove), ("clear", "Clear Finished", do_clear))):
        buttons[key] = ttk.Button(btns, text=label, style="Custom.TButton", width=13, command=cmd)

    def layout_buttons(event=None):
        """Susun tombol selebar jendela; kalau tidak muat, lanjut ke baris berikutnya."""
        order = list(buttons.values())
        unit = max(b.winfo_reqwidth() for b in order) + 8
        per_row = max(1, (btns.winfo_width() if btns.winfo_width() > 1 else 900) // unit)
        for i, b in enumerate(order):
            b.grid(row=i // per_row, column=i % per_row, padx=3, pady=2)
        for c in range(len(order) + 1):
            btns.columnconfigure(c, weight=1 if c < per_row else 0)

    btns.bind("<Configure>", layout_buttons)
    layout_buttons()

    def row_values(job):
        st, total = job["status"], _dl_total(job)
        size = f"{_host.human_size(_dl_done(job))} / {_host.human_size(total)}" if total else ""
        sp = _net_speed(job) if st == "running" and not job["cancelling"] else 0.0
        speed = f"{_host.human_size(int(sp))}/s" if sp > 0 else ""
        text = (("Pausing..." if job["pausing"] else "Cancelling...") if job["cancelling"] else
                job["note"] if st == "done" and job["note"] else _STATUS_TEXT[st])
        return (job["title"], text, _bar(job["progress"], bar_chars()), size, speed)

    def detail_text(job):
        if job is None:
            return "No downloads yet." if not _jobs else ""
        st, pct = job["status"], job["progress"]
        total, done = _dl_total(job), _dl_done(job)
        sizes = f"{_host.human_size(done)} of {_host.human_size(total)}" if total else ""
        where = f"Saved in: {job['dest']}"
        if st == "failed":
            return f"Error: {job['error']}\n{where}"
        if st == "running" and job["cancelling"]:
            return ("Pausing..." if job["pausing"] else "Cancelling...") + f"\n{where}"
        if st == "running":
            sp = _net_speed(job)
            line = f"Downloading {sizes} ({pct:.0f}%)"
            if sp > 0:
                line += f"  -  {_host.human_size(int(sp))}/s from the internet"
                if total and total > done:
                    line += f"  -  about {_fmt_eta((total - done) / sp)} left"
            else:
                line += "  -  connecting..." if done == 0 else "  -  waiting for data..."
            return f"{line}\n{where}"
        if st == "queued":
            return f"Waiting for a free download slot.\n{where}"
        if st == "paused":
            return f"Paused at {pct:.0f}% ({sizes}). Click Resume to continue.\n{where}"
        if st == "cancelled":
            return f"Cancelled.\n{where}"
        return f"Completed - {_host.human_size(total)} downloaded.\n{where}"

    def on_state(*_):
        job = selected_job()
        st = job["status"] if job else None
        if st in _ACTIVE:                           # satu tombol: label + aktif/nonaktif mengikuti status
            label, enabled = (("Pausing..." if job["pausing"] else "Cancelling..."), False) if job["cancelling"] \
                else ("Pause", True)
        elif st in _RESTARTABLE:
            label, enabled = "Resume", True
        else:
            label, enabled = "Pause", False
        buttons["toggle"].config(text=label, state="normal" if enabled else "disabled")
        buttons["cancel"].config(state="normal" if (st in _ACTIVE or st == "paused")
                                 and not job["discard"] else "disabled")
        buttons["remove"].config(state="normal" if job and st not in _ACTIVE else "disabled")
        buttons["runner"].config(state="normal" if job and job.get("kind") == "direct" and st == "done"
                                 else "disabled")
        for k in ("log", "folder"):
            buttons[k].config(state="normal" if job else "disabled")
        pbar["value"] = job["progress"] if job else 0
        detail.config(text=detail_text(job))

    def refresh():
        if not win.winfo_exists():
            return
        ids = set()
        for job in list(_jobs):
            iid = str(job["id"])
            ids.add(iid)
            if tree.exists(iid):
                tree.item(iid, values=row_values(job))
            else:
                tree.insert("", tk.END, iid=iid, values=row_values(job))
        for iid in tree.get_children():
            if iid not in ids:
                tree.delete(iid)
        auto_layout()
        on_state()
        win.after(500, refresh)

    tree.bind("<<TreeviewSelect>>", on_state)
    refresh()
    win_w, win_h, has_size = 1000, 470, False
    try:
        sw_, sh_ = str(saved_ui.get("size", "")).split("x")
        win_w, win_h, has_size = max(480, int(sw_)), max(300, int(sh_)), True
    except ValueError:
        pass
    if not has_size:                         # ukuran awal: cukup lebar untuk seluruh kolom
        win_w = max(win_w, min(sum(needed(c) for c in COLS) + 70, win.winfo_screenwidth() - 40))
        win.update_idletasks()
        win_h = max(win_h, 640, win.winfo_reqheight() + 30)
    _place(win, _host.root, win_w, win_h)
    win.deiconify()
    win.lift()
    win.after(80, auto_layout)               # setelah tampil lebar tabel sudah pasti


# --------------------------------------------------------------------------- cover grid

class _CoverGrid(tk.Frame):
    """Grid kartu game ala Heroic: cover + judul + platform, bisa di-scroll. Cover dimuat lazy
    (hanya kartu yang terlihat) oleh beberapa thread; hasilnya di-cache di disk."""

    def __init__(self, parent, cfg, colors, fonts, on_activate):
        super().__init__(parent, bg=colors["primary"])
        self.cfg, self.c, self.fonts, self.on_activate = cfg, colors, fonts, on_activate
        try:
            self.card_w = max(120, min(400, int(cfg.get("card_width") or 200)))
            ratio = float(cfg.get("cover_ratio") or 0.5625)
        except (TypeError, ValueError):
            self.card_w, ratio = 200, 0.5625
        self.card_h = max(60, int(self.card_w * ratio))

        self.canvas = tk.Canvas(self, bg=colors["primary"], highlightthickness=0, bd=0,
                                yscrollincrement=60)
        self.vscroll = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_yscroll)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vscroll.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.inner = tk.Frame(self.canvas, bg=colors["primary"])
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self._games = None
        self._cards = {}          # gid(str) -> dict kartu
        self._order = []          # semua kartu, urutan library
        self._visible = []        # kartu yang lolos filter pencarian
        self._needle = ""
        self._layout_key = None
        self._selected = None
        self._photos = {}         # gid -> PhotoImage (referensi harus disimpan)
        self._pending = set()
        self._gen = 0             # naik tiap set_games(); hasil thread lama diabaikan
        self._t_layout = self._t_load = None
        self._pool = ThreadPoolExecutor(max_workers=4)
        self._placeholder = self._make_photo(None)

        self.canvas.bind("<Configure>", lambda _e: self._schedule_layout())
        self.bind("<Destroy>", self._on_destroy)
        self._bind_wheel(self.canvas)
        self._bind_wheel(self.inner)

    def _make_photo(self, title):
        """Gambar pengganti (warna tema + huruf pertama judul) selagi cover belum ada."""
        img = Image.new("RGB", (self.card_w, self.card_h), self.c.get("secondary", "#16213e"))
        if title:
            try:
                d = ImageDraw.Draw(img)
                try:
                    font = ImageFont.load_default(size=max(16, self.card_h // 2))
                except TypeError:
                    font = ImageFont.load_default()
                d.text((self.card_w // 2, self.card_h // 2), (title.strip()[:1] or "?").upper(),
                       fill=self.c.get("text_secondary", "#b0b0b0"), font=font, anchor="mm")
            except Exception:
                pass
        return ImageTk.PhotoImage(img)

    def _bind_wheel(self, w):
        w.bind("<MouseWheel>", lambda e: self._scroll(-1 if e.delta > 0 else 1), add="+")
        w.bind("<Button-4>", lambda _e: self._scroll(-1), add="+")
        w.bind("<Button-5>", lambda _e: self._scroll(1), add="+")

    def _scroll(self, n):
        if self.inner.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(n, "units")

    def _on_yscroll(self, first, last):
        self.vscroll.set(first, last)
        self._schedule_load()

    def _on_destroy(self, e):
        if e.widget is self:
            try:
                self._pool.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                self._pool.shutdown(wait=False)

    def _later(self, attr, delay, fn):
        old = getattr(self, attr)
        if old:
            try:
                self.after_cancel(old)
            except Exception:
                pass
        setattr(self, attr, self.after(delay, fn))

    def _schedule_layout(self, delay=30):
        self._later("_t_layout", delay, self._layout)

    def _schedule_load(self, delay=60):
        self._later("_t_load", delay, self._load_visible)

    def set_games(self, games):
        if games is self._games:
            return
        self._games = games
        self._gen += 1
        for card in self._cards.values():
            card["frame"].destroy()
        self._cards, self._order, self._visible = {}, [], []
        self._photos.clear()
        self._pending.clear()
        self._selected = None
        self._layout_key = None
        for g in games:
            card = self._make_card(g)
            self._cards[card["gid"]] = card
            self._order.append(card)
        self._schedule_layout()

    def set_filter(self, needle):
        needle = (needle or "").strip().lower()
        if needle != self._needle:
            self._needle = needle
            self._schedule_layout()

    def selected_id(self):
        return self._selected if self._selected in self._cards else None

    def select(self, gid):
        prev = self._cards.get(self._selected)
        if prev:
            prev["frame"].config(highlightbackground=self.c["card_bg"], highlightcolor=self.c["card_bg"])
        self._selected = gid
        card = self._cards.get(gid)
        if card:
            hl = self.c.get("highlight", "#e94560")
            card["frame"].config(highlightbackground=hl, highlightcolor=hl)

    def _make_card(self, g):
        gid = str(g["id"])
        bg, fg = self.c["card_bg"], self.c["text"]
        f = tk.Frame(self.inner, bg=bg, highlightthickness=2, highlightbackground=bg,
                     highlightcolor=bg, cursor="hand2")
        img = tk.Label(f, image=self._placeholder, bg=bg, bd=0)
        img.pack(padx=4, pady=(4, 2))
        title = tk.Label(f, text=g["title"], bg=bg, fg=fg, font=self.fonts["small"],
                         wraplength=self.card_w, justify="center", anchor="n", height=2)
        title.pack(padx=4)
        plat = tk.Label(f, text=g.get("platforms") or " ", bg=bg,
                        fg=self.c.get("text_secondary", "#b0b0b0"), font=self.fonts["small"])
        plat.pack(padx=4, pady=(0, 4))
        for w in (f, img, title, plat):
            w.bind("<Button-1>", lambda _e, i=gid: self.select(i))
            w.bind("<Double-1>", lambda _e, i=gid: (self.select(i), self.on_activate()))
            self._bind_wheel(w)
        return {"gid": gid, "game": g, "frame": f, "img": img, "lower": g["title"].lower()}

    def _layout(self):
        self._t_layout = None
        if not self.winfo_exists():
            return
        width = self.canvas.winfo_width()
        if width <= 1:  # belum tampil
            self._schedule_layout(50)
            return
        cols = max(1, width // (self.card_w + 26))
        visible = [c for c in self._order if self._needle in c["lower"]]
        key = (cols, tuple(c["gid"] for c in visible))
        if key != self._layout_key:
            filter_changed = self._layout_key is None or key[1] != self._layout_key[1]
            self._layout_key = key
            for c in self._order:
                c["frame"].grid_forget()
            for i, c in enumerate(visible):
                c["frame"].grid(row=i // cols, column=i % cols, padx=6, pady=6, sticky="n")
            self._visible = visible
            self.inner.update_idletasks()
            if filter_changed:
                self.canvas.yview_moveto(0)
        iw, ih = self.inner.winfo_reqwidth(), self.inner.winfo_reqheight()
        self.canvas.coords(self._win, max(0, (width - iw) // 2), 0)
        self.canvas.configure(scrollregion=(0, 0, max(width, iw), max(ih, 1)))
        self._schedule_load()

    def _load_visible(self):
        self._t_load = None
        if not self.winfo_exists():
            return
        top = self.canvas.canvasy(0)
        bottom = top + self.canvas.winfo_height()
        margin = self.card_h
        for c in self._visible:
            f = c["frame"]
            y, h = f.winfo_y(), f.winfo_height()
            if y + h < top - margin or y > bottom + margin:
                continue
            gid = c["gid"]
            if gid in self._pending or gid in self._photos:
                continue
            self._pending.add(gid)
            self._pool.submit(self._worker, c["game"], self._gen)

    def _worker(self, game, gen):
        """Thread: unduh/baca cover. Hasil dikirim ke main thread."""
        try:
            img = load_cover(self.cfg, game, self.card_w, self.card_h)
        except Exception as e:
            print(f"[WLM/GOG] Cover for '{game.get('title')}' failed: {e}")
            img = None
        gid = str(game["id"])
        try:
            _host.root.after(0, lambda: self._on_cover(gid, gen, img))
        except Exception:
            pass

    def _on_cover(self, gid, gen, img):
        try:
            if gen != self._gen or not self.winfo_exists():
                return
            card = self._cards.get(gid)
            self._pending.discard(gid)
            if not card:
                return
            photo = ImageTk.PhotoImage(img) if img is not None else self._make_photo(card["game"]["title"])
            self._photos[gid] = photo
            card["img"].config(image=photo)
        except tk.TclError:
            pass  # jendela ditutup saat cover sedang dimuat


# --------------------------------------------------------------------------- main dialog

def open_dialog(host):
    """Titik masuk dari launcher. host: objek dengan atribut root, colors(), fonts, data_dir,
    human_size(), status(text, level), install_exe(path, choice=None,
    installer_args=None) -> Popen|None, add_shortcut(name, exe, choice, icon_image=None,
    work_dir=None, source=None, gog_id=None), ask_runner(parent=None, game_title=None), clean_env()."""
    global _host, _dialog
    _host = host
    _ensure_jobs_loaded()
    if _dialog is not None and _dialog.winfo_exists():
        _dialog.deiconify()
        _dialog.lift()
        _dialog.focus_set()
        return

    cfg = load_config()
    fonts = host.fonts
    dialog = _dialog = _new_toplevel(host.root, "GOG Store")
    _standalone(dialog)
    dialog.minsize(640, 460)
    state = {"games": []}

    frame = ttk.Frame(dialog, padding=15)
    frame.pack(fill=tk.BOTH, expand=True)

    top = ttk.Frame(frame)
    top.pack(fill=tk.X, pady=(0, 8))
    account_lbl = ttk.Label(top, text="", font=fonts["normal"])
    account_lbl.pack(side=tk.LEFT)
    logout_btn = ttk.Button(top, text="Logout", style="Custom.TButton", width=10)
    logout_btn.pack(side=tk.RIGHT, padx=(6, 0))
    login_btn = ttk.Button(top, text="Login", style="Custom.TButton", width=10)
    login_btn.pack(side=tk.RIGHT, padx=(6, 0))
    refresh_btn = ttk.Button(top, text="Refresh", style="Custom.TButton", width=10)
    refresh_btn.pack(side=tk.RIGHT)
    view_btn = None
    if HAVE_PIL:
        view_btn = ttk.Button(top, text="List View", style="Custom.TButton", width=10)
        view_btn.pack(side=tk.RIGHT, padx=(0, 6))
    downloads_btn = ttk.Button(top, text="Downloads", style="Custom.TButton", width=14,
                               command=open_downloads_window)
    downloads_btn.pack(side=tk.RIGHT, padx=(0, 6))

    search_row = ttk.Frame(frame)
    search_row.pack(fill=tk.X, pady=(0, 6))
    ttk.Label(search_row, text="Search:", font=fonts["normal"]).pack(side=tk.LEFT, padx=(0, 6))
    search_var = tk.StringVar()
    ttk.Entry(search_row, textvariable=search_var, font=fonts["normal"], style="Search.TEntry").pack(
        side=tk.LEFT, fill=tk.X, expand=True)

    status_line = ttk.Label(frame, text="", font=fonts["small"])
    status_line.pack(anchor="w", pady=(0, 6))

    content = ttk.Frame(frame)
    content.pack(fill=tk.BOTH, expand=True)
    list_frame = ttk.Frame(content)
    list_frame.rowconfigure(0, weight=1)
    list_frame.columnconfigure(0, weight=1)
    vscroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL)
    vscroll.grid(row=0, column=1, sticky="ns")
    tree = ttk.Treeview(list_frame, columns=("Title", "Platforms"), show="headings",
                        yscrollcommand=vscroll.set, selectmode="browse", height=12)
    tree.grid(row=0, column=0, sticky="nsew")
    vscroll.config(command=tree.yview)
    tree.heading("Title", text="Game", anchor="w")
    tree.heading("Platforms", text="Platforms", anchor="w")
    tree.column("Title", width=420, anchor="w", stretch=True)
    tree.column("Platforms", width=180, anchor="w", stretch=False)

    cover_grid = None
    if HAVE_PIL:
        cover_grid = _CoverGrid(content, cfg, host.colors(), fonts, lambda: do_download())
    view = {"mode": "list"}

    def show_view(mode):
        if mode == "grid" and cover_grid is None:
            mode = "list"
        view["mode"] = mode
        list_frame.pack_forget()
        if cover_grid is not None:
            cover_grid.pack_forget()
        (cover_grid if mode == "grid" else list_frame).pack(fill=tk.BOTH, expand=True)
        if view_btn is not None:
            view_btn.config(text="List View" if mode == "grid" else "Grid View")

    if view_btn is not None:
        view_btn.config(command=lambda: show_view("list" if view["mode"] == "grid" else "grid"))

    def populate(*_):
        needle = search_var.get().strip().lower()
        tree.delete(*tree.get_children())
        for g in state["games"]:
            if needle in g["title"].lower():
                tree.insert("", tk.END, iid=str(g["id"]), values=(g["title"], g["platforms"]))
        if cover_grid is not None:
            cover_grid.set_games(state["games"])
            cover_grid.set_filter(needle)

    search_var.trace_add("write", populate)
    show_view("list" if str(cfg.get("view", "grid")).lower() == "list" else "grid")

    def update_account_ui():
        auth = load_auth()
        logged_in = bool(auth.get("refresh_token"))
        if logged_in:
            account_lbl.config(text=f"Logged in as {auth.get('username') or 'GOG user'}")
        else:
            account_lbl.config(text="Not logged in")
            state["games"] = []
            populate()
            status_line.config(text="Click Login to connect your GOG account.")
        login_btn.config(state="disabled" if logged_in else "normal")
        logout_btn.config(state="normal" if logged_in else "disabled")
        refresh_btn.config(state="normal" if logged_in else "disabled")
        return logged_in

    def handle_error(err, prefix):
        if isinstance(err, GogAuthError):
            update_account_ui()
        status_line.config(text=f"{prefix}: {err}")
        messagebox.showerror("GOG Store", f"{prefix}:\n{err}", parent=dialog)

    def load_library():
        status_line.config(text="Loading your GOG library...")
        refresh_btn.config(state="disabled")

        def work():
            return fetch_library(cfg, get_access_token(cfg))

        def done(res, err):
            if not dialog.winfo_exists():
                return
            refresh_btn.config(state="normal" if load_auth().get("refresh_token") else "disabled")
            if err:
                handle_error(err, "Failed to load library")
                return
            state["games"] = res
            populate()
            status_line.config(text=f"{len(res)} game(s) in your library. "
                                    "Select one, then click Direct Download or Download Setup.")
        _bg(work, done)

    def on_logged_in():
        update_account_ui()
        host.status("Logged in to GOG.", "success")
        load_library()

    def do_logout():
        if messagebox.askyesno("Logout", "Remove the saved GOG login from this launcher?", parent=dialog):
            clear_auth()
            update_account_ui()
            host.status("Logged out of GOG.", "warning")

    login_btn.config(command=lambda: _open_login_dialog(dialog, cfg, on_logged_in))
    logout_btn.config(command=do_logout)
    refresh_btn.config(command=load_library)

    def selected_game():
        if view["mode"] == "grid" and cover_grid is not None:
            gid = cover_grid.selected_id()
        else:
            sel = tree.selection()
            gid = sel[0] if sel else None
        if gid is None:
            messagebox.showinfo("GOG Store", "Select a game from the list first.", parent=dialog)
            return None
        return next((g for g in state["games"] if str(g["id"]) == str(gid)), None)

    def download_root():
        custom = (cfg.get("download_dir") or "").strip()
        return Path(custom).expanduser() if custom else Path(host.data_dir) / "gog_downloads"

    def game_root():
        custom = (cfg.get("game_dir") or "").strip()
        return Path(custom).expanduser() if custom else Path(host.data_dir) / "gog_games"

    def start_direct(game, picked, choice=None):
        folder = _safe_name(picked.get("install_dir") or game["title"], game["id"])
        try:
            add_direct_download(cfg, game, picked, game_root() / folder, choice)
        except GogError as e:
            messagebox.showinfo("GOG Store", str(e), parent=dialog)
            return
        open_downloads_window()

    def do_direct(_event=None):
        game = selected_game()
        if not game:
            return
        status_line.config(text=f"Looking up direct download for {game['title']}...")

        def work():
            token = get_access_token(cfg)
            builds = fetch_builds(cfg, token, game["id"])
            return builds, (fetch_owned_ids(cfg, token) if builds else set())

        def done(res, err):
            if not dialog.winfo_exists():
                return
            if err:
                handle_error(err, "Failed to look up the game")
                return
            status_line.config(text="")
            builds, owned = res
            if not builds:
                messagebox.showinfo(
                    "GOG Store",
                    f"Direct download is not available for '{game['title']}' "
                    f"({_platform(cfg)} build in the new format not found).\n\n"
                    "Use Download Setup instead.", parent=dialog)
                return
            picked = _choose_direct(dialog, cfg, game, builds, owned)
            if picked is None:
                return
            start_direct(game, picked, picked.get("choice"))
        _bg(work, done)

    def start_download(game, installer, choice=None):
        dest = download_root() / _safe_name(game["title"], game["id"])
        try:
            add_download(cfg, game, installer, dest, choice)
        except GogError as e:
            messagebox.showinfo("GOG Store", str(e), parent=dialog)
            return
        open_downloads_window()

    def do_download(_event=None):
        game = selected_game()
        if not game:
            return
        status_line.config(text=f"Fetching installers for {game['title']}...")

        def work():
            return fetch_installers(cfg, get_access_token(cfg), game["id"])

        def done(installers, err):
            if not dialog.winfo_exists():
                return
            if err:
                handle_error(err, "Failed to fetch installers")
                return
            status_line.config(text="")
            if not installers:
                messagebox.showinfo("GOG Store", f"No downloadable installer found for '{game['title']}'.",
                                    parent=dialog)
                return
            idx = _choose_installer(dialog, game["title"], installers)
            if idx is None:
                return
            installer = installers[idx]
            choice = None
            ask_runner = getattr(host, "ask_runner", None)
            # Installer Windows (.exe) -> pilih runner/prefix SEKARANG, sebelum download, supaya
            # begitu selesai langsung terpasang ke prefix itu (tidak perlu download/pilih 2x).
            if ask_runner and str(installer.get("os", "")).lower() == "windows":
                choice = ask_runner(parent=dialog, game_title=game["title"])
                if choice is None and not messagebox.askyesno(
                        "GOG Store",
                        "No runner selected.\n\nDownload the installer only (without installing)?",
                        parent=dialog):
                    return
            start_download(game, installer, choice)
        _bg(work, done)

    def open_folder():
        folder = download_root()
        folder.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(folder)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=host.clean_env())

    def open_games_folder():
        folder = game_root()
        folder.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(folder)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=host.clean_env())

    tree.bind("<Double-1>", do_download)

    btn_row = ttk.Frame(frame)
    btn_row.pack(pady=(10, 0))
    ttk.Button(btn_row, text="Direct Download", style="Custom.TButton", width=18,
               command=do_direct).grid(row=0, column=0, padx=3)
    ttk.Button(btn_row, text="Download Setup", style="Custom.TButton", width=18,
               command=do_download).grid(row=0, column=1, padx=3)
    ttk.Button(btn_row, text="Open Games Folder", style="Custom.TButton", width=18,
               command=open_games_folder).grid(row=0, column=2, padx=3)
    ttk.Button(btn_row, text="Open Setup Folder", style="Custom.TButton", width=18,
               command=open_folder).grid(row=0, column=3, padx=3)

    # Ukuran jendela tersimpan (gog_ui.json, bagian "store"); tidak kereset saat dibuka lagi.
    # Tidak memakai transient(): jendela terpisah, jadi klik di dalamnya tidak ikut mengangkat
    # jendela launcher / memicu taskbar autohide.
    store_w, store_h = 900, 640
    try:
        sw_, sh_ = str(_ui_load("store").get("size", "")).split("x")
        store_w, store_h = max(640, int(sw_)), max(460, int(sh_))
    except ValueError:
        pass
    _place(dialog, host.root, store_w, store_h)
    dialog.deiconify()
    dialog.lift()
    dialog.focus_set()

    store_timer = {"id": None}

    def save_store_ui(*_):
        try:
            if dialog.winfo_exists() and dialog.state() == "normal":      # jangan simpan saat maximized/minimized
                _ui_save("store", {"size": f"{dialog.winfo_width()}x{dialog.winfo_height()}"})
        except tk.TclError:
            pass

    def save_store_ui_soon(event=None):
        if event is not None and event.widget is not dialog:
            return
        if store_timer["id"]:
            dialog.after_cancel(store_timer["id"])
        store_timer["id"] = dialog.after(700, save_store_ui)

    def on_store_close():
        save_store_ui()
        dialog.destroy()

    dialog.bind("<Configure>", save_store_ui_soon, add="+")
    dialog.protocol("WM_DELETE_WINDOW", on_store_close)

    def refresh_downloads_btn():
        if not dialog.winfo_exists():
            return
        n = len(active_jobs()) + sum(1 for j in _jobs if j["status"] == "paused")
        downloads_btn.config(text=f"Downloads ({n})" if n else "Downloads")
        dialog.after(1000, refresh_downloads_btn)

    refresh_downloads_btn()
    if update_account_ui():
        load_library()
