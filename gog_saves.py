"""gog_saves.py - Save Manager: backup save game lokal + cloud save GOG (cek & download).

Dipakai launcher.py lewat tombol SAVES (open_dialog). Hanya butuh library standar.

Fitur
- Backup / restore lokal: folder save game diarsipkan ke ~/wlm/save_backups/<game>/*.tar.gz.
  Game GOG: lokasi save diambil dari konfigurasi cloud GOG (remote-config) lalu dipetakan ke
  dalam prefix Wine/Proton. Game apa pun: folder tambahan bisa ditambah manual (Add Folder).
- Cloud GOG (hanya game GOG yang punya clientId di goggame-*.info):
    Check Cloud            : bandingkan file cloud dengan file lokal (tidak mengubah apa pun).
    Download & Overwrite   : unduh save cloud dan MENIMPA file lokal yang namanya sama.
                             Selalu ada peringatan + backup otomatis dulu. File yang hanya ada
                             di lokal tidak dihapus.
  UPLOAD ke cloud sengaja belum ada: format upload (gzip + Etag) belum bisa diverifikasi dan
  upload yang salah bisa merusak save di cloud.

Alur cloud (dokumentasi publik Lariaa/GameLauncherResearch + perilaku Heroic/gogdl):
  1. clientId game dari goggame-<id>.info; clientSecret dari meta build
     (content-system.gog.com/products/<id>/os/windows/builds?generation=2 -> link -> zlib JSON).
  2. Token khusus game: auth.gog.com/token (refresh_token dari login GOG Store, without_new_session=1).
  3. Lokasi save: remote-config.gog.com/components/galaxy_client/clients/<clientId>.
  4. Daftar/unduh: cloudstorage.gog.com/v1/<user_id>/<clientId>[/<nama file>].
Semua fungsi jaringan BLOCKING (panggil dari thread).
"""
import getpass
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

try:
    import install_watch          # drive_c_for() / resolve_ci(); satu folder dengan launcher.py
except ImportError:
    install_watch = None

USER_AGENT = "wine-launcher-manager"
REMOTE_CONFIG_URL = "https://remote-config.gog.com/components/galaxy_client/clients/{cid}?component_version=2.0.43"
BUILDS_URL = "https://content-system.gog.com/products/{pid}/os/windows/builds?generation=2"
TOKEN_URL = "https://auth.gog.com/token"
CLOUD_URL = "https://cloudstorage.gog.com/v1/{uid}/{cid}"
MAX_FILE_BYTES = 256 * 1024 * 1024
_GOG_STOP_DIRS = {"drive_c", "gog games", "games", "gog.com", "program files", "program files (x86)"}


class SaveError(Exception):
    """Kesalahan yang pesannya layak ditampilkan langsung ke pengguna."""


class OfflineError(SaveError):
    """Tidak ada koneksi internet / timeout."""


# --------------------------------------------------------------------------- helpers

def _safe(text, fallback="game"):
    s = re.sub(r"[^\w.\- ]+", "_", text or "").strip(" .")
    return s or fallback


def _human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _parse_ts(text):
    """ISO 8601 / RFC 1123 -> epoch (float) atau None. Tanpa zona waktu dianggap UTC."""
    if not text:
        return None
    text = str(text).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(text)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _http(url, headers=None, timeout=20, limit=4 * 1024 * 1024):
    """(headers, body). OfflineError kalau tidak terhubung; SaveError(.code) untuk HTTP error."""
    hdrs = {"User-Agent": USER_AGENT}
    hdrs.update(headers or {})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdrs), timeout=timeout) as resp:
            body = resp.read(limit + 1)
            info = resp.headers
    except urllib.error.HTTPError as e:            # harus sebelum URLError (HTTPError turunannya)
        err = SaveError(f"GOG returned HTTP {e.code} for {urllib.parse.urlparse(url).netloc}.")
        err.code = e.code
        raise err
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise OfflineError(f"Could not reach GOG ({getattr(e, 'reason', e)}). Check your internet connection.")
    if len(body) > limit:
        raise SaveError("Response from GOG was too large.")
    return info, body


def _json(url, headers=None, timeout=20):
    _, body = _http(url, headers, timeout)
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        raise SaveError(f"Unexpected (non-JSON) answer from {urllib.parse.urlparse(url).netloc}.")


def _ci(base, rel):
    """Ikuti path relatif tanpa peduli huruf besar/kecil. None kalau ada bagian yang tidak ada."""
    if install_watch is not None:
        return install_watch.resolve_ci(base, rel)
    cur = Path(base)
    for part in re.split(r"[\\/]+", rel):
        if part in ("", "."):
            continue
        nxt = cur / part
        if not nxt.exists():
            try:
                nxt = next((e for e in cur.iterdir() if e.name.lower() == part.lower()), None)
            except OSError:
                nxt = None
            if nxt is None:
                return None
        cur = nxt
    return cur


def _join_ci(base, rel):
    """base + rel; bagian yang sudah ada mengikuti huruf besar/kecil yang ada, sisanya apa adanya."""
    cur = Path(base)
    parts = [p for p in re.split(r"[\\/]+", rel) if p not in ("", ".")]
    for i, part in enumerate(parts):
        if part == "..":
            raise SaveError("Unsafe path in save location.")
        nxt = _ci(cur, part) if cur.is_dir() else None
        if nxt is None:
            return cur.joinpath(*parts[i:])
        cur = nxt
    return cur


# --------------------------------------------------------------------------- game info / paths

def find_goggame_info(exe_path):
    """(info_path, data) dari goggame-<id>.info di folder exe (naik maksimal 4 tingkat)."""
    if not exe_path:
        return None, None
    p = Path(exe_path).parent
    for _ in range(4):
        found = []
        try:
            with os.scandir(p) as it:
                for e in it:
                    n = e.name.lower()
                    if n.startswith("goggame-") and n.endswith(".info"):
                        try:
                            found.append((Path(e.path), json.loads(Path(e.path).read_text(encoding="utf-8-sig"))))
                        except Exception:
                            pass
        except OSError:
            return None, None
        if found:
            for path, data in found:                 # utamakan game induk (bukan DLC)
                gid, root = str(data.get("gameId", "")), str(data.get("rootGameId", ""))
                if not root or gid == root:
                    return path, data
            return found[0]
        if p.name.lower() in _GOG_STOP_DIRS or p.parent == p:
            break
        p = p.parent
    return None, None


def _user_home(drive_c):
    users = _ci(drive_c, "users")
    if users is None or not users.is_dir():
        return None
    try:
        names = [e.name for e in users.iterdir() if e.is_dir()]
    except OSError:
        return None
    skip = {"public", "all users", "default", "default user"}
    cand = [n for n in names if n.lower() not in skip]
    for want in ("steamuser", getpass.getuser()):
        for n in cand:
            if n.lower() == want.lower():
                return users / n
    return users / sorted(cand)[0] if cand else None


_MACROS = {
    "DOCUMENTS": (("Documents",), ("My Documents",)),
    "SAVED_GAMES": (("Saved Games",),),
    "APPLICATION_DATA_LOCAL": (("AppData", "Local"), ("Local Settings", "Application Data")),
    "APPLICATION_DATA_LOCAL_LOW": (("AppData", "LocalLow"),),
    "APPLICATION_DATA_ROAMING": (("AppData", "Roaming"), ("Application Data",)),
}


def resolve_location(location, drive_c, install_dir):
    """'<?DOCUMENTS?>/My Games/X' -> Path di dalam prefix. None kalau makro tidak dikenal."""
    m = re.match(r"^<\?([A-Z_]+)\?>[\\/]*(.*)$", (location or "").strip())
    if not m:
        return None
    name, rest = m.group(1), m.group(2)
    if name == "INSTALL":
        base = Path(install_dir) if install_dir else None
    elif name in _MACROS:
        home = _user_home(drive_c) if drive_c else None
        if home is None:
            return None
        base = None
        for cand in _MACROS[name]:
            p = _ci(home, "/".join(cand))
            if p is not None and p.is_dir():
                base = p
                break
        base = base or home.joinpath(*_MACROS[name][0])
    else:
        return None
    return _join_ci(base, rest) if base is not None else None


def list_files(root):
    """{relpath(posix): (Path, size, mtime)} untuk semua file biasa di bawah root."""
    out = {}
    root = Path(root)
    if not root.is_dir():
        return out
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = Path(dirpath) / f
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                st = p.stat()
            except OSError:
                continue
            out[p.relative_to(root).as_posix()] = (p, st.st_size, st.st_mtime)
    return out


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_dest(base, rel):
    """base/rel, ditolak (None) kalau rel mencoba keluar dari base."""
    parts = [p for p in re.split(r"[\\/]+", rel) if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or rel.startswith(("/", "\\")):
        return None
    return Path(base).joinpath(*parts)


# --------------------------------------------------------------------------- config / cache

def _cache_path(data_dir):
    return Path(data_dir) / "gog_cloud_cache.json"


def _load_cache(data_dir):
    try:
        return json.loads(_cache_path(data_dir).read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_cache(data_dir, cache):
    try:
        tmp = _cache_path(data_dir).with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, indent=1), encoding="utf-8")
        tmp.replace(_cache_path(data_dir))
    except Exception as e:
        print(f"[WLM] Could not write cloud cache: {e}")


def _custom_path(data_dir):
    return Path(data_dir) / "save_folders.json"


def load_custom_folders(data_dir, game):
    try:
        data = json.loads(_custom_path(data_dir).read_text(encoding="utf-8"))
        return [p for p in data.get(game, []) if isinstance(p, str)]
    except Exception:
        return []


def save_custom_folders(data_dir, game, folders):
    try:
        data = json.loads(_custom_path(data_dir).read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if folders:
        data[game] = folders
    else:
        data.pop(game, None)
    _custom_path(data_dir).write_text(json.dumps(data, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------- GOG cloud API

def galaxy_refresh_token(data_dir):
    """Refresh token login GOG Store (gog_auth.json, ditulis gog_store.py)."""
    try:
        auth = json.loads((Path(data_dir) / "gog_auth.json").read_text(encoding="utf-8"))
    except Exception:
        auth = {}
    if not auth.get("refresh_token"):
        raise SaveError("Not logged in to GOG. Open GOG STORE and log in first.")
    return auth["refresh_token"], str(auth.get("user_id") or "")


def _zlib_json(data):
    for wbits in (15, -15, 31):
        try:
            return json.loads(zlib.decompress(data, wbits).decode("utf-8"))
        except Exception:
            pass
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        raise SaveError("Could not read the GOG build manifest.")


def client_credentials(data_dir, info):
    """(client_id, client_secret). Secret dicari di cache lokal ('secrets' di gog_cloud_cache.json,
    boleh diisi manual), lalu di meta build GOG."""
    client_id = str(info.get("clientId") or "")
    if not client_id:
        raise SaveError("This game has no GOG clientId (it does not use GOG Galaxy cloud saves).")
    cache = _load_cache(data_dir)
    secret = (cache.get("secrets") or {}).get(client_id)
    if secret:
        return client_id, secret
    pids = []
    for key in ("rootGameId", "gameId"):
        v = str(info.get(key) or "")
        if v and v not in pids:
            pids.append(v)
    for pid in pids:
        try:
            builds = _json(BUILDS_URL.format(pid=pid))
        except SaveError as e:
            if isinstance(e, OfflineError):
                raise
            continue
        for item in (builds.get("items") or [])[:3]:
            link = item.get("link")
            if not link:
                continue
            _, body = _http(link, timeout=30)
            meta = _zlib_json(body)
            sec, cid = meta.get("clientSecret"), str(meta.get("clientId") or client_id)
            if sec and cid == client_id:
                cache.setdefault("secrets", {})[client_id] = sec
                _save_cache(data_dir, cache)
                return client_id, sec
    raise SaveError(
        "Could not find the cloud credentials of this game.\n"
        "If you know its client secret, add it to gog_cloud_cache.json under "
        f"\"secrets\": {{\"{client_id}\": \"<secret>\"}}.")


def game_token(client_id, secret, refresh_token):
    q = urllib.parse.urlencode({"client_id": client_id, "client_secret": secret,
                                "grant_type": "refresh_token", "refresh_token": refresh_token,
                                "without_new_session": 1})
    try:
        data = _json(f"{TOKEN_URL}?{q}")
    except OfflineError:
        raise
    except SaveError as e:
        if getattr(e, "code", 0) in (400, 401, 403):
            raise SaveError("GOG rejected the login session. Log in again in GOG STORE and retry.")
        raise
    if not data.get("access_token"):
        raise SaveError("GOG did not return an access token for this game.")
    return data["access_token"], str(data.get("user_id") or "")


def cloud_locations(data_dir, client_id, refresh=True):
    """{'enabled': bool, 'locations': [{'name','location'}], 'from_cache': bool}. Kalau offline
    dipakai hasil terakhir yang tersimpan di cache."""
    cache = _load_cache(data_dir)
    entry = (cache.get("locations") or {}).get(client_id)
    if refresh:
        try:
            data = _json(REMOTE_CONFIG_URL.format(cid=client_id))
            cs = ((data.get("content") or {}).get("Windows") or {}).get("cloudStorage") or {}
            locs = [{"name": str(x.get("name") or ""), "location": str(x.get("location") or "")}
                    for x in (cs.get("locations") or []) if isinstance(x, dict) and x.get("location")]
            entry = {"enabled": bool(cs.get("enabled")) and bool(locs), "locations": locs}
            cache.setdefault("locations", {})[client_id] = entry
            _save_cache(data_dir, cache)
            return dict(entry, from_cache=False)
        except OfflineError:
            if not entry:
                raise
    if entry:
        return dict(entry, from_cache=True)
    raise SaveError("Save locations of this game are unknown yet (connect to the internet once).")


def cloud_list(user_id, client_id, token):
    """[{'name','size','hash','mtime'}] - file save milik pengguna di cloud untuk game ini."""
    data = _json(CLOUD_URL.format(uid=user_id, cid=client_id),
                 {"Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout=30)
    if isinstance(data, dict):
        data = data.get("items") or data.get("files") or []
    out = []
    for f in data if isinstance(data, list) else []:
        if not isinstance(f, dict) or not f.get("name") or str(f["name"]).endswith("/"):
            continue
        try:
            size = int(f.get("bytes") or f.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        out.append({"name": str(f["name"]), "size": size, "hash": str(f.get("hash") or "").lower(),
                    "mtime": _parse_ts(f.get("last_modified"))})
    return out


def cloud_get(user_id, client_id, token, name, expected_hash=""):
    """(bytes isi file asli, mtime lokal asli atau None). File di cloud disimpan gzip."""
    url = CLOUD_URL.format(uid=user_id, cid=client_id) + "/" + urllib.parse.quote(name)
    headers, body = _http(url, {"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"},
                          timeout=60, limit=MAX_FILE_BYTES)
    data = body
    if "gzip" in (headers.get("Content-Encoding") or "").lower():
        try:
            data = gzip.decompress(body)
        except Exception:
            raise SaveError(f"Could not decompress '{name}'.")
    elif body[:2] == b"\x1f\x8b":
        # tanpa header: hash yang cocok dengan isi mentah berarti file aslinya memang gzip
        try:
            dec = gzip.decompress(body)
        except Exception:
            dec = None
        if dec is not None and not (expected_hash and hashlib.md5(body).hexdigest() == expected_hash):
            data = dec
    return data, _parse_ts(headers.get("X-Object-Meta-LocalLastModified"))


# --------------------------------------------------------------------------- targets

def build_targets(ctx, log=None, refresh=True):
    """Daftar folder save game ini: [{'label','kind','name','macro','path': Path|None, 'note'}].
    ctx: dict dari open_dialog (name, exe, choice, data_dir, info)."""
    targets = []
    info = ctx.get("info")
    if info and info.get("clientId"):
        drive_c = install_watch.drive_c_for(ctx.get("choice") or {}) if install_watch else None
        install_dir = ctx["info_path"].parent if ctx.get("info_path") else None
        try:
            cfg = cloud_locations(ctx["data_dir"], str(info["clientId"]), refresh=refresh)
        except SaveError as e:
            if log:
                log(f"Save locations: {e}")
            cfg = {"locations": []}
        for loc in cfg["locations"]:
            path = resolve_location(loc["location"], drive_c, install_dir)
            targets.append({"label": "gog_" + _safe(loc["name"], "saves"), "kind": "gog",
                            "name": loc["name"], "macro": loc["location"], "path": path,
                            "note": "" if path else "unsupported location"})
    for i, folder in enumerate(load_custom_folders(ctx["data_dir"], ctx["name"]), start=1):
        targets.append({"label": f"custom{i}", "kind": "custom", "name": f"custom{i}",
                        "macro": "", "path": Path(folder), "note": ""})
    return targets


# --------------------------------------------------------------------------- local backup

def backups_dir(data_dir, game):
    return Path(data_dir) / "save_backups" / _safe(game)


def list_backups(data_dir, game):
    d = backups_dir(data_dir, game)
    out = []
    if d.is_dir():
        for p in sorted(d.glob("*.tar.gz"), reverse=True):
            m = re.match(r"^(\d{8}-\d{6})_(.*)\.tar\.gz$", p.name)
            when = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M:%S") if m else p.name
            out.append({"path": p, "when": when, "note": (m.group(2) if m else ""), "size": p.stat().st_size})
    return out


def make_backup(data_dir, game, targets, note="manual"):
    """Arsipkan semua target yang ada isinya. Return (Path, jumlah_file). SaveError kalau kosong."""
    entries, total = [], 0
    for t in targets:
        p = t.get("path")
        if p is not None and Path(p).is_dir():
            n = len(list_files(p))
            if n:
                entries.append({"label": t["label"], "kind": t["kind"], "name": t["name"],
                                "macro": t["macro"], "path": str(p), "files": n})
                total += n
    if not entries:
        raise SaveError("There are no save files to back up yet.")
    d = backups_dir(data_dir, game)
    d.mkdir(parents=True, exist_ok=True)
    final = d / f"{time.strftime('%Y%m%d-%H%M%S')}_{_safe(note, 'manual')}.tar.gz"
    tmp = final.with_name(final.name + ".tmp")
    manifest = json.dumps({"version": 1, "game": game, "created": datetime.now().isoformat(timespec="seconds"),
                           "note": note, "entries": entries}, indent=1).encode("utf-8")
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            ti = tarfile.TarInfo("manifest.json")
            ti.size, ti.mtime = len(manifest), time.time()
            tar.addfile(ti, io.BytesIO(manifest))
            for e in entries:
                tar.add(e["path"], arcname=e["label"], recursive=True)
        tmp.replace(final)
    finally:
        tmp.unlink(missing_ok=True)
    return final, total


def restore_backup(archive, targets, log):
    """Kembalikan isi arsip ke folder save saat ini (menimpa file bernama sama). Return jumlah file."""
    by_label = {t["label"]: t.get("path") for t in targets}
    count = 0
    with tarfile.open(archive, "r:gz") as tar:
        try:
            manifest = json.loads(tar.extractfile("manifest.json").read().decode("utf-8"))
        except Exception:
            raise SaveError("This backup has no readable manifest.")
        dest_for = {}
        for e in manifest.get("entries", []):
            dest = by_label.get(e["label"])
            if dest is None and e.get("kind") == "custom" and e.get("path"):
                dest = Path(e["path"])
            if dest is None:
                log(f"Skipped '{e.get('name')}': its folder can't be located now.")
                continue
            dest_for[e["label"]] = Path(dest)
        for m in tar:
            if m.name == "manifest.json" or not m.isfile():
                continue
            label, _, rel = m.name.partition("/")
            base = dest_for.get(label)
            target = _safe_dest(base, rel) if base is not None and rel else None
            if target is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".wlm-tmp")
            with tar.extractfile(m) as src, open(tmp, "wb") as out:
                shutil.copyfileobj(src, out)
            tmp.replace(target)
            try:
                os.utime(target, (m.mtime, m.mtime))
            except OSError:
                pass
            count += 1
    return count


# --------------------------------------------------------------------------- cloud plan / download

def plan_cloud_download(ctx, log):
    """Bandingkan cloud dengan lokal TANPA mengubah apa pun. Return dict rencana."""
    info = ctx.get("info")
    if not info or not info.get("clientId"):
        raise SaveError("This game is not a GOG Galaxy game, so it has no GOG cloud saves.")
    data_dir = ctx["data_dir"]
    refresh, _ = galaxy_refresh_token(data_dir)
    log("Reading GOG game credentials...")
    client_id, secret = client_credentials(data_dir, info)
    token, user_id = game_token(client_id, secret, refresh)
    if not user_id:
        raise SaveError("GOG did not tell which account this is (user id missing).")
    cfg = cloud_locations(data_dir, client_id)
    if not cfg["enabled"]:
        raise SaveError("GOG has no cloud saves for this game.")
    targets = build_targets(ctx, log)
    bases = {t["name"]: t["path"] for t in targets if t["kind"] == "gog" and t["path"] is not None}
    log("Listing cloud saves...")
    files = cloud_list(user_id, client_id, token)
    items, skipped = [], 0
    for f in files:
        loc, _, rel = f["name"].partition("/")
        base = bases.get(loc)
        dest = _safe_dest(base, rel) if base is not None and rel else None
        if dest is None:
            skipped += 1
            continue
        status, local_mtime = "new", None
        if dest.is_file():
            st = dest.stat()
            local_mtime = st.st_mtime
            if f["hash"] and _md5(dest) == f["hash"]:
                status = "same"
            else:
                status = "overwrite"
        newer_local = bool(status == "overwrite" and f["mtime"] and local_mtime and local_mtime > f["mtime"] + 60)
        items.append({"file": f, "dest": dest, "status": status, "newer_local": newer_local})
    return {"client_id": client_id, "user_id": user_id, "token": token, "targets": targets,
            "items": items, "skipped": skipped, "total_cloud": len(files),
            "from_cache": cfg.get("from_cache", False)}


def summarize_plan(plan):
    it = plan["items"]
    new = [i for i in it if i["status"] == "new"]
    over = [i for i in it if i["status"] == "overwrite"]
    same = [i for i in it if i["status"] == "same"]
    newer = [i for i in over if i["newer_local"]]
    return {"new": len(new), "overwrite": len(over), "same": len(same), "newer_local": len(newer),
            "bytes": sum(i["file"]["size"] for i in new + over)}


def execute_download(ctx, plan, data_dir, game, log, skip_newer_local=False):
    """Backup lokal otomatis lalu unduh file 'new'/'overwrite'. Return (diunduh, dilewati, gagal)."""
    todo = [i for i in plan["items"] if i["status"] in ("new", "overwrite")
            and not (skip_newer_local and i["newer_local"])]
    if any(i["status"] == "overwrite" for i in todo):
        try:
            path, n = make_backup(data_dir, game, plan["targets"], note="before-cloud-download")
            log(f"Safety backup created ({n} file(s)): {path.name}")
        except SaveError as e:
            log(f"No safety backup needed: {e}")
    done = failed = 0
    for i, item in enumerate(todo, start=1):
        f, dest = item["file"], item["dest"]
        try:
            data, mtime = cloud_get(plan["user_id"], plan["client_id"], plan["token"], f["name"], f["hash"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".wlm-tmp")
            tmp.write_bytes(data)
            tmp.replace(dest)
            ts = mtime or f["mtime"]
            if ts:
                os.utime(dest, (ts, ts))
            done += 1
            log(f"[{i}/{len(todo)}] {f['name']}")
        except SaveError as e:
            failed += 1
            log(f"[{i}/{len(todo)}] FAILED {f['name']}: {e}")
            if isinstance(e, OfflineError):
                break
        except OSError as e:
            failed += 1
            log(f"[{i}/{len(todo)}] FAILED {f['name']}: {e}")
    return done, len(plan["items"]) - len(todo), failed


# --------------------------------------------------------------------------- dialog

_dialogs = {}


def _bg(work, done):
    """Jalankan work() di thread; done(result, error) dipanggil di main thread."""
    def run():
        try:
            res, err = work(), None
        except Exception as e:
            res, err = None, e
        try:
            _host.root.after(0, lambda: done(res, err))
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


_host = None

_TITLE_H = 36          # perkiraan tinggi title bar jendela
_BOTTOM_RESERVE = 48   # ruang bawah layar untuk taskbar/panel


def _ui_path():
    return Path(_host.data_dir) / "gog_ui.json"


def _ui_read():
    try:
        data = json.loads(_ui_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _ui_get(section):
    val = _ui_read().get(section)
    return val if isinstance(val, dict) else {}


def _ui_put(section, value):
    """Simpan satu bagian di gog_ui.json (file yang sama dipakai jendela GOG lain; bagian lain tidak disentuh)."""
    try:
        data = _ui_read()
        data[section] = value
        tmp = _ui_path().with_name("gog_ui.json.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, _ui_path())
    except OSError:
        pass


def _is_zoomed(win):
    """True kalau jendela sedang maximize (di X11 Tk selalu lapor 'normal', jadi '-zoomed' dicek juga)."""
    try:
        if win.state() == "zoomed":
            return True
    except tk.TclError:
        pass
    try:
        return str(win.attributes("-zoomed")).strip().lower() in ("1", "true", "yes")
    except tk.TclError:
        return False


def _set_zoomed(win, on=True):
    for attempt in (lambda: win.attributes("-zoomed", on),
                    lambda: win.state("zoomed" if on else "normal")):
        try:
            attempt()
            return True
        except tk.TclError:
            continue
    return False


def _center_on_screen(win, w, h):
    """Taruh jendela berukuran w x h di TENGAH layar (area yang terlihat, diluar taskbar bawah).
    Ukuran ikut ditetapkan, jadi posisi dihitung dari ukuran SEBENARNYA - bukan ukuran permintaan
    isi yang bisa berbeda dan membuat jendela tampak turun/menggeser ke samping."""
    try:
        if not win.winfo_exists():
            return
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - _BOTTOM_RESERVE - (h + _TITLE_H)) // 2)
        win.geometry(f"{w}x{h}+{x}+{y}")
    except tk.TclError:
        pass


def open_dialog(host):
    """Titik masuk dari launcher. host: root, fonts, colors(), data_dir, name, exe_path, choice,
    source ('gog'|'other'), human_size(), status(text, level), open_folder(path), is_running()."""
    global _host
    _host = host
    name = host.name
    old = _dialogs.get(name)
    if old is not None and old.winfo_exists():
        old.deiconify()
        old.lift()
        return
    if install_watch is None:
        messagebox.showerror("Save Manager", "install_watch.py was not found next to the launcher.",
                             parent=host.root)
        return

    colors, fonts, root = host.colors(), host.fonts, host.root
    info_path, info = find_goggame_info(host.exe_path)
    ctx = {"name": name, "exe": host.exe_path, "choice": host.choice or {}, "data_dir": host.data_dir,
           "info": info, "info_path": info_path}
    is_gog_cloud = bool(info and info.get("clientId"))
    state = {"targets": [], "busy": False}

    win = tk.Toplevel(root)
    win.withdraw()          # jangan muncul dulu selagi isinya dibangun - supaya begitu tampil,
                             # posisinya langsung di tengah, tanpa "lompat" dari pojok ke tengah.
    _dialogs[name] = win
    win.title(f"Save Manager - {name}")
    win.configure(bg=colors["primary"])
    sw_, sh_ = win.winfo_screenwidth(), win.winfo_screenheight()
    max_w, max_h = sw_ - 20, sh_ - _BOTTOM_RESERVE - _TITLE_H
    min_w, min_h = min(680, max_w), min(600, max_h)
    saved_ui = _ui_get("saves")
    win_w, win_h = 780, 700
    m = re.fullmatch(r"(\d+)x(\d+)", str(saved_ui.get("size") or ""))
    if m:
        win_w, win_h = int(m.group(1)), int(m.group(2))
    win_w, win_h = max(min_w, min(win_w, max_w)), max(min_h, min(win_h, max_h))
    win.geometry(f"{win_w}x{win_h}")
    win.minsize(min_w, min_h)
    win.transient(root)
    last_normal = {"size": f"{win_w}x{win_h}", "zoomed": bool(saved_ui.get("maximized")), "after": None}

    outer = ttk.Frame(win)
    outer.pack(fill=tk.BOTH, expand=True)

    canvas = tk.Canvas(outer, bg=colors["primary"], highlightthickness=0, bd=0)
    vscroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vscroll.set)
    vscroll.pack(side=tk.RIGHT, fill=tk.Y)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    frame = ttk.Frame(canvas, padding=12)
    frame_window = canvas.create_window((0, 0), window=frame, anchor="nw")

    def _sync_scrollregion(_evt=None):
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _sync_frame_width(evt):
        # Isi frame mengikuti lebar canvas supaya tidak ada ruang kosong kaku di kanan
        # atau konten terpotong horizontal saat jendela di-resize.
        canvas.itemconfig(frame_window, width=evt.width)

    frame.bind("<Configure>", _sync_scrollregion)
    canvas.bind("<Configure>", _sync_frame_width)

    def _on_mousewheel(evt):
        if not canvas.winfo_exists():
            return
        if evt.num == 4:
            canvas.yview_scroll(-3, "units")
        elif evt.num == 5:
            canvas.yview_scroll(3, "units")
        else:
            canvas.yview_scroll(-1 if evt.delta > 0 else 1, "units")

    def _bind_mousewheel(_evt=None):
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        canvas.bind_all("<Button-4>", _on_mousewheel)
        canvas.bind_all("<Button-5>", _on_mousewheel)

    def _unbind_mousewheel(_evt=None):
        canvas.unbind_all("<MouseWheel>")
        canvas.unbind_all("<Button-4>")
        canvas.unbind_all("<Button-5>")

    # Mouse wheel hanya "aktif" selagi kursor ada di atas jendela ini (bind_all dilepas lagi
    # saat kursor keluar / jendela ditutup), supaya tidak mengganggu scroll di jendela lain.
    canvas.bind("<Enter>", _bind_mousewheel)
    canvas.bind("<Leave>", _unbind_mousewheel)

    ttk.Label(frame, text=name, font=fonts["subtitle"]).pack(anchor="w")
    src_text = ("GOG game" + (" - cloud saves available to check" if is_gog_cloud
                               else " - no Galaxy client id found, cloud disabled")
                if host.source == "gog" or info else "Non-GOG game - local backup only")
    ttk.Label(frame, text=src_text, font=fonts["small"]).pack(anchor="w", pady=(0, 8))

    # ---- log (dibuat dulu supaya fungsi lain bisa memakainya)
    log_frame = ttk.LabelFrame(frame, text="Log", padding=6)
    log_text = tk.Text(log_frame, height=7, wrap="word", state="disabled", bg=colors.get("text_background", "#1e1e35"),
                       fg=colors["text"], relief="flat", font=fonts["small"])
    log_scroll = ttk.Scrollbar(log_frame, command=log_text.yview)
    log_text.configure(yscrollcommand=log_scroll.set)
    log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
    log_text.pack(fill=tk.BOTH, expand=True)

    def log(text):
        def do():
            if not win.winfo_exists():
                return
            log_text.config(state="normal")
            log_text.insert(tk.END, text + "\n")
            log_text.see(tk.END)
            log_text.config(state="disabled")
        try:
            root.after(0, do)
        except Exception:
            pass

    loc_frame = ttk.LabelFrame(frame, text="Save folders", padding=6)
    loc_frame.pack(fill=tk.X, pady=(0, 8))
    loc_tree = ttk.Treeview(loc_frame, columns=("Name", "Folder", "Files"), show="headings", height=4,
                            selectmode="browse")
    for col, w, stretch in (("Name", 110, False), ("Folder", 470, True), ("Files", 60, False)):
        loc_tree.heading(col, text=col, anchor="w")
        loc_tree.column(col, width=w, anchor="w", stretch=stretch)
    loc_tree.pack(fill=tk.X)
    loc_btns = ttk.Frame(loc_frame)
    loc_btns.pack(fill=tk.X, pady=(6, 0))

    def refresh_locations(refresh=True):
        def work():
            return build_targets(ctx, log, refresh=refresh)

        def done(targets, err):
            if err:
                log(f"Could not read save folders: {err}")
                targets = []
            state["targets"] = targets
            for r in loc_tree.get_children():
                loc_tree.delete(r)
            for t in targets:
                p = t["path"]
                n = len(list_files(p)) if p is not None else 0
                shown = str(p) if p is not None else f"({t['note'] or 'not found'}) {t['macro']}"
                loc_tree.insert("", "end", iid=t["label"], values=(t["name"] + (" *" if t["kind"] == "custom" else ""), shown, n))
            if not targets:
                log("No save folders known yet. Use 'Add Folder' to add one manually.")
        _bg(work, done)

    def selected_target():
        sel = loc_tree.selection()
        return next((t for t in state["targets"] if sel and t["label"] == sel[0]), None)

    def add_folder():
        start = str(Path(state["targets"][0]["path"]).parent) if state["targets"] and state["targets"][0]["path"] else str(Path.home())
        d = filedialog.askdirectory(parent=win, title="Select a save game folder", initialdir=start)
        if d:
            folders = load_custom_folders(host.data_dir, name)
            if d not in folders:
                folders.append(d)
                save_custom_folders(host.data_dir, name, folders)
            refresh_locations(refresh=False)

    def remove_folder():
        t = selected_target()
        if not t or t["kind"] != "custom":
            messagebox.showinfo("Save Manager", "Select a folder you added manually (marked with *).", parent=win)
            return
        folders = [f for f in load_custom_folders(host.data_dir, name) if f != str(t["path"])]
        save_custom_folders(host.data_dir, name, folders)
        refresh_locations(refresh=False)

    def open_folder():
        t = selected_target()
        if t and t["path"] is not None and Path(t["path"]).is_dir():
            host.open_folder(str(t["path"]))
        else:
            messagebox.showinfo("Save Manager", "Select an existing save folder first.", parent=win)

    for text, cmd in (("Add Folder...", add_folder), ("Remove Folder", remove_folder), ("Open Folder", open_folder)):
        ttk.Button(loc_btns, text=text, style="Custom.TButton", command=cmd).pack(side=tk.LEFT, padx=(0, 6))

    bk_frame = ttk.LabelFrame(frame, text="Local backups", padding=6)
    bk_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
    bk_tree = ttk.Treeview(bk_frame, columns=("When", "Note", "Size"), show="headings", height=5, selectmode="browse")
    for col, w, stretch in (("When", 170, False), ("Note", 300, True), ("Size", 90, False)):
        bk_tree.heading(col, text=col, anchor="w")
        bk_tree.column(col, width=w, anchor="w", stretch=stretch)
    bk_tree.pack(fill=tk.BOTH, expand=True)
    bk_btns = ttk.Frame(bk_frame)
    bk_btns.pack(fill=tk.X, pady=(6, 0))
    backups = {"items": []}

    def refresh_backups():
        backups["items"] = list_backups(host.data_dir, name)
        for r in bk_tree.get_children():
            bk_tree.delete(r)
        for i, b in enumerate(backups["items"]):
            bk_tree.insert("", "end", iid=str(i), values=(b["when"], b["note"], _human(b["size"])))

    def selected_backup():
        sel = bk_tree.selection()
        return backups["items"][int(sel[0])] if sel else None

    def game_running():
        try:
            return bool(host.is_running())
        except Exception:
            return False

    def set_busy(flag):
        state["busy"] = flag
        for b in action_buttons:
            b.state(["disabled"] if flag else ["!disabled"])

    def do_backup():
        if state["busy"]:
            return
        set_busy(True)
        log("Creating backup...")
        _bg(lambda: make_backup(host.data_dir, name, state["targets"], "manual"),
            lambda res, err: (set_busy(False), refresh_backups(),
                              log(f"Backup created: {res[0].name} ({res[1]} file(s))" if not err else f"Backup failed: {err}")))

    def do_restore():
        b = selected_backup()
        if not b:
            messagebox.showinfo("Save Manager", "Select a backup first.", parent=win)
            return
        if game_running():
            messagebox.showwarning("Save Manager", "The game is running. Close it before restoring saves.", parent=win)
            return
        if not messagebox.askokcancel(
                "Restore backup - WARNING",
                f"Restoring '{b['when']}' will OVERWRITE the current save files that have the same names.\n\n"
                "Files that are not in the backup are left alone. A safety backup of the current saves is "
                "created first.\n\nContinue?", icon="warning", default=messagebox.CANCEL, parent=win):
            return
        set_busy(True)

        def work():
            try:
                make_backup(host.data_dir, name, state["targets"], "before-restore")
            except SaveError:
                pass
            return restore_backup(b["path"], state["targets"], log)

        def done(n, err):
            set_busy(False)
            refresh_backups()
            refresh_locations(refresh=False)
            log(f"Restore failed: {err}" if err else f"Restored {n} file(s) from {b['when']}.")
        _bg(work, done)

    def do_delete_backup():
        b = selected_backup()
        if b and messagebox.askyesno("Save Manager", f"Delete backup '{b['when']}'?", parent=win):
            b["path"].unlink(missing_ok=True)
            refresh_backups()

    def open_backups_folder():
        d = backups_dir(host.data_dir, name)
        d.mkdir(parents=True, exist_ok=True)
        host.open_folder(str(d))

    action_buttons = []
    for text, cmd in (("Backup Now", do_backup), ("Restore...", do_restore),
                      ("Delete", do_delete_backup), ("Open Backups Folder", open_backups_folder)):
        b = ttk.Button(bk_btns, text=text, style="Custom.TButton", command=cmd)
        b.pack(side=tk.LEFT, padx=(0, 6))
        if text in ("Backup Now", "Restore..."):
            action_buttons.append(b)

    cloud_frame = ttk.LabelFrame(frame, text="GOG cloud saves", padding=6)
    cloud_frame.pack(fill=tk.X, pady=(0, 8))
    cloud_lbl = ttk.Label(cloud_frame, font=fonts["small"], justify=tk.LEFT, wraplength=700,
                          text=("Check Cloud compares your cloud saves with the local files (changes nothing). "
                                "Download & Overwrite replaces local saves with the cloud copy - "
                                "uploading to the cloud is not supported.") if is_gog_cloud
                          else "Only GOG games installed by a GOG installer (with a goggame-*.info file) can use GOG cloud saves.")
    cloud_lbl.pack(anchor="w", pady=(0, 6))
    cloud_btns = ttk.Frame(cloud_frame)
    cloud_btns.pack(fill=tk.X)

    def describe(plan):
        s = summarize_plan(plan)
        log(f"Cloud: {plan['total_cloud']} file(s). Identical to local: {s['same']} | "
            f"would overwrite: {s['overwrite']} (newer locally: {s['newer_local']}) | new: {s['new']}"
            + (f" | outside known folders: {plan['skipped']}" if plan['skipped'] else ""))
        return s

    def do_check():
        if state["busy"]:
            return
        set_busy(True)
        _bg(lambda: plan_cloud_download(ctx, log),
            lambda plan, err: (set_busy(False), log(f"Cloud check failed: {err}") if err else describe(plan)))

    def do_download():
        if state["busy"]:
            return
        if game_running():
            messagebox.showwarning("Save Manager", "The game is running. Close it before downloading cloud saves.", parent=win)
            return
        set_busy(True)

        def planned(plan, err):
            if err:
                set_busy(False)
                log(f"Cloud check failed: {err}")
                return
            s = describe(plan)
            if s["new"] + s["overwrite"] == 0:
                set_busy(False)
                log("Nothing to download - local saves already match the cloud.")
                return
            warn = (f"Downloading will OVERWRITE local save files with the cloud copy.\n\n"
                    f"  - {s['overwrite']} file(s) will be overwritten"
                    + (f"  (!! {s['newer_local']} of them are NEWER on this PC than in the cloud !!)" if s["newer_local"] else "")
                    + f"\n  - {s['new']} new file(s) will be created\n  - {_human(s['bytes'])} will be downloaded\n"
                    "  - Files that exist only on this PC are NOT deleted\n\n"
                    "Your current local saves are backed up first, so you can undo this from 'Local backups'.\n"
                    "Make sure the game is closed.\n\nContinue?")
            if not messagebox.askokcancel("Download cloud saves - WARNING", warn, icon="warning",
                                          default=messagebox.CANCEL, parent=win):
                set_busy(False)
                log("Download cancelled.")
                return
            if s["newer_local"] and messagebox.askyesno(
                    "Keep newer local saves?",
                    f"{s['newer_local']} local file(s) are newer than the cloud copy.\n\n"
                    "Yes = keep those local files (download only the rest)\nNo = overwrite them too",
                    parent=win):
                skip = True
            else:
                skip = False
            log("Downloading cloud saves...")
            _bg(lambda: execute_download(ctx, plan, host.data_dir, name, log, skip_newer_local=skip),
                lambda res, e: (set_busy(False), refresh_backups(), refresh_locations(refresh=False),
                                log(f"Download failed: {e}" if e else
                                    f"Done: {res[0]} downloaded, {res[1]} skipped, {res[2]} failed.")))
        _bg(lambda: plan_cloud_download(ctx, log), planned)

    check_btn = ttk.Button(cloud_btns, text="Check Cloud", style="Custom.TButton", command=do_check)
    dl_btn = ttk.Button(cloud_btns, text="Download & Overwrite", style="Custom.TButton", command=do_download)
    check_btn.pack(side=tk.LEFT, padx=(0, 6))
    dl_btn.pack(side=tk.LEFT)
    if is_gog_cloud:
        action_buttons.extend([check_btn, dl_btn])
    else:
        check_btn.state(["disabled"])
        dl_btn.state(["disabled"])

    log_frame.pack(fill=tk.BOTH, expand=False)
    ttk.Button(frame, text="Close", style="Custom.TButton", command=win.destroy).pack(anchor="e", pady=(8, 0))

    def save_win_ui():
        data = {"maximized": last_normal["zoomed"]}
        if last_normal["size"]:
            data["size"] = last_normal["size"]      # ukuran NORMAL terakhir, bukan ukuran layar penuh
        _ui_put("saves", data)

    def on_win_configure(event):
        if event.widget is not win:
            return
        try:
            last_normal["zoomed"] = _is_zoomed(win)
            if win.state() == "normal" and not last_normal["zoomed"]:
                last_normal["size"] = f"{win.winfo_width()}x{win.winfo_height()}"
        except tk.TclError:
            return
        if last_normal["after"]:
            win.after_cancel(last_normal["after"])
        last_normal["after"] = win.after(700, save_win_ui)

    def on_close():
        _unbind_mousewheel()
        _dialogs.pop(name, None)
        save_win_ui()
        win.destroy()
    win.protocol("WM_DELETE_WINDOW", on_close)

    def on_win_destroy(e):
        if e.widget is win:
            _unbind_mousewheel()
            _dialogs.pop(name, None)
            save_win_ui()          # juga kalau ditutup lewat tombol Close
    win.bind("<Destroy>", on_win_destroy)
    win.bind("<Configure>", on_win_configure, add="+")

    refresh_backups()
    refresh_locations()

    # Tampil langsung di TENGAH LAYAR (bukan di tengah launcher). Window manager sering menggeser
    # jendela begitu ditampilkan, jadi posisinya ditegaskan lagi sebentar kemudian - kecuali kalau
    # jendela sedang/akan di-maximize.
    _center_on_screen(win, win_w, win_h)
    win.deiconify()
    win.lift()
    win.focus_set()
    if saved_ui.get("maximized"):
        win.after(50, lambda: _set_zoomed(win, True))
    else:
        for delay in (30, 150, 400):
            win.after(delay, lambda: None if _is_zoomed(win) else _center_on_screen(
                win, *_current_size(win, win_w, win_h)))


def _current_size(win, fallback_w, fallback_h):
    """Ukuran jendela saat ini (kalau pengguna sudah mengubahnya), dipakai saat menegaskan posisi."""
    try:
        w, h = win.winfo_width(), win.winfo_height()
        if w > 100 and h > 100:
            return w, h
    except tk.TclError:
        pass
    return fallback_w, fallback_h
