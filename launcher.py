import os
import re
import shlex
import shutil
import subprocess
import threading
import queue
import pty
import select
import errno
import tkinter as tk
from tkinter import ttk, filedialog, simpledialog, messagebox
from pathlib import Path
from PIL import Image, ImageTk, ImageDraw
import json
from datetime import datetime
import time

# Configuration paths
directory = Path.home() / "wlm"
icon_dir = directory / "icons"
bashlaunch_dir = directory / "bashlaunch"
theme_config_file = directory / "theme_config.json"
window_config_file = directory / "window_config.json"

# ProtonGE feature paths
protonge_dir = directory / "protonge"                 # tempat ekstrak binary Proton GE (bukan di direktori Steam)
protonge_prefix_root = directory / "protonprefixes"   # prefix ProtonGE dibuat di sini (didalam direktori utama)

# Proton-CachyOS feature paths (sama pola dengan Proton GE, tapi folder ekstrak & prefix terpisah)
protoncachyos_dir = directory / "protoncachyos"                    # tempat ekstrak binary Proton-CachyOS
protoncachyos_prefix_root = directory / "protoncachyosprefixes"    # prefix Proton-CachyOS dibuat disini, terpisah dari Proton GE

# Wine (vanilla) prefix path - dibuat agar prefix Wine juga punya lokasi default rapi
# didalam direktori utama WLM, bukan langsung menumpuk di ~/.wine (root direktori home).
wine_prefix_root = directory / "wineprefixes"

runner_config_file = directory / "runner_config.json"  # menyimpan pilihan runner (wine/protonge/protoncachyos) per game
prefix_location_config_file = directory / "prefix_location_config.json"  # lokasi default kustom untuk prefix BARU, per jenis runner
prefix_registry_file = directory / "prefix_registry.json"  # catatan semua prefix yang pernah dibuat (lokasi & versi proton-nya)
logs_dir = directory / "logs"                          # menyimpan output stdout/stderr wine & proton per game

# Lokasi default (bawaan) tempat prefix BARU dibuat untuk masing-masing runner,
# sebelum ada kustomisasi lokasi oleh pengguna lewat dialog pemilihan runner.
DEFAULT_PREFIX_ROOTS = {
    "wine": wine_prefix_root,
    "protonge": protonge_prefix_root,
    "protoncachyos": protoncachyos_prefix_root,
}

# Buat direktori jika tidak ada.
directory.mkdir(parents=True, exist_ok=True)
bashlaunch_dir.mkdir(parents=True, exist_ok=True)
icon_dir.mkdir(parents=True, exist_ok=True)
protonge_dir.mkdir(parents=True, exist_ok=True)
protonge_prefix_root.mkdir(parents=True, exist_ok=True)
protoncachyos_dir.mkdir(parents=True, exist_ok=True)
protoncachyos_prefix_root.mkdir(parents=True, exist_ok=True)
wine_prefix_root.mkdir(parents=True, exist_ok=True)
logs_dir.mkdir(parents=True, exist_ok=True)

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

# =======================================================================
# Live log
# =======================================================================
# script_name (no .sh) -> {
#   "proc": Popen, "log_path": Path, "queue": queue.Queue,
#   "buffer": [str, ...], "window": Toplevel|None, "text_widget": Text|None,
#   "finished": bool
# }
running_games = {}
MAX_LOG_BUFFER_LINES = 5000

# =======================================================================
# THEME SYSTEM & CONFIG
# =======================================================================
THEMES = {
    "default": {  # Dark Blue - purple (default)
        "name": "Default (Dark Blue)",
        "primary": "#1a1a2e",
        "secondary": "#16213e",
        "accent": "#0f3460",
        "highlight": "#e94560",
        "text": "#ffffff",
        "text_secondary": "#b0b0b0",
        "button_text": "#ffffff",
        "success": "#4CAF50",
        "warning": "#FF9800",
        "danger": "#F44336",
        "card_bg": "#2d3047",
        "border": "#3a3d5c",
        "button_bg": "#0f3460",
        "button_fg": "#ffffff",
        "tree_bg": "#2d3047",
        "tree_fg": "#ffffff",
        "tree_highlight": "#e94560",
        "tree_highlight_text": "#ffffff",
        "text_background": "#1e1e35"
    },
    "dark": {  # Classic dark mode
        "name": "Dark",
        "primary": "#121212",
        "secondary": "#1e1e1e",
        "accent": "#2d2d2d",
        "highlight": "#BB86FC",
        "text": "#ffffff",
        "text_secondary": "#aaaaaa",
        "button_text": "#ffffff",
        "success": "#03DAC6",
        "warning": "#FFB74D",
        "danger": "#CF6679",
        "card_bg": "#2d2d2d",
        "border": "#404040",
        "button_bg": "#3700B3",
        "button_fg": "#ffffff",
        "tree_bg": "#2d2d2d",
        "tree_fg": "#ffffff",
        "tree_highlight": "#BB86FC",
        "tree_highlight_text": "#000000",
        "text_background": "#1e1e1e"
    },
    "light": {  # Light mode
        "name": "Light",
        "primary": "#f5f5f5",
        "secondary": "#ffffff",
        "accent": "#e0e0e0",
        "highlight": "#6200EE",
        "text": "#000000",
        "text_secondary": "#666666",
        "button_text": "#ffffff",
        "success": "#00897B",
        "warning": "#FF8F00",
        "danger": "#C62828",
        "card_bg": "#ffffff",
        "border": "#dddddd",
        "button_bg": "#6200EE",
        "button_fg": "#ffffff",
        "tree_bg": "#ffffff",
        "tree_fg": "#000000",
        "tree_highlight": "#6200EE",
        "tree_highlight_text": "#ffffff",
        "text_background": "#ffffff"
    },
    "pinky": {  # Pink theme (Cooler)
        "name": "Pinky",
        "primary": "#2d1b2e",
        "secondary": "#3d2b3f",
        "accent": "#5d3d5f",
        "highlight": "#f06292", 
        "text": "#ffffff",
        "text_secondary": "#e0c3e0",
        "button_text": "#ffffff",
        "success": "#8e24aa",
        "warning": "#ffb6c1",
        "danger": "#d81b60",
        "card_bg": "#4a3b4c",
        "border": "#6d5a6f",
        "button_bg": "#e91e63",
        "button_fg": "#ffffff",
        "tree_bg": "#4a3b4c",
        "tree_fg": "#ffffff",
        "tree_highlight": "#f06292",
        "tree_highlight_text": "#ffffff",
        "text_background": "#3d2b3f"
    },
    "zombie": {  # Zombie Green (Cooler)
        "name": "Zombie Green",
        "primary": "#1b5e20",
        "secondary": "#2e7d32",
        "accent": "#4caf50",
        "highlight": "#c8e6c9", 
        "text": "#ffffff",
        "text_secondary": "#a0d0a0",
        "button_text": "#ffffff", 
        "success": "#32cd32",
        "warning": "#adff2f",
        "danger": "#ff4500",
        "card_bg": "#1e3a1e",
        "border": "#3a5f3a",
        "button_bg": "#66bb6a",
        "button_fg": "#1b5e20",
        "tree_bg": "#1e3a1e",
        "tree_fg": "#ffffff",
        "tree_highlight": "#81c784",
        "tree_highlight_text": "#000000",
        "text_background": "#2e7d32"
    }
}

# Font
FONT_FAMILY = "Segoe UI"
FONTS = {
    "title": (FONT_FAMILY, 14, "bold"),
    "subtitle": (FONT_FAMILY, 11, "bold"),
    "normal": (FONT_FAMILY, 9),
    "small": (FONT_FAMILY, 8)
}

# ICON SIZE in pixels
ICON_SIZE = 250
ICON_WIDTH = ICON_SIZE
ICON_HEIGHT = ICON_SIZE

# =======================================================================
# CONFIGURATION FUNCTIONS (Improved for Robustness)
# =======================================================================
def load_config():
    """Load all configurations from file with validation"""
    config = {"theme": "default", "window_size": "1000x720", "window_position": None}
    
    # Load theme config
    if theme_config_file.exists():
        try:
            with open(theme_config_file, 'r') as f:
                theme_config = json.load(f)
                config["theme"] = theme_config.get('theme', 'default')
        except:
            pass
    
    # Load window config
    if window_config_file.exists():
        try:
            with open(window_config_file, 'r') as f:
                window_config = json.load(f)
                
                loaded_size = window_config.get('size', '1000x720')
                loaded_position = window_config.get('position', None)

                # Validate Size format (WxH)
                if 'x' in loaded_size and loaded_size.count('x') == 1:
                    config["window_size"] = loaded_size
                
                # Validate Position format (+X+Y)
                if loaded_position and loaded_position.startswith('+') and loaded_position.count('+') == 2:
                    config["window_position"] = loaded_position
                else:
                    # Remove position if format is invalid (prevents TclError)
                    config["window_position"] = None 
                
        except:
            # If file is corrupt, use default
            pass
    
    return config

def save_window_config():
    """Save window size and position in a complete and clean format"""
    if root.winfo_exists():
        # Get Width and Height
        width = root.winfo_width()
        height = root.winfo_height()
        size = f"{width}x{height}"
        
        # Get position X and Y
        pos_x = root.winfo_x()
        pos_y = root.winfo_y()
        # Save in +X+Y format
        position = f"+{pos_x}+{pos_y}" 
        
        config_data = {
            "size": size,
            "position": position
        }
        
        try:
            # Save with indent=4 for readability
            with open(window_config_file, 'w') as f:
                json.dump(config_data, f, indent=4) 
        except Exception as e:
            print(f"Error saving window config: {e}")

# =======================================================================
# PROTON GE FEATURE (RUNNER: WINE VANILLA / PROTON GE)
# =======================================================================
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
    status_label.config(text=f"Extracting {archive_path_obj.name} to WLM directory...", fg=COLORS["text_secondary"])

    # Extraction of a large Proton archive can take a while, and doing it
    # directly on the Tk main thread would freeze the whole window for that
    # entire time (no repaint, no response to clicks) - which looks exactly
    # like the app has crashed/hung. So the actual extraction work runs on a
    # background thread, and we show a small modal popup with an animated
    # progress bar in the meantime to make it clear something is happening.
    loading_dialog = tk.Toplevel(root)
    loading_dialog.title(f"Extracting {build_label}")
    loading_dialog.configure(bg=COLORS["primary"])
    loading_dialog.resizable(False, False)
    loading_dialog.transient(root)
    loading_dialog.protocol("WM_DELETE_WINDOW", lambda: None)  # block closing while extraction is running

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
    progress_bar.start(12)  # animate the bar so movement is visible even with no % info

    # Center the popup over the main window.
    loading_dialog.update_idletasks()
    x = root.winfo_rootx() + (root.winfo_width() - loading_dialog.winfo_width()) // 2
    y = root.winfo_rooty() + (root.winfo_height() - loading_dialog.winfo_height()) // 2
    loading_dialog.geometry(f"+{x}+{y}")
    loading_dialog.grab_set()  # modal: prevent interacting with the main window mid-extraction

    extract_result = {"error": None}

    def do_extract():
        try:
            target_dir.mkdir(exist_ok=True)
            before_entries = {p.name for p in target_dir.iterdir() if p.is_dir()}

            if archive_path_obj.suffix.lower() == ".zip":
                import zipfile
                with zipfile.ZipFile(archive_path, 'r') as zf:
                    top_level_names = {Path(n).parts[0] for n in zf.namelist() if n.strip()}
                    zf.extractall(target_dir)
            else:
                import tarfile
                with tarfile.open(archive_path, 'r:*') as tf:
                    top_level_names = {Path(n).parts[0] for n in tf.getnames() if n.strip()}
                    try:
                        tf.extractall(target_dir, filter='data')
                    except TypeError:
                        # Python versi lama belum mendukung parameter 'filter'
                        tf.extractall(target_dir)

            new_top_names = top_level_names - before_entries

            # Jika arsip tidak memiliki satu folder induk (file berserakan di root arsip),
            # bungkus hasil ekstrak ke dalam satu folder bernama sesuai arsipnya.
            if len(new_top_names) != 1:
                wrapper_name = archive_path_obj.name.replace(".tar.gz", "").replace(".tar.xz", "") \
                                                     .replace(".tgz", "").replace(".zip", "")
                wrapper_dir = target_dir / wrapper_name
                wrapper_dir.mkdir(exist_ok=True)
                for name in new_top_names:
                    src = target_dir / name
                    if src.exists() and src != wrapper_dir:
                        src.rename(wrapper_dir / name)
        except Exception as e:
            extract_result["error"] = e

    def finish_extract():
        progress_bar.stop()
        loading_dialog.grab_release()
        loading_dialog.destroy()

        if extract_result["error"] is None:
            status_label.config(text=f"{build_label} successfully extracted to {target_dir}", fg=COLORS["success"])
            messagebox.showinfo("Done", f"{build_label} successfully extracted to the WLM directory:\n{target_dir}")
        else:
            e = extract_result["error"]
            status_label.config(text=f"Error extracting {build_label}: {str(e)}", fg=COLORS["danger"])
            messagebox.showerror("Error", f"Failed to extract archive:\n{str(e)}")

    def worker():
        do_extract()
        # Hop back onto the Tk main thread before touching any widget.
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
        status_label.config(text=f"Opening {build_label} Folder...", fg=COLORS["text_secondary"])
    except Exception as e:
        status_label.config(text=f"Error opening {build_label} folder: {str(e)}", fg=COLORS["danger"])

def open_protonge_folder():
    """Buka folder tempat Proton GE diekstrak (~/wlm/protonge)."""
    open_proton_folder(protonge_dir, "Proton GE")

def open_protoncachyos_folder():
    """Buka folder tempat Proton-CachyOS diekstrak (~/wlm/protoncachyos)."""
    open_proton_folder(protoncachyos_dir, "Proton-CachyOS")

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

    # Fallback: scan folder default & folder kustom yang sedang aktif, untuk prefix yang
    # mungkin belum sempat tercatat di registry (mis. dibuat sebelum fitur ini ada).
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
    loading_dialog.title("Moving Prefix")
    loading_dialog.configure(bg=COLORS["primary"])
    loading_dialog.resizable(False, False)
    loading_dialog.transient(root)
    loading_dialog.protocol("WM_DELETE_WINDOW", lambda: None)  # block closing while move is running

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
            status_label.config(text=f"Prefix '{prefix_code}' moved to {new_path}", fg=COLORS["success"])
            on_done(new_path)
        else:
            e = move_result["error"]
            status_label.config(text=f"Error moving prefix: {str(e)}", fg=COLORS["danger"])
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
    """Ambil path .exe dari script yang sudah ada (baris wine atau proton run)."""
    try:
        with open(script_path, 'r') as f:
            content = f.read()
        match = re.search(r'wine\s+"([^"]+)"', content)
        if not match:
            match = re.search(r'"\s*[^"]*proton[^"]*"\s+run\s+"([^"]+)"', content, re.IGNORECASE)
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

def ask_runner_choice(parent_script_name=None, purpose="play"):
    """
    Tampilkan dialog pilihan runner: Wine (Vanilla) atau Proton GE.
    - parent_script_name: nama game (untuk konteks PLAY), dipakai untuk mengingat pilihan sebelumnya
      dan menjaga prefix ProtonGE tetap konsisten untuk game yang sama.
    - purpose: "play" atau "setup", hanya memengaruhi teks judul dialog.

    Return dict pilihan, atau None jika dibatalkan.
    """
    protonge_list = find_protonge_installations()
    protoncachyos_list = find_protoncachyos_installations()

    existing_cfg = None
    if parent_script_name:
        existing_cfg = load_runner_config().get(parent_script_name)

    dialog = tk.Toplevel(root)
    dialog.title("Select Runner - Setup" if purpose == "setup" else "Select Runner - Play")
    dialog.configure(bg=COLORS["primary"])
    dialog.resizable(False, False)
    dialog.transient(root)
    dialog.grab_set()

    result = {"value": None}

    frame = ttk.Frame(dialog, padding=15)
    frame.pack(fill=tk.BOTH, expand=True)

    ttk.Label(frame, text="Select Runner - Game:",
              font=FONTS["normal"]).pack(anchor="w", pady=(0, 10))

    existing_runner = existing_cfg.get("runner") if existing_cfg else None
    default_runner = existing_runner if existing_runner in ("protonge", "protoncachyos") else "wine"
    runner_var = tk.StringVar(value=default_runner)

    # Baris pemilihan runner - ditampilkan sebagai tombol bertema (bukan radio
    # bulat bawaan Tk) supaya ukurannya seragam dan warnanya konsisten dengan
    # tombol-tombol lain di aplikasi (highlight saat dipilih).
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

    # Container TETAP untuk semua kontrol yang bergantung pada runner yang dipilih (versi
    # Proton, lokasi/pemindahan prefix, dst). Di-pack HANYA SEKALI disini, tepat dibawah baris
    # pemilihan runner - dan tidak pernah di-pack ulang. Kalau widget didalamnya di-pack_forget()
    # lalu di-pack() lagi (misalnya saat pindah tab Wine/Proton GE/Proton-CachyOS), Tkinter akan
    # menaruhnya di urutan PALING AKHIR relatif terhadap widget lain di parent yang sama - itulah
    # sebabnya sebelumnya panel ini bisa "melompat" ke bawah tombol Cancel/OK. Karena container
    # ini sendiri tidak pernah di-pack ulang, posisinya di dialog selalu tetap, apapun yang
    # terjadi didalamnya.
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

        move_btn = ttk.Button(btn_row, text="🚚 Move Prefix...", style="Custom.TButton")
        browse_btn = ttk.Button(btn_row, text="📁 Browse Other Folder/Disk...", style="Custom.TButton")
        default_btn = ttk.Button(btn_row, text="↺ Use Default (WLM Folder)", style="Custom.TButton")

        def refresh():
            for w in (move_btn, browse_btn, default_btn):
                w.pack_forget()
            if state["existing_path"] is not None:
                info_var.set(f"Prefix: {state['existing_code']} (used previously, kept consistent)\n"
                              f"Location: {state['existing_path']}")
                move_btn.pack(side=tk.LEFT)
            else:
                info_var.set(f"A new prefix will be created automatically at:\n{state['new_base_dir']} (e.g. GAMEXXX)")
                browse_btn.pack(side=tk.LEFT, padx=(0, 5))
                default_btn.pack(side=tk.LEFT)

        def do_move():
            def on_moved(new_path):
                state["existing_path"] = new_path
                # Operasi pindah sudah benar-benar terjadi di disk, jadi langsung simpan
                # perubahannya ke runner_config.json meski dialog ini nanti dibatalkan.
                if parent_script_name:
                    cfg_all = load_runner_config()
                    if parent_script_name in cfg_all and cfg_all[parent_script_name].get("runner") == runner_key:
                        cfg_all[parent_script_name]["prefix_path"] = str(new_path)
                        save_runner_config(cfg_all)
                # Perbarui juga registry global supaya deteksi otomatis (find_owning_prefix)
                # untuk game lain yang mungkin memakai prefix yang sama tetap akurat.
                update_registry_prefix_path(runner_key, state["existing_code"], new_path)
                refresh()
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

        move_btn.config(command=do_move)
        browse_btn.config(command=do_browse)
        default_btn.config(command=do_default)

        refresh()
        return panel, state

    # --- Widget grup untuk Wine (Vanilla) ---
    # Wine dibiarkan pakai prefix bawaan sistem (WINEPREFIX env / ~/.wine) secara default demi
    # kompatibilitas dengan game yang sudah pernah di-setup sebelumnya. Centang opsi dibawah untuk
    # memberi game ini prefix terisolasi sendiri didalam folder WLM (atau lokasi lain pilihan sendiri).
    existing_wine_prefix = bool(existing_cfg and existing_cfg.get("runner") == "wine" and existing_cfg.get("prefix_path"))
    wine_use_prefix_var = tk.BooleanVar(value=existing_wine_prefix)
    wine_checkbox = ttk.Checkbutton(
        runner_dynamic_frame,
        text="Use an isolated prefix for this game (avoids buildup in home folder)",
        variable=wine_use_prefix_var,
        style="Custom.TCheckbutton"
    )
    wine_prefix_panel, wine_prefix_state = build_prefix_panel(runner_dynamic_frame, "wine", "Wine")

    def toggle_wine_panel(*_):
        if wine_use_prefix_var.get():
            wine_prefix_panel.pack(anchor="w", pady=(2, 8), fill=tk.X)
        else:
            wine_prefix_panel.pack_forget()

    wine_use_prefix_var.trace_add("write", toggle_wine_panel)

    # --- Widget grup untuk Proton GE ---
    protonge_version_label = ttk.Label(runner_dynamic_frame, text="Proton GE Version:", font=FONTS["small"])
    protonge_version_combo = ttk.Combobox(runner_dynamic_frame, state="readonly", width=32, font=FONTS["small"])

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

    # --- Widget grup untuk Proton-CachyOS ---
    cachyos_version_label = ttk.Label(runner_dynamic_frame, text="Proton-CachyOS Version:", font=FONTS["small"])
    cachyos_version_combo = ttk.Combobox(runner_dynamic_frame, state="readonly", width=32, font=FONTS["small"])

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

        # Sembunyikan dulu semua widget grup runner, baru tampilkan yang relevan,
        # supaya tidak ada widget grup lain yang menumpuk saat berpindah pilihan.
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
        else:  # protoncachyos
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

    # Batal di pojok kiri, OK di pojok kanan (frame ini sudah fill=X sehingga
    # side=LEFT / side=RIGHT menempatkannya di ujung-ujung dialog).
    ttk.Button(btn_frame, text="Cancel", command=on_cancel, style="Custom.TButton", width=12).pack(side=tk.LEFT)
    ttk.Button(btn_frame, text="OK", command=on_ok, style="Custom.TButton", width=12).pack(side=tk.RIGHT)

    dialog.update_idletasks()
    w, h = dialog.winfo_width(), dialog.winfo_height()
    x = root.winfo_x() + (root.winfo_width() // 2) - (w // 2)
    y = root.winfo_y() + (root.winfo_height() // 2) - (h // 2)
    dialog.geometry(f"+{x}+{y}")

    dialog.wait_window()
    return result["value"]

# =======================================================================
# THEME FUNCTIONS
# =======================================================================
def apply_theme(theme_name):
    """Apply the theme to all widgets"""
    global CURRENT_THEME, COLORS
    if theme_name not in THEMES:
        theme_name = "default"
    
    CURRENT_THEME = theme_name
    COLORS = THEMES[theme_name]
    
    # Save theme choice
    save_theme_config(theme_name)
    
    # Update all ttk styles
    update_style_config()
    
    # Update all tk widgets
    update_widget_colors()
    
    # Update status
    status_label.config(text=f"Changed to {COLORS['name']} theme", fg=COLORS["success"])
    
    # Update theme dropdown display
    theme_combo.set(COLORS["name"])

def save_theme_config(theme_name):
    """Save theme choice to file"""
    with open(theme_config_file, 'w') as f:
        json.dump({'theme': theme_name}, f)

def update_style_config():
    """Update ttk style configuration"""
    style.configure("TFrame", background=COLORS["primary"])
    style.configure("TPanedwindow", background=COLORS["primary"])
    
    style.configure("TLabel", 
                    background=COLORS["primary"], 
                    foreground=COLORS["text"],
                    font=FONTS["normal"])
    
    # Custom Button style
    style.configure("Custom.TButton",
                    background=COLORS["button_bg"],
                    foreground=COLORS["button_fg"],
                    bordercolor=COLORS["border"],
                    borderwidth=1,
                    focusthickness=1,
                    focuscolor=COLORS["highlight"],
                    font=FONTS["normal"],
                    padding=6)
    
    # This ensures consistent hover behavior across all themes
    style.map("Custom.TButton",
              # If active (hover), use highlight. If not (!active), use button_bg.
              background=[("active", COLORS["highlight"]), ("!active", COLORS["button_bg"])],
              # If active (hover), use button_text. If not (!active), use button_fg.
              foreground=[("active", COLORS["button_text"]), ("!active", COLORS["button_fg"])])

    # Runner-selection "radio buttons" restyled to look and size like the
    # rest of the app's buttons (border/focus/padding/label layout copied
    # from TButton so no round indicator is drawn), with the selected
    # runner highlighted the same way an active/hovered button is.
    style.layout("Runner.TRadiobutton", style.layout("TButton"))
    style.configure("Runner.TRadiobutton",
                    background=COLORS["button_bg"],
                    foreground=COLORS["button_fg"],
                    bordercolor=COLORS["border"],
                    borderwidth=1,
                    focusthickness=1,
                    focuscolor=COLORS["highlight"],
                    font=FONTS["normal"],
                    anchor="center",
                    padding=6)
    style.map("Runner.TRadiobutton",
              background=[("disabled", COLORS["card_bg"]),
                          ("selected", COLORS["highlight"]),
                          ("active", COLORS["highlight"]),
                          ("!selected", COLORS["button_bg"])],
              foreground=[("disabled", COLORS["text_secondary"]),
                          ("selected", COLORS["button_text"]),
                          ("active", COLORS["button_text"]),
                          ("!selected", COLORS["button_fg"])])

    # Checkbutton (mis. "Use an isolated prefix...") disesuaikan dengan tema aktif:
    # tanpa ini, kotak centang memakai warna bawaan Tk (kotak putih polos) yang
    # menonjol sendiri diatas latar gelap. Background label disamakan dengan
    # latar frame (menyatu), kotak indikator memakai warna "text_background" saat
    # kosong dan warna "highlight" tema saat dicentang, dengan tanda centang
    # berwarna "button_text" supaya tetap kontras dan terbaca.
    style.configure("Custom.TCheckbutton",
                    background=COLORS["primary"],
                    foreground=COLORS["text"],
                    font=FONTS["normal"],
                    indicatorbackground=COLORS["text_background"],
                    indicatorforeground=COLORS["button_text"],
                    indicatormargin=(0, 0, 6, 0),
                    focuscolor=COLORS["highlight"])
    style.map("Custom.TCheckbutton",
              background=[("active", COLORS["primary"])],
              foreground=[("disabled", COLORS["text_secondary"]),
                          ("active", COLORS["text"])],
              indicatorbackground=[("disabled", COLORS["card_bg"]),
                                    ("selected", COLORS["highlight"]),
                                    ("!selected", COLORS["text_background"])],
              indicatorforeground=[("disabled", COLORS["text_secondary"]),
                                    ("selected", COLORS["button_text"])])
    
    # Combobox style
    style.configure("TCombobox",
                    fieldbackground=COLORS["text_background"],
                    background=COLORS["card_bg"],
                    foreground=COLORS["text"],
                    selectbackground=COLORS["highlight"],
                    selectforeground=COLORS["text"],
                    bordercolor=COLORS["border"],
                    relief="flat",
                    borderwidth=1)
    style.map("TCombobox",
              fieldbackground=[("readonly", COLORS["text_background"])],
              selectbackground=[("readonly", COLORS["highlight"])],
              selectforeground=[("readonly", COLORS["text_background"])],
              background=[("readonly", COLORS["card_bg"])],
              foreground=[("readonly", COLORS["text"])])
    
    # Scrollbar style
    style.configure("TScrollbar",
                    background=COLORS["secondary"],
                    troughcolor=COLORS["primary"],
                    bordercolor=COLORS["primary"],
                    arrowcolor=COLORS["text"])
    style.map("TScrollbar",
              background=[("active", COLORS["highlight"])])

    # Progressbar style (used by the extraction loading popup)
    style.configure("TProgressbar",
                    background=COLORS["highlight"],
                    troughcolor=COLORS["card_bg"],
                    bordercolor=COLORS["border"],
                    lightcolor=COLORS["highlight"],
                    darkcolor=COLORS["highlight"])

    # Treeview style
    style.configure("Treeview",
                    background=COLORS["tree_bg"],
                    foreground=COLORS["tree_fg"],
                    fieldbackground=COLORS["tree_bg"],
                    bordercolor=COLORS["border"],
                    borderwidth=0,
                    rowheight=25)
    
    # Treeview Heading style
    style.configure("Treeview.Heading",
                    background=COLORS["secondary"],
                    foreground=COLORS["highlight"],
                    relief="raised",
                    font=FONTS["subtitle"],
                    padding=6,
                    bordercolor=COLORS["border"])
    
    # Mapping Treeview selected item
    style.map("Treeview", 
              background=[("selected", COLORS["tree_highlight"])],
              foreground=[("selected", COLORS["tree_highlight_text"])])

def update_widget_colors():
    """Update colors of all tk widgets and force refresh of ttk styles"""
    try:
        root.configure(bg=COLORS["primary"])
        
        title_label.config(bg=COLORS["primary"], fg=COLORS["text"])
        status_label.config(bg=COLORS["primary"], fg=COLORS["text_secondary"])
        game_title_label.config(bg=COLORS["primary"], fg=COLORS["text"])
        info_label.config(bg=COLORS["primary"], fg=COLORS["text_secondary"])
        icon_label.config(bg=COLORS["card_bg"]) 
        
        # Update settings menu (This is a tk.Menu widget)
        settings_menu.config(bg=COLORS["card_bg"], fg=COLORS["text"], 
                             activebackground=COLORS["highlight"], 
                             activeforeground=COLORS["button_text"])
        
        # Trigger update for ttk buttons
        # Re-applying the style forces Ttk to re-read the mapping, 
        # which is important for fixing button hover issues.
        for btn in all_buttons:
            btn.configure(style="Custom.TButton")
        
        # Re-applying style for combobox and treeview also helps
        theme_combo.configure(style="TCombobox")
        sort_combo.configure(style="TCombobox")
        launch_mode_combo.configure(style="TCombobox")
        tree.configure(style="Treeview")
            
    except Exception as e:
        # Catch error if widgets have not been created yet (during initial setup)
        pass

# =======================================================================
# MAIN APPLICATION FUNCTIONS
# =======================================================================
def update_script_list(sort_order="ascending"):
    """Update script list with sorting"""
    for row in tree.get_children():
        tree.delete(row)
    
    script_files = sorted(bashlaunch_dir.glob("*.sh"), key=lambda x: x.stem.lower())
    if sort_order == "descending":
        script_files.reverse()
    
    for index, file in enumerate(script_files, start=1):
        tree.insert("", "end", values=(index, file.stem))
    
    # Ensure on_select is called if there are items, to load details
    if tree.get_children():
        root.after(100, on_select) 
    else:
        # Reset details if list is empty
        game_title_label.config(text="No Game Selected")
        icon_label.config(image='')
        icon_label.image = None
        info_text.set("Select a game to view details")

# =======================================================================
# Log streaming sedara real time.
# =======================================================================
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
        entry["window"].focus_force()
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
        status_label.config(text=f"Error: Script file not found: {script_name}", fg=COLORS["danger"])
        return

    # Pilih runner (Wine Vanilla / Proton GE) sebelum menjalankan game
    choice = ask_runner_choice(parent_script_name=script_name_only, purpose="play")
    if choice is None:
        status_label.config(text="Launch cancelled.", fg=COLORS["text_secondary"])
        return

    # Sesuaikan isi script dengan runner yang dipilih (proton GE butuh prefix & binary sendiri)
    exe_path = extract_exe_path_from_script(script_path)
    folder_path = extract_folder_path_from_script(script_path)
    if exe_path and folder_path:
        try:
            new_content = build_script_content(folder_path, exe_path, choice)
            with open(script_path, 'w') as f:
                f.write(new_content)
        except Exception as e:
            status_label.config(text=f"Error updating script for runner: {str(e)}", fg=COLORS["danger"])
            return
    else:
        status_label.config(text="Error: Could not read exe/folder path from script.", fg=COLORS["danger"])
        return

    # Simpan pilihan runner untuk game ini agar konsisten di lain waktu
    runner_cfg = load_runner_config()
    runner_cfg[script_name_only] = choice
    save_runner_config(runner_cfg)

    # Ensure script is executable
    if not os.access(script_path, os.X_OK):
        try:
            script_path.chmod(0o755)
        except Exception as e:
            status_label.config(text=f"Error: Cannot set executable permission for {script_name}.", fg=COLORS["danger"])
            return

    commands = {
        "Normal": ["bash", str(script_path)],
        "GalliumHUD": ["bash", "-c", f"GALLIUM_HUD=GPU-load+cpu+fps \"{str(script_path)}\""],
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
        status_label.config(text=f"Launching {script_name[:-3]} via {runner_label} in {launch_mode} mode...", fg=COLORS["success"])
    except FileNotFoundError:
        messagebox.showerror("Error", f"Launcher command not found. Do you have the necessary tools installed?")
        status_label.config(text=f"Error: Launcher command not found.", fg=COLORS["danger"])
    except Exception as e:
        status_label.config(text=f"Error launching script: {str(e)}", fg=COLORS["danger"])

def add_script():
    """Add a new script"""
    exe_path = filedialog.askopenfilename(
        title="Select Windows Executable (.exe)",
        filetypes=[("Executable Files", "*.exe"), ("All Files", "*.*")]
    )
    
    if exe_path:
        exe_path_obj = Path(exe_path)
        exe_name = exe_path_obj.stem
        
        new_name = simpledialog.askstring(
            "Rename Script",
            "Enter script name (for display):",
            initialvalue=exe_name
        )
        
        if new_name:
            # Sanitize filename, only allow alphanumeric, space, underscore, and hyphen
            safe_new_name = "".join(c for c in new_name if c.isalnum() or c in (' ', '_', '-')).strip()
            
            if not safe_new_name:
                messagebox.showerror("Error", "Invalid script name.")
                return

            script_path = bashlaunch_dir / f"{safe_new_name}.sh"
            folder_path = exe_path_obj.parent
            exe_resolved = exe_path_obj.resolve()
            folder_resolved = folder_path.resolve()

            # Note the use of Path.resolve() to ensure absolute paths
            # and correct handling of spaces when writing to the bash script
            script_content = (
                "#!/bin/bash\n"
                f"cd \"{folder_resolved}\"\n"
                f"wine \"{exe_resolved}\"\n"
            )

            try:
                with open(script_path, "w") as script_file:
                    script_file.write(script_content)
                
                # Set permission
                script_path.chmod(0o755)

                # Jika exe yang baru ditambahkan ternyata sudah berada didalam sebuah prefix
                # yang sudah ada (misalnya baru saja selesai diinstall lewat APPS SETUP), kaitkan
                # langsung game ini ke prefix yang sama - JANGAN biarkan PLAY pertama membuat
                # prefix baru yang kosong. Ini krusial untuk game (mis. GOG) yang lisensi /
                # aktivasinya terikat ke prefix tempat ia pertama kali diinstall.
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

                update_script_list()
                status_label.config(text=f"Added: {safe_new_name}{status_extra}", fg=COLORS["success"])
            except Exception as e:
                status_label.config(text=f"Error creating script: {str(e)}", fg=COLORS["danger"])

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
            # Delete script file and icon (if present)
            script_path.unlink(missing_ok=True)
            icon_path.unlink(missing_ok=True)

            # Hapus mapping runner (prefix ProtonGE tidak dihapus, agar save data game tetap aman)
            runner_cfg = load_runner_config()
            if script_name in runner_cfg:
                del runner_cfg[script_name]
                save_runner_config(runner_cfg)
            
            update_script_list()
            game_title_label.config(text="No Game Selected")
            icon_label.config(image='')
            icon_label.image = None
            info_text.set("Select a game to view details")
            status_label.config(text=f"Removed: {script_name}", fg=COLORS["warning"])
        except Exception as e:
            status_label.config(text=f"Error removing files: {str(e)}", fg=COLORS["danger"])

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

            # Pindahkan mapping runner supaya prefix ProtonGE tetap terkait dengan game yang sama
            runner_cfg = load_runner_config()
            if old_name in runner_cfg:
                runner_cfg[new_name] = runner_cfg.pop(old_name)
                save_runner_config(runner_cfg)
            
            update_script_list()
            
            # Move focus to the newly renamed item
            for item in tree.get_children():
                if tree.item(item, "values")[1] == new_name:
                    tree.focus(item)
                    tree.selection_set(item)
                    on_select()
                    break
                    
            status_label.config(text=f"Renamed to: {new_name}", fg=COLORS["success"])
        except Exception as e:
            status_label.config(text=f"Error renaming script: {str(e)}", fg=COLORS["danger"])

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
            
            # Crop the image to a square from the center
            width, height = image.size
            new_size = min(width, height)
            
            left = (width - new_size) // 2
            top = (height - new_size) // 2
            right = (width + new_size) // 2
            bottom = (height + new_size) // 2
            
            image = image.crop((left, top, right, bottom))
            image = image.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
            
            # Save the resized icon
            image.save(icon_dir / f"{script_name}.png", "PNG", quality=95)
            
            load_icon(script_name)
            status_label.config(text=f"Icon updated for {script_name}", fg=COLORS["success"])
        except Exception as e:
            messagebox.showerror("Error", f"Failed to process image: {str(e)}")
            status_label.config(text=f"Error processing image: {str(e)}", fg=COLORS["danger"])

def load_icon(script_name):
    """Load and display the icon at the correct size"""
    icon_path = icon_dir / f"{script_name}.png"
    
    if icon_path.exists():
        try:
            image = Image.open(icon_path)
            if image.mode != 'RGBA':
                image = image.convert('RGBA')
            
            # Ensure the icon size is correct, resize if necessary
            if image.size != (ICON_WIDTH, ICON_HEIGHT):
                image = image.resize((ICON_WIDTH, ICON_HEIGHT), Image.LANCZOS)
            
            photo = ImageTk.PhotoImage(image)
            
            icon_label.config(image=photo)
            icon_label.image = photo  # Save a reference
            
        except Exception as e:
            # If loading fails, display an empty icon
            icon_label.config(image='')
            icon_label.image = None
    else:
        # If the icon file does not exist
        icon_label.config(image='')
        icon_label.image = None

def on_select(event=None):
    """Handle item selection in the treeview"""
    # Use root.after(0, ...) to handle Treeview focus issue
    # when the list has just been updated.
    def do_select():
        selected = tree.focus()
        
        if selected:
            script_name = tree.item(selected, "values")[1]
            game_title_label.config(text=script_name)
            
            # Load icon as soon as possible
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
                        
                    # Read the second line (cd "...") to get the folder path
                    with open(script_path, 'r') as f:
                        lines = f.readlines()
                        folder_path = ""
                        if len(lines) >= 2:
                            # Extract path string from the second line: cd "PATH"
                            # Need to remove '\n' and double quotes
                            folder_path = lines[1].strip().replace('cd "', '').replace('"', '')
                    
                    info_text.set(f"Script File: {script_name}.sh\n"
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
        status_label.config(text=f"Error: Script file not found: {script_name}", fg=COLORS["danger"])
        return

    try:
        # Read the second line (cd "...") to get the folder path
        with open(script_path, 'r') as f:
            lines = f.readlines()
            folder_path = ""
            if len(lines) >= 2 and lines[1].strip().startswith("cd "):
                # Extract path string from the second line: cd "PATH"
                folder_path = lines[1].strip().replace('cd "', '').replace('"', '')

        if folder_path and Path(folder_path).is_dir():
            # Use xdg-open to open the folder in the default file manager
            subprocess.Popen(["xdg-open", folder_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              env=get_clean_subprocess_env())
            status_label.config(text=f"Opening folder for {script_name}...", fg=COLORS["text_secondary"])
        else:
            messagebox.showerror("Error", f"Folder path not found or invalid in script for {script_name}.")
            status_label.config(text=f"Error: Invalid folder path in script.", fg=COLORS["danger"])
            
    except Exception as e:
        status_label.config(text=f"Error opening file manager: {str(e)}", fg=COLORS["danger"])

def open_wine_prefix_folder():
    """Open the Wine Prefix folder (~/.wine)"""
    # Get WINEPREFIX if set, otherwise default to ~/.wine
    wine_prefix = os.environ.get("WINEPREFIX", Path.home() / ".wine")
    
    try:
        if Path(wine_prefix).is_dir():
            # Use xdg-open to open the folder in the default file manager
            subprocess.Popen(["xdg-open", str(wine_prefix)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              env=get_clean_subprocess_env())
            status_label.config(text="Opening Wine Prefix Folder...", fg=COLORS["text_secondary"])
        else:
            messagebox.showerror("Error", f"Wine Prefix folder not found: {wine_prefix}")
            status_label.config(text="Error: Wine Prefix folder not found.", fg=COLORS["danger"])
    except Exception as e:
        status_label.config(text=f"Error opening Wine Prefix: {str(e)}", fg=COLORS["danger"])

def open_winecfg():
    """Open winecfg"""
    try:
        subprocess.Popen(["winecfg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          env=get_clean_subprocess_env())
        status_label.config(text="Opening Wine Configuration...", fg=COLORS["text_secondary"])
    except FileNotFoundError:
        messagebox.showerror("Error", "The 'wine' command was not found.")
        status_label.config(text="Error: Wine command not found.", fg=COLORS["danger"])

def run_exe_setup():
    """Run EXE setup"""
    exe_path = filedialog.askopenfilename(
        title="Select Setup Executable (.exe)",
        filetypes=[("Executable Files", "*.exe"), ("All Files", "*.*")]
    )
    
    if not exe_path:
        return

    # Pilih runner (Wine Vanilla / Proton GE) sebelum menjalankan installer
    # Setiap kali Setup dijalankan dengan Proton GE, prefix baru dibuat secara berurutan
    # di dalam direktori utama (~/wlm/protonprefixes/GAMEXXX).
    choice = ask_runner_choice(parent_script_name=None, purpose="setup")
    if choice is None:
        status_label.config(text="Setup cancelled.", fg=COLORS["text_secondary"])
        return

    try:
        env_vars, extra_args = parse_launch_options(choice.get("launch_options", ""))
        env = get_clean_subprocess_env()
        for key, val in env_vars:
            env[key] = val

        if choice["runner"] in ("protonge", "protoncachyos"):
            env["STEAM_COMPAT_DATA_PATH"] = choice["prefix_path"]
            env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(find_steam_install_path())
            command = [choice["proton_path"], "run", exe_path] + extra_args
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            runner_label = "Proton GE" if choice["runner"] == "protonge" else "Proton-CachyOS"
            status_label.config(
                text=f"Running setup for {Path(exe_path).name} via {runner_label} (prefix: {choice['prefix_code']})...",
                fg=COLORS["text_secondary"])
        else:
            if choice.get("prefix_path"):
                env["WINEPREFIX"] = choice["prefix_path"]
            command = ["wine", exe_path] + extra_args
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            if choice.get("prefix_code"):
                status_label.config(text=f"Running setup for {Path(exe_path).name} via Wine (prefix: {choice['prefix_code']})...", fg=COLORS["text_secondary"])
            else:
                status_label.config(text=f"Running setup for {Path(exe_path).name} via Wine...", fg=COLORS["text_secondary"])
    except FileNotFoundError:
        messagebox.showerror("Error", "Runner command was not found.")
        status_label.config(text="Error: Runner command not found.", fg=COLORS["danger"])
    except Exception as e:
        status_label.config(text=f"Error running setup: {str(e)}", fg=COLORS["danger"])

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

# =======================================================================
# INITIALIZATION
# =======================================================================
config = load_config()
CURRENT_THEME = config.get("theme", "default")
COLORS = THEMES.get(CURRENT_THEME, THEMES["default"]) 

# =======================================================================
# MAIN WINDOW SETUP
# =======================================================================
root = tk.Tk()
root.title("Wine Launch Manager")
root.protocol("WM_DELETE_WINDOW", lambda: (save_window_config(), root.destroy())) # Save config on close

style = ttk.Style()
# Use 'clam' theme for better color customization compatibility
style.theme_use('clam') 

window_size = config["window_size"]
window_position = config["window_position"]

# Set window size with validation
if window_position:
    root.geometry(f"{window_size}{window_position}") 
else:
    # If no position, set size in the center of the screen
    width, height = map(int, window_size.split('x'))
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    x = (screen_width // 2) - (width // 2)
    y = (screen_height // 2) - (height // 2)
    root.geometry(f"{width}x{height}+{x}+{y}")


root.resizable(True, True)
root.minsize(1000, 720)  # keep a floor so the layout/buttons don't get squished

# =======================================================================
# WIDGET CREATION
# =======================================================================
all_buttons = [] # List to store all Ttk buttons for easy update

# HEADER SECTION
header_frame = ttk.Frame(root, padding=(15, 8))
header_frame.pack(fill=tk.X, side=tk.TOP)

title_label = tk.Label(header_frame,
                        text="WINE LAUNCH MANAGER",
                        font=FONTS["title"])
title_label.pack(side=tk.LEFT)

status_label = tk.Label(header_frame,
                            text=f"Using {COLORS['name']} theme",
                            font=FONTS["small"])
status_label.pack(side=tk.RIGHT)

# TOOLBAR
toolbar = ttk.Frame(root, padding=(15, 5, 15, 0))
toolbar.pack(fill=tk.X, side=tk.TOP)

theme_label = ttk.Label(toolbar, text="Theme:", font=FONTS["normal"])
theme_label.pack(side=tk.LEFT, padx=(0, 5))

theme_names = [data["name"] for data in THEMES.values()]
theme_combo = ttk.Combobox(toolbar, 
                            values=theme_names,
                            state="readonly",
                            width=20,
                            font=FONTS["normal"])
theme_combo.set(COLORS["name"])
theme_combo.pack(side=tk.LEFT, padx=(0, 15))
theme_combo.bind("<<ComboboxSelected>>", on_theme_selected)

# Settings button & menu
settings_btn = ttk.Button(toolbar, text="⚙ SETTINGS", style="Custom.TButton")
all_buttons.append(settings_btn)
settings_btn.pack(side=tk.RIGHT)

settings_menu = tk.Menu(root, tearoff=0)
settings_menu.add_command(label="Wine Configuration (winecfg)", 
                          command=open_winecfg)
settings_menu.add_command(label="Open Wine Prefix Folder", 
                          command=open_wine_prefix_folder)
settings_menu.add_command(label="Uninstall Program", 
                          command=lambda: subprocess.Popen(["wine", "uninstaller"]))
settings_menu.add_command(label="Wine Explorer", 
                          command=lambda: subprocess.Popen(["wine", "explorer"]))
settings_menu.add_separator()
settings_menu.add_command(label="Extract Proton GE Archive...",
                          command=extract_protonge_archive)
settings_menu.add_command(label="Open Proton GE Folder",
                          command=open_protonge_folder)
settings_menu.add_separator()
settings_menu.add_command(label="Extract Proton-CachyOS Archive...",
                          command=extract_protoncachyos_archive)
settings_menu.add_command(label="Open Proton-CachyOS Folder",
                          command=open_protoncachyos_folder)
settings_menu.add_separator()
settings_menu.add_command(label="Refresh List", 
                          command=lambda: update_script_list())

# Tk quirk: while a popup Menu is open it holds a grab, so clicking the
# launcher button again first closes the menu (click-outside) and then
# that same click still reaches the button, instantly reopening it - it
# looks like the menu "can't be closed". We track when the menu closes
# and briefly ignore reopen requests that land right after that.
_settings_menu_state = {"closed_at": 0.0}

def _on_settings_menu_closed(event=None):
    _settings_menu_state["closed_at"] = time.monotonic()

settings_menu.bind("<Unmap>", _on_settings_menu_closed)

def _on_global_click_closes_settings_menu(event):
    # Tk's Menu is supposed to auto-close itself via an implicit grab whenever
    # you click outside it, but that grab isn't reliable on every window
    # manager - so we close it manually here instead of trusting Tk to do it.
    if not settings_menu.winfo_ismapped():
        return
    if event.widget is settings_menu:
        return  # click landed on the menu itself - let it handle its own command
    settings_menu.unpost()

# add="+" so this runs alongside (not instead of) each widget's own bindings
root.bind_all("<Button-1>", _on_global_click_closes_settings_menu, add="+")

def toggle_settings_menu():
    if time.monotonic() - _settings_menu_state["closed_at"] < 0.25:
        return  # this click was the one that just closed the menu - don't reopen
    settings_menu.post(settings_btn.winfo_rootx(),
                        settings_btn.winfo_rooty() + settings_btn.winfo_height() + 5)

settings_btn.config(command=toggle_settings_menu)

# MAIN CONTENT
main_container = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
main_container.pack(fill=tk.BOTH, expand=True, padx=15, pady=(0, 15))

# LEFT PANEL (Game List)
left_panel = ttk.Frame(main_container, padding=(0, 0, 10, 0)) 
main_container.add(left_panel, weight=3)

# Controls bar
controls_frame = ttk.Frame(left_panel)
controls_frame.pack(fill=tk.X, pady=(0, 8))

sort_label = ttk.Label(controls_frame, text="Sort:", font=FONTS["normal"])
sort_label.pack(side=tk.LEFT, padx=(0, 5))

sort_combo = ttk.Combobox(controls_frame, 
                            values=["A-Z", "Z-A"], 
                            state="readonly",
                            width=8,
                            font=FONTS["normal"])
sort_combo.current(0)
sort_combo.pack(side=tk.LEFT, padx=(0, 15))
sort_combo.bind("<<ComboboxSelected>>", sort_by_selected)

launch_label = ttk.Label(controls_frame, text="Launch Mode:", font=FONTS["normal"])
launch_label.pack(side=tk.LEFT, padx=(0, 5))

launch_mode_combo = ttk.Combobox(controls_frame,
                                    values=["Normal", "GalliumHUD", "MangoHud-GL", "Mangohud"],
                                    state="readonly",
                                    width=12,
                                    font=FONTS["normal"])
launch_mode_combo.current(0)
launch_mode_combo.pack(side=tk.LEFT)

# Treeview
tree_frame = ttk.Frame(left_panel)
tree_frame.pack(fill=tk.BOTH, expand=True)

tree_scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL)
tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)

tree = ttk.Treeview(tree_frame,
                    columns=("No", "Game Name"),
                    show="headings",
                    yscrollcommand=tree_scroll.set,
                    selectmode="browse")
tree_scroll.config(command=tree.yview)

tree.heading("No", text="No", anchor="center")
tree.heading("Game Name", text="GAME NAME", anchor="w")
tree.column("#0", width=0, stretch=False)
tree.column("No", width=40, anchor="center", minwidth=40, stretch=False)
tree.column("Game Name", width=300, anchor="w", minwidth=200, stretch=True)

tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
def on_tree_click(event):
    """Clicking an already-selected game a second time deselects it,
    instead of doing nothing (ttk.Treeview's default with selectmode='browse')."""
    row_id = tree.identify_row(event.y)
    if not row_id:
        return  # clicked empty space / header - let default handling run
    if row_id in tree.selection():
        tree.selection_remove(row_id)
        tree.focus('')
        on_select()
        return "break"  # swallow the click so it doesn't just re-select the row

tree.bind("<Button-1>", on_tree_click)
tree.bind("<<TreeviewSelect>>", on_select)

# RIGHT PANEL (Game Details)
right_panel = ttk.Frame(main_container, padding=15)
main_container.add(right_panel, weight=1)

# BUTTON PANEL - packed FIRST (before the icon/title/info widgets below) so
# the packer always reserves its space at the bottom of right_panel first.
# If it were packed last, a long game folder path making info_label taller
# could eat into its space and squeeze/shift the buttons around.
button_panel = ttk.Frame(right_panel, padding=(0, 10))
button_panel.pack(fill=tk.X, side=tk.BOTTOM)

icon_frame = ttk.Frame(right_panel, width=ICON_WIDTH, height=ICON_HEIGHT)
icon_frame.pack(pady=(0, 10))
icon_frame.pack_propagate(False) 

icon_label = tk.Label(icon_frame, relief="flat")
icon_label.pack(expand=True, fill=tk.BOTH)

game_title_label = tk.Label(right_panel,
                                text="No Game Selected",
                                font=FONTS["subtitle"],
                                justify=tk.CENTER,
                                wraplength=ICON_WIDTH) 
game_title_label.pack(pady=(0, 8))

info_text = tk.StringVar(value="Select a game to view details")
info_label = tk.Label(right_panel,
                        textvariable=info_text,
                        font=FONTS["small"],
                        justify=tk.LEFT,
                        wraplength=ICON_WIDTH + 50) 
info_label.pack(pady=(0, 10))

# Button grid - Row 1
btn_row1 = ttk.Frame(button_panel)
btn_row1.pack(pady=3)

play_btn = ttk.Button(btn_row1, text="▶ PLAY", command=run_script, style="Custom.TButton", width=12)
all_buttons.append(play_btn)
play_btn.grid(row=0, column=0, padx=3, pady=3)

add_btn = ttk.Button(btn_row1, text="+ ADD", command=add_script, style="Custom.TButton", width=12)
all_buttons.append(add_btn)
add_btn.grid(row=0, column=1, padx=3, pady=3)

remove_btn = ttk.Button(btn_row1, text="🗑 REMOVE", command=remove_script, style="Custom.TButton", width=12)
all_buttons.append(remove_btn)
remove_btn.grid(row=0, column=2, padx=3, pady=3)

# Button grid - Row 2
btn_row2 = ttk.Frame(button_panel)
btn_row2.pack(pady=3)

rename_btn = ttk.Button(btn_row2, text="✏ RENAME", command=rename_script, style="Custom.TButton", width=12)
all_buttons.append(rename_btn)
rename_btn.grid(row=0, column=0, padx=3, pady=3)

icon_btn = ttk.Button(btn_row2, text="🖼 ICON", command=change_icon, style="Custom.TButton", width=12)
all_buttons.append(icon_btn)
icon_btn.grid(row=0, column=1, padx=3, pady=3)

# File Manager Button - NEW
filemanager_btn = ttk.Button(btn_row2, text="📂 FOLDER", command=open_file_manager, style="Custom.TButton", width=12)
all_buttons.append(filemanager_btn)
filemanager_btn.grid(row=0, column=2, padx=3, pady=3)

# Logs button - Row 3
btn_row3 = ttk.Frame(button_panel)
btn_row3.pack(pady=3)

logs_btn = ttk.Button(btn_row3, text="📜 VIEW LOGS", command=view_logs, style="Custom.TButton", width=38)
all_buttons.append(logs_btn)
logs_btn.grid(row=0, column=0, padx=3, pady=3)

# Setup button - Row 4
setup_btn = ttk.Button(button_panel, text="APPS SETUP", command=run_exe_setup, style="Custom.TButton", width=38)
all_buttons.append(setup_btn)
setup_btn.pack(pady=8)

# =======================================================================
# FINAL SETUP AND RUN
# =======================================================================
# Apply initial theme after all widgets are created
apply_theme(CURRENT_THEME)

# Load script list
update_script_list()

# Start the background poller that feeds live game output into any open Logs window
root.after(150, poll_log_queues)

root.mainloop()
