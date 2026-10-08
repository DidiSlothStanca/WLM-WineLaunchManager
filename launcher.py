import os
import platform
import re
import shlex
import shutil
import subprocess
import threading
import queue
import pty
import select
import errno
import sys
import copy
import tkinter as tk
from tkinter import ttk, filedialog, simpledialog, messagebox
from tkinter import font as tkfont
from pathlib import Path
from PIL import Image, ImageTk, ImageDraw, ImageChops
import json
import tarfile
import tempfile
import urllib.request
import urllib.error
import webbrowser
from datetime import datetime
import time

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    yaml = None
    _YAML_AVAILABLE = False

WLM_VERSION = "0.4.7-Beta"
WLM_DEVELOPER = "Opensource OS Gathering Republic (OOGR)"
WLM_MAINTAINER = "Didi Sloth Stanca"

def _print_version_and_exit():
    print(f"WLM Version: {WLM_VERSION}")
    print(f"Developer: {WLM_DEVELOPER}")
    print(f"Maintener: {WLM_MAINTAINER}")
    sys.exit(0)

if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-v"):
    _print_version_and_exit()

directory = Path.home() / "wlm"
icon_dir = directory / "icons"
bashlaunch_dir = directory / "bashlaunch"
theme_config_file = directory / "theme_config.json"
window_config_file = directory / "window_config.json"
dialog_sizes_file = directory / "dialog_sizes.json"   # ukuran terakhir dialog yang bisa di-resize

protonge_dir = directory / "protonge"
protonge_prefix_root = directory / "protonprefixes"

protoncachyos_dir = directory / "protoncachyos"
protoncachyos_prefix_root = directory / "protoncachyosprefixes"

wine_prefix_root = directory / "wineprefixes"

runner_config_file = directory / "runner_config.json"
prefix_location_config_file = directory / "prefix_location_config.json"
prefix_registry_file = directory / "prefix_registry.json"
logs_dir = directory / "logs"
env_config_file = directory / "env_config.yaml"

DEFAULT_PREFIX_ROOTS = {
    "wine": wine_prefix_root,
    "protonge": protonge_prefix_root,
    "protoncachyos": protoncachyos_prefix_root,
}

directory.mkdir(parents=True, exist_ok=True)
bashlaunch_dir.mkdir(parents=True, exist_ok=True)
icon_dir.mkdir(parents=True, exist_ok=True)
protonge_dir.mkdir(parents=True, exist_ok=True)
protonge_prefix_root.mkdir(parents=True, exist_ok=True)
protoncachyos_dir.mkdir(parents=True, exist_ok=True)
protoncachyos_prefix_root.mkdir(parents=True, exist_ok=True)
wine_prefix_root.mkdir(parents=True, exist_ok=True)
logs_dir.mkdir(parents=True, exist_ok=True)

DEFAULT_ENV_CONFIG = {
    "proton_ge": {
        "releases_api": "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases",
        "releases_page": "https://github.com/GloriousEggroll/proton-ge-custom/releases",
    },
    "proton_cachyos": {
        "releases_api": "https://api.github.com/repos/CachyOS/proton-cachyos/releases",
        "releases_page": "https://github.com/CachyOS/proton-cachyos/releases",
    },
}

def save_env_config_if_missing(cfg):
    """Tulis env_config.yaml berisi nilai bawaan kalau file-nya belum ada sama
    sekali, supaya pengguna langsung punya contoh file yang bisa diedit."""
    if env_config_file.exists() or not _YAML_AVAILABLE:
        return
    try:
        with open(env_config_file, 'w') as f:
            f.write("# Wine Launch Manager - environment configuration\n")
            f.write("# URLs used by the \"Download Online\" feature to check for and fetch new\n")
            f.write("# Proton GE / Proton-CachyOS builds. Edit and save, then restart WLM (or\n")
            f.write("# reopen the Download Online dialog) to apply changes.\n\n")
            yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
    except Exception as e:
        print(f"[WLM] Error writing {env_config_file}: {e}")

def load_env_config():
    """Baca env_config.yaml, buat dengan nilai bawaan kalau belum ada/rusak/tidak
    lengkap. Selalu return dict lengkap (field yang kosong/hilang di file diisi
    dari DEFAULT_ENV_CONFIG), supaya sisa kode tidak perlu cek None dimana-mana."""
    cfg = copy.deepcopy(DEFAULT_ENV_CONFIG)

    if not _YAML_AVAILABLE:
        print("[WLM] PyYAML is not installed - using built-in default URLs. "
              "Install it with 'pip install pyyaml' to customize env_config.yaml.")
        return cfg

    if not env_config_file.exists():
        save_env_config_if_missing(cfg)
        return cfg

    try:
        with open(env_config_file, 'r') as f:
            loaded = yaml.safe_load(f) or {}
        for section, keys in DEFAULT_ENV_CONFIG.items():
            section_data = loaded.get(section) or {}
            for key, default_val in keys.items():
                cfg[section][key] = section_data.get(key) or default_val
    except Exception as e:
        print(f"[WLM] Error reading {env_config_file}, falling back to default URLs: {e}")

    return cfg

ENV_CONFIG = load_env_config()

def get_clean_subprocess_env():
    """Returns a copy of the environment safe to hand to external programs
    (xdg-open, file managers, wine, winecfg, proton, etc).

    When this launcher itself is running from inside an AppImage, the
    AppImage runtime sets LD_LIBRARY_PATH to point at its own bundled
    libraries so *this* process can find them. Child processes spawned
    with subprocess.Popen() inherit that same LD_LIBRARY_PATH by default,
    which makes them try to load the AppImage's bundled libs instead of
    the system's own - this is why things like "open folder" can silently
    fail to do anything when running from an AppImage build."""
    env = os.environ.copy()
    if "APPIMAGE" in env and "LD_LIBRARY_PATH" in env:
        del env["LD_LIBRARY_PATH"]
    return env

running_games = {}
MAX_LOG_BUFFER_LINES = 5000

class TaskCancelledError(Exception):
    """Dilempar oleh worker backup/restore prefix ketika pengguna menekan tombol Cancel
    pada jendela Show Logs, supaya proses tar.add()/extract() yang sedang berjalan berhenti
    secepatnya alih-alih dibiarkan jalan sampai selesai."""
    pass

active_prefix_task = None
"""State task backup/restore prefix yang sedang berjalan di sesi launcher ini (None kalau
tidak ada). Dipakai untuk (1) membatasi hanya SATU backup/restore game/apps yang boleh
berjalan dalam satu waktu, dan (2) supaya tombol "Show Logs" di Prefix Configuration
Manager bisa menampilkan lagi jendela log task yang sedang berjalan itu. Isinya dict:
{"kind": "backup"/"restore", "label": str, "cancel_event": threading.Event,
 "win": Toplevel atau None, "finished": bool}."""

THEMES = {
    "default": {
        "name": "Default (Dark Blue)",
        "primary": "#11131f",
        "secondary": "#181b2b",
        "accent": "#232844",
        "highlight": "#5b7cfa",
        "text": "#eef0fa",
        "text_secondary": "#9aa1c0",
        "button_text": "#ffffff",
        "success": "#34d399",
        "warning": "#fbbf24",
        "danger": "#f87171",
        "card_bg": "#1f2336",
        "border": "#2c3150",
        "button_bg": "#272c47",
        "button_fg": "#e6e9f8",
        "tree_bg": "#171a2a",
        "tree_fg": "#e6e9f8",
        "tree_highlight": "#5b7cfa",
        "tree_highlight_text": "#ffffff",
        "text_background": "#141726"
    },
    "dark": {
        "name": "Dark",
        "primary": "#0e0e12",
        "secondary": "#16161c",
        "accent": "#26262f",
        "highlight": "#a78bfa",
        "text": "#f1f1f5",
        "text_secondary": "#9b9bab",
        "button_text": "#14101f",
        "success": "#2dd4bf",
        "warning": "#fbbf24",
        "danger": "#f87171",
        "card_bg": "#1d1d25",
        "border": "#2c2c38",
        "button_bg": "#26262f",
        "button_fg": "#ececf3",
        "tree_bg": "#16161c",
        "tree_fg": "#ececf3",
        "tree_highlight": "#a78bfa",
        "tree_highlight_text": "#14101f",
        "text_background": "#121217"
    },
    "light": {
        "name": "Light",
        "primary": "#eef0f6",
        "secondary": "#ffffff",
        "accent": "#e3e6f0",
        "highlight": "#4f6df5",
        "text": "#1b1f33",
        "text_secondary": "#5f6680",
        "button_text": "#ffffff",
        "success": "#0f9d74",
        "warning": "#d97706",
        "danger": "#d93a3a",
        "card_bg": "#ffffff",
        "border": "#d5d9e6",
        "button_bg": "#e4e7f1",
        "button_fg": "#1b1f33",
        "tree_bg": "#ffffff",
        "tree_fg": "#1b1f33",
        "tree_highlight": "#4f6df5",
        "tree_highlight_text": "#ffffff",
        "text_background": "#ffffff"
    },
    "pinky": {
        "name": "Pinky",
        "primary": "#1f1220",
        "secondary": "#2a1a2c",
        "accent": "#4a2c4d",
        "highlight": "#f472b6",
        "text": "#fdf2f8",
        "text_secondary": "#d8b4d0",
        "button_text": "#2a0f22",
        "success": "#c084fc",
        "warning": "#fbcfe8",
        "danger": "#fb7185",
        "card_bg": "#35223a",
        "border": "#523559",
        "button_bg": "#41284a",
        "button_fg": "#fdf2f8",
        "tree_bg": "#2a1a2c",
        "tree_fg": "#fdf2f8",
        "tree_highlight": "#f472b6",
        "tree_highlight_text": "#2a0f22",
        "text_background": "#241524"
    },
    "zombie": {
        "name": "Zombie Green",
        "primary": "#0f1d12",
        "secondary": "#15281a",
        "accent": "#1f3a25",
        "highlight": "#7ddc8a",
        "text": "#ecf8ee",
        "text_secondary": "#9cc3a2",
        "button_text": "#0a2410",
        "success": "#a3e635",
        "warning": "#facc15",
        "danger": "#ff6b4a",
        "card_bg": "#1b3322",
        "border": "#2d5036",
        "button_bg": "#244230",
        "button_fg": "#ecf8ee",
        "tree_bg": "#15281a",
        "tree_fg": "#ecf8ee",
        "tree_highlight": "#7ddc8a",
        "tree_highlight_text": "#0a2410",
        "text_background": "#112016"
    },
    "nord": {
        "name": "Nord",
        "primary": "#272c36",
        "secondary": "#2e3440",
        "accent": "#3b4252",
        "highlight": "#88c0d0",
        "text": "#eceff4",
        "text_secondary": "#a3acc0",
        "button_text": "#1f2530",
        "success": "#a3be8c",
        "warning": "#ebcb8b",
        "danger": "#bf616a",
        "card_bg": "#353c4a",
        "border": "#434c5e",
        "button_bg": "#3b4252",
        "button_fg": "#eceff4",
        "tree_bg": "#2e3440",
        "tree_fg": "#eceff4",
        "tree_highlight": "#88c0d0",
        "tree_highlight_text": "#1f2530",
        "text_background": "#2a303b"
    },
    "midnight": {
        "name": "Midnight (OLED)",
        "primary": "#000000",
        "secondary": "#0b0b0f",
        "accent": "#1a1a22",
        "highlight": "#22d3ee",
        "text": "#f5f5f7",
        "text_secondary": "#8d8d99",
        "button_text": "#001418",
        "success": "#4ade80",
        "warning": "#fbbf24",
        "danger": "#f87171",
        "card_bg": "#111116",
        "border": "#23232c",
        "button_bg": "#1a1a22",
        "button_fg": "#f0f0f4",
        "tree_bg": "#08080c",
        "tree_fg": "#f0f0f4",
        "tree_highlight": "#22d3ee",
        "tree_highlight_text": "#001418",
        "text_background": "#0b0b0f"
    }
}

FONT_FAMILY = "Segoe UI"
FONT_CANDIDATES = ("Inter", "Segoe UI", "Noto Sans", "Cantarell", "Ubuntu", "Roboto",
                   "Open Sans", "Source Sans 3", "DejaVu Sans")

def _build_fonts(family):
    # Ukuran key lama (title/subtitle/normal/small) sengaja TIDAK diubah supaya semua dialog
    # tetap muat; key baru (display/heading/body) dipakai khusus jendela utama.
    return {
        "display": (family, 17, "bold"),
        "heading": (family, 12, "bold"),
        "title": (family, 14, "bold"),
        "subtitle": (family, 11, "bold"),
        "body": (family, 10),
        "normal": (family, 9),
        "small": (family, 8),
    }

FONTS = _build_fonts(FONT_FAMILY)

def apply_best_font_family():
    """Pilih font terbaik yang benar-benar terpasang (Segoe UI tidak ada di kebanyakan distro
    Linux sehingga teks jatuh ke font bawaan Tk yang tampak kuno). Dipanggil setelah root dibuat."""
    global FONT_FAMILY
    try:
        installed = {name.lower(): name for name in tkfont.families()}
    except tk.TclError:
        return
    chosen = None
    for candidate in FONT_CANDIDATES:
        if candidate.lower() in installed:
            chosen = installed[candidate.lower()]
            break
    if chosen is None:
        try:
            chosen = tkfont.nametofont("TkDefaultFont").actual("family")
        except tk.TclError:
            return
    FONT_FAMILY = chosen
    FONTS.update(_build_fonts(chosen))
    for named in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
                  "TkCaptionFont", "TkTooltipFont"):
        try:
            tkfont.nametofont(named).configure(family=chosen)
        except tk.TclError:
            pass

ICON_SIZE = 250
ICON_WIDTH = 190      # ukuran tampil di panel detail (file ikon tetap disimpan 250px)
ICON_HEIGHT = 190

library_view_config_file = directory / "library_view.json"
library_grid = None      # LibraryGrid (game_covers.py); tetap None kalau game_covers.py tidak ada
cover_store = None       # CoverStore: cover otomatis/manual di ~/wlm/covers

def load_config():
    """Load all configurations from file with validation"""
    config = {"theme": "default", "window_size": "1000x720", "window_position": None,
              "window_maximized": False, "sash": None}
    
    if theme_config_file.exists():
        try:
            with open(theme_config_file, 'r') as f:
                theme_config = json.load(f)
                config["theme"] = theme_config.get('theme', 'default')
        except:
            pass
    
    if window_config_file.exists():
        try:
            with open(window_config_file, 'r') as f:
                window_config = json.load(f)
                
                loaded_size = window_config.get('size', '1000x720')
                loaded_position = window_config.get('position', None)
                config["window_maximized"] = window_config.get('maximized') is True
                loaded_sash = window_config.get('sash')
                if isinstance(loaded_sash, int) and not isinstance(loaded_sash, bool) and loaded_sash > 0:
                    config["sash"] = loaded_sash

                if 'x' in loaded_size and loaded_size.count('x') == 1:
                    config["window_size"] = loaded_size
                
                if loaded_position and loaded_position.startswith('+') and loaded_position.count('+') == 2:
                    config["window_position"] = loaded_position
                else:
                    config["window_position"] = None 
                
        except:
            pass
    
    return config

_last_normal_geometry = {"size": None, "position": None}   # ukuran/posisi 'normal' terakhir (bukan saat maximize)
_manual_zoom = {"on": False}                                # maximize manual (kalau WM tidak mendukung)

def _is_maximized():
    """True kalau jendela utama sedang maximize. Di Linux/X11 Tk selalu melaporkan state 'normal'
    walau jendela di-maximize window manager, jadi atribut '-zoomed' juga dicek."""
    if _manual_zoom["on"]:
        return True
    try:
        if root.state() == "zoomed":
            return True
    except tk.TclError:
        pass
    try:
        return str(root.attributes("-zoomed")).strip().lower() in ("1", "true", "yes")
    except tk.TclError:
        return False

def _set_maximized(on):
    """Maximize/kembalikan jendela utama lewat window manager. True kalau berhasil."""
    for attempt in (lambda: root.attributes("-zoomed", on),
                    lambda: root.state("zoomed" if on else "normal")):
        try:
            attempt()
            return True
        except tk.TclError:
            continue
    return False

_sash_state = {"restored": False}   # sash baru boleh disimpan setelah posisi lama selesai dipulihkan

def _read_saved_sash():
    try:
        with open(window_config_file, 'r') as f:
            value = json.load(f).get("sash")
        return value if isinstance(value, int) else None
    except Exception:
        return None

def save_window_config():
    """Simpan ukuran + posisi jendela dan apakah jendela sedang maximize. Saat maximize, yang
    disimpan tetap ukuran/posisi NORMAL terakhir (bukan ukuran layar penuh), jadi kalau jendela
    dikembalikan (un-maximize) ukurannya tetap yang Anda atur - dan flag 'maximized' membuat
    jendela dibuka maximize lagi pada sesi berikutnya."""
    if root.winfo_exists():
        maximized = _is_maximized()
        if not maximized:
            _last_normal_geometry["size"] = f"{root.winfo_width()}x{root.winfo_height()}"
            _last_normal_geometry["position"] = f"+{root.winfo_x()}+{root.winfo_y()}"
        size, position = _last_normal_geometry["size"], _last_normal_geometry["position"]
        if size is None:       # belum pernah normal di sesi ini: pertahankan yang sudah tersimpan
            try:
                with open(window_config_file, 'r') as f:
                    saved = json.load(f)
                size, position = saved.get("size"), saved.get("position")
            except Exception:
                pass

        config_data = {
            "size": size or "1000x720",
            "position": position,
            "maximized": maximized
        }
        # Lebar panel library (grid/list) vs panel detail judul+cover = posisi pembatas (sash).
        sash = None
        if _sash_state["restored"]:
            try:
                sash = int(main_container.sashpos(0))
            except Exception:
                sash = None
        if sash is None:
            sash = _read_saved_sash()
        if sash is not None:
            config_data["sash"] = sash

        try:
            with open(window_config_file, 'w') as f:
                json.dump(config_data, f, indent=4)
        except Exception as e:
            print(f"Error saving window config: {e}")

_DIALOG_SIZE_RE = re.compile(r"(\d{3,5})x(\d{3,5})")

def load_dialog_size(key):
    """Ukuran (lebar, tinggi) yang tersimpan untuk dialog `key`, atau None kalau belum ada / rusak."""
    try:
        with open(dialog_sizes_file, 'r') as f:
            value = json.load(f).get(key)
    except Exception:
        return None
    m = _DIALOG_SIZE_RE.fullmatch(value) if isinstance(value, str) else None
    return (int(m.group(1)), int(m.group(2))) if m else None

def save_dialog_size(key, width, height):
    """Simpan ukuran dialog `key` (file ditulis atomik supaya tidak rusak kalau launcher mati mendadak)."""
    data = {}
    try:
        with open(dialog_sizes_file, 'r') as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data = loaded
    except Exception:
        pass
    data[key] = f"{int(width)}x{int(height)}"
    tmp = dialog_sizes_file.with_name(dialog_sizes_file.name + ".tmp")
    try:
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=4)
        os.replace(tmp, dialog_sizes_file)
    except Exception as e:
        print(f"Error saving dialog size: {e}")

def remember_dialog_size(dialog, key, default_size, min_size=(0, 0), parent=None):
    """Pasang ukuran awal dialog = ukuran yang terakhir diatur pengguna (atau `default_size` kalau
    belum pernah), dijaga tidak lebih kecil dari `min_size` dan tidak lebih besar dari layar, lalu
    SIMPAN OTOMATIS setiap kali dialog di-resize (ditunda 0,5 dtk, dan tetap tersimpan kalau dialog
    langsung ditutup). Dialog ditaruh di tengah `parent`. Kembalian: (lebar, tinggi) yang dipakai.
    Panggil SEBELUM dialog.deiconify()."""
    sw, sh = dialog.winfo_screenwidth(), dialog.winfo_screenheight()
    max_w, max_h = max(320, sw - 20), max(240, sh - 90)     # sisakan title bar + taskbar
    min_w, min_h = min(min_size[0], max_w), min(min_size[1], max_h)
    want_w, want_h = load_dialog_size(key) or default_size
    w, h = max(min_w, min(want_w, max_w)), max(min_h, min(want_h, max_h))

    dialog.minsize(min_w, min_h)
    dialog.geometry(f"{w}x{h}")

    def center():
        try:
            if not dialog.winfo_exists():
                return
            cw, ch = (dialog.winfo_width(), dialog.winfo_height())
            cw, ch = (cw, ch) if cw > 1 and ch > 1 else (w, h)
            if parent is not None and parent.winfo_exists():
                cx = parent.winfo_rootx() + parent.winfo_width() // 2
                cy = parent.winfo_rooty() + parent.winfo_height() // 2
            else:
                cx, cy = sw // 2, sh // 2
            x = max(0, min(cx - cw // 2, sw - cw - 10))
            y = max(10, min(cy - ch // 2, sh - ch - 70))
            dialog.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass

    center()
    # Window manager sering menggeser dialog saat ditampilkan -> tengahkan lagi sebentar kemudian.
    for delay in (30, 150, 400):
        dialog.after(delay, center)

    state = {"ready": False, "size": (w, h), "job": None}

    def persist():
        state["job"] = None
        if state["ready"]:
            save_dialog_size(key, *state["size"])

    def arm():
        # Perubahan ukuran akibat window manager saat dialog baru tampil BUKAN resize dari pengguna.
        try:
            if dialog.winfo_exists():
                cw, ch = dialog.winfo_width(), dialog.winfo_height()
                if cw > 1 and ch > 1:
                    state["size"] = (cw, ch)
                state["ready"] = True
        except tk.TclError:
            pass

    def on_configure(event):
        if event.widget is not dialog or not state["ready"]:
            return
        size = (event.width, event.height)
        if size == state["size"] or size[0] < 100 or size[1] < 100:   # cuma dipindah / ukuran aneh
            return
        state["size"] = size
        if state["job"] is not None:
            try:
                dialog.after_cancel(state["job"])
            except (ValueError, tk.TclError):
                pass
        state["job"] = dialog.after(500, persist)

    def on_destroy(event):
        if event.widget is dialog and state["job"] is not None:
            persist()      # ditutup sebelum jeda 0,5 dtk habis -> tetap simpan ukuran terakhir

    dialog.after(800, arm)
    dialog.bind("<Configure>", on_configure, add="+")
    dialog.bind("<Destroy>", on_destroy, add="+")
    return w, h

def find_proton_installations(base_dir):
    """Scan build Proton (GE atau CachyOS) yang sudah diekstrak didalam sebuah folder.
    Tidak membaca dari direktori Steam."""
    found = []
    if base_dir.is_dir():
        for entry in sorted(base_dir.iterdir(), reverse=True):
            if entry.is_dir():
                proton_bin = entry / "proton"
                if proton_bin.exists():
                    found.append((entry.name, proton_bin))
    return found

def find_protonge_installations():
    """Scan Proton GE yang sudah diekstrak didalam direktori utama WLM (~/wlm/protonge).
    Tidak lagi membaca dari direktori Steam."""
    return find_proton_installations(protonge_dir)

def find_protoncachyos_installations():
    """Scan Proton-CachyOS yang sudah diekstrak didalam direktori utama WLM (~/wlm/protoncachyos)."""
    return find_proton_installations(protoncachyos_dir)

def find_steam_install_path():
    """Cari direktori client Steam (opsional, hanya untuk STEAM_COMPAT_CLIENT_INSTALL_PATH).
    Jika Steam tidak terpasang, gunakan folder lokal didalam direktori utama WLM sebagai fallback,
    supaya Proton GE tetap bisa berjalan tanpa bergantung pada Steam."""
    candidates = [
        Path.home() / ".steam" / "steam",
        Path.home() / ".local" / "share" / "Steam",
        Path.home() / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    fallback = directory / "steamclient"
    fallback.mkdir(exist_ok=True)
    return fallback

def extract_archive_to_dir(archive_path, target_dir):
    """Ekstrak SATU arsip Proton (.tar.gz/.tar.xz/.tgz/.zip) ke target_dir, membungkus hasil
    ekstrak dalam satu folder kalau arsipnya sendiri tidak punya satu folder induk. Logika
    inti ini dipakai bersama oleh extract_proton_archive() (pilih arsip lewat file dialog)
    dan download_and_install_protonge_worker() (arsip hasil download online) - aman dipanggil
    dari background thread karena tidak menyentuh widget Tk apapun."""
    archive_path_obj = Path(archive_path)
    target_dir.mkdir(exist_ok=True)
    before_entries = {p.name for p in target_dir.iterdir() if p.is_dir()}

    if archive_path_obj.suffix.lower() == ".zip":
        import zipfile
        with zipfile.ZipFile(archive_path, 'r') as zf:
            top_level_names = {Path(n).parts[0] for n in zf.namelist() if n.strip()}
            zf.extractall(target_dir)
    else:
        with tarfile.open(archive_path, 'r:*') as tf:
            top_level_names = {Path(n).parts[0] for n in tf.getnames() if n.strip()}
            try:
                tf.extractall(target_dir, filter='data')
            except TypeError:
                tf.extractall(target_dir)

    new_top_names = top_level_names - before_entries

    if len(new_top_names) != 1:
        wrapper_name = archive_path_obj.name.replace(".tar.gz", "").replace(".tar.xz", "") \
                                             .replace(".tgz", "").replace(".zip", "")
        wrapper_dir = target_dir / wrapper_name
        wrapper_dir.mkdir(exist_ok=True)
        for name in new_top_names:
            src = target_dir / name
            if src.exists() and src != wrapper_dir:
                src.rename(wrapper_dir / name)

def extract_proton_archive(target_dir, build_label):
    """Pilih arsip Proton (.tar.gz/.tar.xz/.tgz/.zip) lalu ekstrak ke target_dir.
    Dipakai bersama oleh Proton GE dan Proton-CachyOS."""
    archive_path = filedialog.askopenfilename(
        title=f"Select {build_label} Archive",
        filetypes=[(f"{build_label} Archive", "*.tar.gz *.tar.xz *.tgz *.zip"), ("All Files", "*.*")]
    )
    if not archive_path:
        return

    archive_path_obj = Path(archive_path)
    safe_status_config(text=f"Extracting {archive_path_obj.name} to WLM directory...", fg=COLORS["text_secondary"])

    loading_dialog = tk.Toplevel(root)
    loading_dialog.withdraw()
    loading_dialog.title(f"Extracting {build_label}")
    loading_dialog.configure(bg=COLORS["primary"])
    loading_dialog.resizable(False, False)
    loading_dialog.transient(root)
    loading_dialog.protocol("WM_DELETE_WINDOW", lambda: None)

    loading_frame = ttk.Frame(loading_dialog, padding=20)
    loading_frame.pack(fill=tk.BOTH, expand=True)

    ttk.Label(loading_frame,
              text=f"Extracting {archive_path_obj.name}...",
              font=FONTS["subtitle"], justify=tk.CENTER).pack(pady=(0, 4))
    ttk.Label(loading_frame,
              text="This can take a while for large archives.\nPlease wait, the launcher is not frozen.",
              font=FONTS["small"], justify=tk.CENTER).pack(pady=(0, 14))

    progress_bar = ttk.Progressbar(loading_frame, mode="indeterminate", length=280)
    progress_bar.pack()
    progress_bar.start(12)

    loading_dialog.update_idletasks()
    x = root.winfo_rootx() + (root.winfo_width() - loading_dialog.winfo_width()) // 2
    y = root.winfo_rooty() + (root.winfo_height() - loading_dialog.winfo_height()) // 2
    loading_dialog.geometry(f"+{x}+{y}")
    loading_dialog.deiconify()
    loading_dialog.grab_set()

    extract_result = {"error": None}

    def do_extract():
        try:
            extract_archive_to_dir(archive_path, target_dir)
        except Exception as e:
            extract_result["error"] = e

    def finish_extract():
        progress_bar.stop()
        loading_dialog.grab_release()
        loading_dialog.destroy()

        if extract_result["error"] is None:
            safe_status_config(text=f"{build_label} successfully extracted to {target_dir}", fg=COLORS["success"])
            messagebox.showinfo("Done", f"{build_label} successfully extracted to the WLM directory:\n{target_dir}")
        else:
            e = extract_result["error"]
            safe_status_config(text=f"Error extracting {build_label}: {str(e)}", fg=COLORS["danger"])
            messagebox.showerror("Error", f"Failed to extract archive:\n{str(e)}")

    def worker():
        do_extract()
        root.after(0, finish_extract)

    threading.Thread(target=worker, daemon=True).start()

def extract_protonge_archive():
    """Pilih arsip Proton GE (.tar.gz/.tar.xz/.tgz/.zip) lalu ekstrak ke ~/wlm/protonge/."""
    extract_proton_archive(protonge_dir, "Proton GE")

def extract_protoncachyos_archive():
    """Pilih arsip Proton-CachyOS (.tar.gz/.tar.xz/.tgz/.zip) lalu ekstrak ke ~/wlm/protoncachyos/."""
    extract_proton_archive(protoncachyos_dir, "Proton-CachyOS")

def open_proton_folder(target_dir, build_label):
    """Buka folder tempat sebuah build Proton diekstrak."""
    try:
        target_dir.mkdir(exist_ok=True)
        subprocess.Popen(["xdg-open", str(target_dir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          env=get_clean_subprocess_env())
        safe_status_config(text=f"Opening {build_label} Folder...", fg=COLORS["text_secondary"])
    except Exception as e:
        safe_status_config(text=f"Error opening {build_label} folder: {str(e)}", fg=COLORS["danger"])

def open_protonge_folder():
    """Buka folder tempat Proton GE diekstrak (~/wlm/protonge)."""
    open_proton_folder(protonge_dir, "Proton GE")

def open_protoncachyos_folder():
    """Buka folder tempat Proton-CachyOS diekstrak (~/wlm/protoncachyos)."""
    open_proton_folder(protoncachyos_dir, "Proton-CachyOS")

PROTONGE_RELEASES_API = ENV_CONFIG["proton_ge"]["releases_api"]
PROTONGE_RELEASES_PAGE = ENV_CONFIG["proton_ge"]["releases_page"]

def fetch_protonge_releases(limit=100):
    """Ambil daftar rilis ProtonGE terbaru langsung dari GitHub (GloriousEggroll/proton-ge-custom).
    Dipanggil dari background thread karena ini network call yang blocking. Return list of dict:
    {"tag", "name", "published_at", "asset_name", "download_url", "size"} - hanya rilis yang
    punya satu asset .tar.gz utama (bukan file checksum .sha512sum). Melempar exception kalau
    gagal (tidak ada internet, GitHub rate-limit, dll) supaya bisa ditangani oleh pemanggilnya."""
    url = f"{PROTONGE_RELEASES_API}?per_page={limit}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "wine-launcher-manager",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    releases = []
    for rel in data:
        asset = None
        for a in rel.get("assets", []):
            name = a.get("name", "")
            if name.lower().endswith(".tar.gz"):
                asset = a
                break
        if not asset:
            continue
        releases.append({
            "tag": rel.get("tag_name", ""),
            "name": rel.get("name") or rel.get("tag_name", ""),
            "published_at": (rel.get("published_at") or "")[:10],
            "asset_name": asset.get("name", ""),
            "download_url": asset.get("browser_download_url", ""),
            "size": asset.get("size", 0),
        })
    return releases

def download_url_pausable(url, dest_path, total_size, append_line, set_progress=None, pause_event=None):
    """Download url ke dest_path (blocking, panggil dari background thread) dengan dukungan
    PAUSE. pause_event: threading.Event; selama event ter-set, download dijeda. Saat dijeda
    koneksi ditutup (bukan ditahan terbuka, supaya tidak di-timeout server) dan saat dilanjutkan
    koneksi dibuka lagi dengan header Range dari byte terakhir. Kalau server mengabaikan Range,
    download diulang dari awal. Return jumlah byte yang terunduh."""
    downloaded = 0
    last_report = 0.0
    dest_path = Path(dest_path)
    dest_path.write_bytes(b"")
    while True:
        headers = {"User-Agent": "wine-launcher-manager"}
        if downloaded:
            headers["Range"] = f"bytes={downloaded}-"
        req = urllib.request.Request(url, headers=headers)
        paused = False
        with urllib.request.urlopen(req, timeout=30) as resp:
            if downloaded and getattr(resp, "status", 200) != 206:
                append_line("  Server ignored the resume request - restarting from the beginning.")
                downloaded = 0
                dest_path.write_bytes(b"")
            with open(dest_path, "ab") as out_file:
                while True:
                    if pause_event is not None and pause_event.is_set():
                        paused = True
                        break
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    out_file.write(chunk)
                    downloaded += len(chunk)
                    now = time.time()
                    if now - last_report > 0.2:
                        if total_size:
                            pct = downloaded * 100 / total_size
                            append_line(f"  {human_size(downloaded)} / {human_size(total_size)} ({pct:.0f}%)")
                            if set_progress:
                                set_progress(pct)
                        else:
                            append_line(f"  {human_size(downloaded)} downloaded...")
                        last_report = now
        if not paused:
            return downloaded
        append_line("")
        append_line(f"Paused at {human_size(downloaded)}. Click Resume to continue.")
        while pause_event.is_set():
            time.sleep(0.2)
        append_line(f"Resuming from {human_size(downloaded)}...")
        last_report = 0.0

def download_and_install_protonge_worker(release, append_line, set_progress=None,
                                          pause_event=None, win=None):
    """Berjalan di background thread: download asset .tar.gz milik satu rilis ProtonGE dari
    GitHub (dengan progress live via append_line + set_progress kalau ukurannya diketahui),
    lalu ekstrak ke ~/wlm/protonge/ lewat extract_archive_to_dir (fungsi yang sama dipakai
    oleh 'Extract Proton GE Archive...' manual)."""
    tmp_path = None
    try:
        url = release["download_url"]
        total_size = release.get("size") or 0
        append_line(f"Downloading {release['asset_name']} ({human_size(total_size)})...")

        tmp_fd, tmp_name = tempfile.mkstemp(prefix="wlm_protonge_", suffix=".tar.gz")
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)

        downloaded = download_url_pausable(url, tmp_path, total_size, append_line, set_progress, pause_event)

        if set_progress and total_size:
            set_progress(100)
        if win is not None:      # sesudah download, ekstrak tidak bisa di-pause
            root.after(0, lambda: getattr(win, "disable_pause", lambda: None)())

        append_line("")
        append_line(f"Download complete ({human_size(downloaded)}). Extracting to {protonge_dir}...")
        extract_archive_to_dir(tmp_path, protonge_dir)

        append_line("")
        append_line(f"ProtonGE {release['tag']} installed successfully.")
        root.after(0, lambda: safe_status_config(
            text=f"ProtonGE {release['tag']} installed successfully.", fg=COLORS["success"]))
    except Exception as e:
        err = str(e)
        append_line("")
        append_line(f"ERROR: {err}")
        root.after(0, lambda: messagebox.showerror("Download Failed", f"Failed to download/install ProtonGE:\n{err}",
                                                     parent=root))
        root.after(0, lambda: safe_status_config(text=f"ProtonGE download failed: {err}", fg=COLORS["danger"]))
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except Exception:
                pass

def open_protonge_download_dialog():
    """Jendela untuk melihat daftar rilis ProtonGE terbaru langsung dari GitHub dan
    mendownload + memasangnya secara otomatis, tanpa perlu download manual dari browser lalu
    'Extract Proton GE Archive...' sendiri. Gaya jendelanya konsisten dengan Prefix
    Configuration Manager (Treeview list + tombol aksi di bawahnya)."""
    dialog = tk.Toplevel(root)
    dialog.withdraw()
    dialog.title("Download ProtonGE")
    dialog.configure(bg=COLORS["primary"])
    dialog.resizable(True, True)
    dialog.minsize(760, 480)

    frame = ttk.Frame(dialog, padding=15)
    frame.pack(fill=tk.BOTH, expand=True)

    top_bar = ttk.Frame(frame)
    top_bar.pack(fill=tk.X, pady=(0, 8))
    ttk.Label(top_bar, text="Latest ProtonGE releases (GloriousEggroll/proton-ge-custom):",
              font=FONTS["normal"]).pack(side=tk.LEFT, anchor="w")

    refresh_btn = ttk.Button(top_bar, text="Refresh", style="Custom.TButton", width=12)
    refresh_btn.pack(side=tk.RIGHT)

    status_line = ttk.Label(frame, text="Loading releases from GitHub...", font=FONTS["small"])
    status_line.pack(anchor="w", pady=(0, 6))

    list_frame = ttk.Frame(frame)
    list_frame.pack(fill=tk.BOTH, expand=True)
    list_frame.rowconfigure(0, weight=1)
    list_frame.columnconfigure(0, weight=1)

    vscroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL)
    vscroll.grid(row=0, column=1, sticky="ns")

    release_tree = ttk.Treeview(list_frame,
                                 columns=("Version", "File", "Published", "Size", "Status"),
                                 show="headings",
                                 yscrollcommand=vscroll.set,
                                 selectmode="browse",
                                 height=10)
    release_tree.grid(row=0, column=0, sticky="nsew")
    vscroll.config(command=release_tree.yview)

    release_tree.heading("Version", text="Version", anchor="w")
    release_tree.heading("File", text="File", anchor="w")
    release_tree.heading("Published", text="Published", anchor="w")
    release_tree.heading("Size", text="Size", anchor="w")
    release_tree.heading("Status", text="Status", anchor="w")
    release_tree.column("Version", width=170, anchor="w", stretch=False)
    release_tree.column("File", width=260, anchor="w", stretch=True)
    release_tree.column("Published", width=110, anchor="w", stretch=False)
    release_tree.column("Size", width=90, anchor="w", stretch=False)
    release_tree.column("Status", width=100, anchor="w", stretch=False)

    releases_data = {}

    def populate(releases):
        release_tree.delete(*release_tree.get_children())
        releases_data.clear()
        installed_names = {name for name, _ in find_protonge_installations()}
        for rel in releases:
            iid = rel["tag"]
            releases_data[iid] = rel
            asset_folder_name = rel["asset_name"][:-len(".tar.gz")] if rel["asset_name"].lower().endswith(".tar.gz") else rel["asset_name"]
            is_installed = rel["tag"] in installed_names or asset_folder_name in installed_names
            release_tree.insert("", tk.END, iid=iid,
                                 values=(rel["name"], rel["asset_name"], rel["published_at"], human_size(rel["size"]),
                                         "Installed" if is_installed else ""))

    def load_releases():
        status_line.config(text="Loading releases from GitHub...")
        refresh_btn.config(state="disabled")

        def worker():
            try:
                releases = fetch_protonge_releases()
                error = None
            except Exception as e:
                releases = []
                error = str(e)

            def finish():
                refresh_btn.config(state="normal")
                if error:
                    status_line.config(text=f"Failed to load releases: {error}")
                    messagebox.showerror(
                        "Error",
                        f"Failed to fetch ProtonGE release list from GitHub:\n{error}\n\n"
                        "Check your internet connection and try Refresh again.",
                        parent=dialog
                    )
                else:
                    status_line.config(text=f"{len(releases)} release(s) loaded.")
                    populate(releases)

            root.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    refresh_btn.config(command=load_releases)
    load_releases()

    btn_row = ttk.Frame(frame)
    btn_row.pack(pady=(10, 0))

    def get_selected_release():
        sel = release_tree.selection()
        if not sel:
            messagebox.showinfo("Info", "Select a release from the list first.", parent=dialog)
            return None
        return releases_data.get(sel[0])

    def do_download():
        rel = get_selected_release()
        if not rel:
            return
        if not messagebox.askyesno(
            "Download & Install ProtonGE",
            f"Download and install ProtonGE {rel['tag']}?\n\n"
            f"File: {rel['asset_name']}\n"
            f"Size: {human_size(rel['size'])}\n\n"
            f"It will be extracted into:\n{protonge_dir}",
            parent=dialog
        ):
            return

        pause_event = threading.Event()
        append_line, set_progress, win = open_task_log_window(f"Download ProtonGE - {rel['tag']}",
                                                               modal_parent=dialog, pause_event=pause_event)
        append_line(f"Release: {rel['name']} ({rel['tag']})")
        append_line("")
        threading.Thread(target=download_and_install_protonge_worker,
                          args=(rel, append_line, set_progress, pause_event, win), daemon=True).start()

    def do_open_page():
        webbrowser.open(PROTONGE_RELEASES_PAGE)

    ttk.Button(btn_row, text="Download & Install", style="Custom.TButton", width=18,
               command=do_download).grid(row=0, column=0, padx=3)
    ttk.Button(btn_row, text="Open Releases Page", style="Custom.TButton", width=18,
               command=do_open_page).grid(row=0, column=1, padx=3)

    dialog.update_idletasks()
    x = root.winfo_rootx() + (root.winfo_width() - 900) // 2
    y = root.winfo_rooty() + (root.winfo_height() - 560) // 2
    dialog.geometry(f"900x560+{max(x, 0)}+{max(y, 0)}")
    dialog.deiconify()
    dialog.transient(root)
    dialog.grab_set()
    dialog.lift()
    dialog.focus_set()

PROTONCACHYOS_RELEASES_API = ENV_CONFIG["proton_cachyos"]["releases_api"]
PROTONCACHYOS_RELEASES_PAGE = ENV_CONFIG["proton_cachyos"]["releases_page"]
ARCHIVE_SUFFIXES = (".tar.gz", ".tar.xz", ".tgz", ".zip")

def _strip_archive_suffix(name):
    """Buang ekstensi arsip (.tar.gz/.tar.xz/.tgz/.zip) dari sebuah nama file/asset."""
    for suf in ARCHIVE_SUFFIXES:
        if name.lower().endswith(suf):
            return name[:-len(suf)]
    return name

def detect_cpu_profile():
    """Profil CPU untuk memilih build Proton yang cocok. Dibaca dari /proc/cpuinfo (sekali saja).
    level = tingkat mikroarsitektur x86-64 (1-4; 0 = tidak diketahui)."""
    if _CPU_PROFILE:
        return _CPU_PROFILE
    machine = platform.machine().lower()
    flags, vendor = set(), ""
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("flags") and not flags:
                    flags = set(line.split(":", 1)[1].split())
                elif line.startswith("vendor_id") and not vendor:
                    vendor = line.split(":", 1)[1].strip()
                if flags and vendor:
                    break
    except OSError:
        pass
    level = 0
    if machine in ("x86_64", "amd64") and flags:
        v2 = {"cx16", "lahf_lm", "popcnt", "sse4_1", "sse4_2", "ssse3"}
        v3 = v2 | {"avx", "avx2", "bmi1", "bmi2", "f16c", "fma", "abm", "movbe", "xsave"}
        v4 = v3 | {"avx512f", "avx512bw", "avx512cd", "avx512dq", "avx512vl"}
        level = 4 if v4 <= flags else 3 if v3 <= flags else 2 if v2 <= flags else 1
    if machine in ("aarch64", "arm64"):
        text = "ARM64 (aarch64)"
    elif level:
        text = f"x86-64-v{level}" + (" (AMD)" if vendor == "AuthenticAMD" else " (Intel)" if vendor == "GenuineIntel" else "")
    else:
        text = machine or "unknown"
    _CPU_PROFILE.update(machine=machine, flags=flags, vendor=vendor, level=level, text=text)
    return _CPU_PROFILE

_CPU_PROFILE = {}

def proton_asset_arch(asset_name):
    """(label, key) arsitektur dari nama asset Proton. key: 'v1'..'v4' | 'znverN' | 'arm64' | None.
    Contoh: '...-x86_64_v3.tar.xz' -> ('x86-64-v3', 'v3'); '...-znver4' -> ('AMD Zen 4 (znver4)', 'znver4')."""
    n = _strip_archive_suffix(asset_name).lower()
    m = re.search(r"znver(\d)", n)
    if m:
        return f"AMD Zen {m.group(1)} (znver{m.group(1)})", f"znver{m.group(1)}"
    m = re.search(r"x86[_-]64[_-]v([1-4])", n)
    if m:
        return f"x86-64-v{m.group(1)}", f"v{m.group(1)}"
    if re.search(r"aarch64|arm64", n):
        return "ARM64 (aarch64)", "arm64"
    if re.search(r"x86[_-]64|amd64", n):
        return "x86-64 (standard)", "v1"
    return "not stated", None

def proton_arch_compat(key, cpu):
    """True / False = build itu bisa / tidak bisa jalan di CPU ini; None = tidak diketahui."""
    if key is None or not cpu:
        return None
    is_x86 = cpu["machine"] in ("x86_64", "amd64")
    if key == "arm64":
        return not is_x86 and cpu["machine"] in ("aarch64", "arm64")
    if not is_x86:
        return False
    if not cpu["level"]:
        return None
    if key.startswith("v"):
        return cpu["level"] >= int(key[1])
    if key.startswith("znver"):
        if cpu["vendor"] != "AuthenticAMD":
            return False
        n, fl = int(key[5:]), cpu["flags"]
        if n <= 2:
            return cpu["level"] >= 3
        return ("vaes" if n == 3 else "avx512_bf16" if n == 4 else "avx512_vp2intersect") in fl
    return None

def proton_arch_rank(key):
    """Makin tinggi = makin dioptimalkan (dipakai untuk menandai 'Recommended')."""
    if key and key.startswith("znver"):
        return 5
    return {"v1": 1, "v2": 2, "v3": 3, "v4": 4, "arm64": 1}.get(key, 0)

def place_dialog(dialog, parent_win=None):
    """Taruh dialog di TENGAH induknya, tapi selalu utuh di dalam layar (sisakan ruang title bar di
    atas dan taskbar di bawah). Aman dipanggil berulang, mis. setelah isi dialog berubah tinggi."""
    try:
        if not dialog.winfo_exists():
            return
        dialog.update_idletasks()
        sw, sh = dialog.winfo_screenwidth(), dialog.winfo_screenheight()
        w, h = min(dialog.winfo_reqwidth(), sw - 20), min(dialog.winfo_reqheight(), sh - 90)
        if parent_win is not None and parent_win.winfo_exists():
            cx = parent_win.winfo_rootx() + parent_win.winfo_width() // 2
            cy = parent_win.winfo_rooty() + parent_win.winfo_height() // 2
        else:
            cx, cy = sw // 2, sh // 2
        x = max(0, min(cx - w // 2, sw - w - 10))
        y = max(10, min(cy - h // 2, sh - h - 70))
        dialog.geometry(f"+{x}+{y}")
    except tk.TclError:
        pass

def fit_combo_width(values, minimum=32, maximum=76):
    """Lebar Combobox (karakter) supaya nama terpanjang tidak terpotong."""
    return max(minimum, min(maximum, max((len(str(v)) for v in values), default=0) + 3))

def fetch_protoncachyos_releases(limit=20):
    """Ambil daftar rilis Proton-CachyOS terbaru langsung dari GitHub (CachyOS/proton-cachyos).
    Berbeda dengan ProtonGE yang biasanya satu asset .tar.gz per rilis, satu rilis
    Proton-CachyOS sering punya beberapa varian build sekaligus (mis. SLR & Native), jadi
    setiap asset arsip yang valid ditampilkan sebagai baris tersendiri (bukan cuma satu asset
    per rilis). Dipanggil dari background thread karena ini network call yang blocking.
    Return list of dict: {"tag", "release_name", "published_at", "asset_name",
    "download_url", "size"}. Melempar exception kalau gagal (tidak ada internet, GitHub
    rate-limit, dll) supaya bisa ditangani oleh pemanggilnya."""
    url = f"{PROTONCACHYOS_RELEASES_API}?per_page={limit}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "wine-launcher-manager",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    releases = []
    for rel in data:
        tag = rel.get("tag_name", "")
        published = (rel.get("published_at") or "")[:10]
        release_name = rel.get("name") or tag
        for a in rel.get("assets", []):
            name = a.get("name", "")
            if not name.lower().endswith(ARCHIVE_SUFFIXES):
                continue
            releases.append({
                "tag": tag,
                "release_name": release_name,
                "published_at": published,
                "asset_name": name,
                "download_url": a.get("browser_download_url", ""),
                "size": a.get("size", 0),
            })
    return releases

def download_and_install_protoncachyos_worker(release, append_line, set_progress=None,
                                              pause_event=None, win=None):
    """Berjalan di background thread: download asset arsip milik satu build Proton-CachyOS
    dari GitHub (dengan progress live via append_line + set_progress kalau ukurannya
    diketahui), lalu ekstrak ke ~/wlm/protoncachyos/ lewat extract_archive_to_dir (fungsi
    yang sama dipakai oleh 'Extract Proton-CachyOS Archive...' manual)."""
    tmp_path = None
    try:
        url = release["download_url"]
        total_size = release.get("size") or 0
        asset_name = release["asset_name"]
        append_line(f"Downloading {asset_name} ({human_size(total_size)})...")

        suffix = next((s for s in ARCHIVE_SUFFIXES if asset_name.lower().endswith(s)),
                       Path(asset_name).suffix)
        tmp_fd, tmp_name = tempfile.mkstemp(prefix="wlm_protoncachyos_", suffix=suffix)
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)

        downloaded = download_url_pausable(url, tmp_path, total_size, append_line, set_progress, pause_event)

        if set_progress and total_size:
            set_progress(100)
        if win is not None:      # sesudah download, ekstrak tidak bisa di-pause
            root.after(0, lambda: getattr(win, "disable_pause", lambda: None)())

        append_line("")
        append_line(f"Download complete ({human_size(downloaded)}). Extracting to {protoncachyos_dir}...")
        extract_archive_to_dir(tmp_path, protoncachyos_dir)

        append_line("")
        full_name = _strip_archive_suffix(release["asset_name"])
        append_line(f"{full_name} installed successfully.")
        root.after(0, lambda: safe_status_config(
            text=f"{full_name} installed successfully.", fg=COLORS["success"]))
    except Exception as e:
        err = str(e)
        append_line("")
        append_line(f"ERROR: {err}")
        root.after(0, lambda: messagebox.showerror("Download Failed",
                                                     f"Failed to download/install Proton-CachyOS:\n{err}",
                                                     parent=root))
        root.after(0, lambda: safe_status_config(text=f"Proton-CachyOS download failed: {err}", fg=COLORS["danger"]))
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except Exception:
                pass

def open_protoncachyos_download_dialog():
    """Jendela untuk melihat daftar rilis Proton-CachyOS terbaru langsung dari GitHub dan
    mendownload + memasangnya secara otomatis, tanpa perlu download manual dari browser lalu
    'Extract Proton-CachyOS Archive...' sendiri - jadi build Proton-CachyOS bisa selalu
    diupdate sama seperti menu Download ProtonGE Online. Karena satu rilis CachyOS bisa
    berisi beberapa varian (SLR/Native/Standalone), tiap varian ditampilkan sebagai baris
    terpisah supaya jelas mana yang mau dipasang."""
    dialog = tk.Toplevel(root)
    dialog.withdraw()
    dialog.title("Download Proton-CachyOS")
    dialog.configure(bg=COLORS["primary"])
    dialog.resizable(True, True)
    dialog.minsize(900, 480)

    frame = ttk.Frame(dialog, padding=15)
    frame.pack(fill=tk.BOTH, expand=True)

    top_bar = ttk.Frame(frame)
    top_bar.pack(fill=tk.X, pady=(0, 8))
    ttk.Label(top_bar, text="Latest Proton-CachyOS builds (CachyOS/proton-cachyos):",
              font=FONTS["normal"]).pack(side=tk.LEFT, anchor="w")

    refresh_btn = ttk.Button(top_bar, text="Refresh", style="Custom.TButton", width=12)
    refresh_btn.pack(side=tk.RIGHT)

    cpu = detect_cpu_profile()
    ttk.Label(frame, text=f"Your CPU: {cpu['text']}  -  pick a build whose 'Your CPU' column says "
                          "Recommended or Compatible.", font=FONTS["small"]).pack(anchor="w", pady=(0, 2))
    status_line = ttk.Label(frame, text="Loading releases from GitHub...", font=FONTS["small"])
    status_line.pack(anchor="w", pady=(0, 6))

    list_frame = ttk.Frame(frame)
    list_frame.pack(fill=tk.BOTH, expand=True)
    detail_lbl = ttk.Label(frame, text="", font=FONTS["small"], wraplength=980, justify=tk.LEFT)
    detail_lbl.pack(anchor="w", pady=(6, 0))
    list_frame.rowconfigure(0, weight=1)
    list_frame.columnconfigure(0, weight=1)

    vscroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL)
    vscroll.grid(row=0, column=1, sticky="ns")

    release_tree = ttk.Treeview(list_frame,
                                 columns=("Build", "Variant", "Arch", "CPU", "Published", "Size", "Status"),
                                 show="headings",
                                 yscrollcommand=vscroll.set,
                                 selectmode="browse",
                                 height=10)
    release_tree.grid(row=0, column=0, sticky="nsew")
    vscroll.config(command=release_tree.yview)

    for col, text, width, stretch in (
            ("Build", "Build (full name)", 340, True), ("Variant", "Variant", 85, False),
            ("Arch", "Architecture", 150, False), ("CPU", "Your CPU", 100, False),
            ("Published", "Published", 90, False), ("Size", "Size", 80, False),
            ("Status", "Status", 75, False)):
        release_tree.heading(col, text=text, anchor="w")
        release_tree.column(col, width=width, anchor="w", stretch=stretch)

    releases_data = {}

    def variant_label(asset_name):
        n = asset_name.lower()
        if "native" in n:
            return "Native"
        if "slr" in n:
            return "SLR"
        return "Standalone"

    def cpu_verdict(rel):
        return proton_arch_compat(proton_asset_arch(rel["asset_name"])[1], cpu)

    def populate(releases):
        release_tree.delete(*release_tree.get_children())
        releases_data.clear()
        installed_names = {name for name, _ in find_protoncachyos_installations()}
        # 'Recommended' = build paling teroptimasi yang masih cocok, per rilis + varian (SLR/Native/...)
        best = {}
        for rel in releases:
            key = proton_asset_arch(rel["asset_name"])[1]
            if cpu_verdict(rel):
                grp = (rel["tag"], variant_label(rel["asset_name"]))
                if grp not in best or proton_arch_rank(key) > best[grp][0]:
                    best[grp] = (proton_arch_rank(key), rel["asset_name"])
        for rel in releases:
            iid = f"{rel['tag']}::{rel['asset_name']}"
            releases_data[iid] = rel
            full_name = _strip_archive_suffix(rel["asset_name"])
            is_installed = rel["tag"] in installed_names or full_name in installed_names
            ok = cpu_verdict(rel)
            grp = (rel["tag"], variant_label(rel["asset_name"]))
            verdict = ("" if ok is None else "Not compatible" if not ok else
                       "Recommended" if best.get(grp, (0, ""))[1] == rel["asset_name"] else "Compatible")
            release_tree.insert("", tk.END, iid=iid,
                                 values=(full_name, variant_label(rel["asset_name"]),
                                         proton_asset_arch(rel["asset_name"])[0], verdict,
                                         rel["published_at"], human_size(rel["size"]),
                                         "Installed" if is_installed else ""))

    def on_select(_event=None):
        sel = release_tree.selection()
        rel = releases_data.get(sel[0]) if sel else None
        if not rel:
            detail_lbl.config(text="")
            return
        label = proton_asset_arch(rel["asset_name"])[0]
        ok = cpu_verdict(rel)
        note = "" if ok is None else ("  -  compatible with your CPU" if ok else "  -  NOT compatible with your CPU")
        detail_lbl.config(text=f"File: {rel['asset_name']}\n"
                               f"Release: {rel['release_name']} ({rel['tag']})    Architecture: {label}{note}")

    release_tree.bind("<<TreeviewSelect>>", on_select)

    def load_releases():
        status_line.config(text="Loading releases from GitHub...")
        refresh_btn.config(state="disabled")

        def worker():
            try:
                releases = fetch_protoncachyos_releases()
                error = None
            except Exception as e:
                releases = []
                error = str(e)

            def finish():
                refresh_btn.config(state="normal")
                if error:
                    status_line.config(text=f"Failed to load releases: {error}")
                    messagebox.showerror(
                        "Error",
                        f"Failed to fetch Proton-CachyOS release list from GitHub:\n{error}\n\n"
                        "Check your internet connection and try Refresh again.",
                        parent=dialog
                    )
                else:
                    status_line.config(text=f"{len(releases)} build(s) loaded.")
                    populate(releases)

            root.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    refresh_btn.config(command=load_releases)
    load_releases()

    btn_row = ttk.Frame(frame)
    btn_row.pack(pady=(10, 0))

    def get_selected_release():
        sel = release_tree.selection()
        if not sel:
            messagebox.showinfo("Info", "Select a build from the list first.", parent=dialog)
            return None
        return releases_data.get(sel[0])

    def do_download():
        rel = get_selected_release()
        if not rel:
            return
        arch_label, arch_key = proton_asset_arch(rel["asset_name"])
        if proton_arch_compat(arch_key, cpu) is False and not messagebox.askyesno(
            "Architecture mismatch",
            f"'{rel['asset_name']}' targets {arch_label}, which your CPU ({cpu['text']}) "
            "does not appear to support, so games may fail to start.\n\nDownload it anyway?",
            icon="warning", parent=dialog
        ):
            return
        if not messagebox.askyesno(
            "Download & Install Proton-CachyOS",
            f"Download and install {_strip_archive_suffix(rel['asset_name'])}?\n\n"
            f"Variant: {variant_label(rel['asset_name'])}    Architecture: {arch_label}\n"
            f"File: {rel['asset_name']}\n"
            f"Size: {human_size(rel['size'])}\n\n"
            f"It will be extracted into:\n{protoncachyos_dir}",
            parent=dialog
        ):
            return

        pause_event = threading.Event()
        append_line, set_progress, win = open_task_log_window(
            f"Download {_strip_archive_suffix(rel['asset_name'])}", modal_parent=dialog,
            pause_event=pause_event)
        append_line(f"Release: {rel['release_name']} ({rel['tag']}) - {rel['asset_name']}")
        append_line("")
        threading.Thread(target=download_and_install_protoncachyos_worker,
                          args=(rel, append_line, set_progress, pause_event, win), daemon=True).start()

    def do_open_page():
        webbrowser.open(PROTONCACHYOS_RELEASES_PAGE)

    ttk.Button(btn_row, text="Download & Install", style="Custom.TButton", width=18,
               command=do_download).grid(row=0, column=0, padx=3)
    ttk.Button(btn_row, text="Open Releases Page", style="Custom.TButton", width=18,
               command=do_open_page).grid(row=0, column=1, padx=3)

    dialog.update_idletasks()
    x = root.winfo_rootx() + (root.winfo_width() - 1040) // 2
    y = root.winfo_rooty() + (root.winfo_height() - 560) // 2
    dialog.geometry(f"1040x600+{max(x, 0)}+{max(y, 0)}")
    dialog.deiconify()
    dialog.transient(root)
    dialog.grab_set()
    dialog.lift()
    dialog.focus_set()

def generate_next_prefix_code(runner_key):
    """Generate kode prefix baru secara berurutan (GAME001, GAME002, ...) untuk runner_key
    ('wine' / 'protonge' / 'protoncachyos'). Penomoran dibaca dari runner_config.json (bukan
    hanya isi folder) supaya tetap konsisten walau prefix-prefix game sudah dipindah ke
    lokasi/disk yang berbeda-beda. Folder default juga tetap dicek untuk jaga-jaga kalau ada
    prefix lama yang belum tercatat rapi disana."""
    numbers = []
    for cfg in load_runner_config().values():
        if cfg.get("runner") == runner_key:
            code = cfg.get("prefix_code", "") or ""
            if code.startswith("GAME") and code[4:].isdigit():
                numbers.append(int(code[4:]))

    default_root = DEFAULT_PREFIX_ROOTS.get(runner_key)
    if default_root and default_root.is_dir():
        for entry in default_root.iterdir():
            if entry.is_dir() and entry.name.startswith("GAME") and entry.name[4:].isdigit():
                numbers.append(int(entry.name[4:]))

    next_num = max(numbers, default=0) + 1
    return f"GAME{next_num:03d}"

def load_prefix_location_config():
    """Load lokasi dasar (folder/disk) kustom untuk prefix BARU, per jenis runner.
    Jika suatu runner tidak tercatat disini, berarti pakai lokasi default (folder WLM)."""
    if prefix_location_config_file.exists():
        try:
            with open(prefix_location_config_file, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_prefix_location_config(cfg):
    """Simpan lokasi dasar kustom untuk prefix BARU, per jenis runner."""
    try:
        with open(prefix_location_config_file, 'w') as f:
            json.dump(cfg, f, indent=4)
    except Exception as e:
        print(f"Error saving prefix location config: {e}")

def get_prefix_base_dir(runner_key):
    """Base folder tempat prefix BARU untuk runner_key akan dibuat: lokasi kustom yang
    tersimpan (jika masih valid) atau folder default didalam direktori WLM."""
    cfg = load_prefix_location_config()
    custom = cfg.get(runner_key)
    default_root = DEFAULT_PREFIX_ROOTS[runner_key]
    if custom:
        p = Path(custom)
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            return default_root
    return default_root

def set_prefix_base_dir(runner_key, path):
    """Simpan folder/disk kustom sebagai lokasi default berikutnya untuk prefix BARU
    runner_key ini (dipakai lagi otomatis lain kali sampai diubah/direset)."""
    cfg = load_prefix_location_config()
    cfg[runner_key] = str(path)
    save_prefix_location_config(cfg)

def reset_prefix_base_dir(runner_key):
    """Kembalikan lokasi default prefix BARU runner_key ini ke folder WLM (bawaan)."""
    cfg = load_prefix_location_config()
    if runner_key in cfg:
        del cfg[runner_key]
        save_prefix_location_config(cfg)

def load_prefix_registry():
    """Load catatan semua prefix yang pernah dibuat/dipakai: lokasi terakhirnya, dan untuk
    Proton GE/CachyOS, versi Proton mana yang dipakai. Kunci: 'runner_key:prefix_code'."""
    if prefix_registry_file.exists():
        try:
            with open(prefix_registry_file, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_prefix_registry(reg):
    try:
        with open(prefix_registry_file, 'w') as f:
            json.dump(reg, f, indent=4)
    except Exception as e:
        print(f"Error saving prefix registry: {e}")

def record_prefix_usage(runner_key, prefix_code, prefix_path, proton_name=None, proton_path=None):
    """Catat sebuah prefix (lokasi & versi Proton yang dipakai jika ada) ke registry global.
    Dipanggil setiap kali sebuah prefix dipakai/dibuat lewat dialog pemilihan runner, supaya
    game lain yang belakangan terdeteksi memakai prefix yang sama (lihat find_owning_prefix)
    bisa langsung tahu binary Proton mana yang harus dipakai juga."""
    reg = load_prefix_registry()
    key = f"{runner_key}:{prefix_code}"
    reg[key] = {
        "runner": runner_key,
        "prefix_code": prefix_code,
        "prefix_path": str(prefix_path),
        "proton_name": proton_name,
        "proton_path": proton_path,
    }
    save_prefix_registry(reg)

def update_registry_prefix_path(runner_key, prefix_code, new_path):
    """Perbarui lokasi sebuah prefix di registry setelah dipindahkan (Move Prefix),
    tanpa mengubah data lain (mis. versi Proton) yang sudah tercatat untuknya."""
    reg = load_prefix_registry()
    key = f"{runner_key}:{prefix_code}"
    if key in reg:
        reg[key]["prefix_path"] = str(new_path)
    else:
        reg[key] = {"runner": runner_key, "prefix_code": prefix_code, "prefix_path": str(new_path),
                    "proton_name": None, "proton_path": None}
    save_prefix_registry(reg)

def relink_scripts_after_prefix_move(runner_key, prefix_code, old_prefix_path, new_prefix_path):
    """Setelah sebuah prefix dipindah (Move Prefix), perbaiki SEMUA game yang memakai prefix
    ini - bukan cuma metadata prefix_path di runner_config.json, tapi juga ISI script .sh-nya
    sendiri. Ini krusial: kalau file/folder game itu ternyata berada DIDALAM folder prefix
    (mis. game GOG yang terinstall ke drive_c prefix), file-nya ikut berpindah SECARA FISIK
    saat foldernya dipindah - tapi baris 'cd' dan 'wine'/proton run didalam script masih
    berisi alamat LAMA yang sudah tidak ada, sehingga game tidak bisa di-launch sama sekali
    walau sudah "ditimpa"/overwrite. Fungsi ini menghitung ulang path relatifnya terhadap
    prefix dan menulis ulang scriptnya supaya menunjuk ke alamat BARU yang benar.

    Game yang folder/exe-nya ternyata berada DILUAR prefix (tidak ikut fisik pindah) tetap
    diperbaiki juga - hanya export WINEPREFIX/STEAM_COMPAT_DATA_PATH didalam scriptnya yang
    diperbarui ke lokasi baru, path folder/exe-nya dibiarkan seperti semula.

    Return list nama-nama game (script_name) yang scriptnya berhasil ditulis ulang."""
    try:
        old_resolved = Path(old_prefix_path).resolve()
    except Exception:
        old_resolved = Path(old_prefix_path)
    new_resolved = Path(new_prefix_path)

    runner_cfg = load_runner_config()
    changed = False
    updated_scripts = []

    for script_name, cfg in runner_cfg.items():
        if cfg.get("runner") != runner_key or cfg.get("prefix_code") != prefix_code:
            continue

        cfg["prefix_path"] = str(new_resolved)
        changed = True

        script_path = bashlaunch_dir / f"{script_name}.sh"
        if not script_path.exists():
            continue

        folder_path_str = extract_folder_path_from_script(script_path)
        exe_path_str = extract_exe_path_from_script(script_path)
        if not folder_path_str or not exe_path_str:
            continue

        try:
            folder_resolved = Path(folder_path_str).resolve()
        except Exception:
            folder_resolved = Path(folder_path_str)
        try:
            exe_resolved = Path(exe_path_str).resolve()
        except Exception:
            exe_resolved = Path(exe_path_str)

        try:
            new_folder = new_resolved / folder_resolved.relative_to(old_resolved)
        except Exception:
            new_folder = folder_resolved
        try:
            new_exe = new_resolved / exe_resolved.relative_to(old_resolved)
        except Exception:
            new_exe = exe_resolved

        new_content = build_script_content(str(new_folder), str(new_exe), cfg)
        try:
            script_path.write_text(new_content)
            script_path.chmod(0o755)
            updated_scripts.append(script_name)
        except Exception as e:
            print(f"Error relinking script '{script_name}' after prefix move: {e}")

    if changed:
        save_runner_config(runner_cfg)
    return updated_scripts

def find_owning_prefix(exe_path):
    """Cek apakah exe_path berada didalam salah satu folder prefix (Wine/Proton GE/
    Proton-CachyOS) yang sudah pernah dibuat. Dipakai saat menambahkan game baru (+ ADD):
    jika exe yang dipilih ternyata sudah terinstall didalam sebuah prefix (misalnya baru
    saja diinstall lewat APPS SETUP), game itu HARUS langsung dikaitkan ke prefix yang sama
    - bukan diberi prefix baru yang kosong. Ini penting untuk game (mis. GOG) yang lisensi/
    aktivasinya terikat ke prefix tempat ia pertama kali diinstall; prefix baru berarti
    game tidak bisa dijalankan lagi.

    Return dict {"runner", "prefix_code", "prefix_path", "proton_name", "proton_path"}
    atau None jika exe tidak berada didalam prefix manapun yang dikenal."""
    try:
        exe_resolved = exe_path.resolve()
    except Exception:
        exe_resolved = exe_path

    registry = load_prefix_registry()
    candidates = []
    for entry in registry.values():
        p = entry.get("prefix_path")
        r = entry.get("runner")
        c = entry.get("prefix_code")
        if p and r and c:
            candidates.append((r, c, Path(p)))

    loc_cfg = load_prefix_location_config()
    scan_roots = [("wine", wine_prefix_root), ("protonge", protonge_prefix_root), ("protoncachyos", protoncachyos_prefix_root)]
    for key, _default in list(scan_roots):
        custom = loc_cfg.get(key)
        if custom:
            scan_roots.append((key, Path(custom)))

    known_paths = {str(p.resolve()) for _, _, p in candidates if p.exists()}
    for runner_key, base_dir in scan_roots:
        if not base_dir.is_dir():
            continue
        for entry in base_dir.iterdir():
            if entry.is_dir():
                try:
                    resolved = entry.resolve()
                except Exception:
                    continue
                if str(resolved) not in known_paths:
                    candidates.append((runner_key, entry.name, entry))

    for runner_key, prefix_code, prefix_dir in candidates:
        try:
            prefix_resolved = prefix_dir.resolve()
        except Exception:
            continue
        if prefix_resolved == exe_resolved or prefix_resolved in exe_resolved.parents:
            reg_entry = registry.get(f"{runner_key}:{prefix_code}", {})
            return {
                "runner": runner_key,
                "prefix_code": prefix_code,
                "prefix_path": str(prefix_resolved),
                "proton_name": reg_entry.get("proton_name"),
                "proton_path": reg_entry.get("proton_path"),
            }
    return None

def list_all_known_prefixes():
    """Kumpulkan semua prefix yang diketahui (dari prefix_registry.json + hasil scan folder
    default/kustom untuk masing-masing runner), dipakai oleh dialog manajemen konfigurasi
    per-prefix (winecfg/explorer/uninstaller/winetricks). Berbeda dengan find_owning_prefix
    (yang mencari SATU prefix pemilik sebuah exe), fungsi ini mengembalikan SEMUA prefix
    yang ada, sekaligus nama game/aplikasi mana saja (dari runner_config.json) yang memakai
    tiap prefix - satu prefix bisa dipakai oleh lebih dari satu game.

    Return list of dict: {"runner", "prefix_code", "prefix_path", "proton_name",
    "proton_path", "games": [nama_game, ...]}, terurut berdasarkan (runner, prefix_code)."""
    result = {}

    registry = load_prefix_registry()
    for entry in registry.values():
        p = entry.get("prefix_path")
        r = entry.get("runner")
        c = entry.get("prefix_code")
        if p and r and c:
            result[f"{r}:{c}"] = {
                "runner": r,
                "prefix_code": c,
                "prefix_path": p,
                "proton_name": entry.get("proton_name"),
                "proton_path": entry.get("proton_path"),
            }

    loc_cfg = load_prefix_location_config()
    scan_roots = [("wine", wine_prefix_root), ("protonge", protonge_prefix_root),
                  ("protoncachyos", protoncachyos_prefix_root)]
    for key, _default in list(scan_roots):
        custom = loc_cfg.get(key)
        if custom:
            scan_roots.append((key, Path(custom)))

    for runner_key, base_dir in scan_roots:
        if not base_dir.is_dir():
            continue
        for entry in base_dir.iterdir():
            if entry.is_dir():
                key = f"{runner_key}:{entry.name}"
                if key not in result:
                    result[key] = {
                        "runner": runner_key,
                        "prefix_code": entry.name,
                        "prefix_path": str(entry),
                        "proton_name": None,
                        "proton_path": None,
                    }

    games_by_key = {}
    for script_name, cfg in load_runner_config().items():
        r = cfg.get("runner")
        c = cfg.get("prefix_code")
        if r and c:
            games_by_key.setdefault(f"{r}:{c}", []).append(script_name)

    for key, entry in result.items():
        entry["games"] = sorted(games_by_key.get(key, []))

    return sorted(result.values(), key=lambda e: (e["runner"], e["prefix_code"]))

def pick_proton_build_dialog(runner_key, parent=None):
    """Dialog kecil untuk memilih salah satu build Proton (GE/CachyOS) yang sudah diekstrak.
    Dipakai saat sebuah prefix Proton belum tercatat memakai versi Proton mana di registry
    (mis. prefix lama, atau ditemukan lewat scan folder bukan lewat dialog runner biasa).

    parent: window Toplevel modal yang sedang aktif memanggil ini (mis. Prefix Configuration
    Manager), kalau ada - supaya dialog ini dibuka DIATASnya (bukan dibelakangnya) dan
    grab-nya diserahkan sementara, lalu dikembalikan lagi setelah dialog ini ditutup.

    Return tuple (build_name, proton_bin_path) atau None jika dibatalkan/tidak ada build."""
    installs = find_protonge_installations() if runner_key == "protonge" else find_protoncachyos_installations()
    label = "Proton GE" if runner_key == "protonge" else "Proton-CachyOS"

    if not installs:
        messagebox.showerror("Error", f"No {label} build found.\nExtract one first via Settings.")
        return None

    parent_win = parent if (parent is not None and parent.winfo_exists()) else root
    parent_had_grab = parent is not None and parent.winfo_exists()
    if parent_had_grab:
        parent.grab_release()

    dialog = tk.Toplevel(parent_win)
    dialog.withdraw()
    dialog.title(f"Select {label} Build")
    dialog.configure(bg=COLORS["primary"])
    dialog.resizable(False, False)

    result = {"value": None}

    frame = ttk.Frame(dialog, padding=15)
    frame.pack(fill=tk.BOTH, expand=True)

    ttk.Label(frame,
              text=f"This prefix has no {label} version recorded yet.\nSelect the build to use for it:",
              font=FONTS["normal"], justify=tk.LEFT).pack(anchor="w", pady=(0, 10))

    names = [name for name, _ in installs]
    combo = ttk.Combobox(frame, values=names, state="readonly", width=fit_combo_width(names), font=FONTS["normal"])
    combo.current(0)
    combo.pack(fill=tk.X, pady=(0, 15))

    btn_row = ttk.Frame(frame)
    btn_row.pack()

    def close_dialog():
        dialog.destroy()
        if parent_had_grab and parent.winfo_exists():
            parent.grab_set()
            parent.lift()
            parent.focus_set()

    def do_ok():
        result["value"] = installs[combo.current()]
        close_dialog()

    def do_cancel():
        close_dialog()

    dialog.protocol("WM_DELETE_WINDOW", do_cancel)
    ttk.Button(btn_row, text="OK", command=do_ok, style="Custom.TButton", width=10).grid(row=0, column=0, padx=5)
    ttk.Button(btn_row, text="Cancel", command=do_cancel, style="Custom.TButton", width=10).grid(row=0, column=1, padx=5)

    dialog.update_idletasks()
    x = parent_win.winfo_rootx() + (parent_win.winfo_width() - dialog.winfo_width()) // 2
    y = parent_win.winfo_rooty() + (parent_win.winfo_height() - dialog.winfo_height()) // 2
    dialog.geometry(f"+{x}+{y}")
    dialog.deiconify()
    dialog.transient(parent_win)
    dialog.grab_set()
    dialog.lift()
    dialog.focus_set()

    dialog.wait_window()
    return result["value"]

def find_proton_wine_binary(proton_script_path):
    """Cari binary 'wine' asli didalam sebuah build Proton (GE/CachyOS), dari path ke
    script 'proton'-nya. Dipakai khusus untuk Winetricks: winetricks perlu diarahkan
    (lewat env WINE) ke wine milik Proton ini, bukan wine sistem, supaya perubahan yang
    dilakukan benar-benar masuk ke prefix Proton yang dipilih.
    Return Path ke binary wine64/wine, atau None jika tidak ditemukan."""
    proton_root = Path(proton_script_path).parent
    candidates = [
        proton_root / "files" / "bin" / "wine64",
        proton_root / "files" / "bin" / "wine",
        proton_root / "dist" / "bin" / "wine64",
        proton_root / "dist" / "bin" / "wine",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None

def run_prefix_tool(entry, tool="winecfg", extra_args=None, parent=None):
    """Jalankan sebuah utilitas manajemen ('winecfg', 'explorer', 'uninstaller', atau
    'winetricks') untuk SATU prefix tertentu - baik itu prefix Wine vanilla ataupun Proton
    GE/CachyOS - dengan env yang dipasang hanya untuk proses child ini (tidak mengubah env
    launcher itu sendiri). Untuk prefix Proton yang belum tercatat memakai versi Proton mana,
    pengguna akan diminta memilih buildnya lewat pick_proton_build_dialog(), lalu pilihan itu
    disimpan ke registry supaya tidak ditanya lagi lain kali untuk prefix yang sama.

    parent: window Toplevel modal yang sedang aktif memanggilnya (mis. Prefix Configuration
    Manager) - diteruskan ke pick_proton_build_dialog() supaya dialog pemilihan build itu
    dibuka DIATAS-nya, bukan dibelakangnya."""
    runner_key = entry["runner"]
    prefix_code = entry["prefix_code"]
    prefix_path = Path(entry["prefix_path"])
    extra_args = extra_args or []

    if not prefix_path.is_dir():
        messagebox.showerror("Error", f"Prefix folder not found on disk:\n{prefix_path}")
        return

    if tool == "winetricks" and shutil.which("winetricks") is None:
        messagebox.showerror(
            "Error",
            "The 'winetricks' command was not found.\n"
            "Install it first, e.g.:\n"
            "  sudo apt install winetricks\n"
            "  sudo dnf install winetricks\n"
            "  sudo pacman -S winetricks"
        )
        return

    env = get_clean_subprocess_env()
    tool_labels = {"winecfg": "Wine Configuration", "explorer": "Wine Explorer",
                   "uninstaller": "Uninstaller", "winetricks": "Winetricks"}
    tool_label = tool_labels.get(tool, tool)

    if runner_key == "wine":
        env["WINEPREFIX"] = str(prefix_path)
        if tool == "winecfg":
            command = ["winecfg"]
        elif tool == "winetricks":
            command = ["winetricks"] + extra_args
        else:
            command = ["wine", tool]
        runner_label = "Wine"
    else:
        proton_path = entry.get("proton_path")
        if not proton_path or not Path(proton_path).exists():
            picked = pick_proton_build_dialog(runner_key, parent=parent)
            if picked is None:
                return
            proton_name, proton_bin = picked
            proton_path = str(proton_bin)
            reg = load_prefix_registry()
            reg[f"{runner_key}:{prefix_code}"] = {
                "runner": runner_key,
                "prefix_code": prefix_code,
                "prefix_path": str(prefix_path),
                "proton_name": proton_name,
                "proton_path": proton_path,
            }
            save_prefix_registry(reg)

        runner_label = "Proton GE" if runner_key == "protonge" else "Proton-CachyOS"

        if tool == "winetricks":
            pfx_path = prefix_path / "pfx"
            if not pfx_path.is_dir():
                messagebox.showerror(
                    "Error",
                    f"This prefix doesn't have a Wine prefix yet ('pfx' folder missing):\n{pfx_path}\n\n"
                    "Run this game/app via PLAY or APPS SETUP at least once first, so Proton\n"
                    "can create it, then try Winetricks again."
                )
                return

            wine_bin = find_proton_wine_binary(proton_path)
            if wine_bin is None:
                messagebox.showerror(
                    "Error",
                    f"Could not find the 'wine' binary inside this Proton build:\n{proton_path}"
                )
                return

            env["WINE"] = str(wine_bin)
            wineserver_bin = wine_bin.parent / "wineserver"
            if wineserver_bin.exists():
                env["WINESERVER"] = str(wineserver_bin)
            env["WINEPREFIX"] = str(pfx_path)
            command = ["winetricks"] + extra_args
        else:
            env["STEAM_COMPAT_DATA_PATH"] = str(prefix_path)
            env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(find_steam_install_path())
            command = [proton_path, "run", tool]

    if tool == "winetricks":
        task_key = f"winetricks-{runner_key}-{prefix_code}"
        log_path = logs_dir / f"{task_key}.log"
        try:
            master_fd, slave_fd = pty.openpty()
            proc = subprocess.Popen(command,
                                     stdout=slave_fd, stderr=slave_fd, stdin=slave_fd,
                                     close_fds=True, start_new_session=True, env=env)
            os.close(slave_fd)

            running_games[task_key] = {
                "proc": proc,
                "log_path": log_path,
                "queue": queue.Queue(),
                "buffer": [],
                "window": None,
                "text_widget": None,
                "status_label": None,
                "finished": False,
            }
            threading.Thread(target=stream_output, args=(task_key, proc, master_fd, log_path), daemon=True).start()
            open_log_window(task_key)
            safe_status_config(
                text=f"Running Winetricks for prefix {prefix_code} ({runner_label})...",
                fg=COLORS["text_secondary"])
        except FileNotFoundError:
            messagebox.showerror("Error", f"Command not found: {' '.join(str(c) for c in command)}")
            safe_status_config(text="Error: winetricks command not found.", fg=COLORS["danger"])
        except Exception as e:
            messagebox.showerror("Error", f"Failed to launch Winetricks:\n{str(e)}")
            safe_status_config(text=f"Error launching Winetricks: {str(e)}", fg=COLORS["danger"])
        return

    try:
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        safe_status_config(
            text=f"Opening {tool_label} for prefix {prefix_code} ({runner_label})...",
            fg=COLORS["text_secondary"])
    except FileNotFoundError:
        messagebox.showerror("Error", f"Command not found: {' '.join(str(c) for c in command)}")
        safe_status_config(text="Error: runner command not found.", fg=COLORS["danger"])
    except Exception as e:
        messagebox.showerror("Error", f"Failed to launch {tool_label}:\n{str(e)}")
        safe_status_config(text=f"Error launching {tool_label}: {str(e)}", fg=COLORS["danger"])

COMMON_WINETRICKS_VERBS = [
    ("corefonts", "Core Fonts (Arial, Times New Roman, dll)"),
    ("cjkfonts", "CJK Fonts (China/Jepang/Korea)"),
    ("vcrun2005", "Visual C++ 2005 Redist"),
    ("vcrun2008", "Visual C++ 2008 Redist"),
    ("vcrun2010", "Visual C++ 2010 Redist"),
    ("vcrun2012", "Visual C++ 2012 Redist"),
    ("vcrun2013", "Visual C++ 2013 Redist"),
    ("vcrun2019", "Visual C++ 2015-2019 Redist"),
    ("vcrun2022", "Visual C++ 2015-2022 Redist"),
    ("dotnet48", ".NET Framework 4.8"),
    ("dotnet6", ".NET 6 Runtime"),
    ("dotnetdesktop6", ".NET 6 Desktop Runtime"),
    ("d3dx9", "DirectX 9 (d3dx9)"),
    ("d3dx11_43", "DirectX 11 (d3dx11_43)"),
    ("d3dcompiler_47", "D3D Compiler 47"),
    ("xact", "XAudio (xact)"),
    ("physx", "PhysX"),
    ("dxvk", "DXVK (DirectX -> Vulkan)"),
    ("vkd3d", "VKD3D (Direct3D 12 -> Vulkan)"),
    ("faudio", "FAudio"),
]

hud_config_file = directory / "hud_config.json"

VULKAN_HUD_METRICS = [
    ("fps", "FPS"),
    ("frametimes", "Frame time graph"),
    ("gpuload", "GPU load"),
    ("memory", "Memory usage"),
    ("devinfo", "Device info (GPU/driver)"),
    ("version", "DXVK version"),
    ("api", "D3D API version"),
    ("drawcalls", "Draw calls"),
    ("submissions", "Command submissions"),
    ("pipelines", "Pipeline compiles"),
    ("compiler", "Shader compiler activity"),
    ("samplers", "Sampler count"),
]
VULKAN_HUD_SCALE_OPTIONS = ["0.5", "0.75", "1", "1.25", "1.5", "2", "3"]

GALLIUM_HUD_METRICS = [
    ("fps", "FPS"),
    ("cpu", "Overall CPU load"),
    ("GPU-load", "GPU load (RADV/radeonsi)"),
    ("VRAM-usage", "VRAM usage"),
    ("GTT-usage", "GTT (system) memory usage"),
    ("draw-calls", "Draw calls"),
    ("requested-VRAM", "Requested VRAM"),
    ("requested-GTT", "Requested GTT memory"),
]
GALLIUM_HUD_SCALE_OPTIONS = ["1", "2", "3", "4", "5"]

DEFAULT_HUD_CONFIG = {
    "vulkan_hud": {"metrics": ["fps"], "scale": "1"},
    "gallium_hud": {"metrics": ["fps", "cpu", "GPU-load"], "scale": "1"},
}

def load_hud_config():
    """Baca hud_config.json, isi field yang kosong/hilang/rusak dengan nilai
    bawaan (DEFAULT_HUD_CONFIG), supaya selalu return dict yang lengkap."""
    cfg = copy.deepcopy(DEFAULT_HUD_CONFIG)
    if not hud_config_file.exists():
        return cfg
    try:
        with open(hud_config_file, 'r') as f:
            loaded = json.load(f) or {}
        for section, defaults in DEFAULT_HUD_CONFIG.items():
            section_data = loaded.get(section) or {}
            metrics = section_data.get("metrics")
            if isinstance(metrics, list):
                cfg[section]["metrics"] = metrics
            scale = section_data.get("scale")
            if scale:
                cfg[section]["scale"] = str(scale)
    except Exception as e:
        print(f"[WLM] Error reading {hud_config_file}, falling back to default HUD config: {e}")
    return cfg

def _read_hud_config_raw():
    """Baca hud_config.json apa adanya (dict mentah, tanpa normalisasi default),
    supaya field lain (mis. window_position) tidak ikut hilang saat salah satu
    bagian saja yang diupdate."""
    if hud_config_file.exists():
        try:
            with open(hud_config_file, 'r') as f:
                return json.load(f) or {}
        except Exception:
            pass
    return {}

def _write_hud_config_raw(data):
    try:
        with open(hud_config_file, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"[WLM] Error saving {hud_config_file}: {e}")

def save_hud_config(cfg):
    """Simpan pengaturan metrics/scale HUD tanpa menimpa window_position
    yang sudah tersimpan sebelumnya."""
    data = _read_hud_config_raw()
    data["vulkan_hud"] = cfg["vulkan_hud"]
    data["gallium_hud"] = cfg["gallium_hud"]
    _write_hud_config_raw(data)

def load_hud_window_position():
    """Baca posisi terakhir dialog Config HUD, kalau ada dan formatnya valid
    (\"+x+y\"). Return None kalau belum pernah disimpan/tidak valid."""
    pos = _read_hud_config_raw().get("window_position")
    if isinstance(pos, str) and pos.startswith('+') and pos.count('+') == 2:
        return pos
    return None

def save_hud_window_position(pos):
    """Simpan posisi terakhir dialog Config HUD ke hud_config.json tanpa
    menimpa pengaturan metrics/scale yang sudah ada."""
    data = _read_hud_config_raw()
    data["window_position"] = pos
    _write_hud_config_raw(data)

def build_vulkan_hud_env_prefix():
    """Bangun string 'DXVK_HUD=...' berdasarkan hud_config.json, siap ditempel
    didepan perintah launch (lihat run_script())."""
    cfg = load_hud_config()["vulkan_hud"]
    metrics = list(cfg.get("metrics") or []) or ["fps"]
    scale = cfg.get("scale") or "1"
    parts = list(metrics)
    if scale and scale != "1":
        parts.append(f"scale={scale}")
    return "DXVK_HUD=" + ",".join(parts)

def build_gallium_hud_env_prefix():
    """Bangun string 'GALLIUM_HUD=...' (+ 'GALLIUM_HUD_SCALE=...' kalau bukan
    bawaan) berdasarkan hud_config.json, siap ditempel didepan perintah launch."""
    cfg = load_hud_config()["gallium_hud"]
    metrics = list(cfg.get("metrics") or []) or ["fps"]
    scale = cfg.get("scale") or "1"
    env_str = "GALLIUM_HUD=" + "+".join(metrics)
    if scale and scale != "1":
        env_str += f" GALLIUM_HUD_SCALE={scale}"
    return env_str

def open_hud_config_dialog():
    """Dialog konfigurasi HUD (checklist indikator + ukuran) untuk mode Launch
    'VulkanHUD' (DXVK_HUD) dan 'GalliumHUD' (GALLIUM_HUD), dengan tampilan yang
    sama seperti dialog Winetricks di Prefix Configuration Manager."""
    cfg = load_hud_config()

    dialog = tk.Toplevel(root)
    dialog.withdraw()
    dialog.title("Config HUD")
    dialog.configure(bg=COLORS["primary"])
    dialog.resizable(False, False)

    frame = ttk.Frame(dialog, padding=15)
    frame.pack(fill=tk.BOTH, expand=True)

    ttk.Label(frame, text="Choose what each HUD mode shows, and its size.\n"
                          "Applies to the \"VulkanHUD\" and \"GalliumHUD\" Launch Mode options.",
              font=FONTS["small"], justify=tk.LEFT).pack(anchor="w", pady=(0, 12))

    def build_section(title_text, metrics_list, scale_options, saved_metrics, saved_scale):
        section = ttk.Frame(frame)
        section.pack(fill=tk.X, pady=(0, 14))

        ttk.Label(section, text=title_text, font=FONTS["normal"]).pack(anchor="w", pady=(0, 6))

        checks_frame = ttk.Frame(section)
        checks_frame.pack(fill=tk.X)

        check_vars = {}
        columns = 2
        for i, (key, description) in enumerate(metrics_list):
            var = tk.BooleanVar(value=(key in saved_metrics))
            check_vars[key] = var
            ttk.Checkbutton(checks_frame, text=f"{key} - {description}", variable=var,
                            style="Custom.TCheckbutton").grid(
                row=i // columns, column=i % columns, sticky="w", padx=(0, 20), pady=2)

        size_row = ttk.Frame(section)
        size_row.pack(anchor="w", pady=(8, 0))
        ttk.Label(size_row, text="HUD Size:", font=FONTS["small"]).pack(side=tk.LEFT, padx=(0, 6))
        scale_var = tk.StringVar(value=saved_scale if saved_scale in scale_options else scale_options[2])
        scale_combo = ttk.Combobox(size_row, textvariable=scale_var, values=scale_options,
                                    state="readonly", width=6, font=FONTS["small"])
        scale_combo.pack(side=tk.LEFT)
        ttk.Label(size_row, text="(1 = default)", font=FONTS["small"]).pack(side=tk.LEFT, padx=(6, 0))

        return check_vars, scale_var

    vulkan_check_vars, vulkan_scale_var = build_section(
        "Vulkan HUD (DXVK_HUD) - DirectX 9-11 games via Vulkan",
        VULKAN_HUD_METRICS, VULKAN_HUD_SCALE_OPTIONS,
        cfg["vulkan_hud"]["metrics"], cfg["vulkan_hud"]["scale"])

    ttk.Separator(frame, orient="horizontal").pack(fill=tk.X, pady=(0, 14))

    gallium_check_vars, gallium_scale_var = build_section(
        "Gallium HUD (GALLIUM_HUD) - native OpenGL games via Mesa",
        GALLIUM_HUD_METRICS, GALLIUM_HUD_SCALE_OPTIONS,
        cfg["gallium_hud"]["metrics"], cfg["gallium_hud"]["scale"])

    btn_row = ttk.Frame(frame)
    btn_row.pack(pady=(4, 0))

    # Posisi window dilacak tiap kali digeser (event <Configure>), lalu ditulis
    # ke hud_config.json begitu dialog ditutup - baik lewat Save, Cancel,
    # maupun tombol X window manager - supaya posisi terakhir selalu diingat
    # untuk kali berikutnya dialog ini dibuka.
    dialog_pos_state = {"x": None, "y": None}

    def _track_dialog_position(event):
        if event.widget is dialog:
            dialog_pos_state["x"] = dialog.winfo_x()
            dialog_pos_state["y"] = dialog.winfo_y()

    dialog.bind("<Configure>", _track_dialog_position)

    def _persist_dialog_position():
        if dialog_pos_state["x"] is not None:
            save_hud_window_position(f"+{dialog_pos_state['x']}+{dialog_pos_state['y']}")

    def do_save():
        new_cfg = {
            "vulkan_hud": {
                "metrics": [k for k, v in vulkan_check_vars.items() if v.get()],
                "scale": vulkan_scale_var.get(),
            },
            "gallium_hud": {
                "metrics": [k for k, v in gallium_check_vars.items() if v.get()],
                "scale": gallium_scale_var.get(),
            },
        }
        save_hud_config(new_cfg)
        _persist_dialog_position()
        safe_status_config(text="HUD configuration saved.", fg=COLORS["success"])

    def do_cancel():
        _persist_dialog_position()
        dialog.destroy()

    def do_reset():
        default_cfg = copy.deepcopy(DEFAULT_HUD_CONFIG)
        for key, var in vulkan_check_vars.items():
            var.set(key in default_cfg["vulkan_hud"]["metrics"])
        vulkan_scale_var.set(default_cfg["vulkan_hud"]["scale"])
        for key, var in gallium_check_vars.items():
            var.set(key in default_cfg["gallium_hud"]["metrics"])
        gallium_scale_var.set(default_cfg["gallium_hud"]["scale"])

    ttk.Button(btn_row, text="Save", style="Custom.TButton", width=12,
               command=do_save).grid(row=0, column=0, padx=4)
    ttk.Button(btn_row, text="Reset to Defaults", style="Custom.TButton", width=16,
               command=do_reset).grid(row=0, column=1, padx=4)
    ttk.Button(btn_row, text="Close", style="Custom.TButton", width=10,
               command=do_cancel).grid(row=0, column=2, padx=4)

    dialog.protocol("WM_DELETE_WINDOW", do_cancel)

    dialog.update_idletasks()
    saved_pos = load_hud_window_position()
    if saved_pos:
        # Pastikan posisi yang tersimpan masih masuk akal untuk resolusi layar
        # saat ini (mis. kalau monitor sebelumnya lebih besar), supaya dialog
        # tidak muncul di luar layar.
        try:
            px_str, py_str = saved_pos[1:].split('+')
            px, py = int(px_str), int(py_str)
            screen_w = root.winfo_screenwidth()
            screen_h = root.winfo_screenheight()
            px = min(max(px, 0), max(screen_w - dialog.winfo_reqwidth(), 0))
            py = min(max(py, 0), max(screen_h - dialog.winfo_reqheight(), 0))
            x, y = px, py
        except Exception:
            saved_pos = None
    if not saved_pos:
        x = root.winfo_rootx() + (root.winfo_width() - dialog.winfo_reqwidth()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - dialog.winfo_reqheight()) // 2
    dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")
    dialog.deiconify()
    dialog.transient(root)
    dialog.grab_set()
    dialog.lift()
    dialog.focus_set()

def open_winetricks_dialog(entry, parent_dialog=None):
    """GUI untuk memilih verb Winetricks umum lewat checkbox (font, redistributable, dotnet,
    dxvk, dll), plus kolom teks bebas untuk verb tambahan/khusus. Proses instalasinya nanti
    dijalankan lewat run_prefix_tool(..., tool="winetricks", ...) yang membuka jendela Logs
    real-time, supaya progress downloadnya kelihatan.

    parent_dialog: window Toplevel modal yang sedang aktif memanggil ini (mis. Prefix
    Configuration Manager) - dialog ini akan dibuka DIATAS-nya (bukan dibelakangnya, yang
    tadinya bikin dialog ini "hilang" tertutup parent & tombol yang memanggilnya seperti
    macet/terus tertekan), dengan grab modal parent dilepas sementara lalu dikembalikan
    lagi setelah dialog ini ditutup (lewat OK/Cancel maupun tombol close jendela)."""
    parent_win = parent_dialog if (parent_dialog is not None and parent_dialog.winfo_exists()) else root
    parent_had_grab = parent_dialog is not None and parent_dialog.winfo_exists()
    if parent_had_grab:
        parent_dialog.grab_release()

    dialog = tk.Toplevel(parent_win)
    dialog.withdraw()
    dialog.title(f"Winetricks - {entry['prefix_code']}")
    dialog.configure(bg=COLORS["primary"])
    dialog.resizable(False, False)

    frame = ttk.Frame(dialog, padding=15)
    frame.pack(fill=tk.BOTH, expand=True)

    ttk.Label(frame,
              text=f"Select components to install into prefix '{entry['prefix_code']}':",
              font=FONTS["normal"]).pack(anchor="w", pady=(0, 10))

    checks_frame = ttk.Frame(frame)
    checks_frame.pack(fill=tk.BOTH, expand=True)

    check_vars = {}
    columns = 2
    for i, (verb, description) in enumerate(COMMON_WINETRICKS_VERBS):
        var = tk.BooleanVar(value=False)
        check_vars[verb] = var
        ttk.Checkbutton(checks_frame, text=f"{verb} - {description}", variable=var,
                        style="Custom.TCheckbutton").grid(
            row=i // columns, column=i % columns, sticky="w", padx=(0, 20), pady=2)

    ttk.Label(frame, text="Additional/custom verb(s) (space separated):",
              font=FONTS["small"]).pack(anchor="w", pady=(12, 2))
    custom_entry = ttk.Entry(frame, width=64, font=FONTS["normal"])
    custom_entry.pack(fill=tk.X, pady=(0, 14))

    btn_row = ttk.Frame(frame)
    btn_row.pack()

    def close_dialog():
        dialog.destroy()
        if parent_had_grab and parent_dialog.winfo_exists():
            parent_dialog.grab_set()
            parent_dialog.lift()
            parent_dialog.focus_set()

    def do_run():
        verbs = [v for v, var in check_vars.items() if var.get()]
        custom_text = custom_entry.get().strip()
        if custom_text:
            verbs.extend(shlex.split(custom_text))
        if not verbs:
            messagebox.showinfo("Info", "Select at least one component, or type a custom verb.")
            return
        close_dialog()
        run_prefix_tool(entry, "winetricks", extra_args=verbs, parent=parent_dialog)

    def do_interactive():
        close_dialog()
        run_prefix_tool(entry, "winetricks", extra_args=[], parent=parent_dialog)

    dialog.protocol("WM_DELETE_WINDOW", close_dialog)
    ttk.Button(btn_row, text="Run Selected", style="Custom.TButton", width=14,
               command=do_run).grid(row=0, column=0, padx=4)
    ttk.Button(btn_row, text="Open Interactive Menu", style="Custom.TButton", width=18,
               command=do_interactive).grid(row=0, column=1, padx=4)
    ttk.Button(btn_row, text="Cancel", style="Custom.TButton", width=10,
               command=close_dialog).grid(row=0, column=2, padx=4)

    dialog.update_idletasks()
    x = parent_win.winfo_rootx() + (parent_win.winfo_width() - dialog.winfo_width()) // 2
    y = parent_win.winfo_rooty() + (parent_win.winfo_height() - dialog.winfo_height()) // 2
    dialog.geometry(f"+{x}+{y}")
    dialog.deiconify()
    dialog.transient(parent_win)
    dialog.grab_set()
    dialog.lift()
    dialog.focus_set()

def human_size(num_bytes):
    """Ubah jumlah byte jadi string yang gampang dibaca (mis. '482.3 MB'), dipakai untuk
    laporan progress backup/restore."""
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0 or unit == "TB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{num_bytes} B"

def show_loading_overlay(parent, title="Please Wait", message="Working..."):
    """Tampilkan jendela loading kecil (spinner tak-tentu/indeterminate) diatas 'parent',
    dipakai untuk operasi singkat-tapi-bisa-lama yang berjalan di background thread supaya
    pengguna tahu aplikasi sedang bekerja (bukan macet/freeze) - misalnya saat MEMBUKA arsip
    backup .tar.gz untuk membaca manifest-nya (tarfile mode 'r:gz' bersifat stream, jadi
    mencari satu member seperti manifest.json bisa berarti membaca seluruh isi arsip lebih
    dulu, yang untuk arsip besar bisa memakan waktu beberapa detik).

    Return (win, set_message, close): set_message(text) ganti teks status yang ditampilkan,
    close() menghentikan animasi & menutup jendelanya. Keduanya HARUS dipanggil dari main
    thread (mis. lewat root.after / polling queue), sama seperti append_line pada
    open_task_log_window."""
    win = tk.Toplevel(parent)
    win.withdraw()
    win.title(title)
    win.configure(bg=COLORS["primary"])
    win.resizable(False, False)
    win.transient(parent)
    win.protocol("WM_DELETE_WINDOW", lambda: None)

    frame = ttk.Frame(win, padding=(30, 24))
    frame.pack(fill=tk.BOTH, expand=True)

    msg_var = tk.StringVar(value=message)
    ttk.Label(frame, textvariable=msg_var, font=FONTS["normal"],
              justify=tk.CENTER, wraplength=320).pack(pady=(0, 14))

    bar = ttk.Progressbar(frame, mode="indeterminate", length=280)
    bar.pack()
    bar.start(12)

    win.update_idletasks()
    x = parent.winfo_rootx() + (parent.winfo_width() - win.winfo_width()) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - win.winfo_height()) // 2
    win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
    win.deiconify()
    win.grab_set()
    win.lift()

    def set_message(text):
        if win.winfo_exists():
            msg_var.set(text)

    closed = {"done": False}

    def close():
        if closed["done"]:
            return
        closed["done"] = True
        try:
            bar.stop()
        except Exception:
            pass
        try:
            win.grab_release()
        except tk.TclError:
            pass
        try:
            win.destroy()
        except tk.TclError:
            pass

    return win, set_message, close

def run_with_loading_overlay(parent, title, message, work_fn, on_done):
    """Jalankan work_fn() (tanpa argumen) di background thread sambil menampilkan
    show_loading_overlay() diatas 'parent'. Begitu selesai, jendela loading ditutup dan
    on_done(result, error) dipanggil di MAIN thread - result berisi apapun yang
    dikembalikan work_fn(), error berisi exception jika work_fn() melempar satu (None kalau
    sukses). Dipakai supaya operasi blocking singkat (mis. membuka & membaca manifest arsip
    backup) tidak membekukan UI tanpa indikasi apapun ke pengguna."""
    _, set_message, close_overlay = show_loading_overlay(parent, title=title, message=message)
    result_box = {}

    def worker():
        try:
            result_box["result"] = work_fn()
        except Exception as e:
            result_box["error"] = e

    threading.Thread(target=worker, daemon=True).start()

    def poll():
        if "result" in result_box or "error" in result_box:
            close_overlay()
            on_done(result_box.get("result"), result_box.get("error"))
        else:
            root.after(100, poll)

    root.after(100, poll)

def open_task_log_window(title, modal_parent=None, cancel_event=None, pause_event=None):
    """Buka jendela log sederhana untuk task Python di background thread (backup/restore) -
    beda dari open_log_window yang khusus untuk proses via pty (game/winetricks).
    Return (append_line, set_progress, win): append_line(text) aman dipanggil dari thread
    manapun untuk menambah satu baris ke jendela log ini (lewat queue, dipoll oleh main
    thread/Tk).

    cancel_event: threading.Event opsional. Kalau diisi, jendela ini menampilkan tombol
    "Cancel" yang, kalau ditekan (dan dikonfirmasi), akan men-set event tersebut - worker
    backup/restore yang berjalan (lihat run_backup_worker/run_restore_worker) mengecek event
    ini secara berkala dan berhenti secepatnya begitu terlihat ter-set.

    pause_event: threading.Event opsional. Kalau diisi, jendela ini menampilkan tombol
    "Pause"/"Resume" yang men-set/clear event tersebut - worker download Proton
    (download_url_pausable) menjeda downloadnya selama event ter-set. win.disable_pause()
    menonaktifkan tombol itu (dipanggil worker begitu download selesai dan masuk tahap ekstrak).

    Selama task masih berjalan (lihat win.mark_finished(), dipanggil otomatis oleh
    start_prefix_task_thread() saat worker selesai), tombol window manager/"Close" TIDAK
    menutup jendela ini - hanya menyembunyikannya (withdraw), supaya proses background tetap
    berjalan dan jendelanya bisa dimunculkan lagi lewat tombol "Show Logs" di Prefix
    Configuration Manager. Begitu task selesai (sukses/gagal/dibatalkan), jendela ini baru
    benar-benar bisa ditutup (destroy) seperti biasa.

    modal_parent: dialog Toplevel lain (mis. Prefix Configuration Manager) yang mungkin
    sedang memegang grab_set() aktif. Kalau diisi, grab itu DILEPAS dulu supaya jendela log
    ini (yang tidak modal) benar-benar bisa diklik - tanpa ini, tombol "Close"/"Cancel" (dan
    semua isi jendela log ini) tidak akan merespon klik sama sekali selama modal_parent masih
    memegang grab, memaksa pengguna menutupnya lewat window manager. Grab itu otomatis
    dikembalikan ke modal_parent begitu jendela log ini benar-benar ditutup (sesudah task
    selesai)."""
    if modal_parent is not None and modal_parent.winfo_exists():
        try:
            modal_parent.grab_release()
        except tk.TclError:
            pass

    win = tk.Toplevel(root)
    win.withdraw()
    win.title(title)
    win.configure(bg=COLORS["primary"])
    win.geometry("800x480")
    win.minsize(480, 300)

    frame = ttk.Frame(win, padding=10)
    frame.pack(fill=tk.BOTH, expand=True)

    progress_row = ttk.Frame(frame)
    progress_row.pack(fill=tk.X, pady=(0, 8))
    progress_bar = ttk.Progressbar(progress_row, mode="indeterminate")
    progress_bar.pack(side=tk.LEFT, fill=tk.X, expand=True)
    progress_bar.start(12)
    progress_pct_label = ttk.Label(progress_row, text="", font=FONTS["small"], width=6, anchor="e")
    progress_pct_label.pack(side=tk.LEFT, padx=(8, 0))

    text_frame = ttk.Frame(frame)
    text_frame.pack(fill=tk.BOTH, expand=True)
    scroll = ttk.Scrollbar(text_frame, orient=tk.VERTICAL)
    scroll.pack(side=tk.RIGHT, fill=tk.Y)
    text_widget = tk.Text(text_frame, wrap=tk.NONE, state="disabled",
                           bg=COLORS["text_background"], fg=COLORS["text"],
                           insertbackground=COLORS["text"], font=("Courier", 9),
                           yscrollcommand=scroll.set)
    text_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scroll.config(command=text_widget.yview)

    task_state = {"finished": False}

    def on_close():
        if not task_state["finished"]:
            # Task masih berjalan di background - sembunyikan saja jendelanya, jangan
            # dihancurkan, supaya progressnya tidak hilang dan bisa dibuka lagi lewat
            # "Show Logs" di Prefix Configuration Manager.
            win.withdraw()
            return
        win.destroy()
        if modal_parent is not None and modal_parent.winfo_exists():
            try:
                modal_parent.grab_set()
                modal_parent.lift()
                modal_parent.focus_set()
            except tk.TclError:
                pass

    close_row = ttk.Frame(frame)
    close_row.pack(pady=(8, 0))

    pause_btn = None
    if pause_event is not None:
        def on_pause_toggle():
            if task_state["finished"]:
                return
            if pause_event.is_set():
                pause_event.clear()
                pause_btn.config(text="Pause")
            else:
                pause_event.set()
                pause_btn.config(text="Resume")

        pause_btn = ttk.Button(close_row, text="Pause", command=on_pause_toggle,
                                style="Custom.TButton", width=12)
        pause_btn.pack(side=tk.LEFT, padx=(0, 6))

    def disable_pause():
        if pause_btn is not None and pause_btn.winfo_exists():
            pause_event.clear()
            pause_btn.config(state="disabled", text="Pause")

    cancel_btn = None
    if cancel_event is not None:
        def on_cancel():
            if task_state["finished"]:
                return
            if not messagebox.askyesno(
                "Cancel", "Stop the backup/restore process currently running?\n\n"
                          "Files already written so far may be left incomplete.", parent=win):
                return
            cancel_event.set()
            append_line("")
            append_line("Cancelling... please wait for the current step to stop.")
            cancel_btn.config(state="disabled", text="Cancelling...")

        cancel_btn = ttk.Button(close_row, text="Cancel", command=on_cancel,
                                 style="Custom.TButton", width=12)
        cancel_btn.pack(side=tk.LEFT, padx=(0, 6))

    close_btn = ttk.Button(close_row, text="Close", command=on_close, style="Custom.TButton", width=12)
    close_btn.pack(side=tk.LEFT)
    win.protocol("WM_DELETE_WINDOW", on_close)

    def mark_finished():
        """Dipanggil (dari main thread, lewat start_prefix_task_thread) begitu worker
        backup/restore yang memakai jendela ini selesai - sukses, gagal, ataupun
        dibatalkan. Menonaktifkan tombol Cancel dan membuat tombol Close/window-manager-close
        benar-benar menutup jendelanya lagi."""
        task_state["finished"] = True
        if cancel_btn is not None and cancel_btn.winfo_exists():
            cancel_btn.config(state="disabled")
        disable_pause()

    win.mark_finished = mark_finished
    win.disable_pause = disable_pause

    line_queue = queue.Queue()
    latest_progress = {"value": None}

    def poll():
        try:
            while True:
                line = line_queue.get_nowait()
                text_widget.config(state="normal")
                text_widget.insert(tk.END, line + "\n")
                text_widget.see(tk.END)
                text_widget.config(state="disabled")
        except queue.Empty:
            pass

        pct = latest_progress["value"]
        if pct is not None and win.winfo_exists():
            if str(progress_bar["mode"]) != "determinate":
                progress_bar.stop()
                progress_bar.config(mode="determinate", maximum=100)
            progress_bar["value"] = pct
            progress_pct_label.config(text=f"{pct:.0f}%")

        if win.winfo_exists():
            win.after(150, poll)

    poll()

    def append_line(text):
        line_queue.put(text)

    def set_progress(percent):
        """Update progress bar ke nilai persentase (0-100). Aman dipanggil dari thread
        manapun - nilainya dibaca & diterapkan ke widget oleh poll() di main thread."""
        latest_progress["value"] = max(0.0, min(100.0, percent))

    win.update_idletasks()
    x = root.winfo_rootx() + 60
    y = root.winfo_rooty() + 60
    win.geometry(f"+{x}+{y}")
    win.deiconify()

    return append_line, set_progress, win

def prefix_task_is_busy():
    """True kalau ada backup/restore prefix yang masih berjalan di sesi ini. Dipakai untuk
    membatasi hanya SATU backup/restore game/apps yang boleh berjalan dalam satu waktu."""
    return active_prefix_task is not None and not active_prefix_task.get("finished")

def start_prefix_task_thread(kind, label, target, args, win, cancel_event):
    """Jalankan worker backup/restore prefix (run_backup_worker/run_restore_worker) di
    background thread, sekaligus mendaftarkannya sebagai active_prefix_task global -
    dipakai oleh prefix_task_is_busy() untuk membatasi hanya 1 backup/restore per sesi, dan
    oleh tombol "Show Logs" di Prefix Configuration Manager untuk menampilkan lagi jendela
    log task yang sedang berjalan ini. Begitu worker selesai (sukses/gagal/dibatalkan),
    task ini otomatis ditandai selesai lagi (lewat root.after, dari main thread)."""
    global active_prefix_task
    active_prefix_task = {
        "kind": kind,
        "label": label,
        "cancel_event": cancel_event,
        "win": win,
        "finished": False,
    }

    def runner():
        try:
            target(*args)
        finally:
            root.after(0, _mark_prefix_task_finished, win)

    threading.Thread(target=runner, daemon=True).start()

def _mark_prefix_task_finished(win):
    """Callback (main thread, lewat root.after) yang dipanggil begitu worker backup/restore
    selesai - menandai active_prefix_task selesai (supaya backup/restore lain boleh mulai)
    dan memberitahu jendela log-nya (lihat mark_finished() di open_task_log_window) supaya
    tombol Cancel dinonaktifkan dan tombol Close kembali bisa menutup jendelanya."""
    global active_prefix_task
    if active_prefix_task is not None and active_prefix_task.get("win") is win:
        active_prefix_task["finished"] = True
    if win is not None and win.winfo_exists():
        finish_fn = getattr(win, "mark_finished", None)
        if finish_fn is not None:
            finish_fn()

def gather_prefix_backup_info(entry):
    """Kumpulkan info yang dibutuhkan untuk backup: folder tiap game (dari script .sh-nya)
    plus launch_options/comment (dari runner_config.json). Game yang folder-nya sudah tidak
    ada di disk dilaporkan lewat 'missing' supaya bisa diperingatkan ke pengguna SEBELUM
    backup jalan, bukan gagal ditengah proses.

    Juga dideteksi apakah folder game itu berada DIDALAM folder prefix-nya sendiri
    ('inside_prefix') - ini kasus umum untuk game yang di-install lewat installer Windows
    (mis. GOG) langsung ke drive_c prefix. Untuk game seperti ini, filenya SUDAH ikut
    kearsipkan lewat folder prefix, jadi tidak perlu (dan tidak boleh) diarsipkan lagi
    secara terpisah - itu hanya akan menggandakan ukuran backup 2x tanpa manfaat.

    Return list of dict: {"script_name", "folder_path" (Path atau None), "exe_path" (str atau
    None), "relative_exe" (str atau None - exe_path relatif ke folder_path kalau memungkinkan,
    dipakai supaya restore bisa membangun ulang path exe di lokasi baru), "inside_prefix" (bool),
    "relative_to_prefix" (str atau None - folder_path relatif ke prefix_path, kalau
    inside_prefix True), "launch_options", "comment", "missing" (bool), "icon_path" (Path atau
    None - lokasi file icon custom game ini kalau ada), "has_icon" (bool)}."""
    cfg = load_runner_config()
    prefix_path = Path(entry["prefix_path"])
    try:
        prefix_resolved = prefix_path.resolve()
    except Exception:
        prefix_resolved = prefix_path

    games_info = []
    for script_name in entry.get("games") or []:
        script_path = bashlaunch_dir / f"{script_name}.sh"
        folder_path_str = extract_folder_path_from_script(script_path)
        exe_path_str = extract_exe_path_from_script(script_path)
        folder_path = Path(folder_path_str) if folder_path_str else None

        relative_exe = None
        if folder_path and exe_path_str:
            try:
                relative_exe = str(Path(exe_path_str).resolve().relative_to(folder_path.resolve()))
            except Exception:
                relative_exe = None

        inside_prefix = False
        relative_to_prefix = None
        if folder_path and folder_path.is_dir():
            try:
                relative_to_prefix = str(folder_path.resolve().relative_to(prefix_resolved))
                inside_prefix = True
            except Exception:
                inside_prefix = False

        game_cfg = cfg.get(script_name, {})
        icon_path = icon_dir / f"{script_name}.png"
        games_info.append({
            "script_name": script_name,
            "folder_path": folder_path,
            "exe_path": exe_path_str,
            "relative_exe": relative_exe,
            "inside_prefix": inside_prefix,
            "relative_to_prefix": relative_to_prefix,
            "launch_options": game_cfg.get("launch_options", ""),
            "comment": game_cfg.get("comment", ""),
            "missing": not (folder_path and folder_path.is_dir()),
            "icon_path": icon_path,
            "has_icon": icon_path.is_file(),
        })
    return games_info

def compute_total_size(paths):
    """Jumlahkan ukuran (bytes) semua file didalam list path (file atau folder, folder
    di-walk rekursif). Dipakai SEBELUM proses tar.add()/extract() dimulai supaya progress
    backup/restore bisa ditampilkan sebagai persentase (bytes diproses / total bytes),
    bukan cuma jumlah file yang terus bertambah tanpa tahu berapa totalnya. Error per-file
    (mis. broken symlink) diabaikan supaya penghitungan total tidak gagal gara-gara satu file
    bermasalah - file itu tetap akan dicoba diarsipkan/diekstrak seperti biasa nantinya."""
    total = 0
    for p in paths:
        if p is None:
            continue
        p = Path(p)
        try:
            if p.is_file():
                total += p.stat().st_size
            elif p.is_dir():
                for root_dir, _dirs, files in os.walk(p):
                    for fname in files:
                        try:
                            total += (Path(root_dir) / fname).stat().st_size
                        except OSError:
                            pass
        except OSError:
            pass
    return total

def run_backup_worker(entry, games_info, dest_path, append_line, set_progress, cancel_event=None):
    """Berjalan di background thread: bikin satu arsip .tar.gz berisi folder prefix + folder
    tiap game yang masih ada di disk, plus manifest.json (dipakai run_restore_worker() untuk
    membangun ulang semuanya - prefix, script game, runner_config - dilain waktu/komputer).

    cancel_event: threading.Event opsional (lihat open_task_log_window) - dicek secara
    berkala (lewat progress_filter, dipanggil tarfile untuk tiap file yang diarsipkan) supaya
    proses ini bisa berhenti secepatnya begitu pengguna menekan tombol Cancel di jendela log."""
    prefix_path = Path(entry["prefix_path"])
    counters = {"count": 0, "bytes": 0}
    last_report = [0.0]

    append_line("Calculating total size to back up...")
    size_sources = [prefix_path]
    size_sources += [g["folder_path"] for g in games_info if not g["inside_prefix"]]
    size_sources += [g["icon_path"] for g in games_info if g["has_icon"]]
    total_bytes = compute_total_size(size_sources)
    append_line(f"Total size: {human_size(total_bytes)}" if total_bytes else "Total size: unknown")
    append_line("")

    def progress_filter(tarinfo):
        if cancel_event is not None and cancel_event.is_set():
            raise TaskCancelledError("Backup cancelled by user.")
        counters["count"] += 1
        if tarinfo.size and tarinfo.size > 0:
            counters["bytes"] += tarinfo.size
        now = time.time()
        if now - last_report[0] > 0.2 or counters["count"] <= 3:
            append_line(f"[{counters['count']:>6} files, {human_size(counters['bytes'])}] {tarinfo.name}")
            last_report[0] = now
            if total_bytes > 0:
                set_progress(min(counters["bytes"], total_bytes) / total_bytes * 100)
        return tarinfo

    manifest = {
        "format_version": 2,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "runner": entry["runner"],
        "prefix_code": entry["prefix_code"],
        "games": [
            {
                "script_name": g["script_name"],
                "relative_exe": g["relative_exe"],
                "exe_path": g["exe_path"],
                "launch_options": g["launch_options"],
                "comment": g["comment"],
                "inside_prefix": g["inside_prefix"],
                "relative_to_prefix": g["relative_to_prefix"],
                "has_icon": g["has_icon"],
            }
            for g in games_info
        ],
    }

    tmp_manifest_path = None
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        tmp_fd, tmp_manifest_name = tempfile.mkstemp(prefix="wlm_manifest_", suffix=".json")
        os.close(tmp_fd)
        tmp_manifest_path = Path(tmp_manifest_name)
        tmp_manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        append_line("Archiving prefix files...")
        with tarfile.open(dest_path, "w:gz") as tar:
            tar.add(str(prefix_path), arcname="prefix", filter=progress_filter)
            for g in games_info:
                if g["inside_prefix"]:
                    append_line(f"Skipping separate archive for '{g['script_name']}' "
                                f"(already included inside the prefix folder).")
                    continue
                append_line(f"Archiving game files: {g['script_name']}...")
                tar.add(str(g["folder_path"]), arcname=f"games/{g['script_name']}", filter=progress_filter)
            for g in games_info:
                if g["has_icon"]:
                    append_line(f"Archiving icon for '{g['script_name']}'...")
                    tar.add(str(g["icon_path"]), arcname=f"icons/{g['script_name']}.png", filter=progress_filter)
            tar.add(str(tmp_manifest_path), arcname="manifest.json")

        set_progress(100)
        append_line("")
        append_line(f"Done. {counters['count']} files, {human_size(counters['bytes'])} total.")
        append_line(f"Backup saved to: {dest_path}")
        root.after(0, lambda: safe_status_config(
            text=f"Backup of '{entry['prefix_code']}' saved to {dest_path}", fg=COLORS["success"]))
    except TaskCancelledError:
        append_line("")
        append_line("Backup cancelled by user.")
        try:
            if dest_path.exists():
                dest_path.unlink()
                append_line(f"Removed incomplete backup file: {dest_path}")
        except Exception:
            pass
        root.after(0, lambda: safe_status_config(
            text=f"Backup of '{entry['prefix_code']}' cancelled.", fg=COLORS["warning"]))
    except Exception as e:
        err = str(e)
        append_line("")
        append_line(f"ERROR: {err}")
        root.after(0, lambda: messagebox.showerror("Backup Failed", f"Backup failed:\n{err}"))
        root.after(0, lambda: safe_status_config(text=f"Backup failed: {err}", fg=COLORS["danger"]))
    finally:
        if tmp_manifest_path is not None:
            try:
                tmp_manifest_path.unlink()
            except Exception:
                pass

def open_backup_prefix_dialog(entry, parent_dialog=None):
    """Minta konfirmasi & lokasi file backup, lalu jalankan proses backup (prefix + folder
    game-game yang memakainya) di background thread dengan jendela log real-time (progress
    pengarsipan file per file kelihatan langsung)."""
    parent_win = parent_dialog if (parent_dialog is not None and parent_dialog.winfo_exists()) else root

    if prefix_task_is_busy():
        messagebox.showwarning(
            "Backup/Restore Busy",
            f"A backup/restore is already running ({active_prefix_task['label']}).\n\n"
            "Only one prefix/game backup or restore can run at a time. Use \"Show Logs\" "
            "in the Prefix Configuration Manager to check its progress or cancel it first.",
            parent=parent_win)
        return

    prefix_path = Path(entry["prefix_path"])
    if not prefix_path.is_dir():
        messagebox.showerror("Error", f"Prefix folder not found on disk:\n{prefix_path}", parent=parent_win)
        return

    games_info = gather_prefix_backup_info(entry)

    lines = [f"Prefix: {entry['prefix_code']} ({RUNNER_DISPLAY_NAMES.get(entry['runner'], entry['runner'])})",
             f"Location: {prefix_path}", ""]
    if games_info:
        lines.append("Game(s) that will be included:")
        for g in games_info:
            if g["missing"]:
                tag = "  (folder not found on disk - will be SKIPPED)"
            elif g["inside_prefix"]:
                tag = "  (already inside the prefix - no extra space needed)"
            else:
                tag = "  (backed up separately)"
            lines.append(f"  - {g['script_name']}{tag}")
    else:
        lines.append("No game is currently linked to this prefix - only the prefix itself will be backed up.")
    lines.append("")
    lines.append("This can take a while and produce a large file depending on the game size. Continue?")

    if not messagebox.askyesno("Confirm Backup", "\n".join(lines), parent=parent_win):
        return

    default_name = f"backup_{entry['prefix_code']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.tar.gz"
    dest = filedialog.asksaveasfilename(
        title="Save Backup As",
        initialfile=default_name,
        defaultextension=".tar.gz",
        filetypes=[("Backup Archive", "*.tar.gz"), ("All Files", "*.*")],
        parent=parent_win
    )
    if not dest:
        return
    dest_path = Path(dest)

    games_info = [g for g in games_info if not g["missing"]]

    modal_parent = parent_dialog if (parent_dialog is not None and parent_dialog.winfo_exists()) else None
    cancel_event = threading.Event()
    append_line, set_progress, win = open_task_log_window(f"Backup - {entry['prefix_code']}",
                                                            modal_parent=modal_parent,
                                                            cancel_event=cancel_event)
    append_line(f"Starting backup of prefix '{entry['prefix_code']}'...")
    append_line(f"Destination: {dest_path}")
    append_line("")

    start_prefix_task_thread("backup", entry["prefix_code"], run_backup_worker,
                              (entry, games_info, dest_path, append_line, set_progress, cancel_event),
                              win, cancel_event)

def run_restore_worker(archive_path, manifest, runner_key, prefix_code, target_prefix_path,
                        games_dest_base, append_line, set_progress, total_size=0, cancel_event=None):
    """Berjalan di background thread: ekstrak folder prefix & folder tiap game dari arsip
    backup, lalu daftarkan sebagai prefix/game baru di launcher ini (script .sh baru,
    runner_config.json, prefix_registry.json).

    cancel_event: threading.Event opsional (lihat open_task_log_window) - dicek secara
    berkala (lewat extract_members, per file yang diekstrak) supaya proses ini bisa berhenti
    secepatnya begitu pengguna menekan tombol Cancel di jendela log. Kalau dibatalkan
    ditengah ekstraksi, prefix/game TIDAK didaftarkan ke launcher ini (langkah
    record_prefix_usage/save_runner_config baru terjadi setelah ekstraksi selesai penuh) -
    hanya file yang sudah terlanjur diekstrak yang tertinggal di disk.

    total_size: total bytes yang akan diekstrak (dihitung sebelumnya saat manifest dibaca,
    lihat open_restore_backup_dialog), dipakai untuk menampilkan progress bar sebagai
    persentase. 0/tidak diketahui berarti progress bar tetap dalam mode indeterminate.

    Versi Proton (proton_path) SENGAJA tidak diikutkan dari mesin lama - proton_path itu
    hanya valid dikomputer asalnya. Nanti akan ditanya otomatis (lewat pick_proton_build_dialog,
    fitur yang sudah ada) begitu prefix ini pertama kali dipakai lewat winecfg/explorer/
    uninstaller/winetricks/PLAY di komputer ini."""
    games = manifest.get("games") or []
    extracted_count = [0]
    extracted_bytes = [0]
    last_report = [0.0]

    def extract_members(tar, member_prefix, dest_dir):
        matched = [m for m in tar.getmembers() if m.name.startswith(member_prefix)]
        for m in matched:
            if cancel_event is not None and cancel_event.is_set():
                raise TaskCancelledError("Restore cancelled by user.")
            rel = m.name[len(member_prefix):]
            if not rel:
                continue
            extracted_count[0] += 1
            if m.size:
                extracted_bytes[0] += m.size
            now = time.time()
            if now - last_report[0] > 0.2 or extracted_count[0] <= 3:
                append_line(f"[{extracted_count[0]:>6}] {rel}")
                last_report[0] = now
                if total_size > 0:
                    set_progress(min(extracted_bytes[0], total_size) / total_size * 100)
            m.name = rel
            try:
                tar.extract(m, path=str(dest_dir), filter="fully_trusted")
            except TypeError:
                tar.extract(m, path=str(dest_dir))

    try:
        target_prefix_path.mkdir(parents=True, exist_ok=True)

        external_games = [g for g in games if not g.get("inside_prefix")]
        inside_games = [g for g in games if g.get("inside_prefix")]

        with tarfile.open(archive_path, "r:gz") as tar:
            append_line("Extracting prefix files...")
            extract_members(tar, "prefix/", target_prefix_path)

            game_dest_map = {}
            if external_games and games_dest_base:
                games_dest_base.mkdir(parents=True, exist_ok=True)
                for g in external_games:
                    script_name = g["script_name"]
                    game_dir = games_dest_base / script_name
                    append_line(f"Extracting game files: {script_name}...")
                    extract_members(tar, f"games/{script_name}/", game_dir)
                    game_dest_map[script_name] = game_dir

            for g in inside_games:
                append_line(f"'{g['script_name']}' is already inside the restored prefix "
                            f"- no separate extraction needed.")
                rel_to_prefix = g.get("relative_to_prefix") or ""
                game_dest_map[g["script_name"]] = (target_prefix_path / rel_to_prefix) if rel_to_prefix else target_prefix_path

            for g in games:
                if not g.get("has_icon"):
                    continue
                script_name = g["script_name"]
                member_name = f"icons/{script_name}.png"
                try:
                    member = tar.getmember(member_name)
                except KeyError:
                    continue
                append_line(f"Extracting icon for '{script_name}'...")
                icon_dir.mkdir(parents=True, exist_ok=True)
                try:
                    with tar.extractfile(member) as src, open(icon_dir / f"{script_name}.png", "wb") as dst:
                        dst.write(src.read())
                except Exception as e:
                    append_line(f"WARNING: could not restore icon for '{script_name}': {e}")

        record_prefix_usage(runner_key, prefix_code, target_prefix_path,
                             proton_name=None, proton_path=None)

        runner_cfg = load_runner_config()
        for g in games:
            script_name = g["script_name"]
            game_dir = game_dest_map.get(script_name)
            if game_dir is None:
                continue

            relative_exe = g.get("relative_exe")
            if relative_exe:
                exe_path = str(game_dir / relative_exe)
            else:
                exe_path = g.get("exe_path") or ""
                append_line(f"WARNING: could not determine the .exe location for "
                            f"'{script_name}' automatically - please check/fix it manually.")

            choice = {
                "runner": runner_key,
                "proton_name": None,
                "proton_path": "",
                "prefix_code": prefix_code,
                "prefix_path": str(target_prefix_path),
                "launch_options": g.get("launch_options", ""),
                "comment": g.get("comment", ""),
            }
            runner_cfg[script_name] = choice

            script_path = bashlaunch_dir / f"{script_name}.sh"
            script_content = build_script_content(str(game_dir), exe_path, choice)
            script_path.write_text(script_content)
            try:
                script_path.chmod(0o755)
            except Exception:
                pass

        save_runner_config(runner_cfg)

        set_progress(100)
        append_line("")
        append_line("Restore completed successfully.")
        if runner_key != "wine":
            append_line("Note: this prefix's Proton build was not carried over from the old machine -")
            append_line("you'll be asked to pick a locally installed Proton build the first time you")
            append_line("use Winecfg/Explorer/Winetricks/PLAY for it.")
        root.after(0, lambda: safe_status_config(
            text=f"Restore of '{prefix_code}' completed.", fg=COLORS["success"]))
        root.after(0, update_script_list)
    except TaskCancelledError:
        append_line("")
        append_line("Restore cancelled by user. Files already extracted so far remain on disk, "
                    "but no game/prefix was registered in the launcher since the process did "
                    "not finish.")
        root.after(0, lambda: safe_status_config(
            text=f"Restore of '{prefix_code}' cancelled.", fg=COLORS["warning"]))
    except Exception as e:
        err = str(e)
        append_line("")
        append_line(f"ERROR: {err}")
        root.after(0, lambda: messagebox.showerror("Restore Failed", f"Restore failed:\n{err}"))
        root.after(0, lambda: safe_status_config(text=f"Restore failed: {err}", fg=COLORS["danger"]))

def open_restore_backup_dialog(parent_dialog=None):
    """Pilih file backup (.tar.gz), baca manifest-nya, lalu tampilkan dialog untuk memilih
    kode/lokasi tujuan prefix & folder game sebelum benar-benar mengekstrak & mendaftarkannya
    sebagai prefix/game baru di launcher ini (lihat run_restore_worker)."""
    parent_win = parent_dialog if (parent_dialog is not None and parent_dialog.winfo_exists()) else root

    if prefix_task_is_busy():
        messagebox.showwarning(
            "Backup/Restore Busy",
            f"A backup/restore is already running ({active_prefix_task['label']}).\n\n"
            "Only one prefix/game backup or restore can run at a time. Use \"Show Logs\" "
            "in the Prefix Configuration Manager to check its progress or cancel it first.",
            parent=parent_win)
        return

    archive_path = filedialog.askopenfilename(
        title="Select Backup Archive",
        filetypes=[("Backup Archive", "*.tar.gz *.tgz"), ("All Files", "*.*")],
        parent=parent_win
    )
    if not archive_path:
        return
    archive_path = Path(archive_path)

    def read_archive():
        with tarfile.open(archive_path, "r:gz") as tar:
            manifest_member = tar.extractfile("manifest.json")
            if manifest_member is None:
                raise ValueError("manifest.json not found inside the archive.")
            manifest_data = json.loads(manifest_member.read().decode("utf-8"))
            total_size = sum(m.size for m in tar.getmembers()
                              if m.isfile() and (m.name.startswith("prefix/")
                                                  or m.name.startswith("games/")))
        return manifest_data, total_size

    def on_archive_read(result, error):
        if error is not None:
            messagebox.showerror("Error", f"Failed to read backup archive:\n{error}", parent=parent_win)
            return
        manifest, total_size = result
        show_restore_options_dialog(manifest, total_size)

    run_with_loading_overlay(parent_win, title="Opening Backup",
                              message=f"Reading {archive_path.name}...",
                              work_fn=read_archive, on_done=on_archive_read)

    def show_restore_options_dialog(manifest, total_size):
        """Dipanggil sesudah manifest arsip backup berhasil dibaca (lihat
        open_restore_backup_dialog/read_archive) - tampilkan dialog pilihan tujuan restore lalu,
        kalau dikonfirmasi, jalankan proses ekstraksi sesungguhnya lewat run_restore_worker."""
        runner_key = manifest.get("runner")
        if runner_key not in ("wine", "protonge", "protoncachyos"):
            messagebox.showerror("Error", "This backup file's format is not recognized/supported.", parent=parent_win)
            return

        orig_prefix_code = manifest.get("prefix_code") or "RestoredPrefix"
        games = manifest.get("games") or []
        external_games = [g for g in games if not g.get("inside_prefix")]
        inside_games = [g for g in games if g.get("inside_prefix")]

        known_codes = set()
        for cfg in load_runner_config().values():
            if cfg.get("prefix_code"):
                known_codes.add(cfg["prefix_code"])
        for reg_entry in load_prefix_registry().values():
            if reg_entry.get("prefix_code"):
                known_codes.add(reg_entry["prefix_code"])
        suggested_code = orig_prefix_code if orig_prefix_code not in known_codes else generate_next_prefix_code(runner_key)

        parent_had_grab = parent_dialog is not None and parent_dialog.winfo_exists()
        if parent_had_grab:
            parent_dialog.grab_release()

        dialog = tk.Toplevel(parent_win)
        dialog.withdraw()
        dialog.title("Restore Backup")
        dialog.configure(bg=COLORS["primary"])
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text=f"Archive: {archive_path.name}", font=FONTS["subtitle"]).pack(anchor="w")
        ttk.Label(frame, text=f"Runner: {RUNNER_DISPLAY_NAMES.get(runner_key, runner_key)}",
                  font=FONTS["normal"]).pack(anchor="w", pady=(4, 0))

        if games:
            desc_lines = []
            if inside_games:
                desc_lines.append("Included inside the prefix (no extra folder needed): "
                                   + ", ".join(g["script_name"] for g in inside_games))
            if external_games:
                desc_lines.append("Stored separately, needs its own destination folder: "
                                   + ", ".join(g["script_name"] for g in external_games))
            ttk.Label(frame, text="\n".join(desc_lines), font=FONTS["small"],
                      wraplength=560, justify=tk.LEFT).pack(anchor="w", pady=(4, 10))
        else:
            ttk.Label(frame, text="This backup contains only the prefix (no game linked).",
                      font=FONTS["small"]).pack(anchor="w", pady=(4, 10))

        ttk.Label(frame, text="Prefix code (folder name) for the restored prefix:",
                  font=FONTS["normal"]).pack(anchor="w")
        code_entry = ttk.Entry(frame, width=40, font=FONTS["normal"])
        code_entry.insert(0, suggested_code)
        code_entry.pack(fill=tk.X, pady=(2, 2))
        if suggested_code != orig_prefix_code:
            ttk.Label(frame, text=f"Original code was '{orig_prefix_code}', already used on this machine - "
                                  f"'{suggested_code}' suggested instead. You can still change it.",
                      font=FONTS["small"], wraplength=560, justify=tk.LEFT).pack(anchor="w", pady=(0, 8))
        else:
            ttk.Frame(frame, height=8).pack()

        default_root = {"wine": wine_prefix_root, "protonge": protonge_prefix_root,
                         "protoncachyos": protoncachyos_prefix_root}[runner_key]
        loc_cfg = load_prefix_location_config()
        default_root = Path(loc_cfg.get(runner_key) or default_root)

        ttk.Label(frame, text="Restore prefix into folder:", font=FONTS["normal"]).pack(anchor="w")
        prefix_dest_row = ttk.Frame(frame)
        prefix_dest_row.pack(fill=tk.X, pady=(2, 10))
        prefix_dest_var = tk.StringVar(value=str(default_root))
        ttk.Entry(prefix_dest_row, textvariable=prefix_dest_var, font=FONTS["normal"]).pack(
            side=tk.LEFT, fill=tk.X, expand=True)

        def browse_prefix_dest():
            chosen = filedialog.askdirectory(title="Choose folder to restore the prefix into",
                                              initialdir=prefix_dest_var.get(), parent=dialog)
            if chosen:
                prefix_dest_var.set(chosen)

        ttk.Button(prefix_dest_row, text="Browse...", style="Custom.TButton", width=10,
                   command=browse_prefix_dest).pack(side=tk.LEFT, padx=(6, 0))

        games_dest_var = tk.StringVar(value=str(Path.home() / "RestoredGames"))
        if external_games:
            ttk.Label(frame, text="Restore game file(s) into folder (each game gets its own subfolder):",
                      font=FONTS["normal"]).pack(anchor="w")
            games_dest_row = ttk.Frame(frame)
            games_dest_row.pack(fill=tk.X, pady=(2, 10))
            ttk.Entry(games_dest_row, textvariable=games_dest_var, font=FONTS["normal"]).pack(
                side=tk.LEFT, fill=tk.X, expand=True)

            def browse_games_dest():
                chosen = filedialog.askdirectory(title="Choose folder to restore game file(s) into",
                                                  initialdir=games_dest_var.get(), parent=dialog)
                if chosen:
                    games_dest_var.set(chosen)

            ttk.Button(games_dest_row, text="Browse...", style="Custom.TButton", width=10,
                       command=browse_games_dest).pack(side=tk.LEFT, padx=(6, 0))

        btn_row = ttk.Frame(frame)
        btn_row.pack(pady=(6, 0))

        def close_dialog(reacquire_parent_grab=True):
            dialog.destroy()
            if reacquire_parent_grab and parent_had_grab and parent_dialog.winfo_exists():
                parent_dialog.grab_set()
                parent_dialog.lift()
                parent_dialog.focus_set()

        def do_restore():
            if prefix_task_is_busy():
                messagebox.showwarning(
                    "Backup/Restore Busy",
                    f"A backup/restore is already running ({active_prefix_task['label']}).\n\n"
                    "Only one prefix/game backup or restore can run at a time. Use \"Show Logs\" "
                    "in the Prefix Configuration Manager to check its progress or cancel it first.",
                    parent=dialog)
                return
            prefix_code = code_entry.get().strip()
            if not prefix_code:
                messagebox.showinfo("Info", "Prefix code cannot be empty.", parent=dialog)
                return
            prefix_dest_base_str = prefix_dest_var.get().strip()
            if not prefix_dest_base_str:
                messagebox.showinfo("Info", "Please choose a destination folder for the prefix.", parent=dialog)
                return
            target_prefix_path = Path(prefix_dest_base_str) / prefix_code

            conflicting_scripts = [g["script_name"] for g in games
                                    if (bashlaunch_dir / f"{g['script_name']}.sh").exists()]
            if conflicting_scripts and not messagebox.askyesno(
                "Replace Existing Game(s)?",
                "The following game(s) already exist in this launcher (their launch script &\n"
                "runner settings will be REPLACED by this backup):\n\n"
                + "\n".join(f"  - {s}" for s in conflicting_scripts) + "\n\nReplace them?",
                parent=dialog
            ):
                return

            if target_prefix_path.exists() and any(target_prefix_path.iterdir()):
                if not messagebox.askyesno(
                    "Replace Existing Prefix?",
                    f"A prefix folder already exists at:\n{target_prefix_path}\n\n"
                    "Its contents will be REPLACED/overwritten by the files from this backup.\n\n"
                    "Do you want to replace it?",
                    parent=dialog
                ):
                    return

            games_dest_base = None
            if external_games:
                games_dest_base_str = games_dest_var.get().strip()
                if not games_dest_base_str:
                    messagebox.showinfo("Info", "Please choose a destination folder for the game file(s).",
                                         parent=dialog)
                    return
                games_dest_base = Path(games_dest_base_str)

                conflicting_folders = [g["script_name"] for g in external_games
                                        if (games_dest_base / g["script_name"]).exists()
                                        and any((games_dest_base / g["script_name"]).iterdir())]
                if conflicting_folders and not messagebox.askyesno(
                    "Replace Existing Game Folder(s)?",
                    "The following game folder(s) already exist at the chosen destination and are\n"
                    "not empty. Their contents will be REPLACED/overwritten by this backup:\n\n"
                    + "\n".join(f"  - {games_dest_base / s}" for s in conflicting_folders)
                    + "\n\nDo you want to replace them?",
                    parent=dialog
                ):
                    return

            close_dialog(reacquire_parent_grab=False)

            cancel_event = threading.Event()
            append_line, set_progress, win = open_task_log_window(
                f"Restore - {prefix_code}",
                modal_parent=(parent_dialog if parent_had_grab else None),
                cancel_event=cancel_event
            )
            append_line(f"Starting restore from: {archive_path}")
            append_line(f"Prefix destination: {target_prefix_path}")
            if games_dest_base:
                append_line(f"Game(s) destination base folder: {games_dest_base}")
            if total_size:
                append_line(f"Total size to extract: {human_size(total_size)}")
            append_line("")

            start_prefix_task_thread("restore", prefix_code, run_restore_worker,
                                      (archive_path, manifest, runner_key, prefix_code,
                                       target_prefix_path, games_dest_base, append_line,
                                       set_progress, total_size, cancel_event),
                                      win, cancel_event)

        ttk.Button(btn_row, text="Restore", style="Custom.TButton", width=12,
                   command=do_restore).grid(row=0, column=0, padx=4)
        ttk.Button(btn_row, text="Cancel", style="Custom.TButton", width=12,
                   command=close_dialog).grid(row=0, column=1, padx=4)

        dialog.protocol("WM_DELETE_WINDOW", close_dialog)
        dialog.update_idletasks()
        x = parent_win.winfo_rootx() + (parent_win.winfo_width() - dialog.winfo_width()) // 2
        y = parent_win.winfo_rooty() + (parent_win.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{x}+{y}")
        dialog.deiconify()
        dialog.transient(parent_win)
        dialog.grab_set()
        dialog.lift()
        dialog.focus_set()

RUNNER_DISPLAY_NAMES = {"wine": "Wine", "protonge": "Proton GE", "protoncachyos": "Proton-CachyOS"}

def remove_prefix_and_games_dialog(entry, parent_dialog=None):
    """Hapus SEPENUHNYA sebuah prefix: folder prefix di disk + catatan registry-nya, beserta
    SEMUA game/aplikasi launcher yang memakainya (script .sh, icon, entry runner_config.json).

    PENTING: kalau file game itu ternyata berada DIDALAM folder prefix (mis. game GOG yang
    terinstall ke drive_c prefix), file game tersebut IKUT TERHAPUS - karena memang jadi
    bagian dari folder prefix yang dihapus. Game yang file-nya berada DILUAR prefix tidak
    ikut kehapus filenya - hanya entry launcher-nya (script/config) saja yang dibersihkan,
    persis seperti perilaku Remove biasa. Pengguna diberi tahu secara eksplisit mana yang
    mana SEBELUM apapun benar-benar dihapus.

    Return True kalau penghapusan berhasil dilakukan, False kalau dibatalkan/gagal."""
    parent_win = parent_dialog if (parent_dialog is not None and parent_dialog.winfo_exists()) else root

    prefix_path = Path(entry["prefix_path"])
    games = entry.get("games") or []

    try:
        prefix_resolved = prefix_path.resolve()
    except Exception:
        prefix_resolved = prefix_path

    inside_names, outside_names = [], []
    for script_name in games:
        script_path = bashlaunch_dir / f"{script_name}.sh"
        folder_str = extract_folder_path_from_script(script_path)
        is_inside = False
        if folder_str:
            try:
                Path(folder_str).resolve().relative_to(prefix_resolved)
                is_inside = True
            except Exception:
                is_inside = False
        (inside_names if is_inside else outside_names).append(script_name)

    lines = [f"Prefix: {entry['prefix_code']} ({RUNNER_DISPLAY_NAMES.get(entry['runner'], entry['runner'])})",
             f"Location: {prefix_path}", ""]
    if games:
        lines.append("The following game(s)/app(s) using this prefix will be removed from the launcher:")
        for name in games:
            if name in inside_names:
                tag = "  (installed inside this prefix - its files will also be DELETED)"
            else:
                tag = "  (files elsewhere are kept - only removed from the launcher)"
            lines.append(f"  - {name}{tag}")
        lines.append("")
    else:
        lines.append("No game/app is currently linked to this prefix.")
        lines.append("")
    lines.append("This will permanently delete the prefix folder from disk. This cannot be undone.\n\nContinue?")

    if not messagebox.askyesno("Remove Prefix & Game(s)?", "\n".join(lines), parent=parent_win):
        return False

    if inside_names:
        if not messagebox.askyesno(
            "Confirm File Deletion",
            "This will PERMANENTLY DELETE the installed files for:\n\n"
            + "\n".join(f"  - {n}" for n in inside_names)
            + "\n\n(they are installed inside this prefix's folder, including any save data kept "
            "there). Are you absolutely sure?",
            parent=parent_win
        ):
            return False

    try:
        if prefix_path.exists():
            shutil.rmtree(prefix_path)
    except Exception as e:
        messagebox.showerror("Error", f"Failed to delete prefix folder:\n{str(e)}", parent=parent_win)
        return False

    runner_cfg = load_runner_config()
    for script_name in games:
        script_path = bashlaunch_dir / f"{script_name}.sh"
        icon_path = icon_dir / f"{script_name}.png"
        script_path.unlink(missing_ok=True)
        icon_path.unlink(missing_ok=True)
        if script_name in runner_cfg:
            del runner_cfg[script_name]
    save_runner_config(runner_cfg)

    reg = load_prefix_registry()
    reg.pop(f"{entry['runner']}:{entry['prefix_code']}", None)
    save_prefix_registry(reg)

    safe_status_config(text=f"Removed prefix '{entry['prefix_code']}' and {len(games)} game(s)/app(s).",
                         fg=COLORS["warning"])
    update_script_list()
    return True

def open_prefix_manager_dialog():
    """Dialog utama untuk memanajemen konfigurasi per-prefix: menampilkan semua prefix Wine/
    Proton GE/Proton-CachyOS yang sudah pernah dibuat (beserta nama game/aplikasi yang
    memakainya), lalu memungkinkan membuka winecfg, Wine Explorer, uninstaller Windows, atau
    Winetricks KHUSUS untuk prefix yang dipilih saja. Jendelanya bisa di-resize bebas, dan ada
    scrollbar horizontal untuk kolom yang kepotong. Baris tombol di bawah selalu terlihat (yang
    mengecil adalah daftarnya), dan ukuran jendela yang terakhir diatur diingat ke sesi berikutnya."""
    prefixes = list_all_known_prefixes()

    dialog = tk.Toplevel(root)
    dialog.withdraw()
    dialog.title("Prefix Configuration Manager")
    dialog.configure(bg=COLORS["primary"])
    dialog.resizable(True, True)

    frame = ttk.Frame(dialog, padding=15)
    frame.pack(fill=tk.BOTH, expand=True)

    top_bar = ttk.Frame(frame)
    top_bar.pack(fill=tk.X, pady=(0, 8))

    # Tombol dipasang DULU (side=RIGHT) lalu teks - kalau jendela sempit, teksnya yang terpotong/wrap,
    # bukan tombol Refresh / Show Logs.
    refresh_btn = ttk.Button(top_bar, text="Refresh", style="Custom.TButton", width=12)
    refresh_btn.pack(side=tk.RIGHT, padx=(0, 6))

    show_logs_btn = ttk.Button(top_bar, text="Show Logs", style="Custom.TButton", width=12)
    show_logs_btn.pack(side=tk.RIGHT, padx=(0, 6))

    top_label = ttk.Label(top_bar, text="Select a prefix to configure (winecfg / explorer / uninstaller / winetricks):",
                          font=FONTS["normal"], justify=tk.LEFT)
    top_label.pack(side=tk.LEFT, anchor="w")
    top_bar.bind("<Configure>",
                 lambda e: top_label.config(wraplength=max(160, e.width - 260)), add="+")

    # Baris tombol bawah dipasang SEBELUM daftar (side=BOTTOM). Packer memberi ruang sesuai urutan
    # pemasangan, jadi widget yang dipasang belakangan yang terpotong bila ruang kurang: dengan urutan
    # ini, daftar (yang expand) yang mengecil - tombol tidak pernah tertutup.
    bottom = ttk.Frame(frame)
    bottom.pack(side=tk.BOTTOM, fill=tk.X)

    list_frame = ttk.Frame(frame)
    list_frame.pack(fill=tk.BOTH, expand=True)
    list_frame.rowconfigure(0, weight=1)
    list_frame.columnconfigure(0, weight=1)

    vscroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL)
    vscroll.grid(row=0, column=1, sticky="ns")
    hscroll = ttk.Scrollbar(list_frame, orient=tk.HORIZONTAL)
    hscroll.grid(row=1, column=0, sticky="ew")

    prefix_tree = ttk.Treeview(list_frame,
                                columns=("Runner", "Prefix", "Games", "Proton", "Path"),
                                show="headings",
                                yscrollcommand=vscroll.set,
                                xscrollcommand=hscroll.set,
                                selectmode="browse",
                                height=4)     # tinggi minimum; tingginya mengikuti ukuran jendela
    prefix_tree.grid(row=0, column=0, sticky="nsew")
    vscroll.config(command=prefix_tree.yview)
    hscroll.config(command=prefix_tree.xview)

    prefix_tree.heading("Runner", text="Runner", anchor="w")
    prefix_tree.heading("Prefix", text="Prefix Code", anchor="w")
    prefix_tree.heading("Games", text="Game(s)", anchor="w")
    prefix_tree.heading("Proton", text="Proton Version", anchor="w")
    prefix_tree.heading("Path", text="Location", anchor="w")
    prefix_tree.column("Runner", width=100, anchor="w", stretch=False)
    prefix_tree.column("Prefix", width=100, anchor="w", stretch=False)
    prefix_tree.column("Games", width=220, anchor="w", stretch=False)
    prefix_tree.column("Proton", width=170, anchor="w", stretch=False)
    prefix_tree.column("Path", width=320, anchor="w", stretch=True)

    empty_label = ttk.Label(frame,
                             text="No prefixes found yet. A prefix is created the first time\n"
                                  "you PLAY or run APPS SETUP for a game via Wine/Proton.",
                             font=FONTS["small"], justify=tk.LEFT)

    def refresh_prefix_list():
        """Muat ulang daftar prefix dari disk (runner_config.json + prefix_registry.json)
        dan isi ulang treeview - dipakai tombol Refresh, dan juga saat dialog ini pertama
        dibuka. Berguna kalau ada prefix baru dibuat (mis. lewat PLAY di jendela utama)
        SELAGI dialog Prefix Configuration Manager ini masih terbuka."""
        selected_iid = prefix_tree.selection()[0] if prefix_tree.selection() else None
        prefix_tree.delete(*prefix_tree.get_children())
        prefixes[:] = list_all_known_prefixes()
        for entry in prefixes:
            games_display = ", ".join(entry.get("games") or []) or "-"
            prefix_tree.insert("", tk.END, iid=f"{entry['runner']}:{entry['prefix_code']}",
                                values=(RUNNER_DISPLAY_NAMES.get(entry["runner"], entry["runner"]),
                                        entry["prefix_code"],
                                        games_display,
                                        entry.get("proton_name") or "-",
                                        entry["prefix_path"]))
        if prefixes:
            empty_label.pack_forget()
        else:
            empty_label.pack(anchor="w", pady=(0, 8), after=top_bar)
        if selected_iid is not None and prefix_tree.exists(selected_iid):
            prefix_tree.selection_set(selected_iid)

    refresh_prefix_list()
    refresh_btn.config(command=refresh_prefix_list)

    def get_selected_entry():
        sel = prefix_tree.selection()
        if not sel:
            messagebox.showinfo("Info", "Select a prefix from the list first.", parent=dialog)
            return None
        runner_key, prefix_code = sel[0].split(":", 1)
        for entry in prefixes:
            if entry["runner"] == runner_key and entry["prefix_code"] == prefix_code:
                return entry
        return None

    btn_row = ttk.Frame(bottom)
    btn_row.pack(pady=(10, 0))

    def do_winecfg():
        entry = get_selected_entry()
        if entry:
            run_prefix_tool(entry, "winecfg", parent=dialog)

    def do_explorer():
        entry = get_selected_entry()
        if entry:
            run_prefix_tool(entry, "explorer", parent=dialog)

    def do_uninstaller():
        entry = get_selected_entry()
        if entry:
            run_prefix_tool(entry, "uninstaller", parent=dialog)

    def do_winetricks():
        entry = get_selected_entry()
        if not entry:
            return
        open_winetricks_dialog(entry, parent_dialog=dialog)

    ttk.Button(btn_row, text="Winecfg", style="Custom.TButton", width=12,
               command=do_winecfg).grid(row=0, column=0, padx=3)
    ttk.Button(btn_row, text="Explorer", style="Custom.TButton", width=12,
               command=do_explorer).grid(row=0, column=1, padx=3)
    ttk.Button(btn_row, text="Uninstaller", style="Custom.TButton", width=12,
               command=do_uninstaller).grid(row=0, column=2, padx=3)
    ttk.Button(btn_row, text="Winetricks...", style="Custom.TButton", width=12,
               command=do_winetricks).grid(row=0, column=3, padx=3)

    def do_open_folder():
        entry = get_selected_entry()
        if not entry:
            return
        p = Path(entry["prefix_path"])
        if p.is_dir():
            subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              env=get_clean_subprocess_env())
        else:
            messagebox.showerror("Error", f"Folder not found:\n{p}", parent=dialog)

    ttk.Button(btn_row, text="Open Folder", style="Custom.TButton", width=12,
               command=do_open_folder).grid(row=0, column=4, padx=3)

    def do_backup():
        entry = get_selected_entry()
        if entry:
            open_backup_prefix_dialog(entry, parent_dialog=dialog)

    def do_restore():
        open_restore_backup_dialog(parent_dialog=dialog)

    def do_show_logs():
        """Tampilkan lagi jendela log task backup/restore yang sedang berjalan (atau baru
        saja selesai) di sesi ini - berguna kalau jendela log-nya tadi disembunyikan (tombol
        Close saat task masih berjalan hanya menyembunyikannya, lihat open_task_log_window),
        atau untuk sekedar mengecek progress sebelum mencoba memulai backup/restore lain
        (yang dibatasi hanya 1 dalam satu waktu, lihat prefix_task_is_busy())."""
        win = active_prefix_task.get("win") if active_prefix_task is not None else None
        if win is None or not win.winfo_exists():
            messagebox.showinfo("Show Logs", "No backup or restore process is currently running.",
                                 parent=dialog)
            return
        win.deiconify()
        win.lift()
        win.focus_set()

    show_logs_btn.config(command=do_show_logs)

    def do_remove_prefix():
        entry = get_selected_entry()
        if not entry:
            return
        if remove_prefix_and_games_dialog(entry, parent_dialog=dialog):
            iid = f"{entry['runner']}:{entry['prefix_code']}"
            if prefix_tree.exists(iid):
                prefix_tree.delete(iid)
            prefixes[:] = [e for e in prefixes
                           if not (e["runner"] == entry["runner"] and e["prefix_code"] == entry["prefix_code"])]

    btn_row2 = ttk.Frame(bottom)
    btn_row2.pack(pady=(8, 0))
    ttk.Button(btn_row2, text="Backup Apps", style="Custom.TButton", width=12,
               command=do_backup).grid(row=0, column=0, padx=3)
    ttk.Button(btn_row2, text="Restore Apps", style="Custom.TButton", width=12,
               command=do_restore).grid(row=0, column=1, padx=3)
    ttk.Button(btn_row2, text="Remove Apps", style="Custom.TButton", width=12,
               command=do_remove_prefix).grid(row=0, column=2, padx=3)

    # Ukuran minimum = kebutuhan isi yang sebenarnya (bar atas + 2 baris tombol + 4 baris daftar),
    # bukan angka tetap. Lebar minimum tidak memasukkan kolom daftar (itu tugas scrollbar horizontal).
    dialog.update_idletasks()
    min_w = max(640, btn_row.winfo_reqwidth() + 60)
    min_h = dialog.winfo_reqheight()
    try:
        row_h = int(style.lookup("Treeview", "rowheight") or 32)
    except (ValueError, tk.TclError):
        row_h = 32
    default_h = min_h + 6 * row_h        # kira-kira 10 baris daftar terlihat pada ukuran awal
    remember_dialog_size(dialog, "prefix_manager", (1000, default_h), (min_w, min_h), parent=root)
    dialog.deiconify()
    dialog.transient(root)
    dialog.grab_set()

def browse_folder_with_create_option(title, initialdir):
    """Buka dialog pilih folder (native OS), lalu tawarkan membuat SATU folder baru
    didalam folder yang dipilih itu. Ini supaya prefix-prefix baru bisa dikumpulkan
    rapi kedalam folder tujuan sendiri (mis. 'MyPrefixes') dan tidak "menyebar"
    langsung bercampur dengan isi folder/disk yang sudah ada.
    Mengembalikan Path folder terpilih (atau folder baru yang dibuat didalamnya),
    atau None jika dibatalkan."""
    chosen = filedialog.askdirectory(title=title, initialdir=initialdir, mustexist=True)
    if not chosen:
        return None

    chosen_path = Path(chosen)
    make_new = messagebox.askyesno(
        "Create New Folder?",
        f"Selected folder:\n{chosen_path}\n\n"
        "Create a new folder inside it for the prefix(es)?\n"
        "(Recommended so prefixes stay organized and don't mix with other files.)"
    )
    if not make_new:
        return chosen_path

    folder_name = simpledialog.askstring("New Folder", "New folder name:", parent=root)
    if not folder_name or not folder_name.strip():
        return chosen_path

    new_dir = chosen_path / folder_name.strip()
    try:
        new_dir.mkdir(parents=True, exist_ok=True)
        return new_dir
    except Exception as e:
        messagebox.showerror("Error", f"Failed to create folder:\n{str(e)}")
        return chosen_path

def move_prefix_folder_dialog(old_path, prefix_code, runner_key, on_done):
    """Minta pengguna memilih folder/disk tujuan, lalu pindahkan folder prefix (old_path)
    kesana di background thread (supaya UI tidak freeze untuk prefix berukuran besar).
    Memanggil on_done(new_path) di main thread jika berhasil."""
    initial_dir = str(old_path.parent) if old_path.parent.is_dir() else str(Path.home())
    new_base_path = browse_folder_with_create_option(
        title=f"Choose New Location for Prefix {prefix_code}",
        initialdir=initial_dir
    )
    if new_base_path is None:
        return

    new_path = new_base_path / prefix_code

    try:
        same_location = new_path.resolve() == old_path.resolve()
    except Exception:
        same_location = False
    if same_location:
        messagebox.showinfo("Info", "The prefix is already in this location.")
        return

    if new_path.exists():
        messagebox.showerror("Error", f"A folder named '{prefix_code}' already exists in that location.")
        return

    if not old_path.exists():
        messagebox.showerror("Error", f"Prefix folder not found on disk:\n{old_path}")
        return

    if not messagebox.askyesno(
        "Confirm Move",
        f"Move prefix '{prefix_code}' to:\n{new_path}\n\n"
        "Make sure the game/app using this prefix is not currently running.\n"
        "This may take a while for large prefixes. Continue?"
    ):
        return

    loading_dialog = tk.Toplevel(root)
    loading_dialog.withdraw()
    loading_dialog.title("Moving Prefix")
    loading_dialog.configure(bg=COLORS["primary"])
    loading_dialog.resizable(False, False)
    loading_dialog.transient(root)
    loading_dialog.protocol("WM_DELETE_WINDOW", lambda: None)

    loading_frame = ttk.Frame(loading_dialog, padding=20)
    loading_frame.pack(fill=tk.BOTH, expand=True)

    ttk.Label(loading_frame,
              text=f"Moving prefix '{prefix_code}'...",
              font=FONTS["subtitle"], justify=tk.CENTER).pack(pady=(0, 4))
    ttk.Label(loading_frame,
              text="This can take a while for large prefixes.\nPlease wait, the launcher is not frozen.",
              font=FONTS["small"], justify=tk.CENTER).pack(pady=(0, 14))

    progress_bar = ttk.Progressbar(loading_frame, mode="indeterminate", length=280)
    progress_bar.pack()
    progress_bar.start(12)

    loading_dialog.update_idletasks()
    x = root.winfo_rootx() + (root.winfo_width() - loading_dialog.winfo_width()) // 2
    y = root.winfo_rooty() + (root.winfo_height() - loading_dialog.winfo_height()) // 2
    loading_dialog.geometry(f"+{x}+{y}")
    loading_dialog.deiconify()
    loading_dialog.grab_set()

    move_result = {"error": None}

    def do_move():
        try:
            new_base_path.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_path), str(new_path))
        except Exception as e:
            move_result["error"] = e

    def finish_move():
        progress_bar.stop()
        loading_dialog.grab_release()
        loading_dialog.destroy()

        if move_result["error"] is None:
            safe_status_config(text=f"Prefix '{prefix_code}' moved to {new_path}", fg=COLORS["success"])
            on_done(new_path)
        else:
            e = move_result["error"]
            safe_status_config(text=f"Error moving prefix: {str(e)}", fg=COLORS["danger"])
            messagebox.showerror("Error", f"Failed to move prefix:\n{str(e)}")

    def worker():
        do_move()
        root.after(0, finish_move)

    threading.Thread(target=worker, daemon=True).start()

def load_runner_config():
    """Load pemetaan runner (wine/protonge) & prefix untuk tiap game."""
    if runner_config_file.exists():
        try:
            with open(runner_config_file, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_runner_config(cfg):
    """Simpan pemetaan runner (wine/protonge) & prefix untuk tiap game."""
    try:
        with open(runner_config_file, 'w') as f:
            json.dump(cfg, f, indent=4)
    except Exception as e:
        print(f"Error saving runner config: {e}")

def extract_exe_path_from_script(script_path):
    """Ambil path .exe dari script yang sudah ada (baris wine atau proton run).

    Catatan: pola proton SENGAJA tidak mensyaratkan kata "proton" ada didalam tanda kutip
    sebelum "run" - script hasil Restore Backup menulis proton_path kosong ("") dengan
    sengaja (lihat run_restore_worker), jadi baris runnya jadi '"" run "exe.exe"'. Kalau
    regex ini mensyaratkan kata "proton" didalam kutip itu, exe_path tidak akan pernah
    terbaca sama sekali untuk game hasil restore sebelum Proton build dipilih ulang -
    membuat PLAY pertama kali gagal duluan dengan "Could not read exe/folder path"."""
    try:
        with open(script_path, 'r') as f:
            content = f.read()
        match = re.search(r'wine\s+"([^"]+)"', content)
        if not match:
            match = re.search(r'"[^"]*"\s+run\s+"([^"]+)"', content)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None

def parse_launch_options(launch_options_str):
    """
    Parse string launch options ala Steam, contoh:
      'PROTON_USE_WINED3D=1 MANGOHUD=1'
      'PROTON_USE_WINED3D=1 %command% -windowed'
    Token 'KEY=VALUE' dianggap environment variable.
    Jika ada token '%command%', token setelahnya dianggap argumen tambahan untuk game.
    Jika tidak ada '%command%', token non 'KEY=VALUE' dianggap argumen tambahan.
    Return: (env_vars: list[(key, val)], extra_args: list[str])
    """
    if not launch_options_str or not launch_options_str.strip():
        return [], []
    try:
        tokens = shlex.split(launch_options_str)
    except ValueError:
        tokens = launch_options_str.split()

    env_vars = []
    extra_args = []

    if "%command%" in tokens:
        idx = tokens.index("%command%")
        before, after = tokens[:idx], tokens[idx + 1:]
        for t in before:
            if "=" in t and not t.startswith("-"):
                k, v = t.split("=", 1)
                env_vars.append((k, v))
        extra_args = after
    else:
        for t in tokens:
            if "=" in t and not t.startswith("-"):
                k, v = t.split("=", 1)
                env_vars.append((k, v))
            else:
                extra_args.append(t)

    return env_vars, extra_args

def extract_folder_path_from_script(script_path):
    """Ambil folder kerja (baris 'cd \"...\"') dari script yang sudah ada."""
    try:
        with open(script_path, 'r') as f:
            lines = f.readlines()
        if len(lines) >= 2:
            match = re.search(r'cd\s+"([^"]+)"', lines[1].strip())
            if match:
                return match.group(1)
    except Exception:
        pass
    return None

def build_script_content(folder_path, exe_path, choice):
    """Bangun ulang isi script .sh sesuai runner, komentar, dan launch options yang dipilih.
    Baris 'cd \"...\"' selalu di baris ke-2 (index 1) agar tetap kompatibel dengan
    parsing info game (on_select / open_file_manager)."""
    lines = ["#!/bin/bash", f'cd "{folder_path}"']

    comment = (choice.get("comment") or "").strip()
    if comment:
        for comment_line in comment.splitlines():
            lines.append(f'# {comment_line}')

    launch_options = (choice.get("launch_options") or "").strip()
    if launch_options:
        lines.append(f'# Launch Options: {launch_options}')

    env_vars, extra_args = parse_launch_options(launch_options)
    for key, val in env_vars:
        lines.append(f'export {key}={shlex.quote(val)}')

    extra_args_str = (" " + " ".join(shlex.quote(a) for a in extra_args)) if extra_args else ""

    if choice["runner"] in ("protonge", "protoncachyos"):
        lines.append(f'export STEAM_COMPAT_DATA_PATH="{choice["prefix_path"]}"')
        lines.append(f'export STEAM_COMPAT_CLIENT_INSTALL_PATH="{find_steam_install_path()}"')
        lines.append(f'"{choice["proton_path"]}" run "{exe_path}"{extra_args_str}')
    else:
        if choice.get("prefix_path"):
            lines.append(f'export WINEPREFIX="{choice["prefix_path"]}"')
        lines.append(f'wine "{exe_path}"{extra_args_str}')
    return "\n".join(lines) + "\n"

def ask_runner_choice(parent_script_name=None, purpose="play", parent=None, game_title=None):
    """
    Tampilkan dialog pilihan runner: Wine (Vanilla) atau Proton GE.
    - parent_script_name: nama game (untuk konteks PLAY), dipakai untuk mengingat pilihan sebelumnya
      dan menjaga prefix ProtonGE tetap konsisten untuk game yang sama.
    - purpose: "play" atau "setup", hanya memengaruhi teks judul dialog.
    - parent: window induk dialog ini (mis. jendela GOG Store) supaya dialog muncul DI ATASNYA;
      kosong = jendela utama launcher.
    - game_title: nama game yang ditampilkan di dialog (opsional).

    Return dict pilihan, atau None jika dibatalkan.
    """
    protonge_list = find_protonge_installations()
    protoncachyos_list = find_protoncachyos_installations()

    existing_cfg = None
    if parent_script_name:
        existing_cfg = load_runner_config().get(parent_script_name)

    parent_win = parent if (parent is not None and parent.winfo_exists()) else root
    dialog = tk.Toplevel(parent_win)
    dialog.withdraw()
    dialog.title("Select Runner - Setup" if purpose == "setup" else "Select Runner - Play")
    dialog.configure(bg=COLORS["primary"])
    dialog.resizable(False, False)

    result = {"value": None}

    frame = ttk.Frame(dialog, padding=15)
    frame.pack(fill=tk.BOTH, expand=True)

    ttk.Label(frame, text=f"Select Runner - {game_title}:" if game_title else "Select Runner - Game:",
              font=FONTS["normal"], wraplength=420, justify=tk.LEFT).pack(anchor="w", pady=(0, 10))

    existing_runner = existing_cfg.get("runner") if existing_cfg else None
    default_runner = existing_runner if existing_runner in ("protonge", "protoncachyos") else "wine"
    runner_var = tk.StringVar(value=default_runner)

    runner_row = ttk.Frame(frame)
    runner_row.pack(anchor="w", pady=(0, 2))

    wine_radio = ttk.Radiobutton(runner_row, text="Wine (Vanilla)", variable=runner_var,
                                  value="wine", style="Runner.TRadiobutton", width=14)
    wine_radio.grid(row=0, column=0, padx=3, pady=3)

    protonge_radio = ttk.Radiobutton(runner_row, text="Proton GE", variable=runner_var,
                                      value="protonge", style="Runner.TRadiobutton", width=14)
    protonge_radio.grid(row=0, column=1, padx=3, pady=3)

    protoncachyos_radio = ttk.Radiobutton(runner_row, text="Proton-CachyOS", variable=runner_var,
                                           value="protoncachyos", style="Runner.TRadiobutton", width=14)
    protoncachyos_radio.grid(row=0, column=2, padx=3, pady=3)

    runner_dynamic_frame = ttk.Frame(frame)
    runner_dynamic_frame.pack(anchor="w", fill=tk.X, pady=(0, 0))

    def build_prefix_panel(parent_frame, runner_key, runner_label_text):
        """Membuat panel kontrol lokasi prefix untuk satu runner (wine/protonge/protoncachyos)
        didalam dialog ini. Dua kondisi:
          - Prefix untuk game ini SUDAH ADA (dipakai ulang) -> tampilkan lokasinya sekarang
            + tombol "Move Prefix..." untuk memindahkannya ke folder/disk lain kapan saja.
          - Prefix untuk game ini BELUM ADA (akan dibuat baru saat OK ditekan) -> tampilkan
            lokasi tujuan (default folder WLM, atau lokasi kustom tersimpan) + tombol untuk
            memilih folder/disk lain atau mengembalikannya ke lokasi default.
        Mengembalikan (panel_frame, state_dict). state_dict dibaca saat OK ditekan untuk
        menentukan prefix_path final yang dipakai."""
        state = {"existing_path": None, "existing_code": None, "new_base_dir": get_prefix_base_dir(runner_key)}
        if existing_cfg and existing_cfg.get("runner") == runner_key and existing_cfg.get("prefix_code") and existing_cfg.get("prefix_path"):
            state["existing_path"] = Path(existing_cfg["prefix_path"])
            state["existing_code"] = existing_cfg["prefix_code"]

        panel = ttk.Frame(parent_frame)
        info_var = tk.StringVar()
        ttk.Label(panel, textvariable=info_var, font=FONTS["small"], justify=tk.LEFT, wraplength=380).pack(anchor="w", pady=(2, 4))

        btn_row = ttk.Frame(panel)
        btn_row.pack(anchor="w", pady=(0, 8))

        move_btn = ttk.Button(btn_row, text="Move Prefix...", style="Custom.TButton")
        saves_btn_inline = ttk.Button(btn_row, text="Saves...", style="Custom.TButton")
        browse_btn = ttk.Button(btn_row, text="Browse Other Folder/Disk...", style="Custom.TButton")
        default_btn = ttk.Button(btn_row, text="Use Default (WLM Folder)", style="Custom.TButton")

        # Saves hanya masuk akal kalau game-nya sudah pernah di-launch (prefix sudah ada) dan
        # kita tahu game mana yang dimaksud (purpose == "play", bukan dialog Setup/Install).
        show_saves_btn = purpose == "play" and bool(parent_script_name)

        def refresh():
            for w in (move_btn, saves_btn_inline, browse_btn, default_btn):
                w.pack_forget()
            if state["existing_path"] is not None:
                info_var.set(f"Prefix: {state['existing_code']} (used previously, kept consistent)\n"
                              f"Location: {state['existing_path']}")
                move_btn.pack(side=tk.LEFT)
                if show_saves_btn:
                    saves_btn_inline.pack(side=tk.LEFT, padx=(5, 0))
            else:
                info_var.set(f"A new prefix will be created automatically at:\n{state['new_base_dir']} (e.g. GAMEXXX)")
                browse_btn.pack(side=tk.LEFT, padx=(0, 5))
                default_btn.pack(side=tk.LEFT)

        def do_move():
            def on_moved(new_path):
                old_path = state["existing_path"]
                state["existing_path"] = new_path
                updated = relink_scripts_after_prefix_move(runner_key, state["existing_code"], old_path, new_path)
                update_registry_prefix_path(runner_key, state["existing_code"], new_path)
                refresh()
                if updated:
                    safe_status_config(
                        text=f"Prefix moved - relinked {len(updated)} game(s): {', '.join(updated)}",
                        fg=COLORS["success"])
            move_prefix_folder_dialog(state["existing_path"], state["existing_code"], runner_key, on_moved)

        def do_browse():
            initial = str(state["new_base_dir"]) if state["new_base_dir"].is_dir() else str(Path.home())
            chosen_dir = browse_folder_with_create_option(
                title=f"Choose Default Location for New {runner_label_text} Prefixes",
                initialdir=initial
            )
            if chosen_dir:
                state["new_base_dir"] = chosen_dir
                set_prefix_base_dir(runner_key, state["new_base_dir"])
                refresh()

        def do_default():
            state["new_base_dir"] = DEFAULT_PREFIX_ROOTS[runner_key]
            reset_prefix_base_dir(runner_key)
            refresh()

        def do_saves():
            open_save_manager(script_name=parent_script_name, parent_win=dialog)

        move_btn.config(command=do_move)
        saves_btn_inline.config(command=do_saves)
        browse_btn.config(command=do_browse)
        default_btn.config(command=do_default)

        refresh()
        return panel, state

    existing_wine_prefix = bool(existing_cfg and existing_cfg.get("runner") == "wine" and existing_cfg.get("prefix_path"))
    wine_use_prefix_var = tk.BooleanVar(value=existing_wine_prefix)
    wine_checkbox = ttk.Checkbutton(
        runner_dynamic_frame,
        text="Use an isolated prefix for this game (avoids buildup in home folder)",
        variable=wine_use_prefix_var,
        style="Custom.TCheckbutton"
    )
    wine_prefix_panel, wine_prefix_state = build_prefix_panel(runner_dynamic_frame, "wine", "Wine")

    def refit_dialog():
        """Isi dialog berubah tinggi (panel Wine/Proton berganti) -> tengahkan lagi supaya
        bagian bawahnya (tombol OK) tidak masuk ke bawah taskbar."""
        if dialog.winfo_exists() and dialog.winfo_viewable():
            dialog.after_idle(lambda: place_dialog(dialog, parent_win))

    def toggle_wine_panel(*_):
        if wine_use_prefix_var.get():
            wine_prefix_panel.pack(anchor="w", pady=(2, 8), fill=tk.X)
        else:
            wine_prefix_panel.pack_forget()
        refit_dialog()

    wine_use_prefix_var.trace_add("write", toggle_wine_panel)

    protonge_version_label = ttk.Label(runner_dynamic_frame, text="Proton GE Version:", font=FONTS["small"])
    protonge_version_combo = ttk.Combobox(runner_dynamic_frame, state="readonly",
                                           width=fit_combo_width([n for n, _ in protonge_list]), font=FONTS["small"])

    if protonge_list:
        protonge_version_combo["values"] = [name for name, _ in protonge_list]
        default_idx = 0
        if existing_cfg and existing_cfg.get("runner") == "protonge" and existing_cfg.get("proton_name") in protonge_version_combo["values"]:
            default_idx = list(protonge_version_combo["values"]).index(existing_cfg["proton_name"])
        protonge_version_combo.current(default_idx)
    else:
        protonge_version_combo["values"] = ["(None yet - Extract via Settings menu)"]
        protonge_version_combo.current(0)
        protonge_radio.config(state="disabled")

    protonge_prefix_panel, protonge_prefix_state = build_prefix_panel(runner_dynamic_frame, "protonge", "Proton GE")

    cachyos_version_label = ttk.Label(runner_dynamic_frame, text="Proton-CachyOS Version:", font=FONTS["small"])
    cachyos_version_combo = ttk.Combobox(runner_dynamic_frame, state="readonly",
                                          width=fit_combo_width([n for n, _ in protoncachyos_list]), font=FONTS["small"])

    if protoncachyos_list:
        cachyos_version_combo["values"] = [name for name, _ in protoncachyos_list]
        default_idx = 0
        if existing_cfg and existing_cfg.get("runner") == "protoncachyos" and existing_cfg.get("proton_name") in cachyos_version_combo["values"]:
            default_idx = list(cachyos_version_combo["values"]).index(existing_cfg["proton_name"])
        cachyos_version_combo.current(default_idx)
    else:
        cachyos_version_combo["values"] = ["(None yet - Extract via Settings menu)"]
        cachyos_version_combo.current(0)
        protoncachyos_radio.config(state="disabled")

    cachyos_prefix_panel, cachyos_prefix_state = build_prefix_panel(runner_dynamic_frame, "protoncachyos", "Proton-CachyOS")

    def toggle_runner_widgets(*_):
        chosen = runner_var.get()

        wine_checkbox.pack_forget()
        wine_prefix_panel.pack_forget()
        protonge_version_label.pack_forget()
        protonge_version_combo.pack_forget()
        protonge_prefix_panel.pack_forget()
        cachyos_version_label.pack_forget()
        cachyos_version_combo.pack_forget()
        cachyos_prefix_panel.pack_forget()

        if chosen == "wine":
            wine_checkbox.pack(anchor="w", pady=(10, 4))
            toggle_wine_panel()
        elif chosen == "protonge":
            protonge_version_label.pack(anchor="w", pady=(10, 2))
            protonge_version_combo.pack(anchor="w", pady=(0, 2))
            protonge_prefix_panel.pack(anchor="w", pady=(2, 6), fill=tk.X)
        elif chosen == "protoncachyos":
            cachyos_version_label.pack(anchor="w", pady=(10, 2))
            cachyos_version_combo.pack(anchor="w", pady=(0, 2))
            cachyos_prefix_panel.pack(anchor="w", pady=(2, 6), fill=tk.X)
        refit_dialog()

    runner_var.trace_add("write", toggle_runner_widgets)
    toggle_runner_widgets()

    ttk.Separator(frame, orient="horizontal").pack(fill=tk.X, pady=(5, 10))

    ttk.Label(frame, text="Launch Options / Environment Variable (optional):",
              font=FONTS["small"]).pack(anchor="w", pady=(0, 2))
    launch_options_entry = ttk.Entry(frame, width=48, font=FONTS["small"])
    launch_options_entry.pack(anchor="w", fill=tk.X)
    if existing_cfg and existing_cfg.get("launch_options"):
        launch_options_entry.insert(0, existing_cfg["launch_options"])
    ttk.Label(frame, text="Example: PROTON_USE_WINED3D=1 MANGOHUD=1",
              font=FONTS["small"]).pack(anchor="w", pady=(2, 10))

    ttk.Label(frame, text="Comment (optional):",
              font=FONTS["small"]).pack(anchor="w", pady=(0, 2))
    comment_entry = ttk.Entry(frame, width=48, font=FONTS["small"])
    comment_entry.pack(anchor="w", fill=tk.X, pady=(0, 10))
    if existing_cfg and existing_cfg.get("comment"):
        comment_entry.insert(0, existing_cfg["comment"])

    btn_frame = ttk.Frame(frame)
    btn_frame.pack(fill=tk.X, pady=(5, 0))

    def on_ok():
        chosen = runner_var.get()
        launch_options_value = launch_options_entry.get().strip()
        comment_value = comment_entry.get().strip()

        if chosen == "wine":
            result_value = {
                "runner": "wine",
                "launch_options": launch_options_value,
                "comment": comment_value
            }
            if wine_use_prefix_var.get():
                if wine_prefix_state["existing_path"] is not None:
                    prefix_code = wine_prefix_state["existing_code"]
                    prefix_path = wine_prefix_state["existing_path"]
                else:
                    prefix_code = generate_next_prefix_code("wine")
                    prefix_path = wine_prefix_state["new_base_dir"] / prefix_code
                prefix_path.mkdir(parents=True, exist_ok=True)
                record_prefix_usage("wine", prefix_code, prefix_path)
                result_value["prefix_code"] = prefix_code
                result_value["prefix_path"] = str(prefix_path)
            result["value"] = result_value
            dialog.destroy()
            return

        if chosen == "protonge":
            build_list = protonge_list
            version_combo = protonge_version_combo
            build_label = "Proton GE"
            runner_key = "protonge"
            prefix_state = protonge_prefix_state
        else:
            build_list = protoncachyos_list
            version_combo = cachyos_version_combo
            build_label = "Proton-CachyOS"
            runner_key = "protoncachyos"
            prefix_state = cachyos_prefix_state

        if not build_list:
            messagebox.showerror("Error", f"{build_label} not found. Extract it first via the Settings menu.")
            return

        idx = version_combo.current()
        proton_name, proton_path = build_list[idx]

        if prefix_state["existing_path"] is not None:
            prefix_code = prefix_state["existing_code"]
            prefix_path = prefix_state["existing_path"]
        else:
            prefix_code = generate_next_prefix_code(runner_key)
            prefix_path = prefix_state["new_base_dir"] / prefix_code

        prefix_path.mkdir(parents=True, exist_ok=True)
        record_prefix_usage(runner_key, prefix_code, prefix_path, proton_name, str(proton_path))

        result["value"] = {
            "runner": chosen,
            "proton_name": proton_name,
            "proton_path": str(proton_path),
            "prefix_code": prefix_code,
            "prefix_path": str(prefix_path),
            "launch_options": launch_options_value,
            "comment": comment_value
        }
        dialog.destroy()

    def on_cancel():
        result["value"] = None
        dialog.destroy()

    ttk.Button(btn_frame, text="Cancel", command=on_cancel, style="Custom.TButton", width=12).pack(side=tk.LEFT)
    ttk.Button(btn_frame, text="OK", command=on_ok, style="Custom.TButton", width=12).pack(side=tk.RIGHT)

    place_dialog(dialog, parent_win)
    dialog.deiconify()
    dialog.transient(parent_win)
    dialog.grab_set()
    dialog.lift()
    dialog.focus_set()

    # Window manager sering menggeser dialog saat ditampilkan -> tengahkan lagi sebentar kemudian.
    for delay in (30, 150, 400):
        dialog.after(delay, lambda: place_dialog(dialog, parent_win))

    dialog.wait_window()
    return result["value"]

def apply_theme(theme_name, announce=True):
    """Apply the theme to all widgets"""
    global CURRENT_THEME, COLORS
    if theme_name not in THEMES:
        theme_name = "default"

    CURRENT_THEME = theme_name
    COLORS = THEMES[theme_name]

    save_theme_config(theme_name)

    update_style_config()

    update_widget_colors()

    if announce:
        safe_status_config(text=f"Changed to {COLORS['name']} theme", fg=COLORS["success"])

    theme_combo.set(COLORS["name"])

def save_theme_config(theme_name):
    """Save theme choice to file"""
    with open(theme_config_file, 'w') as f:
        json.dump({'theme': theme_name}, f)

def mix_color(c1, c2, t):
    """Campur dua warna hex '#rrggbb': t=0 -> c1, t=1 -> c2 (dipakai untuk warna disabled/pressed)."""
    try:
        if len(c1) != 7 or len(c2) != 7:
            return c1
        a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    except (ValueError, TypeError):
        return c1
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))

def readable_on(bg_color, dark="#0b0d14", light="#ffffff"):
    """Pilih teks gelap/terang yang kontrasnya paling nyaman di atas warna latar tertentu."""
    try:
        r, g, b = (int(bg_color[i:i + 2], 16) for i in (1, 3, 5))
    except (ValueError, TypeError, IndexError):
        return light
    return dark if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else light

_RB = {"n": 0, "keep": []}

def _rounded_img(fill, border, parent, radius, inset, lip=0, pressed=False):
    """Gambar 9-patch tombol rounded (anti-alias lewat supersampling). Sudut diisi warna parent
    supaya menyatu dengan latar. 'lip' = tepi bawah gelap (tombol terasa timbul); 'pressed' =
    bayangan di tepi atas (tombol terasa masuk)."""
    k = 4
    size = 2 * (inset + radius) + 2
    big = size * k

    def mask(off, rad):
        m = Image.new("L", (big, big), 0)
        ImageDraw.Draw(m).rounded_rectangle(
            [off * k, off * k, big - 1 - off * k, big - 1 - off * k],
            radius=max(rad, 0) * k, fill=255)
        return m

    outer, inner = mask(inset, radius), mask(inset + 1, radius - 1)
    canvas = Image.new("RGB", (big, big), parent)
    canvas.paste(border, mask=outer)
    canvas.paste(fill, mask=inner)
    shade = mix_color(fill, "#000000", 0.28)
    if pressed:
        canvas.paste(shade, mask=ImageChops.subtract(inner, ImageChops.offset(inner, 0, 2 * k)))
    elif lip:
        canvas.paste(shade, mask=ImageChops.subtract(inner, ImageChops.offset(inner, 0, -lip * k)))
    resample = getattr(Image, "Resampling", Image).LANCZOS
    photo = ImageTk.PhotoImage(canvas.resize((size, size), resample))
    _RB["keep"].append(photo)
    return photo

def round_style(name, specs, parent, radius=8, inset=0, lip=2):
    """Ganti layout ttk style 'name' dengan tombol rounded.
    specs: [(statetuple|None, fill, border, pressed)] - entri pertama (None) = keadaan normal."""
    _RB["n"] += 1
    elem = f"RoundBtnBg{_RB['n']}"
    default, extra = None, []
    for states, fill, brd, pressed in specs:
        img = _rounded_img(fill, brd, parent, radius, inset, 0 if pressed else lip, pressed)
        if states is None:
            default = img
        else:
            extra.append((*states, img))
    b = inset + radius
    style.element_create(elem, "image", default, *extra, border=(b, b, b, b),
                         padding=(inset + 1, inset + 1, inset + 1, inset + 1 + lip), sticky="nsew")
    style.layout(name, [(elem, {"sticky": "nswe", "children": [
        ("Button.padding", {"sticky": "nswe", "children": [
            ("Button.label", {"sticky": "nswe"})]})]})])

def round_entry(name, fill, border, focus_border, disabled_fill, parent, radius=10):
    """Ganti layout ttk.Entry style 'name' dengan kolom input rounded (garis tepi berubah saat fokus)."""
    _RB["n"] += 1
    elem = f"RoundEntryBg{_RB['n']}"
    normal = _rounded_img(fill, border, parent, radius, 0)
    focus = _rounded_img(fill, focus_border, parent, radius, 0)
    off = _rounded_img(disabled_fill, border, parent, radius, 0)
    style.element_create(elem, "image", normal, ("disabled", off), ("readonly", off), ("focus", focus),
                         border=(radius, radius, radius, radius), padding=(1, 1, 1, 1), sticky="nsew")
    style.layout(name, [(elem, {"sticky": "nswe", "children": [
        ("Entry.padding", {"sticky": "nswe", "children": [
            ("Entry.textarea", {"sticky": "nswe"})]})]})])

def update_style_config():
    """Update ttk style configuration - tampilan flat modern: tombol datar dengan hover halus,
    tombol utama (Play/Accent), tombol bahaya (Remove), scrollbar tipis, tabel tanpa border."""
    C = COLORS
    bg, surface, card, border = C["primary"], C["secondary"], C["card_bg"], C["border"]
    hl, on_hl = C["highlight"], C["button_text"]
    text, muted = C["text"], C["text_secondary"]
    field = C["text_background"]
    white, black = "#ffffff", "#000000"

    def flat_button(name, bg_, fg_, hover_bg, press_bg, hover_fg=None, border_=None,
                    hover_border=None, font=None, padding=(14, 8), radius=8, lip=2, parent=None):
        border_ = border_ or bg_
        hover_border = hover_border or hover_bg
        dis_bg = mix_color(bg_, bg, 0.6)
        dis_fg = mix_color(fg_, bg, 0.55)
        style.configure(name, background=bg_, foreground=fg_, bordercolor=border_,
                        lightcolor=bg_, darkcolor=bg_, borderwidth=1, relief="flat",
                        focusthickness=1, focuscolor=hl, font=font or FONTS["normal"],
                        padding=padding, anchor="center", shiftrelief=1)
        # Urutan penting (yang cocok pertama menang): disabled -> pressed -> active -> normal.
        style.map(name,
                  background=[("disabled", dis_bg), ("pressed", press_bg), ("active", hover_bg)],
                  foreground=[("disabled", dis_fg), ("active", hover_fg or fg_)],
                  bordercolor=[("disabled", dis_bg), ("pressed", press_bg), ("active", hover_border)],
                  lightcolor=[("disabled", dis_bg), ("pressed", press_bg), ("active", hover_bg)],
                  darkcolor=[("disabled", dis_bg), ("pressed", press_bg), ("active", hover_bg)],
                  relief=[("pressed", "sunken"), ("!pressed", "flat")])
        try:
            round_style(name, [
                (None, bg_, border_, False),
                (("disabled",), dis_bg, dis_bg, False),
                (("pressed",), press_bg, mix_color(press_bg, black, 0.2), True),
                (("active",), hover_bg, hover_border, False),
                (("focus",), bg_, hl, False),
            ], parent or bg, radius=radius, lip=lip)
        except Exception as e:
            print(f"[WLM] rounded button {name}: {e}")

    style.configure("TFrame", background=bg)
    style.configure("Header.TFrame", background=surface)
    style.configure("Footer.TFrame", background=surface)
    style.configure("Side.TFrame", background=surface)
    style.configure("Card.TFrame", background=card)
    style.configure("TPanedwindow", background=bg)

    style.configure("TLabel", background=bg, foreground=text, font=FONTS["normal"])
    style.configure("Muted.TLabel", foreground=muted)
    style.configure("Heading.TLabel", font=FONTS["heading"])
    style.configure("Card.TLabel", background=card, foreground=text, font=FONTS["normal"])
    style.configure("CardMuted.TLabel", background=card, foreground=muted, font=FONTS["small"])
    style.configure("FooterMuted.TLabel", background=surface, foreground=muted, font=FONTS["small"])

    btn_bg, btn_fg = C["button_bg"], C["button_fg"]
    flat_button("Custom.TButton", btn_bg, btn_fg, mix_color(btn_bg, hl, 0.32),
                mix_color(btn_bg, hl, 0.55), border_=border, hover_border=hl, padding=(12, 7))
    flat_button("TButton", btn_bg, btn_fg, mix_color(btn_bg, hl, 0.32),
                mix_color(btn_bg, hl, 0.55), border_=border, hover_border=hl, padding=(12, 7))
    flat_button("Action.TButton", btn_bg, btn_fg, mix_color(btn_bg, hl, 0.32),
                mix_color(btn_bg, hl, 0.55), border_=border, hover_border=hl, padding=(6, 7))
    flat_button("Accent.TButton", hl, on_hl, mix_color(hl, white, 0.14), mix_color(hl, black, 0.22),
                font=(FONT_FAMILY, 9, "bold"), padding=(14, 7))
    play_bg = C["success"]
    flat_button("Play.TButton", play_bg, readable_on(play_bg), mix_color(play_bg, white, 0.12),
                mix_color(play_bg, black, 0.22), font=(FONT_FAMILY, 12, "bold"), padding=(12, 11))
    danger = C["danger"]
    flat_button("Danger.TButton", btn_bg, danger, danger, mix_color(danger, black, 0.25),
                hover_fg=readable_on(danger), border_=border, hover_border=danger, padding=(6, 7))
    flat_button("SegOn.TButton", hl, on_hl, mix_color(hl, white, 0.10), mix_color(hl, black, 0.22),
                padding=(14, 6), lip=0)
    flat_button("SegOff.TButton", card, muted, mix_color(card, hl, 0.25), mix_color(card, hl, 0.45),
                hover_fg=text, border_=border, padding=(14, 6), lip=0)
    # Tombol hamburger (header) - datar menyatu dengan header. Nama lama dipertahankan.
    flat_button("Hamburger.Custom.TButton", surface, text, mix_color(surface, hl, 0.25),
                mix_color(surface, hl, 0.45), font=(FONT_FAMILY, 14), padding=(8, 2),
                lip=0, parent=surface)

    side_hover = mix_color(surface, hl, 0.22)
    style.configure("SideTitle.TLabel", background=surface, foreground=text, font=FONTS["title"])
    style.configure("SideSection.TLabel", background=surface, foreground=hl,
                    font=(FONT_FAMILY, 8, "bold"))
    style.configure("SideMuted.TLabel", background=surface, foreground=muted, font=FONTS["small"])
    style.configure("Side.TSeparator", background=border)
    style.configure("Sidebar.TButton",
                    background=surface, foreground=text, bordercolor=surface, lightcolor=surface,
                    darkcolor=surface, borderwidth=0, focusthickness=0, focuscolor=surface,
                    font=FONTS["normal"], padding=(18, 9), anchor="w", relief="flat")
    style.map("Sidebar.TButton",
              background=[("pressed", hl), ("active", side_hover), ("!active", surface)],
              foreground=[("pressed", on_hl), ("active", text), ("!active", text)],
              bordercolor=[("pressed", hl), ("active", side_hover)],
              lightcolor=[("pressed", hl), ("active", side_hover)],
              darkcolor=[("pressed", hl), ("active", side_hover)],
              relief=[("pressed", "flat"), ("!pressed", "flat")])
    try:
        round_style("Sidebar.TButton", [
            (None, surface, surface, False),
            (("pressed",), hl, hl, True),
            (("active",), side_hover, side_hover, False),
        ], surface, radius=8, inset=4, lip=0)
    except Exception as e:
        print(f"[WLM] rounded sidebar button: {e}")

    style.layout("Runner.TRadiobutton", style.layout("TButton"))
    style.configure("Runner.TRadiobutton",
                    background=C["button_bg"],
                    foreground=C["button_fg"],
                    bordercolor=C["border"],
                    borderwidth=1,
                    focusthickness=1,
                    focuscolor=C["highlight"],
                    font=FONTS["normal"],
                    anchor="center",
                    padding=6)
    style.map("Runner.TRadiobutton",
              background=[("disabled", C["card_bg"]),
                          ("selected", C["highlight"]),
                          ("active", C["highlight"]),
                          ("!selected", C["button_bg"])],
              foreground=[("disabled", C["text_secondary"]),
                          ("selected", C["button_text"]),
                          ("active", C["button_text"]),
                          ("!selected", C["button_fg"])])
    try:
        round_style("Runner.TRadiobutton", [
            (None, C["button_bg"], C["border"], False),
            (("disabled",), C["card_bg"], C["card_bg"], False),
            (("selected",), hl, hl, False),
            (("active",), hl, hl, False),
        ], bg, radius=8, lip=2)
    except Exception as e:
        print(f"[WLM] rounded runner button: {e}")

    for base_name in ("TCheckbutton", "TRadiobutton"):
        style.configure(base_name, background=bg, foreground=text, font=FONTS["normal"],
                        focuscolor=hl)
        style.map(base_name, background=[("active", bg)], foreground=[("disabled", muted)])
    style.configure("Custom.TCheckbutton",
                    background=bg,
                    foreground=text,
                    font=FONTS["normal"],
                    indicatorbackground=field,
                    indicatorforeground=on_hl,
                    indicatormargin=(0, 0, 6, 0),
                    focuscolor=hl)
    style.map("Custom.TCheckbutton",
              background=[("active", bg)],
              foreground=[("disabled", muted), ("active", text)],
              indicatorbackground=[("disabled", card), ("selected", hl), ("!selected", field)],
              indicatorforeground=[("disabled", muted), ("selected", on_hl)])

    # ---- input: entry & combobox (fokus = garis highlight)
    style.configure("TEntry", fieldbackground=field, foreground=text, insertcolor=text,
                    bordercolor=border, lightcolor=border, darkcolor=border, borderwidth=1,
                    padding=4, selectbackground=hl, selectforeground=on_hl)
    style.map("TEntry",
              bordercolor=[("focus", hl)], lightcolor=[("focus", hl)], darkcolor=[("focus", hl)],
              fieldbackground=[("readonly", mix_color(field, bg, 0.5)),
                               ("disabled", mix_color(field, bg, 0.5))],
              foreground=[("disabled", muted)])
    style.configure("Search.TEntry", padding=(12, 7))
    try:
        round_entry("Search.TEntry", field, border, hl, mix_color(field, bg, 0.5), bg, radius=10)
    except Exception as e:
        print(f"[WLM] rounded search entry: {e}")

    style.configure("TCombobox",
                    fieldbackground=field, background=card, foreground=text,
                    arrowcolor=muted, arrowsize=14,
                    selectbackground=field, selectforeground=text,
                    bordercolor=border, lightcolor=border, darkcolor=border,
                    relief="flat", borderwidth=1, padding=4)
    style.map("TCombobox",
              fieldbackground=[("readonly", field)],
              background=[("readonly", card)],
              foreground=[("readonly", text)],
              selectbackground=[("readonly", field)],
              selectforeground=[("readonly", text)],
              bordercolor=[("focus", hl), ("active", hl)],
              lightcolor=[("focus", hl), ("active", hl)],
              darkcolor=[("focus", hl), ("active", hl)],
              arrowcolor=[("active", hl), ("focus", hl)])
    # Daftar dropdown combobox (Listbox Tk biasa) ikut warna tema.
    try:
        root.option_add("*TCombobox*Listbox.background", field)
        root.option_add("*TCombobox*Listbox.foreground", text)
        root.option_add("*TCombobox*Listbox.selectBackground", hl)
        root.option_add("*TCombobox*Listbox.selectForeground", on_hl)
        root.option_add("*TCombobox*Listbox.borderWidth", 0)
        root.option_add("*TCombobox*Listbox.highlightThickness", 0)
    except tk.TclError:
        pass

    # ---- scrollbar tipis tanpa tombol panah
    thumb = mix_color(bg, text, 0.22)
    style.layout("Vertical.TScrollbar",
                 [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
                     ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
    style.layout("Horizontal.TScrollbar",
                 [("Horizontal.Scrollbar.trough", {"sticky": "ew", "children": [
                     ("Horizontal.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
    style.configure("TScrollbar", background=thumb, troughcolor=bg, bordercolor=bg,
                    lightcolor=thumb, darkcolor=thumb, arrowcolor=muted, gripcount=0, width=10,
                    relief="flat", borderwidth=0)
    style.map("TScrollbar",
              background=[("pressed", hl), ("active", mix_color(thumb, hl, 0.6))],
              lightcolor=[("pressed", hl), ("active", mix_color(thumb, hl, 0.6))],
              darkcolor=[("pressed", hl), ("active", mix_color(thumb, hl, 0.6))])

    style.configure("TProgressbar", background=hl, troughcolor=card, bordercolor=card,
                    lightcolor=hl, darkcolor=hl, thickness=10)

    # ---- tabel (Treeview): tanpa border, baris lega, header datar
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    style.configure("Treeview",
                    background=C["tree_bg"], foreground=C["tree_fg"], fieldbackground=C["tree_bg"],
                    bordercolor=C["tree_bg"], borderwidth=0, relief="flat", rowheight=32,
                    font=FONTS["body"])
    style.configure("Treeview.Heading",
                    background=surface, foreground=muted, relief="flat", borderwidth=0,
                    bordercolor=surface, lightcolor=surface, darkcolor=surface,
                    font=(FONT_FAMILY, 9, "bold"), padding=(8, 8))
    style.map("Treeview.Heading",
              background=[("active", card)], foreground=[("active", text)])
    style.map("Treeview",
              background=[("selected", C["tree_highlight"])],
              foreground=[("selected", C["tree_highlight_text"])])

    style.configure("TSeparator", background=border)

    # LabelFrame (mis. kotak "Save folders"/"Local backups"/"GOG cloud saves" di Save Manager) -
    # tanpa ini ttk.LabelFrame memakai tampilan default tema 'clam' (kotak abu-abu terang) yang
    # tidak nyambung sama sekali dengan tema gelap/custom aplikasi.
    style.configure("TLabelframe",
                    background=bg,
                    bordercolor=border,
                    darkcolor=border,
                    lightcolor=border,
                    relief="solid",
                    borderwidth=1)
    style.configure("TLabelframe.Label",
                    background=bg,
                    foreground=hl,
                    font=FONTS["subtitle"])

def _style_combo_popdown(combo):
    """Daftar dropdown yang SUDAH pernah dibuat tidak ikut option_add -> warnai langsung."""
    try:
        popdown = root.tk.call("ttk::combobox::PopdownWindow", str(combo))
        root.tk.call(f"{popdown}.f.l", "configure",
                     "-background", COLORS["text_background"], "-foreground", COLORS["text"],
                     "-selectbackground", COLORS["highlight"],
                     "-selectforeground", COLORS["button_text"],
                     "-borderwidth", 0, "-highlightthickness", 0)
    except tk.TclError:
        pass

def update_widget_colors():
    """Update colors of all tk widgets (yang tidak ikut ttk style) + segarkan style tombol"""
    C = COLORS
    try:
        root.configure(bg=C["primary"])
    except tk.TclError:
        return

    def cfg(widget_name, **kwargs):
        widget = globals().get(widget_name)
        if widget is None:
            return
        try:
            widget.configure(**kwargs)
        except tk.TclError:
            pass

    cfg("title_label", bg=C["secondary"], fg=C["text"])
    cfg("version_label", bg=C["secondary"], fg=C["text_secondary"])
    cfg("header_line", bg=C["border"])
    safe_status_config(bg=C["secondary"], fg=C["text_secondary"])
    cfg("detail_card", bg=C["card_bg"], highlightbackground=C["border"],
        highlightcolor=C["border"])
    cfg("game_title_label", bg=C["card_bg"], fg=C["text"])
    cfg("info_label", bg=C["card_bg"], fg=C["text_secondary"])
    cfg("icon_label", bg=C["card_bg"], fg=C["text_secondary"])
    cfg("tree_frame", bg=C["tree_bg"], highlightbackground=C["border"],
        highlightcolor=C["border"])
    cfg("empty_label", fg=C["text_secondary"])
    _sync = globals().get("_sync_empty_bg")
    if _sync:
        _sync()
    cfg("search_hint", bg=C["text_background"], fg=C["text_secondary"])
    cfg("side_canvas", bg=C["secondary"])

    for btn in all_buttons:
        try:
            btn.configure(style=_BUTTON_STYLES.get(btn, "Custom.TButton"))
        except tk.TclError:
            pass

    for combo_name in ("theme_combo", "sort_combo", "launch_mode_combo", "size_combo"):
        combo = globals().get(combo_name)
        if combo is not None:
            try:
                combo.configure(style="TCombobox")
            except tk.TclError:
                continue
            _style_combo_popdown(combo)

    try:
        tree.configure(style="Treeview")
    except tk.TclError:
        pass
    if library_grid is not None:
        try:
            library_grid.recolor()
        except Exception as e:
            print(f"[WLM] Could not recolor the cover grid: {e}")

_GOG_STOP_DIRS = {"drive_c", "gog games", "games", "gog.com", "program files", "program files (x86)"}
_source_cache = {}

def _detect_gog_id(exe_path):
    """id GOG dari goggame-<id>.info di folder exe (naik maksimal 4 tingkat), atau None."""
    try:
        p = Path(exe_path).parent
    except Exception:
        return None
    for _ in range(4):
        try:
            with os.scandir(p) as it:
                for e in it:
                    n = e.name.lower()
                    if n.startswith("goggame-") and n.endswith(".info"):
                        return e.name[len("goggame-"):-len(".info")]
        except OSError:
            return None
        if p.name.lower() in _GOG_STOP_DIRS or p.parent == p:
            return None
        p = p.parent
    return None

def game_source(name):
    """{'source': 'gog'|'other', 'gog_id': str|None}. Game dianggap GOG kalau (1) dibuat lewat GOG
    Store (tanda 'source' di runner_config.json) atau (2) folder game-nya berisi goggame-*.info -
    jadi game GOG yang diinstall manual lewat INSTALL APPS / + ADD juga ikut terdeteksi."""
    hit = _source_cache.get(name)
    if hit is not None:
        return hit
    cfg = load_runner_config().get(name) or {}
    result = {"source": "other", "gog_id": None}
    if cfg.get("source") == "gog":
        result = {"source": "gog", "gog_id": cfg.get("gog_id")}
    else:
        exe = extract_exe_path_from_script(bashlaunch_dir / f"{name}.sh")
        gid = _detect_gog_id(exe) if exe else None
        if gid:
            result = {"source": "gog", "gog_id": gid}
    _source_cache[name] = result
    return result

def source_label(name):
    return "GOG" if game_source(name)["source"] == "gog" else "Non-GOG"

def update_script_list(sort_order=None, keep_cache=False):
    """Isi ulang daftar game: urutan (A-Z/Z-A) + filter pencarian. Game yang sedang dipilih
    tetap terpilih setelah daftar dimuat ulang (kalau masih lolos filter)."""
    if not keep_cache:
        _source_cache.clear()

    if sort_order is None:
        try:
            sort_order = "descending" if sort_combo.get() == "Z-A" else "ascending"
        except NameError:
            sort_order = "ascending"

    previous = None
    try:
        focused = tree.focus()
        if focused:
            previous = tree.item(focused, "values")[1]
    except (tk.TclError, IndexError):
        previous = None

    for row in tree.get_children():
        tree.delete(row)

    all_files = sorted(bashlaunch_dir.glob("*.sh"), key=lambda x: x.stem.lower())
    total = len(all_files)
    try:
        query = search_var.get().strip().lower()
    except NameError:
        query = ""
    script_files = [f for f in all_files if query in f.stem.lower()] if query else all_files
    if sort_order == "descending":
        script_files.reverse()

    restore_iid = None
    for index, file in enumerate(script_files, start=1):
        iid = tree.insert("", "end", values=(index, file.stem, source_label(file.stem)))
        if file.stem == previous:
            restore_iid = iid

    if library_grid is not None:
        library_grid.set_items([f.stem for f in script_files])

    shown = len(script_files)
    try:
        if query:
            count_label.config(text=f"{shown} of {total} games")
        else:
            count_label.config(text=f"{total} game" + ("" if total == 1 else "s"))
        if total == 0:
            set_empty_state("Your library is empty\n\nUse + Add Game, Install Apps or the GOG Store\nto get started")
        elif shown == 0:
            set_empty_state(f'No games match "{query}"')
        else:
            set_empty_state("")
    except NameError:
        pass

    if restore_iid is not None:
        tree.selection_set(restore_iid)
        tree.focus(restore_iid)
        tree.see(restore_iid)
        root.after(100, on_select)
    elif tree.get_children():
        root.after(100, on_select)
    else:
        game_title_label.config(text="No Game Selected")
        icon_label.config(image='')
        icon_label.image = None
        info_text.set("Select a game to view details")
        _sync_action_buttons(False)

ANSI_ESCAPE_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

def stream_output(script_name_only, proc, master_fd, log_path):
    """Berjalan pada thread latar belakang dan juga menggabungkan stdout dan stderr dari proses game dan menampilkannya
    secara langsung/ real time"""
    entry = running_games.get(script_name_only)
    try:
        with open(log_path, "w", encoding="utf-8", errors="replace") as log_f:
            while True:
                try:
                    ready, _, _ = select.select([master_fd], [], [], 0.25)
                except OSError:
                    break

                if master_fd in ready:
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError as e:
                        if e.errno == errno.EIO:
                            break
                        raise
                    if not data:
                        break
                    text = ANSI_ESCAPE_RE.sub('', data.decode("utf-8", errors="replace"))
                    text = text.replace("\r\n", "\n").replace("\r", "\n")
                    log_f.write(text)
                    log_f.flush()
                    if entry:
                        entry["queue"].put(text)

                if proc.poll() is not None and not ready:
                    break
    except Exception as e:
        if entry:
            entry["queue"].put(f"\n[launcher] Error reading process output: {e}\n")
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        proc.wait()
        if entry:
            entry["queue"].put(f"\n[launcher] Process exited (code {proc.returncode}).\n")
            entry["finished"] = True

def poll_log_queues():
    """Runs periodically on the GUI thread (via root.after). Drains any
    pending output for every tracked game and, if that game's log window
    is currently open, appends the new lines to its Text widget."""
    for script_name_only, entry in list(running_games.items()):
        new_lines = []
        try:
            while True:
                new_lines.append(entry["queue"].get_nowait())
        except queue.Empty:
            pass

        if new_lines:
            entry["buffer"].extend(new_lines)
            if len(entry["buffer"]) > MAX_LOG_BUFFER_LINES:
                entry["buffer"] = entry["buffer"][-MAX_LOG_BUFFER_LINES:]

            text_widget = entry.get("text_widget")
            if text_widget and text_widget.winfo_exists():
                was_at_bottom = text_widget.yview()[1] >= 0.999
                text_widget.config(state="normal")
                text_widget.insert(tk.END, "".join(new_lines))
                text_widget.config(state="disabled")
                if was_at_bottom:
                    text_widget.see(tk.END)

            status_label_widget = entry.get("status_label")
            if entry["finished"] and status_label_widget and status_label_widget.winfo_exists():
                status_label_widget.config(text="⚪ Finished", fg=COLORS["text_secondary"])

    root.after(150, poll_log_queues)

def open_log_window(script_name_only):
    """Open (or focus, if already open) a real-time log viewer window
    for the given game. Works both while the game is running (live tail)
    and afterwards (shows the last saved log file)."""
    entry = running_games.get(script_name_only)
    log_path = logs_dir / f"{script_name_only}.log"

    if entry and entry.get("window") is not None and entry["window"].winfo_exists():
        entry["window"].lift()
        entry["window"].focus_set()
        return

    win = tk.Toplevel(root)
    win.title(f"Logs - {script_name_only}")
    win.configure(bg=COLORS["primary"])
    win.geometry("800x500")
    win.minsize(400, 250)

    top_bar = ttk.Frame(win, padding=(10, 8))
    top_bar.pack(fill=tk.X)

    ttk.Label(top_bar, text=script_name_only, font=FONTS["subtitle"]).pack(side=tk.LEFT)

    is_live = bool(entry and not entry.get("finished"))
    state_text = "🟢 Running (live)" if is_live else "⚪ Not running (last saved log)"
    state_color = COLORS["success"] if is_live else COLORS["text_secondary"]
    run_state_label = tk.Label(top_bar, text=state_text, font=FONTS["small"], fg=state_color)
    run_state_label.pack(side=tk.RIGHT)

    text_frame = ttk.Frame(win)
    text_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

    text_scroll = ttk.Scrollbar(text_frame, orient=tk.VERTICAL)
    text_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    text_widget = tk.Text(text_frame, wrap=tk.NONE, state="disabled",
                           bg=COLORS["text_background"], fg=COLORS["text"],
                           insertbackground=COLORS["text"], font=("Courier", 9),
                           yscrollcommand=text_scroll.set)
    text_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    text_scroll.config(command=text_widget.yview)

    def insert_initial(content):
        text_widget.config(state="normal")
        text_widget.insert(tk.END, content)
        text_widget.config(state="disabled")
        text_widget.see(tk.END)

    if entry:
        insert_initial("".join(entry["buffer"]) if entry["buffer"] else "(waiting for output...)\n")
        entry["window"] = win
        entry["text_widget"] = text_widget
        entry["status_label"] = run_state_label
    elif log_path.exists():
        try:
            insert_initial(log_path.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            insert_initial(f"[launcher] Could not read log file: {e}\n")
    else:
        insert_initial("(No logs yet - launch this game at least once first)\n")

    def on_close():
        if entry:
            entry["window"] = None
            entry["text_widget"] = None
            entry["status_label"] = None
        win.destroy()
    win.protocol("WM_DELETE_WINDOW", on_close)

    bottom_bar = ttk.Frame(win, padding=(10, 0, 10, 10))
    bottom_bar.pack(fill=tk.X)

    def clear_view():
        text_widget.config(state="normal")
        text_widget.delete("1.0", tk.END)
        text_widget.config(state="disabled")

    ttk.Button(bottom_bar, text="Clear View", command=clear_view).pack(side=tk.LEFT)
    ttk.Button(bottom_bar, text="Open Log File",
               command=lambda: subprocess.Popen(["xdg-open", str(log_path)],
                                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                 env=get_clean_subprocess_env())
               if log_path.exists() else messagebox.showinfo("Info", "No log file yet")
               ).pack(side=tk.LEFT, padx=(8, 0))

def view_logs():
    """Handler for the LOGS button - shows the live/last log for whichever
    game is currently selected in the tree."""
    selected = tree.focus()
    if not selected:
        messagebox.showinfo("Info", "Please select a game first")
        return
    script_name_only = tree.item(selected, "values")[1]
    open_log_window(script_name_only)

def run_script():
    """Run the selected script"""
    selected = tree.focus()
    if not selected:
        messagebox.showinfo("Info", "Please select a game first")
        return
    
    script_name_only = tree.item(selected, "values")[1]
    script_name = script_name_only + ".sh"
    script_path = bashlaunch_dir / script_name
    launch_mode = launch_mode_combo.get()
    
    if not script_path.exists():
        safe_status_config(text=f"Error: Script file not found: {script_name}", fg=COLORS["danger"])
        return

    choice = ask_runner_choice(parent_script_name=script_name_only, purpose="play")
    if choice is None:
        safe_status_config(text="Launch cancelled.", fg=COLORS["text_secondary"])
        return

    exe_path = extract_exe_path_from_script(script_path)
    folder_path = extract_folder_path_from_script(script_path)
    if exe_path and folder_path:
        try:
            new_content = build_script_content(folder_path, exe_path, choice)
            with open(script_path, 'w') as f:
                f.write(new_content)
        except Exception as e:
            safe_status_config(text=f"Error updating script for runner: {str(e)}", fg=COLORS["danger"])
            return
    else:
        safe_status_config(text="Error: Could not read exe/folder path from script.", fg=COLORS["danger"])
        return

    runner_cfg = load_runner_config()
    prev_entry = runner_cfg.get(script_name_only) or {}
    choice = dict(choice)
    for key in ("source", "gog_id"):          # tanda GOG jangan hilang saat runner diganti
        if key in prev_entry and key not in choice:
            choice[key] = prev_entry[key]
    runner_cfg[script_name_only] = choice
    save_runner_config(runner_cfg)

    if not os.access(script_path, os.X_OK):
        try:
            script_path.chmod(0o755)
        except Exception as e:
            safe_status_config(text=f"Error: Cannot set executable permission for {script_name}.", fg=COLORS["danger"])
            return

    commands = {
        "Normal": ["bash", str(script_path)],
        "GalliumHUD": ["bash", "-c", f"{build_gallium_hud_env_prefix()} \"{str(script_path)}\""],
        "VulkanHUD": ["bash", "-c", f"{build_vulkan_hud_env_prefix()} \"{str(script_path)}\""],
        "MangoHud-GL": ["bash", "-c", f"mangohud --dlsym \"{str(script_path)}\""],
        "Mangohud": ["bash", "-c", f"mangohud \"{str(script_path)}\""]
    }
    
    command = commands.get(launch_mode, commands["Normal"])
    log_path = logs_dir / f"{script_name_only}.log"

    try:
        master_fd, slave_fd = pty.openpty()
        proc = subprocess.Popen(command,
                                 stdout=slave_fd,
                                 stderr=slave_fd,
                                 stdin=slave_fd,
                                 close_fds=True,
                                 start_new_session=True,
                                 env=get_clean_subprocess_env())
        os.close(slave_fd)

        running_games[script_name_only] = {
            "proc": proc,
            "log_path": log_path,
            "queue": queue.Queue(),
            "buffer": [],
            "window": None,
            "text_widget": None,
            "status_label": None,
            "finished": False,
        }
        threading.Thread(target=stream_output, args=(script_name_only, proc, master_fd, log_path), daemon=True).start()

        if choice["runner"] == "protonge":
            runner_label = f"Proton GE ({choice['prefix_code']})"
        elif choice["runner"] == "protoncachyos":
            runner_label = f"Proton-CachyOS ({choice['prefix_code']})"
        elif choice.get("prefix_code"):
            runner_label = f"Wine ({choice['prefix_code']})"
        else:
            runner_label = "Wine"
        safe_status_config(text=f"Launching {script_name[:-3]} via {runner_label} in {launch_mode} mode...", fg=COLORS["success"])
    except FileNotFoundError:
        messagebox.showerror("Error", f"Launcher command not found. Do you have the necessary tools installed?")
        safe_status_config(text=f"Error: Launcher command not found.", fg=COLORS["danger"])
    except Exception as e:
        safe_status_config(text=f"Error launching script: {str(e)}", fg=COLORS["danger"])

def reset_button_hover_state(btn):
    """Ttk's 'active' (hover-highlight) state normally gets cleared by a real mouse <Leave>
    event, but that event can get lost when a button's command opens a modal dialog
    (grab_set, seperti ask_runner_choice) or launches an external, focus-stealing program
    (game) right in the middle of the click. Akibatnya tombol tetap terlihat menyala
    (warna hover) walau game sudah ditutup, sampai pengguna mengarahkan mouse ke tombol lain
    dulu (yang barulah memaksa Tk menyadari statenya sudah tidak sesuai). Fungsi ini
    membersihkan flag tersebut secara manual lalu menentukan ulang state-nya dari posisi
    pointer mouse yang sebenarnya saat ini, supaya highlight-nya tetap benar (bukan cuma
    dimatikan paksa walau mouse masih diatas tombol)."""
    try:
        btn.state(["!pressed", "!active"])
        x, y = btn.winfo_pointerxy()
        if btn.winfo_containing(x, y) is btn:
            btn.state(["active"])
    except Exception:
        pass

def add_game_shortcut(name, exe_path, choice, icon_image=None, work_dir=None, source=None, gog_id=None):
    """Tambah game ke daftar launcher TANPA dialog (script .sh + runner config + ikon), dipakai
    GOG Store setelah installer selesai - setara dengan + ADD, tapi otomatis dan langsung
    terkait ke runner/prefix tempat game itu diinstall (penting untuk game GOG).

    choice: dict hasil ask_runner_choice (runner, prefix_code, prefix_path, proton_*, ...).
    icon_image: PIL.Image atau path gambar (mis. cover GOG / .ico) - dipotong persegi seperti Change Icon.
    Kalau nama sudah dipakai game LAIN, nama diberi angka ('Nama 2'); kalau exe-nya sama
    (mis. install ulang) entri lama diperbarui. Harus dipanggil dari main thread (Tk).
    source: "gog" untuk game yang diunduh/diinstall lewat GOG Store (gog_id = id produk GOG);
    game tanpa tanda ini tetap dideteksi lewat goggame-*.info di folder game (lihat game_source).
    Return nama script (tanpa .sh) yang dipakai."""
    exe_resolved = Path(exe_path).resolve()
    folder_resolved = Path(work_dir).resolve() if work_dir else exe_resolved.parent
    base = "".join(c for c in (name or "") if c.isalnum() or c in (' ', '_', '-')).strip() or exe_resolved.stem
    safe_name, n = base, 2
    while True:
        script_path = bashlaunch_dir / f"{safe_name}.sh"
        if not script_path.exists():
            break
        existing = extract_exe_path_from_script(script_path)
        if existing and Path(existing) == exe_resolved:
            break
        safe_name = f"{base} {n}"
        n += 1

    cfg_entry = dict(choice)
    cfg_entry.setdefault("launch_options", "")
    cfg_entry.setdefault("comment", "")
    if source:
        cfg_entry["source"] = source
    if gog_id not in (None, ""):
        cfg_entry["gog_id"] = str(gog_id)
    bashlaunch_dir.mkdir(parents=True, exist_ok=True)
    with open(script_path, "w") as script_file:
        script_file.write(build_script_content(str(folder_resolved), str(exe_resolved), cfg_entry))
    script_path.chmod(0o755)

    runner_cfg = load_runner_config()
    runner_cfg[safe_name] = cfg_entry
    save_runner_config(runner_cfg)

    icon_path = icon_dir / f"{safe_name}.png"
    if icon_image is not None and not icon_path.exists():
        try:
            src = icon_image
            if isinstance(src, (str, Path)):
                src = Image.open(src)
                sizes = src.info.get("sizes")
                if sizes:                      # .ico multi-ukuran: pakai yang terbesar
                    src.size = max(sizes)
                src.load()
            image = src.convert("RGBA")
            side = min(image.size)
            left, top = (image.width - side) // 2, (image.height - side) // 2
            image = image.crop((left, top, left + side, top + side)).resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
            image.save(icon_path, "PNG")
        except Exception as e:
            print(f"[WLM] Could not save icon for {safe_name}: {e}")

    update_script_list()
    for iid in tree.get_children():
        if tree.item(iid, "values")[1] == safe_name:
            tree.selection_set(iid)
            tree.focus(iid)
            tree.see(iid)
            break
    return safe_name

def add_script():
    """Add a new script"""
    exe_path = filedialog.askopenfilename(
        title="Select Windows Executable (.exe)",
        filetypes=[("Executable Files", "*.exe"), ("All Files", "*.*")]
    )

    if not exe_path:
        return

    exe_path_obj = Path(exe_path)
    exe_name = exe_path_obj.stem
    suggested_name = exe_name

    while True:
        new_name = simpledialog.askstring(
            "Rename Script",
            "Enter script name (for display):",
            initialvalue=suggested_name
        )

        if not new_name:
            return

        safe_new_name = "".join(c for c in new_name if c.isalnum() or c in (' ', '_', '-')).strip()

        if not safe_new_name:
            messagebox.showerror("Error", "Invalid script name.")
            return

        script_path = bashlaunch_dir / f"{safe_new_name}.sh"
        if script_path.exists():
            choice = messagebox.askyesnocancel(
                "Name Already Exists",
                f"A game named '{safe_new_name}' already exists.\n\n"
                "Yes = Replace it (its old launch script & runner settings will be overwritten)\n"
                "No = Enter a different name instead\n"
                "Cancel = Don't add this game"
            )
            if choice is None:
                return
            if choice is False:
                suggested_name = safe_new_name
                continue
        break

    folder_path = exe_path_obj.parent
    exe_resolved = exe_path_obj.resolve()
    folder_resolved = folder_path.resolve()

    script_content = (
        "#!/bin/bash\n"
        f"cd \"{folder_resolved}\"\n"
        f"wine \"{exe_resolved}\"\n"
    )

    try:
        with open(script_path, "w") as script_file:
            script_file.write(script_content)

        script_path.chmod(0o755)

        owning = find_owning_prefix(exe_resolved)
        status_extra = ""
        if owning and (owning["runner"] == "wine" or owning.get("proton_path")):
            cfg_entry = {
                "runner": owning["runner"],
                "prefix_code": owning["prefix_code"],
                "prefix_path": owning["prefix_path"],
                "launch_options": "",
                "comment": ""
            }
            if owning["runner"] in ("protonge", "protoncachyos"):
                cfg_entry["proton_name"] = owning.get("proton_name") or ""
                cfg_entry["proton_path"] = owning["proton_path"]

            new_script_content = build_script_content(str(folder_resolved), str(exe_resolved), cfg_entry)
            with open(script_path, "w") as script_file:
                script_file.write(new_script_content)
            script_path.chmod(0o755)

            runner_cfg = load_runner_config()
            runner_cfg[safe_new_name] = cfg_entry
            save_runner_config(runner_cfg)
            status_extra = f" (linked to existing prefix {owning['prefix_code']})"
        else:
            runner_cfg = load_runner_config()
            if safe_new_name in runner_cfg:
                del runner_cfg[safe_new_name]
                save_runner_config(runner_cfg)

        update_script_list()
        safe_status_config(text=f"Added: {safe_new_name}{status_extra}", fg=COLORS["success"])
    except Exception as e:
        safe_status_config(text=f"Error creating script: {str(e)}", fg=COLORS["danger"])

def remove_script():
    """Remove the selected script"""
    selected = tree.focus()
    if not selected:
        messagebox.showinfo("Info", "Please select a game first")
        return
    
    script_name = tree.item(selected, "values")[1]
    
    if messagebox.askyesno("Confirm", f"Are you sure you want to remove '{script_name}'?"):
        script_path = bashlaunch_dir / f"{script_name}.sh"
        icon_path = icon_dir / f"{script_name}.png"
        
        try:
            script_path.unlink(missing_ok=True)
            icon_path.unlink(missing_ok=True)
            if cover_store is not None:
                cover_store.forget(script_name)

            runner_cfg = load_runner_config()
            if script_name in runner_cfg:
                del runner_cfg[script_name]
                save_runner_config(runner_cfg)
            
            update_script_list()
            game_title_label.config(text="No Game Selected")
            icon_label.config(image='')
            icon_label.image = None
            info_text.set("Select a game to view details")
            safe_status_config(text=f"Removed: {script_name}", fg=COLORS["warning"])
        except Exception as e:
            safe_status_config(text=f"Error removing files: {str(e)}", fg=COLORS["danger"])

def rename_script():
    """Rename the selected script"""
    selected = tree.focus()
    if not selected:
        messagebox.showinfo("Info", "Please select a game first")
        return
    
    old_name = tree.item(selected, "values")[1]
    new_name_input = simpledialog.askstring(
        "Rename Script",
        "Enter new script name:",
        initialvalue=old_name
    )
    
    if new_name_input and new_name_input != old_name:
        new_name = "".join(c for c in new_name_input if c.isalnum() or c in (' ', '_', '-')).strip()
        if not new_name:
            messagebox.showerror("Error", "Invalid new script name.")
            return

        old_path = bashlaunch_dir / f"{old_name}.sh"
        new_path = bashlaunch_dir / f"{new_name}.sh"
        old_icon = icon_dir / f"{old_name}.png"
        new_icon = icon_dir / f"{new_name}.png"
        
        try:
            if new_path.exists():
                messagebox.showerror("Error", f"Script '{new_name}' already exists.")
                return

            old_path.rename(new_path)
            if old_icon.exists():
                old_icon.rename(new_icon)
            if cover_store is not None:
                cover_store.rename(old_name, new_name)

            runner_cfg = load_runner_config()
            if old_name in runner_cfg:
                runner_cfg[new_name] = runner_cfg.pop(old_name)
                save_runner_config(runner_cfg)
            
            update_script_list()
            
            for item in tree.get_children():
                if tree.item(item, "values")[1] == new_name:
                    tree.focus(item)
                    tree.selection_set(item)
                    on_select()
                    break
                    
            safe_status_config(text=f"Renamed to: {new_name}", fg=COLORS["success"])
        except Exception as e:
            safe_status_config(text=f"Error renaming script: {str(e)}", fg=COLORS["danger"])

def change_icon():
    """Change the icon for the selected script"""
    selected = tree.focus()
    if not selected:
        messagebox.showinfo("Info", "Please select a game first")
        return
    
    script_name = tree.item(selected, "values")[1]
    icon_path = filedialog.askopenfilename(
        title="Select Icon",
        filetypes=[("Image Files", "*.png *.jpg *.jpeg *.ico *.bmp"), ("All Files", "*.*")]
    )
    
    if icon_path:
        try:
            image = Image.open(icon_path)
            if image.mode != 'RGBA':
                image = image.convert('RGBA')
            
            width, height = image.size
            new_size = min(width, height)
            
            left = (width - new_size) // 2
            top = (height - new_size) // 2
            right = (width + new_size) // 2
            bottom = (height + new_size) // 2
            
            image = image.crop((left, top, right, bottom))
            image = image.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
            
            image.save(icon_dir / f"{script_name}.png", "PNG", quality=95)
            
            load_icon(script_name)
            safe_status_config(text=f"Icon updated for {script_name}", fg=COLORS["success"])
        except Exception as e:
            messagebox.showerror("Error", f"Failed to process image: {str(e)}")
            safe_status_config(text=f"Error processing image: {str(e)}", fg=COLORS["danger"])

def round_icon_corners(image, radius=16):
    """Bulatkan sudut gambar ikon (RGBA) supaya tampil seperti kartu modern."""
    try:
        w, h = image.size
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
        alpha = ImageChops.multiply(image.getchannel("A"), mask)
        rounded = image.copy()
        rounded.putalpha(alpha)
        return rounded
    except Exception:
        return image

def load_icon(script_name):
    """Load and display the icon at the correct size"""
    icon_path = icon_dir / f"{script_name}.png"
    
    if icon_path.exists():
        try:
            image = Image.open(icon_path)
            if image.mode != 'RGBA':
                image = image.convert('RGBA')
            
            if image.size != (ICON_WIDTH, ICON_HEIGHT):
                image = image.resize((ICON_WIDTH, ICON_HEIGHT), Image.LANCZOS)
            
            image = round_icon_corners(image)
            photo = ImageTk.PhotoImage(image)
            
            icon_label.config(image=photo)
            icon_label.image = photo
            
        except Exception as e:
            icon_label.config(image='')
            icon_label.image = None
    else:
        icon_label.config(image='')
        icon_label.image = None

def on_select(event=None):
    """Handle item selection in the treeview"""
    def do_select():
        selected = tree.focus()
        _sync_action_buttons(bool(selected))

        if library_grid is not None:
            library_grid.highlight(tree.item(selected, "values")[1] if selected else None)
        
        if selected:
            script_name = tree.item(selected, "values")[1]
            game_title_label.config(text=script_name)
            
            load_icon(script_name)
            
            script_path = bashlaunch_dir / f"{script_name}.sh"
            if script_path.exists():
                try:
                    stat = script_path.stat()
                    mod_time = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                    
                    size_bytes = stat.st_size
                    size_kb = size_bytes / 1024
                    size_mb = size_kb / 1024
                    
                    if size_mb > 1:
                        size_str = f"{size_mb:.2f} MB"
                    elif size_kb > 1:
                        size_str = f"{size_kb:.2f} KB"
                    else:
                        size_str = f"{size_bytes} bytes"
                        
                    with open(script_path, 'r') as f:
                        lines = f.readlines()
                        folder_path = ""
                        if len(lines) >= 2:
                            folder_path = lines[1].strip().replace('cd "', '').replace('"', '')
                    
                    src = game_source(script_name)
                    src_text = "GOG" + (f" (id {src['gog_id']})" if src["gog_id"] else "") \
                        if src["source"] == "gog" else "Non-GOG (added manually / other installer)"
                    info_text.set(f"Source: {src_text}\n"
                                  f"Script File: {script_name}.sh\n"
                                  f"Location: {folder_path}\n"
                                  f"Last Modified: {mod_time}\n"
                                  f"Size: {size_str}")
                except Exception as e:
                    info_text.set(f"File information unavailable: {e}")
            else:
                info_text.set("File information not available (script file missing)")
        else:
            game_title_label.config(text="No Game Selected")
            icon_label.config(image='')
            icon_label.image = None
            info_text.set("Select a game to view details")

    root.after(0, do_select)


def open_file_manager():
    """Open the selected game's installation folder location in the system file manager"""
    selected = tree.focus()
    if not selected:
        messagebox.showinfo("Info", "Please select a game first")
        return
    
    script_name = tree.item(selected, "values")[1]
    script_path = bashlaunch_dir / f"{script_name}.sh"
    
    if not script_path.exists():
        safe_status_config(text=f"Error: Script file not found: {script_name}", fg=COLORS["danger"])
        return

    try:
        with open(script_path, 'r') as f:
            lines = f.readlines()
            folder_path = ""
            if len(lines) >= 2 and lines[1].strip().startswith("cd "):
                folder_path = lines[1].strip().replace('cd "', '').replace('"', '')

        if folder_path and Path(folder_path).is_dir():
            subprocess.Popen(["xdg-open", folder_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              env=get_clean_subprocess_env())
            safe_status_config(text=f"Opening folder for {script_name}...", fg=COLORS["text_secondary"])
        else:
            messagebox.showerror("Error", f"Folder path not found or invalid in script for {script_name}.")
            safe_status_config(text=f"Error: Invalid folder path in script.", fg=COLORS["danger"])
            
    except Exception as e:
        safe_status_config(text=f"Error opening file manager: {str(e)}", fg=COLORS["danger"])

def open_save_manager(script_name=None, parent_win=None):
    """Buka Save Manager (gog_saves.py) untuk sebuah game: backup/restore save lokal dan cloud
    save GOG (cek + download yang menimpa, dengan peringatan).

    - script_name: nama game (tanpa .sh). Kalau tidak diisi, diambil dari game yang sedang
      dipilih di daftar/tree utama (dipakai kalau dipanggil dari luar dialog Play, mis. menu lain).
    - parent_win: window induk tempat dialog Saves harus muncul DI ATASNYA (mis. dialog
      Select Runner - Play). Kosong = jendela utama launcher.
    """
    from types import SimpleNamespace
    if script_name is None:
        selected = tree.focus()
        if not selected:
            messagebox.showinfo("Info", "Please select a game first")
            return
        script_name = tree.item(selected, "values")[1]
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import gog_saves
    except ImportError as e:
        messagebox.showerror("Saves", f"gog_saves.py was not found next to the launcher:\n{e}")
        return
    script_path = bashlaunch_dir / f"{script_name}.sh"
    src = game_source(script_name)
    parent_for_dialog = parent_win if (parent_win is not None and parent_win.winfo_exists()) else root

    # Kalau dipanggil dari dialog modal (mis. Select Runner - Play, yang memegang grab_set()),
    # lepas dulu grab-nya - kalau tidak, jendela Save Manager (non-modal) akan muncul tapi
    # semua isinya tidak merespon klik sama sekali selama dialog Play masih memegang grab.
    # Grab itu dikembalikan otomatis begitu jendela Save Manager ditutup (lihat <Destroy> di bawah).
    release_grab_on = parent_win if (parent_win is not None and parent_win.winfo_exists()) else None
    if release_grab_on is not None:
        try:
            release_grab_on.grab_release()
        except tk.TclError:
            pass

    def is_running():
        entry = running_games.get(script_name)
        return bool(entry and entry.get("proc") is not None and entry["proc"].poll() is None)

    def open_folder(path):
        try:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=get_clean_subprocess_env())
        except Exception as e:
            safe_status_config(text=f"Could not open folder: {e}", fg=COLORS["danger"])

    gog_saves.open_dialog(SimpleNamespace(
        root=parent_for_dialog, fonts=FONTS, colors=lambda: COLORS, data_dir=directory, name=script_name,
        exe_path=extract_exe_path_from_script(script_path), choice=load_runner_config().get(script_name) or {},
        source=src["source"], human_size=human_size, open_folder=open_folder, is_running=is_running,
        place_dialog=place_dialog,
        status=lambda text, level="info": safe_status_config(
            text=text, fg=COLORS["text_secondary" if level == "info" else level])))

    if release_grab_on is not None:
        saves_win = gog_saves._dialogs.get(script_name)
        if saves_win is not None and saves_win.winfo_exists():
            def restore_grab(_evt=None, _target=release_grab_on):
                if _target.winfo_exists():
                    try:
                        _target.grab_set()
                        _target.lift()
                        _target.focus_set()
                    except tk.TclError:
                        pass
            saves_win.bind("<Destroy>", restore_grab, add="+")

def open_wine_prefix_folder():
    """Open the Wine Prefix folder (~/.wine)"""
    wine_prefix = os.environ.get("WINEPREFIX", Path.home() / ".wine")
    
    try:
        if Path(wine_prefix).is_dir():
            subprocess.Popen(["xdg-open", str(wine_prefix)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              env=get_clean_subprocess_env())
            safe_status_config(text="Opening Wine Prefix Folder...", fg=COLORS["text_secondary"])
        else:
            messagebox.showerror("Error", f"Wine Prefix folder not found: {wine_prefix}")
            safe_status_config(text="Error: Wine Prefix folder not found.", fg=COLORS["danger"])
    except Exception as e:
        safe_status_config(text=f"Error opening Wine Prefix: {str(e)}", fg=COLORS["danger"])

def open_winecfg():
    """Open winecfg"""
    try:
        subprocess.Popen(["winecfg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          env=get_clean_subprocess_env())
        safe_status_config(text="Opening Wine Configuration...", fg=COLORS["text_secondary"])
    except FileNotFoundError:
        messagebox.showerror("Error", "The 'wine' command was not found.")
        safe_status_config(text="Error: Wine command not found.", fg=COLORS["danger"])

def _get_install_watch():
    """Muat install_watch.py (satu folder dengan launcher). None kalau file itu tidak ada -
    fitur auto shortcut saja yang mati, install tetap jalan."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import install_watch
        return install_watch
    except ImportError:
        print("[WLM] install_watch.py not found next to the launcher - auto shortcut disabled.")
        return None

def ask_shortcut_dialog(candidates, drive_c, default_name):
    """Dialog konfirmasi shortcut untuk install manual NON-GOG (exe kandidat tidak bisa dipastikan).
    candidates: hasil install_watch.find_candidates (terbesar dulu). Return {"name", "exe"} atau None."""
    def rel(path):
        try:
            return str(Path(path).relative_to(drive_c))
        except (ValueError, TypeError):
            return str(path)

    exe_paths = [c["exe"] for c in candidates]
    labels = [f"{rel(c['exe'])}  ({human_size(c['size'])})" for c in candidates]

    dialog = tk.Toplevel(root)
    dialog.withdraw()
    dialog.title("Add Shortcut")
    dialog.configure(bg=COLORS["primary"])
    dialog.resizable(False, False)
    result = {"value": None}

    frame = ttk.Frame(dialog, padding=15)
    frame.pack(fill=tk.BOTH, expand=True)
    ttk.Label(frame, text="Setup finished. Add the installed program to the launcher?",
              font=FONTS["normal"]).pack(anchor="w", pady=(0, 10))
    ttk.Label(frame, text="Name:", font=FONTS["small"]).pack(anchor="w", pady=(0, 2))
    name_entry = ttk.Entry(frame, width=60, font=FONTS["small"])
    name_entry.insert(0, default_name)
    name_entry.pack(fill=tk.X, pady=(0, 8))
    ttk.Label(frame, text="Executable to launch:", font=FONTS["small"]).pack(anchor="w", pady=(0, 2))
    combo = ttk.Combobox(frame, values=labels, state="readonly", width=60, font=FONTS["small"])
    combo.current(0)
    combo.pack(fill=tk.X, pady=(0, 6))

    def do_browse():
        start = str(drive_c) if drive_c and Path(drive_c).is_dir() else str(Path.home())
        picked = filedialog.askopenfilename(
            parent=dialog, title="Select Executable", initialdir=start,
            filetypes=[("Executable Files", "*.exe"), ("All Files", "*.*")])
        if picked:
            exe_paths.append(Path(picked))
            labels.append(picked)
            combo["values"] = labels
            combo.current(len(labels) - 1)

    ttk.Button(frame, text="Browse Other...", command=do_browse, style="Custom.TButton").pack(anchor="w", pady=(0, 12))

    def do_ok():
        name = name_entry.get().strip()
        if not name:
            messagebox.showerror("Error", "Please enter a name.", parent=dialog)
            return
        result["value"] = {"name": name, "exe": exe_paths[combo.current()]}
        dialog.destroy()

    row = ttk.Frame(frame)
    row.pack()
    ttk.Button(row, text="Add Shortcut", command=do_ok, style="Custom.TButton", width=14).grid(row=0, column=0, padx=4)
    ttk.Button(row, text="Skip", command=dialog.destroy, style="Custom.TButton", width=14).grid(row=0, column=1, padx=4)

    dialog.update_idletasks()
    w, h = dialog.winfo_reqwidth(), dialog.winfo_reqheight()
    x = max(root.winfo_x() + (root.winfo_width() // 2) - (w // 2), 0)
    y = max(root.winfo_y() + (root.winfo_height() // 2) - (h // 2), 0)
    dialog.geometry(f"+{x}+{y}")
    dialog.deiconify()
    dialog.transient(root)
    dialog.grab_set()
    dialog.lift()
    dialog.focus_set()
    dialog.wait_window()
    return result["value"]

def _watch_manual_install(iw, proc, choice, started):
    """Thread: pantau installer manual (INSTALL APPS) sampai game terdeteksi/installer berhenti."""
    res = iw.wait_for_install(proc, choice, None, started)
    root.after(0, lambda: _finish_manual_install(choice, res))

def _finish_manual_install(choice, res):
    """Main thread: tambahkan hasil install manual ke daftar game. Installer GOG (ada
    goggame-*.info) langsung otomatis; installer lain minta konfirmasi dulu karena exe-nya
    hanya bisa ditebak."""
    found, rc, drive_c = res["found"], res["rc"], res["drive_c"]
    try:
        if found is not None:
            name = add_game_shortcut(found.get("name") or Path(found["exe"]).stem, found["exe"], choice,
                                     icon_image=found.get("icon"), work_dir=found.get("workdir"))
            safe_status_config(text=f"Setup finished - '{name}' was added to the launcher.", fg=COLORS["success"])
            return
        if rc != 0:
            safe_status_config(text=f"Setup closed (exit code {rc}) - no shortcut was added.",
                               fg=COLORS["text_secondary"])
            return
        if not res["candidates"]:
            safe_status_config(text="Setup finished - no new game detected, so no shortcut was added.",
                               fg=COLORS["text_secondary"])
            return
        picked = ask_shortcut_dialog(res["candidates"], drive_c, res["candidates"][0]["game_dir"].name)
        if not picked:
            safe_status_config(text="Setup finished - no shortcut added.", fg=COLORS["text_secondary"])
            return
        name = add_game_shortcut(picked["name"], picked["exe"], choice)
        safe_status_config(text=f"Setup finished - '{name}' was added to the launcher.", fg=COLORS["success"])
    except Exception as e:
        safe_status_config(text=f"Could not add shortcut: {e}", fg=COLORS["danger"])

def run_exe_setup(exe_path=None, choice=None, installer_args=None, auto_shortcut=True):
    """Run EXE setup. exe_path opsional - kalau diisi (mis. installer hasil download GOG Store),
    dialog pilih file dilewati dan langsung lanjut ke pilihan runner.
    choice opsional - kalau diisi (hasil ask_runner_choice, mis. dipilih saat download di GOG
    Store), dialog pilih runner juga dilewati dan installer langsung jalan di runner/prefix itu.
    installer_args opsional - list argumen tambahan untuk installer (mis. ["/SILENT"]).
    auto_shortcut - kalau True (bawaan), setelah installer selesai launcher mendeteksi game yang
    baru terpasang di prefix itu dan menambahkannya ke daftar game (lihat install_watch.py).
    GOG Store memberi False karena punya watcher sendiri.
    Return subprocess.Popen proses installer (atau None kalau dibatalkan/gagal dijalankan)."""
    if not exe_path:
        exe_path = filedialog.askopenfilename(
            title="Select Setup Executable (.exe)",
            filetypes=[("Executable Files", "*.exe"), ("All Files", "*.*")]
        )
    
    if not exe_path:
        return

    if choice is None:
        choice = ask_runner_choice(parent_script_name=None, purpose="setup")
    if choice is None:
        safe_status_config(text="Setup cancelled.", fg=COLORS["text_secondary"])
        return

    proc = None
    started = time.time()
    try:
        env_vars, extra_args = parse_launch_options(choice.get("launch_options", ""))
        extra_args = list(extra_args) + list(installer_args or [])
        env = get_clean_subprocess_env()
        for key, val in env_vars:
            env[key] = val

        if choice["runner"] in ("protonge", "protoncachyos"):
            env["STEAM_COMPAT_DATA_PATH"] = choice["prefix_path"]
            env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(find_steam_install_path())
            command = [choice["proton_path"], "run", exe_path] + extra_args
            proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            runner_label = "Proton GE" if choice["runner"] == "protonge" else "Proton-CachyOS"
            safe_status_config(
                text=f"Running setup for {Path(exe_path).name} via {runner_label} (prefix: {choice['prefix_code']})...",
                fg=COLORS["text_secondary"])
        else:
            if choice.get("prefix_path"):
                env["WINEPREFIX"] = choice["prefix_path"]
            command = ["wine", exe_path] + extra_args
            proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            if choice.get("prefix_code"):
                safe_status_config(text=f"Running setup for {Path(exe_path).name} via Wine (prefix: {choice['prefix_code']})...", fg=COLORS["text_secondary"])
            else:
                safe_status_config(text=f"Running setup for {Path(exe_path).name} via Wine...", fg=COLORS["text_secondary"])
    except FileNotFoundError:
        messagebox.showerror("Error", "Runner command was not found.")
        safe_status_config(text="Error: Runner command not found.", fg=COLORS["danger"])
    except Exception as e:
        safe_status_config(text=f"Error running setup: {str(e)}", fg=COLORS["danger"])
    if proc is not None and auto_shortcut:
        iw = _get_install_watch()
        if iw is not None:
            threading.Thread(target=_watch_manual_install, args=(iw, proc, choice, started),
                             daemon=True).start()
    return proc

def open_gog_store():
    """Buka jendela GOG Store. Seluruh logikanya ada di gog_store.py (file terpisah, satu folder
    dengan launcher ini) - disini hanya jembatan ke fungsi-fungsi launcher."""
    from types import SimpleNamespace
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import gog_store
    except ImportError as e:
        messagebox.showerror("GOG Store", f"gog_store.py was not found next to the launcher:\n{e}")
        return
    gog_store.open_dialog(SimpleNamespace(
        root=root, fonts=FONTS, data_dir=directory, colors=lambda: COLORS,
        human_size=human_size, clean_env=get_clean_subprocess_env,
        task_log=open_task_log_window, install_exe=run_exe_setup,
        add_shortcut=add_game_shortcut,
        ask_runner=lambda parent=None, game_title=None: ask_runner_choice(
            parent_script_name=None, purpose="setup", parent=parent, game_title=game_title),
        status=lambda text, level="info": safe_status_config(
            text=text, fg=COLORS["text_secondary" if level == "info" else level]),
    ))

def sort_by_selected(event=None):
    """Handle sorting change"""
    sort_value = sort_combo.get()
    if sort_value == "A-Z":
        update_script_list("ascending")
    elif sort_value == "Z-A":
        update_script_list("descending")

def on_theme_selected(event=None):
    """Handle theme selection from dropdown"""
    selected_display = theme_combo.get()
    theme_name = None
    
    for key, data in THEMES.items():
        if data["name"] == selected_display:
            theme_name = key
            break
    
    if theme_name and theme_name != CURRENT_THEME:
        apply_theme(theme_name)

config = load_config()
CURRENT_THEME = config.get("theme", "default")
COLORS = THEMES.get(CURRENT_THEME, THEMES["default"]) 

root = tk.Tk()
root.title("Wine Launch Manager")
def on_app_close():
    """Tutup launcher. Download GOG yang sedang berjalan dijeda dan disimpan dulu (lihat
    gog_store.shutdown_downloads), supaya bisa di-Resume saat launcher dijalankan lagi."""
    try:
        save_window_config()
    finally:
        gs = sys.modules.get("gog_store")
        if gs is not None and hasattr(gs, "shutdown_downloads"):
            try:
                root.withdraw()          # jendela hilang dulu selagi download dijeda
                gs.shutdown_downloads()
            except Exception as e:
                print(f"[WLM] Could not pause GOG downloads on exit: {e}")
        root.destroy()

root.protocol("WM_DELETE_WINDOW", on_app_close)

style = ttk.Style()
style.theme_use('clam')
apply_best_font_family() 

window_size = config["window_size"]
window_position = config["window_position"]

# Ukuran jendela utama dibatasi ke area layar yang benar-benar tersedia. Sebelumnya minsize
# 1000x720 + title bar (~36px) lebih tinggi dari layar 768px dikurangi panel (~32px), jadi bagian
# bawah jendela menyelinap di bawah taskbar dan taskbar autohide ikut naik tiap jendela diklik.
# Margin vertikal 70px = title bar (~36px) + panel (~32px).
_screen_w, _screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
_MAX_W = max(640, _screen_w - 20)
_MAX_H = max(480, _screen_h - 70)
try:
    _w, _h = (int(v) for v in window_size.split('x'))
except ValueError:
    _w, _h = 1000, 720
_w, _h = min(max(_w, 640), _MAX_W), min(max(_h, 480), _MAX_H)

_pos = None
if window_position:
    _m = re.fullmatch(r"\+(-?\d+)\+(-?\d+)", window_position)
    if _m:
        _pos = (int(_m.group(1)), int(_m.group(2)))
if _pos is None:
    _pos = ((_screen_w - _w) // 2, max(0, (_screen_h - _h) // 2 - 30))
# Posisi tersimpan dijaga tetap didalam layar (mis. setelah resolusi/monitor berubah).
_x = max(0, min(_pos[0], _screen_w - _w))
_y = max(0, min(_pos[1], _screen_h - _h - 70))
root.geometry(f"{_w}x{_h}+{_x}+{_y}")
_last_normal_geometry["size"], _last_normal_geometry["position"] = f"{_w}x{_h}", f"+{_x}+{_y}"


root.resizable(True, True)
root.minsize(min(1000, _MAX_W), min(720, _MAX_H))

_pre_zoom_geometry = {"value": None}

def _maximize_manually():
    """Fallback kalau window manager tidak mendukung maximize lewat Tk: samakan ke ukuran layar."""
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{max(640, sw - 20)}x{max(480, sh - 70)}+0+0")
    _manual_zoom["on"] = True

def toggle_maximize_window(_evt=None):
    """Maximize/un-maximize jendela utama. Kebanyakan window manager Linux sudah punya tombol
    maximize sendiri di titlebar (otomatis jalan karena root.resizable(True, True) di atas),
    tapi tidak semua desktop/WM menampilkannya (mis. beberapa tiling WM) - F11 dijadikan cara
    pasti yang selalu ada, dan dipakai juga kalau tombol WM-nya entah kenapa tidak merespon."""
    if _is_maximized():
        if _manual_zoom["on"]:
            _manual_zoom["on"] = False
            if _pre_zoom_geometry["value"]:
                root.geometry(_pre_zoom_geometry["value"])
        else:
            _set_maximized(False)
        _pre_zoom_geometry["value"] = None
    else:
        _pre_zoom_geometry["value"] = root.geometry()
        if not _set_maximized(True):
            _maximize_manually()
    _save_window_config_debounced()

root.bind("<F11>", toggle_maximize_window)

if config.get("window_maximized"):
    # Terakhir ditutup dalam keadaan maximize -> buka maximize lagi. Tk menerapkannya begitu jendela
    # tampil; dicek lagi sebentar kemudian kalau window manager mengabaikannya.
    if not _set_maximized(True):
        _maximize_manually()

    def _reapply_maximize():
        if not _manual_zoom["on"] and not _is_maximized():
            _set_maximized(True)
    root.after(400, _reapply_maximize)

_window_config_save_after_id = {"id": None}

def _save_window_config_debounced(_evt=None):
    """Simpan ukuran & posisi jendela utama otomatis setiap kali pengguna selesai
    menggeser/mengubah ukurannya - jadi tidak perlu menutup aplikasi dulu supaya posisinya
    "diingat", dan jendela tidak kembali geser ke posisi lama tiap kali dibuka lagi.
    Di-debounce ~600ms (ditunda ulang tiap ada event baru) supaya tidak menulis file berkali-kali
    selagi jendela masih aktif digeser/di-resize - baru benar-benar disimpan begitu gerakannya
    berhenti."""
    pending = _window_config_save_after_id["id"]
    if pending is not None:
        try:
            root.after_cancel(pending)
        except (ValueError, tk.TclError):
            pass

    def _do_save():
        _window_config_save_after_id["id"] = None
        try:
            # Jangan simpan saat window di-minimize. Saat maximize tetap disimpan, tapi
            # save_window_config() menyimpan ukuran/posisi NORMAL terakhir + flag 'maximized'
            # (bukan ukuran layar penuh), jadi jendela dibuka maximize lagi di sesi berikutnya.
            if root.state() in ("iconic", "withdrawn"):
                return
        except tk.TclError:
            return
        save_window_config()

    _window_config_save_after_id["id"] = root.after(600, _do_save)

root.bind("<Configure>", _save_window_config_debounced)

all_buttons = []
_BUTTON_STYLES = {}

def make_button(parent, text, command, style_name="Custom.TButton", **kwargs):
    """Buat ttk.Button + daftarkan style-nya (dipakai update_widget_colors saat tema berganti)."""
    btn = ttk.Button(parent, text=text, command=command, style=style_name, **kwargs)
    all_buttons.append(btn)
    _BUTTON_STYLES[btn] = style_name
    return btn

# --------------------------------------------------------------------------- header
header_frame = ttk.Frame(root, style="Header.TFrame", padding=(16, 10))
header_frame.pack(fill=tk.X, side=tk.TOP)

# Hamburger: membuka panel menu di sisi kiri (di dalam jendela, jadi tidak pernah "nyasar" ke monitor lain).
menu_btn = ttk.Button(header_frame, text="\u2630", style="Hamburger.Custom.TButton", width=3, takefocus=False)
menu_btn.pack(side=tk.LEFT, padx=(0, 14))

title_label = tk.Label(header_frame,
                        text="Wine Launch Manager",
                        font=FONTS["display"],
                        bd=0)
title_label.pack(side=tk.LEFT)

version_label = tk.Label(header_frame, text=f"v{WLM_VERSION}", font=FONTS["small"], bd=0)
version_label.pack(side=tk.LEFT, padx=(10, 0), pady=(6, 0))

header_line = tk.Frame(root, height=1, bd=0)
header_line.pack(fill=tk.X, side=tk.TOP)

# --------------------------------------------------------------------------- toolbar: cari + aksi library
toolbar = ttk.Frame(root, padding=(16, 12, 16, 4))
toolbar.pack(fill=tk.X, side=tk.TOP)

search_var = tk.StringVar()
search_entry = ttk.Entry(toolbar, textvariable=search_var, style="Search.TEntry",
                         font=FONTS["body"], width=34)
search_entry.pack(side=tk.LEFT)

# Placeholder digambar sebagai label di atas entry (ttk.Entry tidak punya placeholder).
search_hint = tk.Label(search_entry, text="Search games...   (Ctrl+F)", font=FONTS["normal"],
                       bd=0, cursor="xterm")

search_hint.place(x=12, rely=0.5, anchor="w")
search_hint.bind("<Button-1>", lambda e: search_entry.focus_set())

_search_focus = {"on": False}

def _update_search_hint():
    if search_var.get() or _search_focus["on"]:
        search_hint.place_forget()
    else:
        search_hint.place(x=12, rely=0.5, anchor="w")

_search_job = {"id": None}

def _apply_search():
    _search_job["id"] = None
    update_script_list(keep_cache=True)

def _on_search_changed(*_):
    _update_search_hint()
    if _search_job["id"] is not None:
        try:
            root.after_cancel(_search_job["id"])
        except (ValueError, tk.TclError):
            pass
    _search_job["id"] = root.after(150, _apply_search)

search_var.trace_add("write", _on_search_changed)

def _focus_search(_event=None):
    search_entry.focus_set()
    search_entry.select_range(0, tk.END)
    return "break"

def _clear_search(_event=None):
    if search_var.get():
        search_var.set("")
        return "break"
    root.focus_set()

search_entry.bind("<Escape>", _clear_search)

def _search_focus_changed(on):
    _search_focus["on"] = on
    _update_search_hint()

search_entry.bind("<FocusIn>", lambda e: _search_focus_changed(True), add="+")
search_entry.bind("<FocusOut>", lambda e: _search_focus_changed(False), add="+")
root.bind("<Control-f>", _focus_search)
root.bind("<Control-F>", _focus_search)

add_btn = make_button(toolbar, "+  Add Game", add_script, "Accent.TButton")
add_btn.pack(side=tk.RIGHT)

install_apps_btn = make_button(toolbar, "Install Apps", run_exe_setup)
install_apps_btn.pack(side=tk.RIGHT, padx=(0, 8))

gog_btn = make_button(toolbar, "GOG Store", open_gog_store)
gog_btn.pack(side=tk.RIGHT, padx=(0, 8))

# --------------------------------------------------------------------------- footer: status + tema
footer = ttk.Frame(root, style="Footer.TFrame", padding=(16, 7))
footer.pack(fill=tk.X, side=tk.BOTTOM)

status_label = tk.Label(footer, text="Ready", font=FONTS["small"], anchor="w", bd=0)
status_label.pack(side=tk.LEFT)

theme_names = [data["name"] for data in THEMES.values()]
theme_combo = ttk.Combobox(footer,
                            values=theme_names,
                            state="readonly",
                            width=20,
                            font=FONTS["normal"])
theme_combo.set(COLORS["name"])
theme_combo.pack(side=tk.RIGHT)
theme_combo.bind("<<ComboboxSelected>>", on_theme_selected)

theme_label = ttk.Label(footer, text="Theme", style="FooterMuted.TLabel")
theme_label.pack(side=tk.RIGHT, padx=(0, 8))

def safe_status_config(**kwargs):
    """Update status_label dengan aman. Kalau window utama sudah ditutup (mis. pengguna
    menutup aplikasi lewat tombol close SAAT sebuah dialog modal seperti Select Runner
    masih terbuka), status_label ikut hancur duluan sebelum kode yang memanggilnya sempat
    selesai jalan - update langsung ke widget akan crash dengan TclError 'invalid command
    name'. Fungsi ini mengecek dulu widget-nya masih ada sebelum menyentuhnya, dan diam
    saja (tidak melakukan apa-apa) kalau ternyata sudah tidak ada lagi."""
    try:
        if status_label.winfo_exists():
            status_label.configure(**kwargs)
    except tk.TclError:
        pass

# --------------------------------------------------------------------------- panel samping (menu hamburger)
# Menu lama (tk.Menu popup yang di-post ke koordinat layar) bisa muncul di monitor lain pada setup
# dua monitor. Panel ini bagian dari jendela utama: meluncur dari kiri, menutupi sisi kiri jendela,
# dan tertutup sendiri setelah memilih menu / klik di luar panel / tekan Esc.
SIDE_PANEL_WIDTH = 300
_side = {"open": False, "x": -SIDE_PANEL_WIDTH, "job": None}

side_panel = ttk.Frame(root, style="Side.TFrame")
_side_head = ttk.Frame(side_panel, style="Side.TFrame", padding=(16, 12, 16, 8))
_side_head.pack(fill=tk.X)
ttk.Label(_side_head, text="MENU", style="SideTitle.TLabel").pack(side=tk.LEFT)
ttk.Separator(side_panel, orient=tk.HORIZONTAL, style="Side.TSeparator").pack(fill=tk.X)

_side_body = ttk.Frame(side_panel, style="Side.TFrame")
_side_body.pack(fill=tk.BOTH, expand=True)
side_canvas = tk.Canvas(_side_body, highlightthickness=0, bd=0, bg=COLORS["secondary"])
side_scroll = ttk.Scrollbar(_side_body, orient=tk.VERTICAL, command=side_canvas.yview)
side_canvas.configure(yscrollcommand=side_scroll.set)
side_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
side_inner = ttk.Frame(side_canvas, style="Side.TFrame")
_side_inner_id = side_canvas.create_window((0, 0), window=side_inner, anchor="nw")

def _side_sync(_event=None):
    """Lebar isi mengikuti kanvas; scrollbar hanya muncul kalau isi lebih tinggi dari panel."""
    side_canvas.configure(scrollregion=side_canvas.bbox("all"))
    side_canvas.itemconfigure(_side_inner_id, width=side_canvas.winfo_width())
    needs_scroll = side_inner.winfo_reqheight() > side_canvas.winfo_height() > 1
    if needs_scroll and not side_scroll.winfo_ismapped():
        side_scroll.pack(side=tk.RIGHT, fill=tk.Y, before=side_canvas)
    elif not needs_scroll and side_scroll.winfo_ismapped():
        side_scroll.pack_forget()
        side_canvas.yview_moveto(0)

side_inner.bind("<Configure>", _side_sync)
side_canvas.bind("<Configure>", _side_sync)

def _side_place(x):
    """Taruh panel di bawah header, setinggi sisa jendela."""
    _side["x"] = x
    top = header_frame.winfo_height()
    side_panel.place(x=x, y=top, width=SIDE_PANEL_WIDTH, relheight=1.0, height=-top)

def _side_slide(target, on_done=None, steps=8):
    """Animasi singkat meluncur ke posisi x target (ease-out)."""
    if _side["job"] is not None:
        try:
            root.after_cancel(_side["job"])
        except (ValueError, tk.TclError):
            pass
        _side["job"] = None
    start = _side["x"]

    def tick(i=1):
        t = i / steps
        _side_place(round(start + (target - start) * (1 - (1 - t) ** 3)))
        if i < steps:
            _side["job"] = root.after(15, tick, i + 1)
        else:
            _side["job"] = None
            if on_done:
                on_done()
    tick()

def show_side_panel():
    if _side["open"]:
        return
    _side["open"] = True
    _side_place(0)                  # langsung tampil, tanpa animasi
    side_panel.tkraise()

def hide_side_panel():
    if not _side["open"]:
        return
    _side["open"] = False
    side_panel.place_forget()

def toggle_side_panel():
    hide_side_panel() if _side["open"] else show_side_panel()

def _side_run(command):
    """Tutup panel dulu, baru jalankan menunya (dialog muncul di jendela yang bersih)."""
    hide_side_panel()
    root.after(20, command)

SIDE_MENU = [
    ("LOGS & PREFIX", [
        ("View Logs", view_logs),
        ("Prefix Configuration Manager...", open_prefix_manager_dialog)]),
    ("WINE", [
        ("Wine Configuration (winecfg)", open_winecfg),
        ("Open Wine Prefix Folder", open_wine_prefix_folder),
        ("Uninstall Program", lambda: subprocess.Popen(["wine", "uninstaller"])),
        ("Wine Explorer", lambda: subprocess.Popen(["wine", "explorer"]))]),
    ("PROTON GE", [
        ("Extract Proton GE Archive...", extract_protonge_archive),
        ("Download ProtonGE Online...", open_protonge_download_dialog),
        ("Open Proton GE Folder", open_protonge_folder)]),
    ("PROTON-CACHYOS", [
        ("Extract Proton-CachyOS Archive...", extract_protoncachyos_archive),
        ("Download Proton-CachyOS Online...", open_protoncachyos_download_dialog),
        ("Open Proton-CachyOS Folder", open_protoncachyos_folder)]),
    ("LIBRARY", [
        ("Refresh List", lambda: update_script_list())]),
]

for _i, (_section, _items) in enumerate(SIDE_MENU):
    ttk.Label(side_inner, text=_section, style="SideSection.TLabel").pack(
        anchor="w", padx=16, pady=(8 if _i == 0 else 16, 4))
    for _label, _cmd in _items:
        ttk.Button(side_inner, text=_label, style="Sidebar.TButton", takefocus=False,
                   command=lambda c=_cmd: _side_run(c)).pack(fill=tk.X)
ttk.Frame(side_inner, style="Side.TFrame", height=12).pack(fill=tk.X)
ttk.Separator(side_inner, orient=tk.HORIZONTAL, style="Side.TSeparator").pack(fill=tk.X, padx=16, pady=(4, 8))
ttk.Label(side_inner, text=f"Wine Launch Manager  v{WLM_VERSION}", style="SideMuted.TLabel").pack(anchor="w", padx=16)
ttk.Label(side_inner, text=WLM_DEVELOPER, style="SideMuted.TLabel").pack(anchor="w", padx=16, pady=(2, 0))
ttk.Frame(side_inner, style="Side.TFrame", height=14).pack(fill=tk.X)

def _side_wheel(event):
    if side_scroll.winfo_ismapped():
        up = getattr(event, "num", 0) == 4 or getattr(event, "delta", 0) > 0
        side_canvas.yview_scroll(-2 if up else 2, "units")
    return "break"

def _side_bind_wheel(widget):
    for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        widget.bind(seq, _side_wheel, add="+")
    for child in widget.winfo_children():
        _side_bind_wheel(child)

_side_bind_wheel(side_panel)

def _side_click_outside(event):
    """Klik di luar panel (dan bukan tombol hamburger) menutup panel."""
    if not _side["open"]:
        return
    w = event.widget
    if not isinstance(w, tk.Misc):
        return
    try:
        if w.winfo_toplevel() is not root:
            return
        path = str(w)
    except tk.TclError:
        return
    panel_path = str(side_panel)
    if path == str(menu_btn) or path == panel_path or path.startswith(panel_path + "."):
        return
    hide_side_panel()

root.bind_all("<Button-1>", _side_click_outside, add="+")
root.bind("<Escape>", lambda e: hide_side_panel(), add="+")
menu_btn.config(command=toggle_side_panel)

main_container = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
main_container.pack(fill=tk.BOTH, expand=True, padx=16, pady=(6, 14))

left_panel = ttk.Frame(main_container, padding=(0, 0, 12, 0))
main_container.add(left_panel, weight=3)

# --- Tampilan library: Grid (kartu cover ala GOG) atau List (daftar teks) ---------------------
try:
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import game_covers
except ImportError as e:
    game_covers = None
    print(f"[WLM] game_covers.py not found next to the launcher - grid view disabled: {e}")

view_config = (game_covers.load_view_config(library_view_config_file) if game_covers
               else {"view": "list", "card_width": 200})

# --- baris judul library: "Library · 12 games" ........ Sort [A-Z] [Grid|List] ----------------
controls_frame = ttk.Frame(left_panel)
controls_frame.pack(fill=tk.X, pady=(0, 10))

ttk.Label(controls_frame, text="Library", style="Heading.TLabel").pack(side=tk.LEFT)
count_label = ttk.Label(controls_frame, text="", style="Muted.TLabel")
count_label.pack(side=tk.LEFT, padx=(10, 0), pady=(3, 0))

if game_covers:
    view_toggle = ttk.Frame(controls_frame)
    view_toggle.pack(side=tk.RIGHT)
    view_grid_btn = ttk.Button(view_toggle, text="Grid", style="SegOff.TButton", takefocus=False,
                               command=lambda: apply_view("grid"))
    view_grid_btn.pack(side=tk.LEFT, padx=(0, 8))
    view_list_btn = ttk.Button(view_toggle, text="List", style="SegOff.TButton", takefocus=False,
                               command=lambda: apply_view("list"))
    view_list_btn.pack(side=tk.LEFT)

sort_combo = ttk.Combobox(controls_frame,
                            values=["A-Z", "Z-A"],
                            state="readonly",
                            width=6,
                            font=FONTS["normal"])
sort_combo.current(0)
sort_combo.pack(side=tk.RIGHT, padx=(0, 12 if game_covers else 0))
sort_label = ttk.Label(controls_frame, text="Sort", style="Muted.TLabel")
sort_label.pack(side=tk.RIGHT, padx=(0, 6))
sort_combo.bind("<<ComboboxSelected>>", sort_by_selected)

# Baris kedua (hanya tampil di mode Grid): ukuran kartu + ambil cover.
view_frame = ttk.Frame(left_panel)

# --- daftar teks (List) dalam bingkai tipis -----------------------------------------------------
tree_frame = tk.Frame(left_panel, bd=0, highlightthickness=1)

tree_scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL)
tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)

tree = ttk.Treeview(tree_frame,
                    columns=("No", "Game Name", "Source"),
                    show="headings",
                    yscrollcommand=tree_scroll.set,
                    selectmode="browse")
tree_scroll.config(command=tree.yview)

tree.heading("No", text="#", anchor="center")
tree.heading("Game Name", text="GAME", anchor="w")
tree.heading("Source", text="SOURCE", anchor="center")
tree.column("#0", width=0, stretch=False)
tree.column("No", width=48, anchor="center", minwidth=40, stretch=False)
tree.column("Game Name", width=300, anchor="w", minwidth=200, stretch=True)
tree.column("Source", width=90, anchor="center", minwidth=70, stretch=False)

tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

_last_tree_click = {"time": 0.0, "row": None}

def _play_selected(event=None):
    run_script()
    reset_button_hover_state(play_btn)

def on_tree_click(event):
    """Klik dua kali cepat pada game = langsung Play. Klik sekali pada game yang sudah terpilih
    membatalkan pilihan, instead of doing nothing (ttk.Treeview's default with selectmode='browse')."""
    row_id = tree.identify_row(event.y)
    if not row_id:
        return
    now = time.monotonic()
    is_double = (row_id == _last_tree_click["row"] and now - _last_tree_click["time"] < 0.35)
    _last_tree_click["time"], _last_tree_click["row"] = now, row_id
    if is_double:
        _last_tree_click["row"] = None
        tree.selection_set(row_id)
        tree.focus(row_id)
        root.after(10, _play_selected)
        return "break"
    if row_id in tree.selection():
        tree.selection_remove(row_id)
        tree.focus('')
        on_select()
        return "break"

tree.bind("<Button-1>", on_tree_click)
tree.bind("<<TreeviewSelect>>", on_select)
tree.bind("<Return>", _play_selected)

# Pesan di tengah area daftar: library kosong / hasil pencarian kosong.
empty_label = tk.Label(left_panel, text="", font=FONTS["subtitle"], justify=tk.CENTER, bd=0)

def _sync_empty_bg():
    """Latar tulisan kosong disamakan dengan area di bawahnya: mode List = warna tabel
    (tree_bg), mode Grid = warna panel. Sebelumnya selalu 'primary' sehingga di mode List
    tampak seperti kotak berbeda warna di atas tabel."""
    bg = COLORS["tree_bg"] if view_config.get("view") == "list" else COLORS["primary"]
    try:
        empty_label.config(bg=bg)
    except tk.TclError:
        pass

def set_empty_state(message):
    _sync_empty_bg()
    if message:
        empty_label.config(text=message)
        empty_label.place(relx=0.5, rely=0.5, anchor="center")
        empty_label.lift()
    else:
        empty_label.place_forget()

def _grid_select(name):
    """Kartu diklik -> pilih baris yang sama di Treeview, supaya PLAY/RENAME/dll tetap jalan."""
    for iid in tree.get_children():
        if tree.item(iid, "values")[1] == name:
            tree.selection_set(iid)
            tree.focus(iid)
            tree.see(iid)
            break

def _grid_deselect():
    sel = tree.selection()
    if sel:
        tree.selection_remove(*sel)
    tree.focus('')
    on_select()

def _grid_activate(name):
    run_script()
    reset_button_hover_state(play_btn)

def _save_view_config():
    if game_covers:
        game_covers.save_view_config(library_view_config_file, view_config)

def apply_view(mode):
    """Tampilkan Grid (cover) atau List (teks); pilihan disimpan."""
    if mode == "grid" and library_grid is None:
        mode = "list"
    view_config["view"] = mode
    tree_frame.pack_forget()
    if library_grid is not None:
        library_grid.pack_forget()
    view_frame.pack_forget()
    if game_covers:
        view_grid_btn.configure(style="SegOn.TButton" if mode == "grid" else "SegOff.TButton")
        view_list_btn.configure(style="SegOn.TButton" if mode == "list" else "SegOff.TButton")
    if mode == "grid":
        view_frame.pack(fill=tk.X, pady=(0, 10), after=controls_frame)
        library_grid.pack(fill=tk.BOTH, expand=True)
    else:
        tree_frame.pack(fill=tk.BOTH, expand=True)
    _save_view_config()
    _sync_empty_bg()
    if empty_label.winfo_ismapped():
        empty_label.lift()

def on_size_selected(event=None):
    width = game_covers.CARD_SIZES.get(size_combo.get())
    if width and library_grid is not None:
        view_config["card_width"] = width
        library_grid.set_card_width(width)
        _save_view_config()

def fetch_covers():
    """Cari cover untuk semua game yang belum punya (perlu internet)."""
    if library_grid is None:
        return
    n = library_grid.fetch_missing()
    safe_status_config(text=(f"Looking for {n} cover(s) online..." if n
                             else "All games already have a cover"),
                       fg=COLORS["text_secondary"])

if game_covers:
    cover_store = game_covers.CoverStore(directory)
    library_grid = game_covers.LibraryGrid(
        left_panel, lambda: COLORS, FONTS, icon_dir, cover_store, view_config["card_width"],
        on_select=_grid_select, on_deselect=_grid_deselect, on_activate=_grid_activate,
        source_of=lambda name: game_source(name)["source"],
        on_status=lambda text, level="info": safe_status_config(
            text=text, fg=COLORS["text_secondary" if level == "info" else level]))

    ttk.Label(view_frame, text="Card size", style="Muted.TLabel").pack(side=tk.LEFT, padx=(0, 6))
    size_combo = ttk.Combobox(view_frame, values=list(game_covers.CARD_SIZES), state="readonly",
                              width=8, font=FONTS["normal"])
    size_combo.set(next((k for k, v in game_covers.CARD_SIZES.items()
                         if v == view_config["card_width"]), "Medium"))
    size_combo.pack(side=tk.LEFT, padx=(0, 10))
    size_combo.bind("<<ComboboxSelected>>", on_size_selected)

    fetch_covers_btn = make_button(view_frame, "Fetch Covers", fetch_covers)
    fetch_covers_btn.pack(side=tk.LEFT)

apply_view(view_config["view"])

# =========================================================================== panel kanan: detail game + aksi
right_panel = ttk.Frame(main_container, padding=(4, 0, 0, 0))
main_container.add(right_panel, weight=1)

# Bagian bawah dipasang DULU (side=BOTTOM) supaya tombol selalu terlihat walau jendela pendek.
button_panel = ttk.Frame(right_panel)
button_panel.pack(fill=tk.X, side=tk.BOTTOM, pady=(12, 0))

launch_row = ttk.Frame(button_panel)
launch_row.pack(fill=tk.X)

launch_label = ttk.Label(launch_row, text="Launch", style="Muted.TLabel")
launch_label.pack(side=tk.LEFT)

hud_config_btn = make_button(launch_row, "Config HUD", open_hud_config_dialog, "Action.TButton")
hud_config_btn.pack(side=tk.RIGHT)

launch_mode_combo = ttk.Combobox(launch_row,
                                    values=["Normal", "GalliumHUD", "VulkanHUD", "MangoHud-GL", "Mangohud"],
                                    state="readonly",
                                    width=12,
                                    font=FONTS["normal"])
launch_mode_combo.current(0)
launch_mode_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)

play_btn = make_button(button_panel, "\u25B6  Play",
                       lambda: (run_script(), reset_button_hover_state(play_btn)),
                       "Play.TButton")
play_btn.pack(fill=tk.X, pady=(10, 8))

action_row = ttk.Frame(button_panel)
action_row.pack(fill=tk.X)
for _col in range(4):
    action_row.columnconfigure(_col, weight=1, uniform="actions")

rename_btn = make_button(action_row, "Rename", rename_script, "Action.TButton")
rename_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))

icon_btn = make_button(action_row, "Icon", change_icon, "Action.TButton")
icon_btn.grid(row=0, column=1, sticky="ew", padx=(0, 6))

filemanager_btn = make_button(action_row, "Folder", open_file_manager, "Action.TButton")
filemanager_btn.grid(row=0, column=2, sticky="ew", padx=(0, 6))

remove_btn = make_button(action_row, "Remove", remove_script, "Danger.TButton")
remove_btn.grid(row=0, column=3, sticky="ew")

_ACTION_BUTTONS = (play_btn, rename_btn, icon_btn, filemanager_btn, remove_btn)

def _sync_action_buttons(has_selection):
    """Tombol yang butuh game terpilih dinonaktifkan kalau belum ada pilihan."""
    for btn in _ACTION_BUTTONS:
        try:
            btn.state(["!disabled"] if has_selection else ["disabled"])
        except tk.TclError:
            pass

# --- kartu detail: cover, judul, info ----------------------------------------------------------
detail_card = tk.Frame(right_panel, bd=0, highlightthickness=1)
detail_card.pack(fill=tk.X, side=tk.TOP)

icon_frame = ttk.Frame(detail_card, style="Card.TFrame", width=ICON_WIDTH, height=ICON_HEIGHT)
icon_frame.pack(padx=16, pady=(16, 10))
icon_frame.pack_propagate(False)

icon_label = tk.Label(icon_frame, relief="flat", bd=0, text="No cover", font=FONTS["small"])
icon_label.pack(expand=True, fill=tk.BOTH)

game_title_label = tk.Label(detail_card,
                                text="No Game Selected",
                                font=FONTS["heading"],
                                justify=tk.CENTER,
                                wraplength=ICON_WIDTH,
                                bd=0)
game_title_label.pack(fill=tk.X, padx=16, pady=(0, 8))

# info_text tetap dipakai kode lain (on_select dst.). Isi "Source: ...\nLocation: ..." otomatis
# ditampilkan sebagai baris label/nilai; teks biasa (mis. "Select a game...") tampil sebagai pesan.
info_text = tk.StringVar(value="Select a game to view details")
info_label = tk.Label(detail_card,
                        textvariable=info_text,
                        font=FONTS["small"],
                        justify=tk.CENTER,
                        wraplength=ICON_WIDTH,
                        bd=0)
info_label.pack(padx=16, pady=(0, 16))

INFO_ROWS = (("Source", "Source"), ("Location", "Location"),
             ("Last Modified", "Modified"), ("Size", "Size"))
info_rows_frame = ttk.Frame(detail_card, style="Card.TFrame")
info_rows_frame.columnconfigure(1, weight=1)
_info_value_labels = {}
for _row, (_key, _caption) in enumerate(INFO_ROWS):
    ttk.Label(info_rows_frame, text=_caption, style="CardMuted.TLabel").grid(
        row=_row, column=0, sticky="nw", padx=(0, 10), pady=2)
    _value_label = ttk.Label(info_rows_frame, text="", style="Card.TLabel", justify=tk.LEFT,
                             wraplength=170)
    _value_label.grid(row=_row, column=1, sticky="nw", pady=2)
    _info_value_labels[_key] = _value_label

def _render_info(*_):
    parsed = {}
    for line in info_text.get().split("\n"):
        if ": " in line:
            key, value = line.split(": ", 1)
            parsed[key.strip()] = value.strip()
    if "Source" in parsed and "Location" in parsed:
        info_label.pack_forget()
        for key, label in _info_value_labels.items():
            label.configure(text=parsed.get(key, "-"))
        if not info_rows_frame.winfo_manager():
            info_rows_frame.pack(fill=tk.X, padx=16, pady=(0, 16))
    else:
        info_rows_frame.pack_forget()
        if not info_label.winfo_manager():
            info_label.pack(padx=16, pady=(0, 16))

info_text.trace_add("write", _render_info)

def _resize_detail_card(event):
    inner = max(120, event.width - 34)
    game_title_label.configure(wraplength=inner)
    info_label.configure(wraplength=inner)
    for label in _info_value_labels.values():
        label.configure(wraplength=max(80, inner - 72))

detail_card.bind("<Configure>", _resize_detail_card)

_sync_action_buttons(False)

apply_theme(CURRENT_THEME, announce=False)

update_script_list()

def _restore_sash(_attempt=0):
    """Kembalikan lebar panel library/detail yang tersimpan (dijaga agar kedua panel tetap terlihat)."""
    saved = config.get("sash")
    if saved:
        try:
            root.update_idletasks()
            total = main_container.winfo_width()
            if total < 200 and _attempt < 10:
                root.after(100, _restore_sash, _attempt + 1)
                return
            main_container.sashpos(0, max(300, min(saved, total - 300)))
        except tk.TclError:
            pass
    _sash_state["restored"] = True

root.after(200, _restore_sash)
root.after(800, _restore_sash)      # ulangi sekali: window maximize bisa baru selesai belakangan
main_container.bind("<ButtonRelease-1>", lambda e: _save_window_config_debounced(), add="+")

root.after(150, poll_log_queues)

root.mainloop()
