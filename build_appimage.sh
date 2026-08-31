#!/bin/bash
# build_appimage.sh
# Membangun WLM-x86_64.AppImage dari launcher.py (Python di-bundle, tanpa
# ketergantungan Python sistem di mesin PENGGUNA akhir).
#
# PENTING SOAL KOMPATIBILITAS:
# AppImage yang dihasilkan hanya kompatibel dengan glibc versi >= glibc di
# mesin BUILD ini. Jalankan script ini di distro Linux setua mungkin yang
# masih ingin Anda dukung (mis. Ubuntu 20.04/22.04 atau Debian 11/12),
# BUKAN di distro rolling-release/terbaru, supaya AppImage-nya jalan di
# lebih banyak mesin. Kalau Anda build di distro Anda sendiri untuk dipakai
# sendiri saja, ini tidak masalah.
#
# Cara pakai:
#   1. Taruh launcher.py dan file ikon (beri nama my_gear.png) di folder yang sama dengan script ini
#   2. chmod +x build_appimage.sh && ./build_appimage.sh
#   3. Hasil: WLM_0.2.0_Beta-x86_64.AppImage
set -e

APPDIR="WLM_0.2.0_Beta-x86_64.AppDir"
APPNAME="WLM_0.2.0_Beta-x86_64"

echo "== 1. Install dependency build (butuh sudo) =="
sudo apt-get update -qq
sudo apt-get install -y python3-tk python3-pil python3-pil.imagetk \
    python3-venv libfuse2 wget

echo "== 2. Buat virtualenv + install PyInstaller =="
rm -rf venv build dist "$APPNAME.spec" "$APPDIR" WLM_0.2.0_Beta-x86_64.AppImage
python3 -m venv venv
. venv/bin/activate
pip install --quiet --upgrade pip pyinstaller pillow

echo "== 3. Bekukan launcher.py jadi binari mandiri (bundling Python+Tk+Pillow) =="
pyinstaller --onefile --name "$APPNAME" --clean \
    --hidden-import=PIL._tkinter_finder \
    launcher.py

echo "== 4. Susun struktur AppDir =="
mkdir -p "$APPDIR/usr/bin"
cp "dist/$APPNAME" "$APPDIR/usr/bin/$APPNAME"
chmod +x "$APPDIR/usr/bin/$APPNAME"

cat > "$APPDIR/AppRun" <<EOF
#!/bin/bash
HERE="\$(dirname "\$(readlink -f "\${0}")")"
exec "\${HERE}/usr/bin/${APPNAME}" "\$@"
EOF
chmod +x "$APPDIR/AppRun"

cat > "$APPDIR/wlm.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=WLM_0.2.0-Beta
Comment=Wine/Proton Launcher Manager
Exec=${APPNAME}
Icon=wlm
Categories=Game;Utility;
Terminal=false
EOF

# Salin file ikon 'my_gear.png' langsung ke AppDir
if [ -f "my_gear.png" ]; then
    cp "my_gear.png" "$APPDIR/wlm.png"
    cp "my_gear.png" "$APPDIR/.DirIcon"
    echo "Ikon 'my_gear.png' berhasil dipasang ke AppDir."
else
    echo "ERROR: File ikon 'my_gear.png' tidak ditemukan di folder ini!"
    exit 1
fi

echo "== 5. Download appimagetool (sekali saja, lalu cache lokal) =="
if [ ! -f appimagetool ]; then
    wget -q "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage" -O appimagetool
    chmod +x appimagetool
fi

echo "== 6. Bungkus jadi AppImage =="
ARCH=x86_64 ./appimagetool --appimage-extract-and-run "$APPDIR" WLM_0.2.0_Beta-x86_64.AppImage

echo ""
echo "Selesai! -> WLM_0.2.0_Beta-x86_64.AppImage"
echo "Jalankan dengan: chmod +x WLM_0.2.0_Beta-x86_64.AppImage && ./WLM_0.2.0_Beta-x86_64.AppImage"
