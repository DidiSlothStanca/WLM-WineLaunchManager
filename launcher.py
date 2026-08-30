import os
import re
import shlex
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, simpledialog, messagebox
from pathlib import Path
from PIL import Image, ImageTk, ImageDraw
import json
from datetime import datetime

# Configuration paths
directory = Path.home() / "wlm"
icon_dir = directory / "icons"
bashlaunch_dir = directory / "bashlaunch"
theme_config_file = directory / "theme_config.json"
window_config_file = directory / "window_config.json"

# ProtonGE feature paths
protonge_dir = directory / "protonge"                 # tempat ekstrak binary Proton GE (bukan di direktori Steam)
protonge_prefix_root = directory / "protonprefixes"   # prefix ProtonGE dibuat di sini (didalam direktori utama)
runner_config_file = directory / "runner_config.json"  # menyimpan pilihan runner (wine/protonge) per game

# Create directories if they don't exist
bashlaunch_dir.mkdir(exist_ok=True)
icon_dir.mkdir(exist_ok=True)
protonge_dir.mkdir(exist_ok=True)
protonge_prefix_root.mkdir(exist_ok=True)

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
    config = {"theme": "default", "window_size": "1000x650", "window_position": None}
    
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
                
                loaded_size = window_config.get('size', '1000x650')
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
def find_protonge_installations():
    """Scan Proton GE yang sudah diekstrak didalam direktori utama WLM (~/wlm/protonge).
    Tidak lagi membaca dari direktori Steam."""
    found = []
    if protonge_dir.is_dir():
        for entry in sorted(protonge_dir.iterdir(), reverse=True):
            if entry.is_dir():
                proton_bin = entry / "proton"
                if proton_bin.exists():
                    found.append((entry.name, proton_bin))
    return found

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

def extract_protonge_archive():
    """Pilih arsip Proton GE (.tar.gz/.tar.xz/.tgz/.zip) lalu ekstrak ke ~/wlm/protonge/."""
    archive_path = filedialog.askopenfilename(
        title="Pilih Arsip Proton GE",
        filetypes=[("Proton GE Archive", "*.tar.gz *.tar.xz *.tgz *.zip"), ("All Files", "*.*")]
    )
    if not archive_path:
        return

    archive_path_obj = Path(archive_path)
    status_label.config(text=f"Mengekstrak {archive_path_obj.name} ke direktori WLM...", fg=COLORS["text_secondary"])
    root.update_idletasks()

    try:
        protonge_dir.mkdir(exist_ok=True)
        before_entries = {p.name for p in protonge_dir.iterdir() if p.is_dir()}

        if archive_path_obj.suffix.lower() == ".zip":
            import zipfile
            with zipfile.ZipFile(archive_path, 'r') as zf:
                top_level_names = {Path(n).parts[0] for n in zf.namelist() if n.strip()}
                zf.extractall(protonge_dir)
        else:
            import tarfile
            with tarfile.open(archive_path, 'r:*') as tf:
                top_level_names = {Path(n).parts[0] for n in tf.getnames() if n.strip()}
                try:
                    tf.extractall(protonge_dir, filter='data')
                except TypeError:
                    # Python versi lama belum mendukung parameter 'filter'
                    tf.extractall(protonge_dir)

        new_top_names = top_level_names - before_entries

        # Jika arsip tidak memiliki satu folder induk (file berserakan di root arsip),
        # bungkus hasil ekstrak ke dalam satu folder bernama sesuai arsipnya.
        if len(new_top_names) != 1:
            wrapper_name = archive_path_obj.name.replace(".tar.gz", "").replace(".tar.xz", "") \
                                                 .replace(".tgz", "").replace(".zip", "")
            wrapper_dir = protonge_dir / wrapper_name
            wrapper_dir.mkdir(exist_ok=True)
            for name in new_top_names:
                src = protonge_dir / name
                if src.exists() and src != wrapper_dir:
                    src.rename(wrapper_dir / name)

        status_label.config(text=f"Proton GE berhasil diekstrak ke {protonge_dir}", fg=COLORS["success"])
        messagebox.showinfo("Selesai", f"Proton GE berhasil diekstrak ke direktori WLM:\n{protonge_dir}")
    except Exception as e:
        status_label.config(text=f"Error mengekstrak Proton GE: {str(e)}", fg=COLORS["danger"])
        messagebox.showerror("Error", f"Gagal mengekstrak arsip:\n{str(e)}")

def open_protonge_folder():
    """Buka folder tempat Proton GE diekstrak (~/wlm/protonge)."""
    try:
        protonge_dir.mkdir(exist_ok=True)
        subprocess.Popen(["xdg-open", str(protonge_dir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        status_label.config(text="Opening Proton GE Folder...", fg=COLORS["text_secondary"])
    except Exception as e:
        status_label.config(text=f"Error opening Proton GE folder: {str(e)}", fg=COLORS["danger"])

def generate_next_prefix_code():
    """Generate kode prefix baru secara berurutan (GAME001, GAME002, ...) sesuai urutan installer dijalankan."""
    numbers = []
    for entry in protonge_prefix_root.iterdir():
        if entry.is_dir() and entry.name.startswith("GAME") and entry.name[4:].isdigit():
            numbers.append(int(entry.name[4:]))
    next_num = max(numbers, default=0) + 1
    return f"GAME{next_num:03d}"

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

    if choice["runner"] == "protonge":
        lines.append(f'export STEAM_COMPAT_DATA_PATH="{choice["prefix_path"]}"')
        lines.append(f'export STEAM_COMPAT_CLIENT_INSTALL_PATH="{find_steam_install_path()}"')
        lines.append(f'"{choice["proton_path"]}" run "{exe_path}"{extra_args_str}')
    else:
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

    existing_cfg = None
    if parent_script_name:
        existing_cfg = load_runner_config().get(parent_script_name)

    dialog = tk.Toplevel(root)
    dialog.title("Pilih Runner - Setup" if purpose == "setup" else "Pilih Runner - Play")
    dialog.configure(bg=COLORS["primary"])
    dialog.resizable(False, False)
    dialog.transient(root)
    dialog.grab_set()

    result = {"value": None}

    frame = ttk.Frame(dialog, padding=15)
    frame.pack(fill=tk.BOTH, expand=True)

    ttk.Label(frame, text="Pilih compatibility layer untuk menjalankan game:",
              font=FONTS["normal"]).pack(anchor="w", pady=(0, 10))

    default_runner = "protonge" if (existing_cfg and existing_cfg.get("runner") == "protonge") else "wine"
    runner_var = tk.StringVar(value=default_runner)

    ttk.Radiobutton(frame, text="Wine (Vanilla)", variable=runner_var, value="wine").pack(anchor="w", pady=2)
    protonge_radio = ttk.Radiobutton(frame, text="Proton GE", variable=runner_var, value="protonge")
    protonge_radio.pack(anchor="w", pady=2)

    version_label = ttk.Label(frame, text="Versi Proton GE:", font=FONTS["small"])
    version_combo = ttk.Combobox(frame, state="readonly", width=32, font=FONTS["small"])

    if protonge_list:
        version_combo["values"] = [name for name, _ in protonge_list]
        default_idx = 0
        if existing_cfg and existing_cfg.get("proton_name") in version_combo["values"]:
            default_idx = list(version_combo["values"]).index(existing_cfg["proton_name"])
        version_combo.current(default_idx)
    else:
        version_combo["values"] = ["(Belum ada - Extract di menu Settings)"]
        version_combo.current(0)
        protonge_radio.config(state="disabled")

    prefix_info_var = tk.StringVar()
    if existing_cfg and existing_cfg.get("runner") == "protonge" and existing_cfg.get("prefix_code"):
        prefix_info_var.set(f"Prefix: {existing_cfg.get('prefix_code')} (dipakai sebelumnya, konsisten)")
    else:
        prefix_info_var.set("Prefix baru akan dibuat otomatis di direktori utama")
    prefix_label = ttk.Label(frame, textvariable=prefix_info_var, font=FONTS["small"])

    def toggle_protonge_widgets(*_):
        if runner_var.get() == "protonge":
            version_label.pack(anchor="w", pady=(10, 2))
            version_combo.pack(anchor="w", pady=(0, 2))
            prefix_label.pack(anchor="w", pady=(2, 10))
        else:
            version_label.pack_forget()
            version_combo.pack_forget()
            prefix_label.pack_forget()

    runner_var.trace_add("write", toggle_protonge_widgets)
    toggle_protonge_widgets()

    ttk.Separator(frame, orient="horizontal").pack(fill=tk.X, pady=(5, 10))

    ttk.Label(frame, text="Launch Options / Environment Variable (opsional):",
              font=FONTS["small"]).pack(anchor="w", pady=(0, 2))
    launch_options_entry = ttk.Entry(frame, width=48, font=FONTS["small"])
    launch_options_entry.pack(anchor="w", fill=tk.X)
    if existing_cfg and existing_cfg.get("launch_options"):
        launch_options_entry.insert(0, existing_cfg["launch_options"])
    ttk.Label(frame, text="Contoh: PROTON_USE_WINED3D=1 MANGOHUD=1",
              font=FONTS["small"]).pack(anchor="w", pady=(2, 10))

    ttk.Label(frame, text="Catatan / Komentar (opsional):",
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
            result["value"] = {
                "runner": "wine",
                "launch_options": launch_options_value,
                "comment": comment_value
            }
            dialog.destroy()
            return

        if not protonge_list:
            messagebox.showerror("Error", "Proton GE tidak ditemukan. Extract dulu lewat menu Settings.")
            return

        idx = version_combo.current()
        proton_name, proton_path = protonge_list[idx]

        if existing_cfg and existing_cfg.get("runner") == "protonge" and existing_cfg.get("prefix_code"):
            prefix_code = existing_cfg["prefix_code"]
        else:
            prefix_code = generate_next_prefix_code()

        prefix_path = protonge_prefix_root / prefix_code
        prefix_path.mkdir(parents=True, exist_ok=True)

        result["value"] = {
            "runner": "protonge",
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

    ttk.Button(btn_frame, text="OK", command=on_ok, style="Custom.TButton").pack(side=tk.RIGHT, padx=(5, 0))
    ttk.Button(btn_frame, text="Batal", command=on_cancel, style="Custom.TButton").pack(side=tk.RIGHT)

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
        status_label.config(text="Launch dibatalkan.", fg=COLORS["text_secondary"])
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
        status_label.config(text="Error: Tidak bisa membaca path exe/folder dari script.", fg=COLORS["danger"])
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
    
    try:
        # Use Popen to run in the background and prevent GUI blocking
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        runner_label = f"Proton GE ({choice['prefix_code']})" if choice["runner"] == "protonge" else "Wine"
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
            
            # Note the use of Path.resolve() to ensure absolute paths
            # and correct handling of spaces when writing to the bash script
            script_content = (
                "#!/bin/bash\n"
                f"cd \"{folder_path.resolve()}\"\n"
                f"wine \"{exe_path_obj.resolve()}\"\n"
            )
            
            try:
                with open(script_path, "w") as script_file:
                    script_file.write(script_content)
                
                # Set permission
                script_path.chmod(0o755)
                
                update_script_list()
                status_label.config(text=f"Added: {safe_new_name}", fg=COLORS["success"])
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
            subprocess.Popen(["xdg-open", folder_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
            subprocess.Popen(["xdg-open", str(wine_prefix)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            status_label.config(text="Opening Wine Prefix Folder...", fg=COLORS["text_secondary"])
        else:
            messagebox.showerror("Error", f"Wine Prefix folder not found: {wine_prefix}")
            status_label.config(text="Error: Wine Prefix folder not found.", fg=COLORS["danger"])
    except Exception as e:
        status_label.config(text=f"Error opening Wine Prefix: {str(e)}", fg=COLORS["danger"])

def open_winecfg():
    """Open winecfg"""
    try:
        subprocess.Popen(["winecfg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        status_label.config(text="Setup dibatalkan.", fg=COLORS["text_secondary"])
        return

    try:
        env_vars, extra_args = parse_launch_options(choice.get("launch_options", ""))
        env = os.environ.copy()
        for key, val in env_vars:
            env[key] = val

        if choice["runner"] == "protonge":
            env["STEAM_COMPAT_DATA_PATH"] = choice["prefix_path"]
            env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(find_steam_install_path())
            command = [choice["proton_path"], "run", exe_path] + extra_args
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            status_label.config(
                text=f"Running setup for {Path(exe_path).name} via Proton GE (prefix: {choice['prefix_code']})...",
                fg=COLORS["text_secondary"])
        else:
            command = ["wine", exe_path] + extra_args
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
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


root.minsize(1000, 650)
root.maxsize(1920, 1080)

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
settings_menu.add_command(label="Refresh List", 
                          command=lambda: update_script_list())

settings_btn.config(command=lambda: settings_menu.post(settings_btn.winfo_rootx(), 
                                                      settings_btn.winfo_rooty() + settings_btn.winfo_height() + 5))

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
tree.bind("<<TreeviewSelect>>", on_select)

# RIGHT PANEL (Game Details)
right_panel = ttk.Frame(main_container, padding=15)
main_container.add(right_panel, weight=1)

icon_frame = ttk.Frame(right_panel, width=ICON_WIDTH, height=ICON_HEIGHT)
icon_frame.pack(pady=(0, 15))
icon_frame.pack_propagate(False) 

icon_label = tk.Label(icon_frame, relief="flat")
icon_label.pack(expand=True, fill=tk.BOTH)

game_title_label = tk.Label(right_panel,
                                text="No Game Selected",
                                font=FONTS["subtitle"],
                                justify=tk.CENTER,
                                wraplength=ICON_WIDTH) 
game_title_label.pack(pady=(0, 15))

info_text = tk.StringVar(value="Select a game to view details")
info_label = tk.Label(right_panel,
                        textvariable=info_text,
                        font=FONTS["small"],
                        justify=tk.LEFT,
                        wraplength=ICON_WIDTH + 50) 
info_label.pack(pady=(0, 25))

# BUTTON PANEL 
button_panel = ttk.Frame(right_panel, padding=(0, 10))
button_panel.pack(fill=tk.X, side=tk.BOTTOM)

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

# Setup button - Row 3
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

root.mainloop()
