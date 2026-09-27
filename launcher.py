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
import sys
import tkinter as tk
from tkinter import ttk, filedialog, simpledialog, messagebox
from pathlib import Path
from PIL import Image, ImageTk, ImageDraw
import json
import tarfile
import tempfile
import urllib.request
import urllib.error
import webbrowser
from datetime import datetime
import time

# =======================================================================
# APP VERSION / IDENTITY
# =======================================================================
WLM_VERSION = "0.3.5-Beta"
WLM_DEVELOPER = "Opensource OS Gathering Republic (OOGR)"
WLM_MAINTAINER = "Didi Sloth Stanca & Ikan Goreng"

def _print_version_and_exit():
    print(f"WLM Version: {WLM_VERSION}")
    print(f"Developer: {WLM_DEVELOPER}")
    print(f"Maintener: {WLM_MAINTAINER}")
    sys.exit(0)

# Handle `winelaunchmanager --version` / `-v` before doing anything else
# (no need to touch the filesystem or open a display just to print this).
if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-v"):
    _print_version_and_exit()

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
            extract_archive_to_dir(archive_path, target_dir)
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

PROTONGE_RELEASES_API = "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases"
PROTONGE_RELEASES_PAGE = "https://github.com/GloriousEggroll/proton-ge-custom/releases"

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

def download_and_install_protonge_worker(release, append_line, set_progress=None):
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
        tmp_path = Path(tmp_name)

        req = urllib.request.Request(url, headers={"User-Agent": "wine-launcher-manager"})
        downloaded = 0
        last_report = 0.0
        with urllib.request.urlopen(req, timeout=30) as resp, os.fdopen(tmp_fd, "wb") as out_file:
            while True:
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

        if set_progress and total_size:
            set_progress(100)

        append_line("")
        append_line(f"Download complete ({human_size(downloaded)}). Extracting to {protonge_dir}...")
        extract_archive_to_dir(tmp_path, protonge_dir)

        append_line("")
        append_line(f"ProtonGE {release['tag']} installed successfully.")
        root.after(0, lambda: status_label.config(
            text=f"ProtonGE {release['tag']} installed successfully.", fg=COLORS["success"]))
    except Exception as e:
        err = str(e)
        append_line("")
        append_line(f"ERROR: {err}")
        root.after(0, lambda: messagebox.showerror("Download Failed", f"Failed to download/install ProtonGE:\n{err}",
                                                     parent=root))
        root.after(0, lambda: status_label.config(text=f"ProtonGE download failed: {err}", fg=COLORS["danger"]))
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
    dialog.title("Download ProtonGE")
    dialog.configure(bg=COLORS["primary"])
    dialog.resizable(True, True)
    dialog.minsize(640, 480)
    dialog.transient(root)
    dialog.grab_set()

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
                                 columns=("Version", "Published", "Size", "Status"),
                                 show="headings",
                                 yscrollcommand=vscroll.set,
                                 selectmode="browse",
                                 height=10)
    release_tree.grid(row=0, column=0, sticky="nsew")
    vscroll.config(command=release_tree.yview)

    release_tree.heading("Version", text="Version", anchor="w")
    release_tree.heading("Published", text="Published", anchor="w")
    release_tree.heading("Size", text="Size", anchor="w")
    release_tree.heading("Status", text="Status", anchor="w")
    release_tree.column("Version", width=220, anchor="w", stretch=False)
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
                                 values=(rel["name"], rel["published_at"], human_size(rel["size"]),
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

        append_line, set_progress, win = open_task_log_window(f"Download ProtonGE - {rel['tag']}", modal_parent=dialog)
        append_line(f"Release: {rel['name']} ({rel['tag']})")
        append_line("")
        threading.Thread(target=download_and_install_protonge_worker, args=(rel, append_line, set_progress),
                          daemon=True).start()

    def do_open_page():
        webbrowser.open(PROTONGE_RELEASES_PAGE)

    ttk.Button(btn_row, text="Download & Install", style="Custom.TButton", width=18,
               command=do_download).grid(row=0, column=0, padx=3)
    ttk.Button(btn_row, text="Open Releases Page", style="Custom.TButton", width=18,
               command=do_open_page).grid(row=0, column=1, padx=3)

    dialog.update_idletasks()
    x = root.winfo_rootx() + (root.winfo_width() - 720) // 2
    y = root.winfo_rooty() + (root.winfo_height() - 560) // 2
    dialog.geometry(f"720x560+{max(x, 0)}+{max(y, 0)}")

# =======================================================================
# Proton-CachyOS - Download Online (sama pola dengan Download ProtonGE Online)
# =======================================================================
PROTONCACHYOS_RELEASES_API = "https://api.github.com/repos/CachyOS/proton-cachyos/releases"
PROTONCACHYOS_RELEASES_PAGE = "https://github.com/CachyOS/proton-cachyos/releases"
ARCHIVE_SUFFIXES = (".tar.gz", ".tar.xz", ".tgz", ".zip")

def _strip_archive_suffix(name):
    """Buang ekstensi arsip (.tar.gz/.tar.xz/.tgz/.zip) dari sebuah nama file/asset."""
    for suf in ARCHIVE_SUFFIXES:
        if name.lower().endswith(suf):
            return name[:-len(suf)]
    return name

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

def download_and_install_protoncachyos_worker(release, append_line, set_progress=None):
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
        tmp_path = Path(tmp_name)

        req = urllib.request.Request(url, headers={"User-Agent": "wine-launcher-manager"})
        downloaded = 0
        last_report = 0.0
        with urllib.request.urlopen(req, timeout=30) as resp, os.fdopen(tmp_fd, "wb") as out_file:
            while True:
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

        if set_progress and total_size:
            set_progress(100)

        append_line("")
        append_line(f"Download complete ({human_size(downloaded)}). Extracting to {protoncachyos_dir}...")
        extract_archive_to_dir(tmp_path, protoncachyos_dir)

        append_line("")
        append_line(f"Proton-CachyOS {release['release_name']} installed successfully.")
        root.after(0, lambda: status_label.config(
            text=f"Proton-CachyOS {release['release_name']} installed successfully.", fg=COLORS["success"]))
    except Exception as e:
        err = str(e)
        append_line("")
        append_line(f"ERROR: {err}")
        root.after(0, lambda: messagebox.showerror("Download Failed",
                                                     f"Failed to download/install Proton-CachyOS:\n{err}",
                                                     parent=root))
        root.after(0, lambda: status_label.config(text=f"Proton-CachyOS download failed: {err}", fg=COLORS["danger"]))
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
    dialog.title("Download Proton-CachyOS")
    dialog.configure(bg=COLORS["primary"])
    dialog.resizable(True, True)
    dialog.minsize(640, 480)
    dialog.transient(root)
    dialog.grab_set()

    frame = ttk.Frame(dialog, padding=15)
    frame.pack(fill=tk.BOTH, expand=True)

    top_bar = ttk.Frame(frame)
    top_bar.pack(fill=tk.X, pady=(0, 8))
    ttk.Label(top_bar, text="Latest Proton-CachyOS builds (CachyOS/proton-cachyos):",
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
                                 columns=("Version", "Variant", "Published", "Size", "Status"),
                                 show="headings",
                                 yscrollcommand=vscroll.set,
                                 selectmode="browse",
                                 height=10)
    release_tree.grid(row=0, column=0, sticky="nsew")
    vscroll.config(command=release_tree.yview)

    release_tree.heading("Version", text="Version", anchor="w")
    release_tree.heading("Variant", text="Variant", anchor="w")
    release_tree.heading("Published", text="Published", anchor="w")
    release_tree.heading("Size", text="Size", anchor="w")
    release_tree.heading("Status", text="Status", anchor="w")
    release_tree.column("Version", width=200, anchor="w", stretch=True)
    release_tree.column("Variant", width=90, anchor="w", stretch=False)
    release_tree.column("Published", width=100, anchor="w", stretch=False)
    release_tree.column("Size", width=90, anchor="w", stretch=False)
    release_tree.column("Status", width=90, anchor="w", stretch=False)

    releases_data = {}

    def variant_label(asset_name):
        n = asset_name.lower()
        if "native" in n:
            return "Native"
        if "slr" in n:
            return "SLR"
        return "Standalone"

    def populate(releases):
        release_tree.delete(*release_tree.get_children())
        releases_data.clear()
        installed_names = {name for name, _ in find_protoncachyos_installations()}
        for rel in releases:
            iid = f"{rel['tag']}::{rel['asset_name']}"
            releases_data[iid] = rel
            asset_folder_name = _strip_archive_suffix(rel["asset_name"])
            is_installed = rel["tag"] in installed_names or asset_folder_name in installed_names
            release_tree.insert("", tk.END, iid=iid,
                                 values=(rel["release_name"], variant_label(rel["asset_name"]),
                                         rel["published_at"], human_size(rel["size"]),
                                         "Installed" if is_installed else ""))

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
        if not messagebox.askyesno(
            "Download & Install Proton-CachyOS",
            f"Download and install Proton-CachyOS {rel['release_name']} ({variant_label(rel['asset_name'])})?\n\n"
            f"File: {rel['asset_name']}\n"
            f"Size: {human_size(rel['size'])}\n\n"
            f"It will be extracted into:\n{protoncachyos_dir}",
            parent=dialog
        ):
            return

        append_line, set_progress, win = open_task_log_window(
            f"Download Proton-CachyOS - {rel['release_name']}", modal_parent=dialog)
        append_line(f"Release: {rel['release_name']} ({rel['tag']}) - {rel['asset_name']}")
        append_line("")
        threading.Thread(target=download_and_install_protoncachyos_worker, args=(rel, append_line, set_progress),
                          daemon=True).start()

    def do_open_page():
        webbrowser.open(PROTONCACHYOS_RELEASES_PAGE)

    ttk.Button(btn_row, text="Download & Install", style="Custom.TButton", width=18,
               command=do_download).grid(row=0, column=0, padx=3)
    ttk.Button(btn_row, text="Open Releases Page", style="Custom.TButton", width=18,
               command=do_open_page).grid(row=0, column=1, padx=3)

    dialog.update_idletasks()
    x = root.winfo_rootx() + (root.winfo_width() - 720) // 2
    y = root.winfo_rooty() + (root.winfo_height() - 560) // 2
    dialog.geometry(f"720x560+{max(x, 0)}+{max(y, 0)}")

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

        # Kalau folder/exe game ini berada didalam prefix lama, ikutkan pindah ke lokasi
        # baru (path relatifnya dipertahankan persis). Kalau tidak (game diluar prefix,
        # cuma "menumpang" prefix ini), biarkan path folder/exe-nya seperti semula.
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

    # Tambahan: scan folder default & folder kustom yang sedang aktif, untuk prefix yang
    # sudah ada di disk tapi belum (atau belum sempat) tercatat di registry.
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

    # Petakan tiap prefix ke nama game/aplikasi yang memakainya, dari runner_config.json
    # (bisa lebih dari satu game untuk prefix yang sama, mis. game yang lisensinya terikat
    # ke prefix yang sama lewat find_owning_prefix).
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
        # Lepas grab modal parent SEMENTARA - kalau tidak, dialog baru ini bisa terbuka
        # dibelakang parent (yang masih memegang grab) dan jadi tidak bisa diklik sama
        # sekali (tombol dibelakangnya yang malah "menyala terus" menerima klik).
        parent.grab_release()

    dialog = tk.Toplevel(parent_win)
    dialog.title(f"Select {label} Build")
    dialog.configure(bg=COLORS["primary"])
    dialog.resizable(False, False)
    dialog.transient(parent_win)
    dialog.grab_set()
    dialog.lift()
    dialog.focus_force()

    result = {"value": None}

    frame = ttk.Frame(dialog, padding=15)
    frame.pack(fill=tk.BOTH, expand=True)

    ttk.Label(frame,
              text=f"This prefix has no {label} version recorded yet.\nSelect the build to use for it:",
              font=FONTS["normal"], justify=tk.LEFT).pack(anchor="w", pady=(0, 10))

    names = [name for name, _ in installs]
    combo = ttk.Combobox(frame, values=names, state="readonly", width=32, font=FONTS["normal"])
    combo.current(0)
    combo.pack(fill=tk.X, pady=(0, 15))

    btn_row = ttk.Frame(frame)
    btn_row.pack()

    def close_dialog():
        dialog.destroy()
        if parent_had_grab and parent.winfo_exists():
            parent.grab_set()
            parent.lift()
            parent.focus_force()

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
            # Simpan pilihan ini ke registry, supaya prefix ini tidak ditanya lagi lain kali.
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
            # Prefix Wine SEBENARNYA punya Proton ada di subfolder 'pfx' didalam
            # STEAM_COMPAT_DATA_PATH - itulah yang harus dipakai sebagai WINEPREFIX,
            # dan winetricks harus diarahkan ke binary wine milik Proton ini sendiri
            # (env WINE), bukan wine sistem, atau perubahannya bisa salah sasaran.
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
        # Winetricks sering menampilkan progress download (curl/wget) di stdout/stderr dan,
        # kalau dijalankan tanpa verb, membuka GUI pemilihan komponennya sendiri (zenity/
        # kdialog) - itu tetap tampil normal karena cuma proses GUI terpisah, tidak
        # dipengaruhi oleh redirect stdio dibawah ini. Supaya prosesnya (terutama progress
        # downloadnya) bisa dipantau live, dijalankan lewat pty yang sama dengan yang
        # dipakai untuk menjalankan game (running_games + jendela Logs), bukan Popen diam-diam.
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
            open_log_window(task_key)  # langsung buka jendela log real-time-nya
            status_label.config(
                text=f"Running Winetricks for prefix {prefix_code} ({runner_label})...",
                fg=COLORS["text_secondary"])
        except FileNotFoundError:
            messagebox.showerror("Error", f"Command not found: {' '.join(str(c) for c in command)}")
            status_label.config(text="Error: winetricks command not found.", fg=COLORS["danger"])
        except Exception as e:
            messagebox.showerror("Error", f"Failed to launch Winetricks:\n{str(e)}")
            status_label.config(text=f"Error launching Winetricks: {str(e)}", fg=COLORS["danger"])
        return

    try:
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        status_label.config(
            text=f"Opening {tool_label} for prefix {prefix_code} ({runner_label})...",
            fg=COLORS["text_secondary"])
    except FileNotFoundError:
        messagebox.showerror("Error", f"Command not found: {' '.join(str(c) for c in command)}")
        status_label.config(text="Error: runner command not found.", fg=COLORS["danger"])
    except Exception as e:
        messagebox.showerror("Error", f"Failed to launch {tool_label}:\n{str(e)}")
        status_label.config(text=f"Error launching {tool_label}: {str(e)}", fg=COLORS["danger"])

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
    dialog.title(f"Winetricks - {entry['prefix_code']}")
    dialog.configure(bg=COLORS["primary"])
    dialog.resizable(False, False)
    dialog.transient(parent_win)
    dialog.grab_set()
    dialog.lift()
    dialog.focus_force()

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
            parent_dialog.focus_force()

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
        # Tanpa verb sama sekali - winetricks akan membuka menu pemilihan komponennya
        # sendiri (GUI zenity/kdialog bawaannya), tetap lewat jendela Logs yang sama.
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
    win.title(title)
    win.configure(bg=COLORS["primary"])
    win.resizable(False, False)
    win.transient(parent)
    # Sengaja tidak boleh ditutup manual oleh pengguna selagi operasinya masih berjalan -
    # supaya tidak ada window "loading" yang ditinggal menggantung tanpa proses dibaliknya.
    win.protocol("WM_DELETE_WINDOW", lambda: None)
    win.grab_set()

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

def open_task_log_window(title, modal_parent=None):
    """Buka jendela log sederhana untuk task Python di background thread (backup/restore) -
    beda dari open_log_window yang khusus untuk proses via pty (game/winetricks).
    Return (append_line, win): append_line(text) aman dipanggil dari thread manapun untuk
    menambah satu baris ke jendela log ini (lewat queue, dipoll oleh main thread/Tk).

    modal_parent: dialog Toplevel lain (mis. Prefix Configuration Manager) yang mungkin
    sedang memegang grab_set() aktif. Kalau diisi, grab itu DILEPAS dulu supaya jendela log
    ini (yang tidak modal) benar-benar bisa diklik - tanpa ini, tombol "Close" (dan semua
    isi jendela log ini) tidak akan merespon klik sama sekali selama modal_parent masih
    memegang grab, memaksa pengguna menutupnya lewat window manager. Grab itu otomatis
    dikembalikan ke modal_parent begitu jendela log ini ditutup (lewat tombol Close ATAU
    lewat tombol close window manager)."""
    if modal_parent is not None and modal_parent.winfo_exists():
        try:
            modal_parent.grab_release()
        except tk.TclError:
            pass

    win = tk.Toplevel(root)
    win.title(title)
    win.configure(bg=COLORS["primary"])
    win.geometry("800x480")
    win.minsize(480, 300)

    frame = ttk.Frame(win, padding=10)
    frame.pack(fill=tk.BOTH, expand=True)

    # Progress bar + label persentase - dipakai supaya pengguna langsung lihat seberapa jauh
    # proses backup/restore-nya (bukan cuma baris log berjalan) dan tahu aplikasinya masih
    # bekerja, bukan macet. Dimulai dalam mode "indeterminate" (animasi bolak-balik) karena
    # totalnya belum diketahui saat jendela ini pertama dibuka (mis. masih menghitung ukuran
    # total); dipindah ke mode "determinate" begitu total sudah diketahui lewat set_progress().
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

    def on_close():
        win.destroy()
        if modal_parent is not None and modal_parent.winfo_exists():
            try:
                modal_parent.grab_set()
                modal_parent.lift()
                modal_parent.focus_force()
            except tk.TclError:
                pass

    close_row = ttk.Frame(frame)
    close_row.pack(pady=(8, 0))
    ttk.Button(close_row, text="Close", command=on_close, style="Custom.TButton", width=12).pack()
    win.protocol("WM_DELETE_WINDOW", on_close)

    line_queue = queue.Queue()
    # Cuma menyimpan progress TERBARU (bukan queue) - progress bisa di-update sangat sering
    # (tiap file diarsipkan/diekstrak), jadi yang penting hanya nilai paling akhir tiap kali
    # UI di-poll, bukan riwayat semuanya. dict assignment/read ini aman dipanggil lintas
    # thread untuk kebutuhan sederhana seperti ini (GIL Python).
    latest_progress = {"value": None}  # None = belum ada update / masih indeterminate

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

    return append_line, set_progress, win

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

def run_backup_worker(entry, games_info, dest_path, append_line, set_progress):
    """Berjalan di background thread: bikin satu arsip .tar.gz berisi folder prefix + folder
    tiap game yang masih ada di disk, plus manifest.json (dipakai run_restore_worker() untuk
    membangun ulang semuanya - prefix, script game, runner_config - dilain waktu/komputer)."""
    prefix_path = Path(entry["prefix_path"])
    counters = {"count": 0, "bytes": 0}
    last_report = [0.0]

    # Hitung dulu total ukuran yang akan diarsipkan supaya progress bar bisa ditampilkan
    # sebagai persentase yang berarti (bukan cuma spinner tak-tentu) selama proses tar.add()
    # dibawah - langkah ini sendiri hanya melakukan stat() per file (cepat), bukan membaca
    # isi filenya, jadi jauh lebih ringan dibanding proses pengarsipan (kompresi) sesudahnya.
    append_line("Calculating total size to back up...")
    size_sources = [prefix_path]
    size_sources += [g["folder_path"] for g in games_info if not g["inside_prefix"]]
    size_sources += [g["icon_path"] for g in games_info if g["has_icon"]]
    total_bytes = compute_total_size(size_sources)
    append_line(f"Total size: {human_size(total_bytes)}" if total_bytes else "Total size: unknown")
    append_line("")

    def progress_filter(tarinfo):
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
                    # Folder game ini sudah ikut terarsipkan lewat "prefix" diatas (mis. game
                    # GOG yang terinstall langsung ke drive_c prefix) - JANGAN diarsipkan lagi
                    # secara terpisah, supaya ukuran backup tidak dobel 2x untuk data yang sama.
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
        root.after(0, lambda: status_label.config(
            text=f"Backup of '{entry['prefix_code']}' saved to {dest_path}", fg=COLORS["success"]))
    except Exception as e:
        err = str(e)
        append_line("")
        append_line(f"ERROR: {err}")
        root.after(0, lambda: messagebox.showerror("Backup Failed", f"Backup failed:\n{err}"))
        root.after(0, lambda: status_label.config(text=f"Backup failed: {err}", fg=COLORS["danger"]))
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

    # Kalau dipanggil dari Prefix Configuration Manager (yang modal/grab_set()), lepas
    # grab-nya selagi jendela log ini terbuka - lihat docstring open_task_log_window.
    modal_parent = parent_dialog if (parent_dialog is not None and parent_dialog.winfo_exists()) else None
    append_line, set_progress, win = open_task_log_window(f"Backup - {entry['prefix_code']}",
                                                            modal_parent=modal_parent)
    append_line(f"Starting backup of prefix '{entry['prefix_code']}'...")
    append_line(f"Destination: {dest_path}")
    append_line("")

    threading.Thread(target=run_backup_worker,
                      args=(entry, games_info, dest_path, append_line, set_progress),
                      daemon=True).start()

def run_restore_worker(archive_path, manifest, runner_key, prefix_code, target_prefix_path,
                        games_dest_base, append_line, set_progress, total_size=0):
    """Berjalan di background thread: ekstrak folder prefix & folder tiap game dari arsip
    backup, lalu daftarkan sebagai prefix/game baru di launcher ini (script .sh baru,
    runner_config.json, prefix_registry.json).

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
            rel = m.name[len(member_prefix):]
            if not rel:
                continue  # entri direktori teratasnya sendiri - sudah dibuat lewat mkdir
            extracted_count[0] += 1
            if m.size:
                extracted_bytes[0] += m.size
            now = time.time()
            if now - last_report[0] > 0.2 or extracted_count[0] <= 3:
                append_line(f"[{extracted_count[0]:>6}] {rel}")
                last_report[0] = now
                if total_size > 0:
                    set_progress(min(extracted_bytes[0], total_size) / total_size * 100)
            m.name = rel  # ekstrak relatif ke dest_dir, bukan ke path lengkap didalam arsip
            try:
                tar.extract(m, path=str(dest_dir), filter="fully_trusted")
            except TypeError:
                # Python <3.12 belum punya parameter 'filter' untuk extract().
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
                # Sudah ikut terekstrak lewat "prefix/" diatas - tidak perlu ekstraksi
                # terpisah, sesuai bagaimana file itu diarsipkan (lihat run_backup_worker).
                append_line(f"'{g['script_name']}' is already inside the restored prefix "
                            f"- no separate extraction needed.")
                rel_to_prefix = g.get("relative_to_prefix") or ""
                game_dest_map[g["script_name"]] = (target_prefix_path / rel_to_prefix) if rel_to_prefix else target_prefix_path

            # Ekstrak icon custom tiap game (kalau ada) - backup lama (dibuat sebelum fitur
            # ini ada) tidak akan punya "has_icon"/entri "icons/..." sama sekali di arsipnya,
            # jadi cukup dilewati saja untuk game itu (bukan error) lewat .get() + getmember().
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

        # Daftarkan prefix ke registry - proton_path/proton_name dikosongkan dengan sengaja
        # (lihat docstring diatas).
        record_prefix_usage(runner_key, prefix_code, target_prefix_path,
                             proton_name=None, proton_path=None)

        # Daftarkan tiap game: bangun ulang script .sh + entry runner_config.json-nya.
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
                # Tidak ada relative_exe tercatat (mis. exe-nya ada diluar folder game) -
                # pakai path exe lama sebagai fallback; kemungkinan besar perlu diperbaiki
                # manual lewat Edit/Rename game ini kalau path itu tidak ada dikomputer ini.
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
        root.after(0, lambda: status_label.config(
            text=f"Restore of '{prefix_code}' completed.", fg=COLORS["success"]))
        root.after(0, update_script_list)
    except Exception as e:
        err = str(e)
        append_line("")
        append_line(f"ERROR: {err}")
        root.after(0, lambda: messagebox.showerror("Restore Failed", f"Restore failed:\n{err}"))
        root.after(0, lambda: status_label.config(text=f"Restore failed: {err}", fg=COLORS["danger"]))

def open_restore_backup_dialog(parent_dialog=None):
    """Pilih file backup (.tar.gz), baca manifest-nya, lalu tampilkan dialog untuk memilih
    kode/lokasi tujuan prefix & folder game sebelum benar-benar mengekstrak & mendaftarkannya
    sebagai prefix/game baru di launcher ini (lihat run_restore_worker)."""
    parent_win = parent_dialog if (parent_dialog is not None and parent_dialog.winfo_exists()) else root

    archive_path = filedialog.askopenfilename(
        title="Select Backup Archive",
        filetypes=[("Backup Archive", "*.tar.gz *.tgz"), ("All Files", "*.*")],
        parent=parent_win
    )
    if not archive_path:
        return
    archive_path = Path(archive_path)

    def read_archive():
        # tarfile mode "r:gz" bersifat stream (tidak bisa seek bebas seperti file biasa),
        # jadi mencari member "manifest.json" - yang ditulis PALING TERAKHIR saat backup
        # dibuat (lihat run_backup_worker) - berarti seluruh isi arsip harus dibaca dulu dari
        # awal. Untuk arsip besar ini bisa memakan waktu nyata, makanya dijalankan di
        # background thread (lewat run_with_loading_overlay) supaya UI tidak membeku tanpa
        # indikasi apapun. getmembers() dipanggil lagi setelahnya untuk menghitung total
        # ukuran (dipakai progress bar saat restore nanti) - ini praktis gratis karena
        # tarfile sudah menge-cache daftar member itu sejak pencarian manifest.json diatas.
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

        # Kalau kode prefix aslinya sudah dipakai prefix lain dimesin ini, sarankan kode
        # GAMEXXX berikutnya yang masih kosong (tetap mengikuti pola penamaan GAME001,
        # GAME002, dst yang dipakai konsisten diseluruh launcher) - bukan malah membiarkan
        # dua prefix berbeda berbagi nama folder yang sama.
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
        dialog.title("Restore Backup")
        dialog.configure(bg=COLORS["primary"])
        dialog.resizable(False, False)
        dialog.transient(parent_win)
        dialog.grab_set()
        dialog.lift()
        dialog.focus_force()

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

        # Folder tujuan terpisah untuk game HANYA ditanyakan kalau memang ada game yang
        # filenya disimpan terpisah dari prefix di backup ini. Kalau semua game backup ini
        # sudah termasuk didalam prefix (kasus paling umum, mis. game GOG), bagian ini
        # dilewati sepenuhnya - tinggal restore prefix-nya saja, game ikut otomatis.
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
            # reacquire_parent_grab=False dipakai saat lanjut ke proses restore (do_restore) -
            # grab parent_dialog SENGAJA belum dikembalikan disini; open_task_log_window yang
            # akan segera dibuka butuh grab itu tetap terlepas selama jendela log-nya terbuka
            # (lihat docstring open_task_log_window untuk alasannya).
            if reacquire_parent_grab and parent_had_grab and parent_dialog.winfo_exists():
                parent_dialog.grab_set()
                parent_dialog.lift()
                parent_dialog.focus_force()

        def do_restore():
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

            append_line, set_progress, win = open_task_log_window(
                f"Restore - {prefix_code}",
                modal_parent=(parent_dialog if parent_had_grab else None)
            )
            append_line(f"Starting restore from: {archive_path}")
            append_line(f"Prefix destination: {target_prefix_path}")
            if games_dest_base:
                append_line(f"Game(s) destination base folder: {games_dest_base}")
            if total_size:
                append_line(f"Total size to extract: {human_size(total_size)}")
            append_line("")

            threading.Thread(target=run_restore_worker,
                              args=(archive_path, manifest, runner_key, prefix_code,
                                    target_prefix_path, games_dest_base, append_line,
                                    set_progress, total_size),
                              daemon=True).start()

        ttk.Button(btn_row, text="Restore", style="Custom.TButton", width=12,
                   command=do_restore).grid(row=0, column=0, padx=4)
        ttk.Button(btn_row, text="Cancel", style="Custom.TButton", width=12,
                   command=close_dialog).grid(row=0, column=1, padx=4)

        dialog.protocol("WM_DELETE_WINDOW", close_dialog)
        dialog.update_idletasks()
        x = parent_win.winfo_rootx() + (parent_win.winfo_width() - dialog.winfo_width()) // 2
        y = parent_win.winfo_rooty() + (parent_win.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{x}+{y}")

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

    # Bersihkan entry game-game yang memakai prefix ini dari launcher (script .sh, icon,
    # runner_config.json). File game yang berada DILUAR prefix sengaja TIDAK disentuh -
    # sama seperti perilaku Remove biasa (remove_script).
    runner_cfg = load_runner_config()
    for script_name in games:
        script_path = bashlaunch_dir / f"{script_name}.sh"
        icon_path = icon_dir / f"{script_name}.png"
        script_path.unlink(missing_ok=True)
        icon_path.unlink(missing_ok=True)
        if script_name in runner_cfg:
            del runner_cfg[script_name]
    save_runner_config(runner_cfg)

    # Hapus juga catatan prefix ini dari registry global supaya tidak muncul lagi di
    # Prefix Configuration Manager maupun dideteksi keliru oleh find_owning_prefix.
    reg = load_prefix_registry()
    reg.pop(f"{entry['runner']}:{entry['prefix_code']}", None)
    save_prefix_registry(reg)

    status_label.config(text=f"Removed prefix '{entry['prefix_code']}' and {len(games)} game(s)/app(s).",
                         fg=COLORS["warning"])
    update_script_list()
    return True

def open_prefix_manager_dialog():
    """Dialog utama untuk memanajemen konfigurasi per-prefix: menampilkan semua prefix Wine/
    Proton GE/Proton-CachyOS yang sudah pernah dibuat (beserta nama game/aplikasi yang
    memakainya), lalu memungkinkan membuka winecfg, Wine Explorer, uninstaller Windows, atau
    Winetricks KHUSUS untuk prefix yang dipilih saja. Jendelanya bisa di-resize bebas, dan ada
    scrollbar horizontal untuk kolom yang kepotong."""
    prefixes = list_all_known_prefixes()

    dialog = tk.Toplevel(root)
    dialog.title("Prefix Configuration Manager")
    dialog.configure(bg=COLORS["primary"])
    dialog.resizable(True, True)
    dialog.minsize(640, 360)
    dialog.transient(root)
    dialog.grab_set()

    frame = ttk.Frame(dialog, padding=15)
    frame.pack(fill=tk.BOTH, expand=True)

    top_bar = ttk.Frame(frame)
    top_bar.pack(fill=tk.X, pady=(0, 8))

    ttk.Label(top_bar, text="Select a prefix to configure (winecfg / explorer / uninstaller / winetricks):",
              font=FONTS["normal"]).pack(side=tk.LEFT, anchor="w")

    refresh_btn = ttk.Button(top_bar, text="Refresh", style="Custom.TButton", width=12)
    refresh_btn.pack(side=tk.RIGHT, padx=(0, 6))

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
                                height=10)
    prefix_tree.grid(row=0, column=0, sticky="nsew")
    vscroll.config(command=prefix_tree.yview)
    hscroll.config(command=prefix_tree.xview)

    prefix_tree.heading("Runner", text="Runner", anchor="w")
    prefix_tree.heading("Prefix", text="Prefix Code", anchor="w")
    prefix_tree.heading("Games", text="Game(s)", anchor="w")
    prefix_tree.heading("Proton", text="Proton Version", anchor="w")
    prefix_tree.heading("Path", text="Location", anchor="w")
    # stretch=False untuk semua kolom - lebar kolom TETAP walau jendela diperkecil,
    # sehingga kalau tidak muat, scrollbar horizontal-lah yang dipakai untuk melihat
    # sisanya (bukan tulisan yang otomatis terpotong/menyempit).
    prefix_tree.column("Runner", width=100, anchor="w", stretch=False)
    prefix_tree.column("Prefix", width=100, anchor="w", stretch=False)
    prefix_tree.column("Games", width=220, anchor="w", stretch=False)
    prefix_tree.column("Proton", width=170, anchor="w", stretch=False)
    prefix_tree.column("Path", width=320, anchor="w", stretch=False)

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
            empty_label.pack(anchor="w", pady=(8, 0))
        # Pertahankan seleksi lama kalau prefix itu masih ada setelah refresh, supaya
        # tombol Winecfg/Explorer/dst tidak perlu diklik ulang tanpa alasan.
        if selected_iid is not None and prefix_tree.exists(selected_iid):
            prefix_tree.selection_set(selected_iid)

    refresh_prefix_list()
    refresh_btn.config(command=refresh_prefix_list)

    def get_selected_entry():
        sel = prefix_tree.selection()
        if not sel:
            messagebox.showinfo("Info", "Select a prefix from the list first.")
            return None
        runner_key, prefix_code = sel[0].split(":", 1)
        for entry in prefixes:
            if entry["runner"] == runner_key and entry["prefix_code"] == prefix_code:
                return entry
        return None

    btn_row = ttk.Frame(frame)
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

    def do_remove_prefix():
        entry = get_selected_entry()
        if not entry:
            return
        if remove_prefix_and_games_dialog(entry, parent_dialog=dialog):
            iid = f"{entry['runner']}:{entry['prefix_code']}"
            if prefix_tree.exists(iid):
                prefix_tree.delete(iid)
            # Perbarui daftar lokal supaya get_selected_entry() berikutnya tetap konsisten
            # tanpa perlu menutup & membuka ulang dialog ini.
            prefixes[:] = [e for e in prefixes
                           if not (e["runner"] == entry["runner"] and e["prefix_code"] == entry["prefix_code"])]

    btn_row2 = ttk.Frame(frame)
    btn_row2.pack(pady=(8, 0))
    ttk.Button(btn_row2, text="Backup Apps", style="Custom.TButton", width=12,
               command=do_backup).grid(row=0, column=0, padx=3)
    ttk.Button(btn_row2, text="Restore Apps", style="Custom.TButton", width=12,
               command=do_restore).grid(row=0, column=1, padx=3)
    ttk.Button(btn_row2, text="Remove Apps", style="Custom.TButton", width=12,
               command=do_remove_prefix).grid(row=0, column=2, padx=3)

    # Ukuran awal dibuat cukup lega untuk menampung kolom Game(s) yang baru, tapi jendela
    # tetap bisa di-resize/maximize bebas oleh pengguna (lihat resizable(True, True) diatas).
    init_width, init_height = 1000, 520
    dialog.update_idletasks()
    x = root.winfo_rootx() + (root.winfo_width() - init_width) // 2
    y = root.winfo_rooty() + (root.winfo_height() - init_height) // 2
    dialog.geometry(f"{init_width}x{init_height}+{max(x, 0)}+{max(y, 0)}")

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

        move_btn = ttk.Button(btn_row, text="Move Prefix...", style="Custom.TButton")
        browse_btn = ttk.Button(btn_row, text="Browse Other Folder/Disk...", style="Custom.TButton")
        default_btn = ttk.Button(btn_row, text="Use Default (WLM Folder)", style="Custom.TButton")

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
                old_path = state["existing_path"]
                state["existing_path"] = new_path
                # Perbaiki metadata DAN isi script .sh untuk SEMUA game yang memakai prefix
                # ini (bukan cuma game yang sedang dibuka di dialog ini) - lihat docstring
                # relink_scripts_after_prefix_move untuk kenapa ini penting (game yang
                # file-nya ada didalam prefix ikut pindah secara fisik, jadi scriptnya harus
                # menunjuk ke alamat baru juga, atau game itu tidak akan bisa di-launch).
                updated = relink_scripts_after_prefix_move(runner_key, state["existing_code"], old_path, new_path)
                # Perbarui juga registry global supaya deteksi otomatis (find_owning_prefix)
                # untuk game lain yang mungkin memakai prefix yang sama tetap akurat.
                update_registry_prefix_path(runner_key, state["existing_code"], new_path)
                refresh()
                if updated:
                    status_label.config(
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
        "VulkanHUD": ["bash", "-c", f"DXVK_HUD=full \"{str(script_path)}\""],
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

    # Loop supaya kalau nama yang dipilih ternyata bentrok dengan game yang sudah ada,
    # pengguna bisa langsung diberi pilihan: Replace (timpa), pakai nama lain (Rename), atau
    # batalkan sepenuhnya - bukan langsung menimpa diam-diam atau gagal tanpa penjelasan.
    while True:
        new_name = simpledialog.askstring(
            "Rename Script",
            "Enter script name (for display):",
            initialvalue=suggested_name
        )

        if not new_name:
            return  # dibatalkan oleh pengguna

        # Sanitize filename, only allow alphanumeric, space, underscore, and hyphen
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
                return  # Cancel - batalkan sepenuhnya
            if choice is False:
                suggested_name = safe_new_name
                continue  # No - kembali ke dialog nama, biarkan pengguna ketik nama baru
            # choice is True (Yes) - lanjut, replace game yang sudah ada
        break

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
        else:
            # Tidak/tidak lagi terkait prefix manapun (mis. dulu ada di runner_config tapi
            # sekarang di-Replace dengan exe yang berdiri sendiri) - bersihkan entry lama
            # supaya tidak ada info runner/prefix yang basi tertinggal untuk nama ini.
            runner_cfg = load_runner_config()
            if safe_new_name in runner_cfg:
                del runner_cfg[safe_new_name]
                save_runner_config(runner_cfg)

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
settings_btn = ttk.Button(toolbar, text="SETTINGS", style="Custom.TButton")
all_buttons.append(settings_btn)
settings_btn.pack(side=tk.RIGHT)

install_apps_btn = ttk.Button(toolbar, text="INSTALL APPS", command=run_exe_setup, style="Custom.TButton")
all_buttons.append(install_apps_btn)
install_apps_btn.pack(side=tk.RIGHT, padx=(0, 8))

settings_menu = tk.Menu(root, tearoff=0)
settings_menu.add_command(label="View Logs",
                          command=view_logs)
settings_menu.add_separator()
settings_menu.add_command(label="Prefix Configuration Manager...",
                          command=open_prefix_manager_dialog)
settings_menu.add_separator()
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
settings_menu.add_command(label="Download ProtonGE Online...",
                          command=open_protonge_download_dialog)
settings_menu.add_command(label="Open Proton GE Folder",
                          command=open_protonge_folder)
settings_menu.add_separator()
settings_menu.add_command(label="Extract Proton-CachyOS Archive...",
                          command=extract_protoncachyos_archive)
settings_menu.add_command(label="Download Proton-CachyOS Online...",
                          command=open_protoncachyos_download_dialog)
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
                                    values=["Normal", "GalliumHUD", "VulkanHUD", "MangoHud-GL", "Mangohud"],
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

play_btn = ttk.Button(btn_row1, text="▶ PLAY",
                       command=lambda: (run_script(), reset_button_hover_state(play_btn)),
                       style="Custom.TButton", width=12)
all_buttons.append(play_btn)
play_btn.grid(row=0, column=0, padx=3, pady=3)

add_btn = ttk.Button(btn_row1, text="+ ADD", command=add_script, style="Custom.TButton", width=12)
all_buttons.append(add_btn)
add_btn.grid(row=0, column=1, padx=3, pady=3)

remove_btn = ttk.Button(btn_row1, text="REMOVE", command=remove_script, style="Custom.TButton", width=12)
all_buttons.append(remove_btn)
remove_btn.grid(row=0, column=2, padx=3, pady=3)

# Button grid - Row 2
btn_row2 = ttk.Frame(button_panel)
btn_row2.pack(pady=3)

rename_btn = ttk.Button(btn_row2, text="RENAME", command=rename_script, style="Custom.TButton", width=12)
all_buttons.append(rename_btn)
rename_btn.grid(row=0, column=0, padx=3, pady=3)

icon_btn = ttk.Button(btn_row2, text="ICON", command=change_icon, style="Custom.TButton", width=12)
all_buttons.append(icon_btn)
icon_btn.grid(row=0, column=1, padx=3, pady=3)

# File Manager Button - NEW
filemanager_btn = ttk.Button(btn_row2, text="FOLDER", command=open_file_manager, style="Custom.TButton", width=12)
all_buttons.append(filemanager_btn)
filemanager_btn.grid(row=0, column=2, padx=3, pady=3)

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
