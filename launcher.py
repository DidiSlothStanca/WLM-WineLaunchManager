import os
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, simpledialog, messagebox
from pathlib import Path
from PIL import Image, ImageTk
import tkinter.font as tkFont

directory = Path.home() / "wlm"
icon_dir = directory / "icons"
bashlaunch_dir = directory / "bashlaunch"

#=======================================================================
# Buat Folder Penyimpanan Default Launcher
#=======================================================================
bashlaunch_dir.mkdir(exist_ok=True)
icon_dir.mkdir(exist_ok=True)

#=======================================================================
#AUTO_UPDATE
#=======================================================================
def update_script_list(sort_order="ascending"):
    for row in tree.get_children():
        tree.delete(row)

    # SHORT LIST (KONFIG AMBIL URUTAN)
    script_files = sorted(bashlaunch_dir.glob("*.sh"), key=lambda x: x.stem.lower())
    if sort_order == "descending":
        script_files.reverse()

    for index, file in enumerate(script_files, start=1):
        tree.insert("", "end", values=(index, file.stem))

#=======================================================================
#RUN_SCRIPT PLAY BUTTON
#=======================================================================
def run_script():
    selected = tree.focus()
    if not selected:
        return
    script_name = tree.item(selected, "values")[1] + ".sh"
    script_path = bashlaunch_dir / script_name
    
    # Mengambil pilihan dari dropdown launch mode
    launch_mode = launch_mode_combo.get()

    # Menyusun perintah yang sesuai dengan pilihan launch mode
    command = []
    if launch_mode == "Normal":
        command = ["bash", str(script_path)]
    elif launch_mode == "GalliumHUD":
        command = ["bash", "-c", f"GALLIUM_HUD=GPU-load+cpu+fps \"{str(script_path)}\""]
    elif launch_mode == "MangoHud-GL":
        command = ["bash", "-c", f"mangohud --dlsym \"{str(script_path)}\""]
    elif launch_mode == "Mangohud":
        command = ["bash", "-c", f"mangohud \"{str(script_path)}\""]

    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

#=======================================================================
#ADD_SCRIPT BUTTON
#=======================================================================
def add_script():
    exe_path = filedialog.askopenfilename(filetypes=[("Executable Files", "*.exe")])
    if exe_path:
        exe_name = Path(exe_path).stem
        new_name = simpledialog.askstring("Rename Script", "Enter script name:", initialvalue=exe_name)
        if new_name:
            script_path = bashlaunch_dir / f"{new_name}.sh"
            folder_path = Path(exe_path).parent
            with open(script_path, "w") as script_file:
                script_file.write(f"#!/bin/bash\ncd \"{folder_path}\"\nwine \"{exe_path}\"\n")
            script_path.chmod(0o755)
            update_script_list()

#=======================================================================
#REMOVE_SCRIPT BUTTON
#=======================================================================
def remove_script():
    selected = tree.focus()
    if not selected:
        return
    script_name = tree.item(selected, "values")[1] + ".sh"
    script_path = bashlaunch_dir / script_name
    icon_path = icon_dir / f"{tree.item(selected, 'values')[1]}.png"
    script_path.unlink(missing_ok=True)
    icon_path.unlink(missing_ok=True)
    update_script_list()

#=======================================================================
#BUTTON RENAME_SCRIPT
#=======================================================================
def rename_script():
    selected = tree.focus()
    if not selected:
        return
    old_name = tree.item(selected, "values")[1]
    new_name = simpledialog.askstring("Rename Script", "Enter new script name:", initialvalue=old_name)
    if new_name and new_name != old_name:
        old_path = bashlaunch_dir / f"{old_name}.sh"
        new_path = bashlaunch_dir / f"{new_name}.sh"
        old_icon = icon_dir / f"{old_name}.png"
        new_icon = icon_dir / f"{new_name}.png"
        old_path.rename(new_path)
        if old_icon.exists():
            old_icon.rename(new_icon)
        update_script_list()

#=======================================================================
#UNTUK ATUR TITLE, ICON & GAMBAR
#=======================================================================
def change_icon():
    selected = tree.focus()
    if not selected:
        return
    script_name = tree.item(selected, "values")[1]
    icon_path = filedialog.askopenfilename(filetypes=[("All Files", "*.*")])
    if icon_path:
        try:
            # Untuk cek gambar
            image = Image.open(icon_path)
            # Resize gambar
            image.thumbnail((64, 64), Image.LANCZOS)
            # Simpan gambar
            image.save(icon_dir / f"{script_name}.png")
            load_icon(script_name)
        except Exception as e:
            print(f"Error opening image: {e}")
            messagebox.showerror("Error", "Failed to open the image. Please try another one.")

def load_icon(script_name):
    icon_path = icon_dir / f"{script_name}.png"
    if icon_path.exists():
        image = Image.open(icon_path)
        image = ImageTk.PhotoImage(image)
        icon_label.config(image=image)
        icon_label.image = image
    else:
        icon_label.config(image=None)
        icon_label.image = None

def on_select(event):
    selected = tree.focus()
    if selected:
        script_name = tree.item(selected, "values")[1]
        # Update game title
        game_title_label.config(text=script_name)
        load_icon(script_name)
        
        # Show icon label
        icon_path = icon_dir / f"{script_name}.png"
        if icon_path.exists():
            icon_label.grid(row=1, column=3, padx=5, pady=5, rowspan=2)
        else:
            icon_label.grid_forget()
    else:
        icon_label.grid_forget()

#=======================================================================
#BUTTON WINECFG_SCRIPT
#=======================================================================
def open_winecfg():
    subprocess.Popen(["winecfg"])

#=======================================================================
#BUTTON SETUP (TO RUN EXE)
#=======================================================================
def run_exe_setup():
    exe_path = filedialog.askopenfilename(filetypes=[("Executable Files", "*.exe")])
    if exe_path:
        subprocess.Popen(["wine", exe_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

#=======================================================================
# SETTING UNTUK WINDOW SCALE
#=======================================================================
root = tk.Tk()
root.title("Wine Launch Manager")
root.geometry("460x430")  # Default
root.minsize(460, 430)  # Min
root.maxsize(1280, 800)  # Max
root.resizable(True, True)

default_font = tkFont.nametofont("TkDefaultFont")
font_family = default_font.actual("family") if default_font else "Sans-serif"  # DEF_FONT

#=======================================================================
# TOOLBAR SETTINGS
#=======================================================================
toolbar = ttk.Frame(root, padding=5)
toolbar.pack(fill=tk.X)

# Menu dropdown untuk Uninstall Program dan Explorer
settings_menu = ttk.Menubutton(toolbar, text="Settings", direction="below")
settings_menu.grid(row=0, column=0, padx=5)

settings_menu_dropdown = tk.Menu(settings_menu, tearoff=0)
settings_menu["menu"] = settings_menu_dropdown

# Menambahkan item pada menu
settings_menu_dropdown.add_command(label="Uninstall Program", command=lambda: subprocess.Popen(["wine", "uninstaller"]))
settings_menu_dropdown.add_command(label="Explorer", command=lambda: subprocess.Popen(["wine", "explorer"]))

#=======================================================================
#BUTTON LIST & KOLOM
#=======================================================================
frame = ttk.Frame(root, padding=10)
frame.pack(fill=tk.BOTH, expand=True)

# TAMPILKAN DAFTAR FILE
columns = ("No", "File Name")
tree = ttk.Treeview(frame, columns=columns, show="headings")
tree.heading("No", text="No")
tree.heading("File Name", text="File Name")
tree.column("No", width=50, anchor="center")
tree.column("File Name", width=300, anchor="w")
tree.pack(fill=tk.BOTH, expand=True)

# SHORT BY
sort_frame = ttk.Frame(root)
sort_frame.pack(fill=tk.X)

sort_label = ttk.Label(sort_frame, text="Sort By")
sort_label.pack(side=tk.LEFT, padx=5)

# Dropdown SHORT BY
sort_combo = ttk.Combobox(sort_frame, values=["A-Z", "Z-A"], state="readonly")
sort_combo.current(0)  # Default value
sort_combo.pack(side=tk.LEFT, padx=5)

# Dropdown Launch Mode
launch_mode_label = ttk.Label(sort_frame, text="Launch Mode")
launch_mode_label.pack(side=tk.LEFT, padx=5)

launch_mode_combo = ttk.Combobox(sort_frame, values=["Normal", "GalliumHUD", "MangoHud-GL", "Mangohud"], state="readonly")
launch_mode_combo.current(0)  # Default value
launch_mode_combo.pack(side=tk.LEFT, padx=5)

# UPDATE SORT BY
def sort_by_selected(event):
    sort_value = sort_combo.get()
    if sort_value == "A-Z":
        update_script_list("ascending")
    elif sort_value == "Z-A":
        update_script_list("descending")

sort_combo.bind("<<ComboboxSelected>>", sort_by_selected)

# Treeview
tree.bind("<<TreeviewSelect>>", on_select)

button_frame = ttk.Frame(frame)
button_frame.pack(fill=tk.X)

# Row atas (saat muncul)
play_button = ttk.Button(button_frame, text="Play", command=run_script)
play_button.grid(row=0, column=0, padx=5, pady=5, sticky="ew")

rename_button = ttk.Button(button_frame, text="Rename", command=rename_script)
rename_button.grid(row=0, column=1, padx=5, pady=5, sticky="ew")

remove_button = ttk.Button(button_frame, text="Remove", command=remove_script)
remove_button.grid(row=0, column=2, padx=5, pady=5, sticky="ew")

# Row bawah (def)
add_button = ttk.Button(button_frame, text="Add", command=add_script)
add_button.grid(row=1, column=0, padx=5, pady=5, sticky="ew")

change_icon_button = ttk.Button(button_frame, text="Change Icon", command=change_icon)
change_icon_button.grid(row=1, column=1, padx=5, pady=5, sticky="ew")

winecfg_button = ttk.Button(button_frame, text="Winecfg", command=open_winecfg)
winecfg_button.grid(row=1, column=2, padx=5, pady=5, sticky="ew")

# SETUP MODE (PERSINGKAT COMMAND WINE)
setup_button = ttk.Button(button_frame, text="Setup", command=run_exe_setup)
setup_button.grid(row=2, column=0, columnspan=3, padx=5, pady=5, sticky="ew")

# JUDUL + ICON GAME
game_title_label = tk.Label(button_frame, text="Game Title", font=(font_family, 10, "bold"))
game_title_label.grid(row=0, column=3, padx=5, pady=5)

# ICON TETAP
icon_label = tk.Label(button_frame, width=64, height=64, relief="flat", bg=root.cget('bg'))
icon_label.grid_forget()

# MULAI PERTAMA KALI
update_script_list()

root.mainloop()

