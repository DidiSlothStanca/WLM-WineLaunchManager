"""install_watch.py - deteksi game yang baru terpasang di sebuah prefix Wine/Proton.

Dipakai launcher.py (INSTALL APPS / install manual) dan gog_store.py (install dari GOG Store)
untuk membuat shortcut otomatis setelah sebuah installer selesai. Hanya butuh library standar.

Dua cara deteksi:
1. GOG: installer GOG menulis 'goggame-<id>.info' di folder game; isinya 'playTasks' dengan exe
   utama. Ini akurat, jadi hasilnya bisa dipakai tanpa bertanya ke pengguna.
2. Umum (installer non-GOG): exe yang BARU ditulis installer (dilihat dari waktu ctime file,
   karena Inno Setup dkk. mempertahankan mtime asli) di folder game, minus uninstaller/redist/
   crash handler. Hasilnya berupa daftar kandidat - pemanggil sebaiknya minta konfirmasi.

Semua fungsi memakai path Linux ke isi 'drive_c' prefix, dan tidak menyentuh Tk.
"""
import json
import os
import re
import time
from pathlib import Path

# Folder di drive_c yang ISINYA adalah folder-folder game (satu tingkat dibawahnya = satu game).
_CONTAINERS = ("gog games", "games", "gog.com", "program files", "program files (x86)")
# Folder level atas drive_c yang bukan lokasi game.
_SYSTEM_TOP = {"windows", "users", "programdata", "temp", "windows.old"}
# Folder bawaan Windows/Wine/Proton didalam Program Files yang bukan game.
_IGNORE_DIRS = {"common files", "internet explorer", "windows nt", "windows media player",
                "windows mail", "windows photo viewer", "windows portable devices",
                "windows defender", "windowspowershell", "microsoft.net", "microsoft",
                "reference assemblies", "msbuild", "modifiablewindowsapps", "wine", "steam"}
# Folder dan nama file yang bukan exe game (redistributable, installer, crash handler, dll).
_JUNK_DIRS = {"redist", "_redist", "_commonredist", "commonredist", "directx", "dotnet",
              "vcredist", "redistributables", "prerequisites", "__installer", "support",
              "crashreporter", "_commonredist"}
_JUNK_FILE = re.compile(
    r"(^unins\d*|unwise|uninst|setup|install|vc_?redist|vcredist|dxsetup|dxwebsetup|dotnet|"
    r"ndp\d|oalinst|physx|crashpad|crashreport|crashhandler|redist|updater|notification_helper)",
    re.IGNORECASE)
SEARCH_DIRS = ("GOG Games", "Games", "GOG.com", "Program Files (x86)", "Program Files")


def drive_c_for(choice):
    """Folder drive_c milik runner/prefix hasil ask_runner_choice (None kalau tidak diketahui)."""
    prefix = choice.get("prefix_path")
    if choice.get("runner") in ("protonge", "protoncachyos"):
        if not prefix:
            return None
        base = Path(prefix)
        if not (base / "pfx").exists() and (base / "drive_c").is_dir():
            return base / "drive_c"      # layout lama tanpa subfolder pfx
        return base / "pfx" / "drive_c"
    base = Path(prefix) if prefix else Path(os.environ.get("WINEPREFIX") or Path.home() / ".wine")
    return base / "drive_c"


def resolve_ci(base, rel):
    """Ikuti path Windows relatif ('Bin\\Game.exe') dari base tanpa peduli huruf besar/kecil,
    karena filesystem prefix biasanya case-sensitive. None kalau tidak ada."""
    cur = Path(base)
    for part in re.split(r"[\\/]+", (rel or "").strip()):
        if part in ("", "."):
            continue
        if part == "..":
            cur = cur.parent
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


def _fresh(path, since):
    """True kalau file dibuat/diubah oleh installer ini. ctime dipakai selain mtime karena
    installer sering mengembalikan mtime file ke tanggal aslinya."""
    try:
        st = Path(path).stat()
    except OSError:
        return False
    return max(st.st_mtime, st.st_ctime) >= since - 2


def _primary_exe(info_dir, data):
    """(exe, workdir) dari playTasks sebuah goggame-*.info; utamakan task 'isPrimary'."""
    tasks = [t for t in (data.get("playTasks") or []) if isinstance(t, dict) and t.get("path")]
    tasks.sort(key=lambda t: (not t.get("isPrimary"), str(t.get("category", "")).lower() != "game"))
    for t in tasks:
        exe = resolve_ci(info_dir, str(t["path"]))
        if exe is not None and exe.is_file():
            workdir = resolve_ci(info_dir, str(t["workingDir"])) if t.get("workingDir") else None
            return exe, (workdir if workdir is not None and workdir.is_dir() else None)
    return None, None


def find_goggame(drive_c, game_id, since, installer_running):
    """Cari game GOG yang baru terpasang (lewat goggame-*.info). game_id None = game apa saja
    yang baru ditulis installer ini. Selama installer masih berjalan, sebuah folder dianggap
    SELESAI terpasang kalau uninstaller-nya (unins*.exe, ditulis di akhir install) sudah ada
    dan baru; setelah installer berhenti, info file saja sudah cukup.
    Return {'exe','workdir','name','icon'} atau None."""
    if not drive_c or not Path(drive_c).is_dir():
        return None
    best = None      # (skor, hasil)
    for top in SEARCH_DIRS:
        top_dir = resolve_ci(drive_c, top)
        if top_dir is None or not top_dir.is_dir():
            continue
        depth0 = len(top_dir.parts)
        for dirpath, dirnames, filenames in os.walk(top_dir):
            if len(Path(dirpath).parts) - depth0 >= 3:
                dirnames[:] = []
            infos = [f for f in filenames if f.lower().startswith("goggame-") and f.lower().endswith(".info")]
            if not infos:
                continue
            uninst = [Path(dirpath) / f for f in filenames
                      if f.lower().startswith("unins") and f.lower().endswith(".exe")]
            if installer_running and not any(_fresh(u, since) for u in uninst):
                continue
            for f in infos:
                info_path = Path(dirpath) / f
                try:
                    data = json.loads(info_path.read_text(encoding="utf-8-sig"))
                except Exception:
                    continue
                gid, root_id = str(data.get("gameId", "")), str(data.get("rootGameId", ""))
                if game_id is not None and str(game_id) == gid:
                    score = 4
                elif game_id is not None and str(game_id) == root_id:
                    score = 3                    # DLC dari game ini
                elif _fresh(info_path, since):
                    score = 2 if (not root_id or gid == root_id) else 1
                else:
                    continue
                exe, workdir = _primary_exe(dirpath, data)
                if exe is None or (best is not None and score <= best[0]):
                    continue
                icon = resolve_ci(dirpath, info_path.stem + ".ico")
                best = (score, {"exe": exe, "workdir": workdir, "name": data.get("name") or "",
                                "icon": icon if icon is not None and icon.is_file() else None})
    return best[1] if best else None


def game_dirs(drive_c):
    """Semua folder yang mungkin berisi sebuah game: anak langsung dari GOG Games/Games/Program
    Files (minus folder bawaan Windows), plus folder level atas drive_c buatan installer."""
    out = set()
    root = Path(drive_c) if drive_c else None
    if root is None or not root.is_dir():
        return out
    try:
        tops = [e for e in root.iterdir() if e.is_dir()]
    except OSError:
        return out
    for top in tops:
        lname = top.name.lower()
        if lname in _CONTAINERS:
            try:
                kids = [k for k in top.iterdir() if k.is_dir()]
            except OSError:
                continue
            out.update(k for k in kids if k.name.lower() not in _IGNORE_DIRS)
        elif lname not in _SYSTEM_TOP:
            out.add(top)
    return out


def find_candidates(drive_c, since, limit=15):
    """Exe yang baru ditulis installer (bukan uninstaller/redist), terbesar dulu. Setiap item:
    {'exe': Path, 'game_dir': Path, 'size': int}. Untuk installer non-GOG."""
    found = {}
    for gd in game_dirs(drive_c):
        depth0 = len(gd.parts)
        for dirpath, dirnames, filenames in os.walk(gd):
            dirnames[:] = [d for d in dirnames if d.lower() not in _JUNK_DIRS]
            if len(Path(dirpath).parts) - depth0 >= 4:
                dirnames[:] = []
            for f in filenames:
                if not f.lower().endswith(".exe") or _JUNK_FILE.search(f):
                    continue
                p = Path(dirpath) / f
                if _fresh(p, since):
                    try:
                        found[str(p)] = {"exe": p, "game_dir": gd, "size": p.stat().st_size}
                    except OSError:
                        pass
    return sorted(found.values(), key=lambda c: -c["size"])[:limit]


def wait_for_install(proc, choice, game_id=None, started=None, interval=2.0):
    """BLOCKING (panggil dari background thread): pantau installer 'proc' (subprocess.Popen)
    sampai game GOG terdeteksi lengkap terpasang, atau installer berhenti. Dengan begitu
    shortcut bisa dibuat begitu game siap - tanpa menunggu jendela installer ditutup (mis. kalau
    'Launch game' dicentang di halaman akhir).

    Return dict: found (hasil find_goggame atau None), candidates (find_candidates, hanya bila
    found None dan installer sukses), rc (exit code installer, None kalau masih berjalan),
    drive_c."""
    started = started or time.time()
    drive_c = drive_c_for(choice)
    found = None
    while True:
        running = proc.poll() is None
        try:
            found = find_goggame(drive_c, game_id, started, running)
        except Exception as e:
            print(f"[WLM] install scan error: {e}")
        if found or not running:
            break
        time.sleep(interval)
    rc = proc.poll()
    candidates = []
    if found is None and rc == 0:
        try:
            candidates = find_candidates(drive_c, started)
        except Exception as e:
            print(f"[WLM] candidate scan error: {e}")
    return {"found": found, "candidates": candidates, "rc": rc, "drive_c": drive_c}
