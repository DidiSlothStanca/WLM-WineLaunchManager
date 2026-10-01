#!/bin/bash
# build_appimage.sh
# Membangun AppImage WLM dari launcher.py + SEMUA file pendukungnya (Python di-bundle,
# tanpa ketergantungan Python sistem di mesin PENGGUNA akhir).
#
# File yang ikut masuk ke AppImage:
#   - launcher.py                  -> program utama
#   - game_covers.py, gog_store.py,
#     gog_saves.py, install_watch.py -> modul yang di-import launcher (ikut dibekukan)
#   - gog_login_browser.py         -> skrip browser mini login GOG (dijalankan sebagai PROSES
#                                     TERPISAH oleh python3 sistem, jadi disimpan sebagai file
#                                     asli di dalam bundle, bukan dibekukan)
#   - env_config.yaml              -> dipasang sebagai bawaan ke ~/wlm/env_config.yaml saat
#                                     pertama kali dijalankan (kalau pengguna belum punya)
#   - my_gear.png                  -> ikon AppImage (nama file TIDAK diubah)
#
# PENTING SOAL KOMPATIBILITAS:
# AppImage yang dihasilkan hanya kompatibel dengan glibc versi >= glibc di
# mesin BUILD ini. Jalankan script ini di distro Linux setua mungkin yang
# masih ingin Anda dukung (mis. Ubuntu 20.04/22.04 atau Debian 11/12),
# BUKAN di distro rolling-release/terbaru, supaya AppImage-nya jalan di
# lebih banyak mesin. Kalau Anda build di distro Anda sendiri untuk dipakai
# sendiri saja, ini tidak masalah.
#
# PENTING SOAL LOGIN GOG:
# Browser mini login GOG butuh pywebview + GTK/WebKit2GTK (atau Qt WebEngine) milik SISTEM,
# yang tidak bisa dibundel. Di mesin pengguna harus ada python3 + pywebview (mis. paket
# python3-gi, gir1.2-webkit2-4.1, lalu 'pip install pywebview').
#
# Cara pakai:
#   1. Taruh semua file di atas (dan my_gear.png) di folder yang sama dengan script ini
#   2. chmod +x build_appimage.sh && ./build_appimage.sh
#   3. Hasil: WLM_<versi>-x86_64.AppImage  (versi dibaca otomatis dari WLM_VERSION di launcher.py)
set -e

# Selalu bekerja dari folder script ini, apa pun folder tempat script dipanggil.
cd "$(dirname "$(readlink -f "$0")")"

# --- File yang wajib ada: cek di AWAL supaya tidak gagal setelah build panjang -------------------
MAIN_SCRIPT="launcher.py"
MODULES=(game_covers gog_store gog_saves install_watch)   # di-import oleh launcher.py
HELPER_SCRIPT="gog_login_browser.py"                      # dijalankan sebagai proses terpisah
ENV_CONFIG="env_config.yaml"
ICON="my_gear.png"

REQUIRED=("$MAIN_SCRIPT" "$HELPER_SCRIPT" "$ENV_CONFIG" "$ICON")
for m in "${MODULES[@]}"; do REQUIRED+=("$m.py"); done

MISSING=0
for f in "${REQUIRED[@]}"; do
    if [ ! -f "$f" ]; then
        echo "ERROR: file '$f' tidak ditemukan di folder ini!"
        MISSING=1
    fi
done
[ "$MISSING" -eq 0 ] || exit 1

# --- Versi dibaca dari launcher.py (WLM_VERSION = "x.y.z-Beta") -> nama file mengikuti versi ------
VERSION="$(sed -n 's/^WLM_VERSION *= *"\([^"]*\)".*/\1/p' "$MAIN_SCRIPT" | head -n1)"
if [ -z "$VERSION" ]; then
    echo "ERROR: WLM_VERSION tidak ditemukan di $MAIN_SCRIPT"
    exit 1
fi
VTAG="${VERSION//-/_}"                 # 0.4.7-Beta -> 0.4.7_Beta (sama polanya dengan nama lama)
APPNAME="WLM_${VTAG}-x86_64"
APPDIR="${APPNAME}.AppDir"
OUTPUT="${APPNAME}.AppImage"
echo "Membangun WLM $VERSION -> $OUTPUT"

# Pakai sudo hanya kalau bukan root.
SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

echo "== 1. Install dependency build (butuh sudo) =="
$SUDO apt-get update -qq
$SUDO apt-get install -y python3-tk python3-pil python3-pil.imagetk \
    python3-venv libfuse2 wget

echo "== 2. Buat virtualenv + install PyInstaller =="
rm -rf venv build dist "$APPNAME.spec" "$APPDIR" "$OUTPUT"
python3 -m venv venv
. venv/bin/activate
# pyyaml wajib: launcher.py memakainya untuk membaca env_config.yaml
pip install --quiet --upgrade pip pyinstaller pillow pyyaml

echo "== 3. Bekukan launcher.py + semua modulnya jadi binari mandiri (Python+Tk+Pillow+PyYAML) =="
# Modul di-import di dalam fungsi (lazy) dan lewat sys.path, jadi dicantumkan eksplisit
# lewat --hidden-import agar PASTI ikut. gog_login_browser.py disertakan sebagai file DATA
# (bukan modul) karena dijalankan oleh python3 sistem, bukan oleh binari ini.
HIDDEN=()
for m in "${MODULES[@]}"; do HIDDEN+=(--hidden-import="$m"); done

pyinstaller --onefile --name "$APPNAME" --clean \
    --paths . \
    --hidden-import=PIL._tkinter_finder \
    --hidden-import=yaml \
    "${HIDDEN[@]}" \
    --add-data "$HELPER_SCRIPT:." \
    "$MAIN_SCRIPT"

echo "== 3b. Verifikasi isi binari =="
LISTING="$(pyi-archive_viewer --list --recursive "dist/$APPNAME" 2>/dev/null || true)"
for m in launcher "${MODULES[@]}"; do
    if ! grep -q "$m" <<<"$LISTING"; then
        echo "ERROR: modul '$m' TIDAK ikut terbundel di binari!"
        exit 1
    fi
done
if ! grep -q "gog_login_browser.py" <<<"$LISTING"; then
    echo "ERROR: gog_login_browser.py TIDAK ikut terbundel di binari!"
    exit 1
fi
echo "OK: ${MODULES[*]} + launcher + $HELPER_SCRIPT ada di dalam binari."

echo "== 4. Susun struktur AppDir =="
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/wlm"
cp "dist/$APPNAME" "$APPDIR/usr/bin/$APPNAME"
chmod +x "$APPDIR/usr/bin/$APPNAME"

# env_config.yaml bawaan disimpan di dalam AppImage; AppRun menyalinnya ke ~/wlm/ HANYA kalau
# pengguna belum punya (file milik pengguna tidak pernah ditimpa).
cp "$ENV_CONFIG" "$APPDIR/usr/share/wlm/$ENV_CONFIG"

cat > "$APPDIR/AppRun" <<EOF
#!/bin/bash
HERE="\$(dirname "\$(readlink -f "\${0}")")"
WLM_HOME="\${HOME}/wlm"
if [ ! -f "\${WLM_HOME}/${ENV_CONFIG}" ] && [ -f "\${HERE}/usr/share/wlm/${ENV_CONFIG}" ]; then
    mkdir -p "\${WLM_HOME}" && cp "\${HERE}/usr/share/wlm/${ENV_CONFIG}" "\${WLM_HOME}/${ENV_CONFIG}"
fi
exec "\${HERE}/usr/bin/${APPNAME}" "\$@"
EOF
chmod +x "$APPDIR/AppRun"

cat > "$APPDIR/wlm.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=WLM ${VERSION}
Comment=Wine/Proton Launcher Manager
Exec=${APPNAME}
Icon=wlm
Categories=Game;Utility;
Terminal=false
EOF

# Salin file ikon 'my_gear.png' langsung ke AppDir (nama file ikon tidak diubah)
cp "$ICON" "$APPDIR/wlm.png"
cp "$ICON" "$APPDIR/.DirIcon"
echo "Ikon '$ICON' berhasil dipasang ke AppDir."

echo "== 5. Download appimagetool (sekali saja, lalu cache lokal) =="
if [ ! -f appimagetool ]; then
    wget -q "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage" -O appimagetool
    chmod +x appimagetool
fi

echo "== 6. Bungkus jadi AppImage =="
ARCH=x86_64 ./appimagetool --appimage-extract-and-run "$APPDIR" "$OUTPUT"

echo ""
echo "Selesai! -> $OUTPUT"
echo "Jalankan dengan: chmod +x $OUTPUT && ./$OUTPUT"
