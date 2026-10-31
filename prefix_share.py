"""prefix_share.py - memakai SATU prefix bersama untuk beberapa aplikasi/runner, dan
menyinkronkan save/konfigurasi game saat runner diganti tanpa saling menimpa.

Dipakai launcher.py. Hanya butuh library standar, tidak menyentuh Tk.

1. Deteksi prefix (inspect_prefix): sebuah folder dikenali sebagai
     - prefix Wine vanilla      : <folder>/drive_c
     - prefix Proton (GE/Cachy) : <folder>/pfx/drive_c  (+ version, pfx.lock, dst)
   Folder boleh juga 'pfx' itu sendiri atau sub-folder didalam drive_c; semuanya dinormalisasi
   ke prefix induknya. Dengan begitu launcher TIDAK PERNAH membuat prefix baru didalam prefix.

2. Memakai prefix yang sama dari runner lain:
     - Wine   -> Proton : dibuat folder 'wrapper' kecil berisi symlink pfx -> prefix Wine,
                          jadi data game/save/registry tetap SATU tempat (tidak disalin).
     - Proton -> Wine   : WINEPREFIX diarahkan ke <compat>/pfx.

3. Sinkronisasi save (sync_user_homes): Wine memakai user <nama login>, Proton memakai
   'steamuser', jadi pindah runner = folder save terpisah. Fungsi ini menggabungkan kedua
   folder user dua arah: file yang hanya ada disatu sisi disalin, file yang beda -> yang
   TERBARU menang, dan versi yang kalah dicadangkan dulu (tidak ada yang hilang).
"""
import filecmp
import getpass
import os
import re
import shutil
import time
from pathlib import Path

_PROTON_MARKERS = ("version", "pfx.lock", "config_info", "tracked_files")
_SYSTEM_USERS = {"public", "all users", "default", "default user"}

# Subfolder profil user yang berisi save/konfigurasi game (nama alternatif = layout Windows lama).
SYNC_SUBTREES = (
    ("Documents", "My Documents"),
    ("Saved Games",),
    ("AppData/Local", "Local Settings/Application Data"),
    ("AppData/LocalLow",),
    ("AppData/Roaming", "Application Data"),
)
# Folder yang dilewati: milik Wine/Proton/driver, cache, atau sementara.
_SKIP_DIRS = {"temp", "tmp", "wine", "microsoft", "packages", "crashdumps", "d3dscache",
              "fontconfig", "gstreamer-1.0", "history", "inetcache", "shadercache",
              "mesa_shader_cache", "mesa_shader_cache_db", "nvidia", "dxvk", "vkd3d"}
_SKIP_SUFFIX = (".tmp", ".lock", ".log", ".wlm-tmp")
_MAX_FILE = 512 * 1024 * 1024
_SAME_MTIME = 2.0


# --------------------------------------------------------------------------- deteksi prefix

def _ci_child(base, name):
    """Anak 'name' dari base tanpa peduli huruf besar/kecil (None kalau tidak ada)."""
    base = Path(base)
    direct = base / name
    if direct.exists():
        return direct
    try:
        for e in base.iterdir():
            if e.name.lower() == name.lower():
                return e
    except OSError:
        pass
    return None


def inspect_prefix(path):
    """Kenali 'path' sebagai prefix. Return dict atau None:
      kind     : 'wine' | 'proton'
      root     : nilai yang disimpan sebagai prefix_path untuk runner aslinya
                 (wine: folder berisi drive_c; proton: folder compat yang berisi pfx/)
      wine_dir : folder yang langsung berisi drive_c"""
    try:
        p = Path(path)
        if not p.is_dir():
            return None
    except OSError:
        return None
    pfx = _ci_child(p, "pfx")
    if pfx is not None and pfx.is_dir() and _ci_child(pfx, "drive_c") is not None:
        return {"kind": "proton", "root": p, "wine_dir": pfx}
    if _ci_child(p, "drive_c") is not None:
        if p.name.lower() == "pfx" and any((p.parent / m).exists() for m in _PROTON_MARKERS):
            return {"kind": "proton", "root": p.parent, "wine_dir": p}
        if any((p / m).exists() for m in _PROTON_MARKERS):       # layout Proton lama tanpa pfx/
            return {"kind": "proton", "root": p, "wine_dir": p}
        return {"kind": "wine", "root": p, "wine_dir": p}
    return None


def prefix_containing(path):
    """Prefix yang berisi 'path' (path itu sendiri, atau salah satu folder didalam drive_c)."""
    p = Path(path)
    info = inspect_prefix(p)
    if info:
        return info
    parts = list(p.parts)
    for i, part in enumerate(parts):
        if part.lower() == "drive_c" and i > 0:
            return inspect_prefix(Path(*parts[:i]))
    return None


def find_prefix_for_exe(exe_path):
    """Prefix tempat sebuah exe terpasang (exe berada didalam drive_c-nya), None kalau diluar."""
    p = Path(exe_path)
    for parent in p.parents:
        if parent.name.lower() == "drive_c":
            return inspect_prefix(parent.parent)
    return None


def physical_key(info):
    """Identitas fisik prefix (path asli, symlink diikuti) - dua runner yang berbagi prefix
    yang sama menghasilkan key yang sama."""
    try:
        return str(Path(info["wine_dir"]).resolve())
    except OSError:
        return str(info["wine_dir"])


def _safe_code(name):
    name = (name or "").strip().strip(".")
    if name.lower() == "wine":
        name = "wine-default"
    code = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
    return code or "prefix"


def code_for_prefix(info, registry):
    """Kode prefix untuk prefix yang sudah ada: pakai kode di registry kalau prefix fisik yang
    sama sudah tercatat (oleh runner mana pun), kalau belum buat dari nama foldernya."""
    key = physical_key(info)
    used = set()
    for e in (registry or {}).values():
        used.add(e.get("prefix_code"))
        other = inspect_prefix(e.get("prefix_path") or "")
        if other and physical_key(other) == key and e.get("prefix_code"):
            return e["prefix_code"]
    root = Path(info["root"])
    base = _safe_code(root.parent.name if root.name.lower() == "pfx" else root.name)
    code, n = base, 2
    while code in used:
        code = f"{base}-{n}"
        n += 1
    return code


def make_proton_wrapper(wine_dir, wrapper_dir):
    """Folder compat Proton yang berisi pfx -> symlink ke prefix Wine yang sudah ada.
    Return wrapper_dir. Data tetap satu tempat."""
    wine_dir, wrapper_dir = Path(wine_dir), Path(wrapper_dir)
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    link = wrapper_dir / "pfx"
    if link.is_symlink() or link.exists():
        if link.resolve() != wine_dir.resolve():
            raise RuntimeError(f"{link} already points to another prefix")
        return wrapper_dir
    link.symlink_to(wine_dir, target_is_directory=True)
    return wrapper_dir


def drive_c_of(choice):
    """Folder drive_c dari hasil pilihan runner (None kalau tidak ketahuan)."""
    prefix = choice.get("prefix_path")
    if choice.get("runner") in ("protonge", "protoncachyos"):
        if not prefix:
            return None
        base = Path(prefix)
        if not (base / "pfx").exists() and (base / "drive_c").is_dir():
            return base / "drive_c"
        return base / "pfx" / "drive_c"
    base = Path(prefix) if prefix else Path(os.environ.get("WINEPREFIX") or Path.home() / ".wine")
    return base / "drive_c"


# --------------------------------------------------------------------------- folder user

def home_for(drive_c, runner):
    """Folder profil user yang dipakai 'runner' didalam drive_c (boleh belum ada).
    Proton selalu 'steamuser'; Wine memakai nama login."""
    users = Path(drive_c) / "users"
    if runner in ("protonge", "protoncachyos"):
        return users / "steamuser"
    me = users / getpass.getuser()
    if me.is_dir():
        return me
    try:
        others = [e for e in users.iterdir()
                  if e.is_dir() and e.name.lower() not in _SYSTEM_USERS and e.name.lower() != "steamuser"]
    except OSError:
        others = []
    return others[0] if len(others) == 1 else me


def plan_sync(old_choice, new_choice):
    """Perlu sinkronisasi saat runner/prefix berganti? Return {'a': home_lama, 'b': home_baru}
    atau None (tidak perlu: prefix & folder user sama, atau salah satunya belum ada)."""
    if not old_choice or not new_choice:
        return None
    dc_a, dc_b = drive_c_of(old_choice), drive_c_of(new_choice)
    if dc_a is None or dc_b is None or not dc_a.is_dir():
        return None
    home_a = home_for(dc_a, old_choice.get("runner"))
    home_b = home_for(dc_b, new_choice.get("runner"))
    try:
        if home_a.resolve() == home_b.resolve():
            return None
    except OSError:
        pass
    if not home_a.is_dir():           # tidak ada yang bisa disinkronkan dari sisi lama
        return None
    return {"a": home_a, "b": home_b}


# --------------------------------------------------------------------------- sinkronisasi

def _find_subtree(home, names):
    """(path, ada) subfolder profil; coba nama-nama alternatif, tanpa peduli huruf."""
    home = Path(home)
    for rel in names:
        cur = home
        for part in rel.split("/"):
            cur = _ci_child(cur, part) if cur is not None and cur.is_dir() else None
            if cur is None:
                break
        if cur is not None and cur.is_dir():
            return cur
    return None


def _scan(root, only_children=None):
    """{rel.lower(): (rel, Path, size, mtime)} - lewati symlink, cache, file sementara."""
    out = {}
    root = Path(root)
    for dirpath, dirnames, files in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        if str(rel_dir) == "." and only_children:
            dirnames[:] = [d for d in dirnames if d.lower() in only_children]
            files = []
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in _SKIP_DIRS and not d.lower().endswith("cache")
                       and not (Path(dirpath) / d).is_symlink()]
        for f in files:
            if f.lower().endswith(_SKIP_SUFFIX):
                continue
            p = Path(dirpath) / f
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                st = p.stat()
            except OSError:
                continue
            if st.st_size > _MAX_FILE:
                continue
            rel = (rel_dir / f).as_posix()
            out[rel.lower()] = (rel, p, st.st_size, st.st_mtime)
    return out


def _dest_file(root, rel):
    """Path tujuan untuk 'rel' di bawah root; ikuti nama folder yang sudah ada tanpa peduli
    huruf (filesystem prefix case-sensitive), buat sisanya."""
    cur = Path(root)
    parts = rel.split("/")
    for part in parts[:-1]:
        nxt = _ci_child(cur, part)
        cur = nxt if nxt is not None else cur / part
    cur.mkdir(parents=True, exist_ok=True)
    existing = _ci_child(cur, parts[-1])
    return existing if existing is not None else cur / parts[-1]


def _copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".wlm-tmp")
    try:
        shutil.copy2(src, tmp)
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)


def sync_user_homes(home_a, home_b, backup_root, log=None):
    """Gabungkan save/konfigurasi dua folder user dua arah, TANPA menimpa yang lebih baru:
      - hanya ada di satu sisi -> disalin ke sisi lain
      - ada di kedua sisi tapi isi beda -> yang mtime-nya lebih baru menang; salinan yang
        kalah dicadangkan dulu ke backup_root/<waktu>/ (tidak ada data yang hilang)
      - mtime sama tapi isi beda -> dibiarkan keduanya (dilaporkan sebagai konflik)
    Return dict statistik."""
    log = log or (lambda *_: None)
    stats = {"a_to_b": 0, "b_to_a": 0, "backed_up": 0, "conflicts": 0, "errors": 0}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    home_a, home_b = Path(home_a), Path(home_b)
    for names in SYNC_SUBTREES:
        ra, rb = _find_subtree(home_a, names), _find_subtree(home_b, names)
        if ra is None and rb is None:
            continue
        if ra is None:
            ra = home_a.joinpath(*names[0].split("/"))
        if rb is None:
            rb = home_b.joinpath(*names[0].split("/"))
        try:
            if ra.exists() and rb.exists() and ra.resolve() == rb.resolve():
                continue                      # sudah satu folder (mis. Documents -> ~/Documents)
        except OSError:
            pass
        # 'Documents' yang berupa symlink ke folder asli user hanya disinkronkan 'My Games'.
        def limit(r):
            return {"my games"} if (r.is_symlink() and names[0] == "Documents") else None
        fa = _scan(ra, limit(ra)) if ra.is_dir() else {}
        fb = _scan(rb, limit(rb)) if rb.is_dir() else {}
        tag = names[0].replace("/", "_")
        for key in sorted(set(fa) | set(fb)):
            a, b = fa.get(key), fb.get(key)
            try:
                if a and not b:
                    _copy(a[1], _dest_file(rb, a[0]))
                    stats["a_to_b"] += 1
                elif b and not a:
                    _copy(b[1], _dest_file(ra, b[0]))
                    stats["b_to_a"] += 1
                else:
                    if a[2] == b[2] and filecmp.cmp(a[1], b[1], shallow=False):
                        continue                  # isi sama persis
                    if abs(a[3] - b[3]) <= _SAME_MTIME:
                        stats["conflicts"] += 1
                        log(f"Conflict (same time, different content), left alone: {a[0]}")
                        continue
                    newer, older, side = (a, b, "b") if a[3] > b[3] else (b, a, "a")
                    keep = Path(backup_root) / stamp / side / tag / older[0]
                    _copy(older[1], keep)
                    stats["backed_up"] += 1
                    _copy(newer[1], older[1])
                    stats["a_to_b" if side == "b" else "b_to_a"] += 1
            except OSError as e:
                stats["errors"] += 1
                log(f"Skipped {key}: {e}")
    return stats
