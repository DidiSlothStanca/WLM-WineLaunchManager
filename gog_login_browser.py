"""gog_login_browser.py - browser mini untuk login GOG di Wine Launch Manager (WLM).

Dijalankan gog_store.py sebagai PROSES TERPISAH (bukan di dalam launcher), karena event loop
GTK/Qt milik pywebview tidak bisa dicampur dengan mainloop Tkinter dalam satu proses.

Cara kerja: buka halaman login GOG, pantau URL-nya, dan begitu GOG mengalihkan ke halaman
sukses (path URL memuat --watch dan ada parameter 'code=') kode itu diambil, dicetak ke stdout
sebagai satu baris JSON, lalu jendela ditutup otomatis. Password hanya diketik di halaman
GOG itu sendiri - skrip ini tidak pernah membacanya.

Login Google / Facebook / Steam / Xbox di dalam jendela ini:
  1. BACKEND TERDETEKSI OTOMATIS (GTK/WebKit2GTK atau Qt/Chromium), termasuk pywebview lama yang
     tidak punya window.native (dicari lewat BrowserView.instances).
  2. USER-AGENT SESUAI ENGINE: Chromium (Qt) -> UA Chrome tanpa token "QtWebEngine";
     WebKit (GTK) -> UA Safari. Jika halaman diblokir Google ATAU tampil PUTIH/KOSONG
     (dideteksi lewat JS: teks halaman kosong setelah ~8 dtk), jendela ditutup dan diulang
     otomatis dengan UA berikutnya (Chrome -> Firefox -> Safari), lalu menyerah dengan pesan jelas.
  3. POPUP: GTK -> popup asli (sinyal 'create'); backend lain -> window.open/target=_blank
     dialihkan ke jendela yang sama.
  4. ANTI LAYAR PUTIH (GTK): cookie pihak ketiga diizinkan, ITP mati, proses web yang crash
     di-reload, semua navigasi dicegat lewat 'decide-policy' sehingga 'code=' tertangkap
     walau halaman tujuannya putih, dan jendela induk yang macet di gog.com dimuat ulang.
  5. PROFIL RENDERING: software (tanpa GPU/Vulkan, bawaan) -> nodmabuf -> gpu. Kalau tampilan
     polos satu warna (dicek lewat snapshot WebKit, bukan hanya teks DOM) jendela diulang dengan
     profil berikutnya. Paksa satu profil: WLM_GOG_RENDER=software|nodmabuf|gpu (atau --render);
     WLM_GOG_KEEP_GPU=1 sama dengan 'gpu'.
  6. LOG: semua URL + hasil 'probe' halaman (code/token disamarkan) ditulis ke
     <tmp>/wlm_gog_login.log (Linux: /tmp/wlm_gog_login.log). WLM_GOG_DEBUG=1 -> juga ke stderr.

UKURAN JENDELA: bisa diubah (drag tepi/maximize). Ukuran awal otomatis menyesuaikan layar
(--width/--height atau env WLM_GOG_WIDTH/WLM_GOG_HEIGHT untuk mengatur manual). Di backend GTK:
Ctrl + / Ctrl - / Ctrl 0 = zoom halaman, F11 = layar penuh (jendela utama).

Keluaran (satu baris JSON di stdout):
    {"code": "..."}          login berhasil
    {"cancelled": true}      jendela ditutup sebelum login selesai
    {"error": "..."}         gagal (mis. pywebview belum terpasang, Google memblokir, halaman putih)

Butuh: pip install pywebview  (+ GTK3/WebKit2GTK atau Qt WebEngine dari distro).
Pemakaian manual: python3 gog_login_browser.py <url> [--watch on_login_success] [--ua auto|chrome|firefox|safari]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

# Profil rendering (anti layar putih). Harus di-set SEBELUM webview/WebKit/Qt di-import.
#   software = tanpa GPU sama sekali; nodmabuf = hanya matikan DMABUF; gpu = bawaan sistem.
_ENV_SET = []      # variabel yang kita isi sendiri (dibuang lagi untuk percobaan ulang)

def _setenv_default(key, value):
    if key not in os.environ:
        os.environ[key] = value
        _ENV_SET.append(key)

RENDER_PROFILES = {
    "software": {
        "WEBKIT_DISABLE_COMPOSITING_MODE": "1",
        "WEBKIT_DISABLE_DMABUF_RENDERER": "1",
        "LIBGL_ALWAYS_SOFTWARE": "1",
        "GALLIUM_DRIVER": "llvmpipe",
        "QT_QUICK_BACKEND": "software",
        "QTWEBENGINE_CHROMIUM_FLAGS": "--disable-gpu --disable-gpu-compositing --disable-features=Vulkan",
    },
    "nodmabuf": {"WEBKIT_DISABLE_DMABUF_RENDERER": "1"},
    "gpu": {},
}
RENDER_ORDER = ("software", "nodmabuf", "gpu")


def _pre_render():
    argv = sys.argv
    for i, a in enumerate(argv):
        if a == "--render" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--render="):
            return a.split("=", 1)[1]
    return None


RENDER = (_pre_render() or os.environ.get("WLM_GOG_RENDER")
          or ("gpu" if os.environ.get("WLM_GOG_KEEP_GPU") else "software"))
if RENDER not in RENDER_PROFILES:
    RENDER = "software"
NO_GPU = RENDER == "software"
for _k, _v in RENDER_PROFILES[RENDER].items():
    _setenv_default(_k, _v)

UA_PROFILES = {
    "safari": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 "
               "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"),
    "chrome": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "firefox": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
}

POPUP_FIX = """
(function () {
  if (window.__wlmFix) return;
  window.__wlmFix = true;
  window.open = function (u) { if (u) { location.href = u; } return null; };
  document.addEventListener('click', function (e) {
    var a = e.target && e.target.closest ? e.target.closest('a[target=_blank]') : null;
    if (a && a.href) { e.preventDefault(); location.href = a.href; }
  }, true);
  document.addEventListener('submit', function (e) {
    if (e.target && e.target.target === '_blank') { e.target.target = '_self'; }
  }, true);
})();
"""

# Isi halaman saat ini: dipakai untuk mendeteksi halaman putih/kosong.
PROBE_JS = """
(function () {
  var b = document.body, t = b ? (b.innerText || '').trim() : '';
  return JSON.stringify({len: t.length, title: document.title, text: t.slice(0, 120),
                         ready: document.readyState, ua: navigator.userAgent});
})()
"""

BLOCKED_HINT = ("Google/the provider refused to sign in from an embedded browser. "
                "Use the manual option (open the login page in your normal browser and "
                "paste the address).")
BLANK_HINT = ("The login page stayed blank in the embedded browser (even after trying other "
              "browser identities). Use the manual option (open the login page in your normal "
              "browser and paste the address).")

LOG_PATH = os.path.join(tempfile.gettempdir(), "wlm_gog_login.log")
_log_fh = None


def log(msg):
    """Log ke file (dan stderr jika WLM_GOG_DEBUG). Nilai code/token disamarkan."""
    msg = re.sub(r"(code|access_token|refresh_token|id_token)=[^&\s#]+", r"\1=***", str(msg))
    line = f"{time.strftime('%H:%M:%S')} {msg}\n"
    try:
        if _log_fh:
            _log_fh.write(line)
            _log_fh.flush()
        if os.environ.get("WLM_GOG_DEBUG"):
            sys.stderr.write(line)
            sys.stderr.flush()
    except Exception:
        pass


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def is_blocked_url(url):
    return "disallowed_useragent" in url or "/signin/rejected" in url


def guess_backend():
    """Tebak backend yang akan dipakai pywebview TANPA memuat WebKit/Qt (agar tidak bentrok)."""
    gui = (os.environ.get("PYWEBVIEW_GUI") or "").lower()
    if gui in ("gtk", "qt"):
        return gui
    try:
        import gi
        if gi.Repository.get_default().enumerate_versions("WebKit2"):
            return "gtk"
    except Exception:
        pass
    try:
        import importlib.util as iu
        if any(iu.find_spec(m) for m in ("PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy")):
            return "qt"
    except Exception:
        pass
    return "unknown"


def screen_size(backend):
    """Ukuran layar utama (lebar, tinggi) atau None. Dipakai agar jendela login tidak lebih
    besar dari layar (tombol Sign in bisa terpotong/tertutup di layar kecil)."""
    if backend in ("gtk", "unknown"):
        try:
            import gi
            gi.require_version("Gdk", "3.0")
            from gi.repository import Gdk
            d = Gdk.Display.get_default()
            m = d.get_primary_monitor() or d.get_monitor(0)
            g = m.get_geometry()
            if g.width > 0 and g.height > 0:
                return g.width, g.height
        except Exception as e:
            log(f"screen (gdk): {e}")
    try:
        import webview
        scr = webview.screens
        if scr:
            return int(scr[0].width), int(scr[0].height)
    except Exception as e:
        log(f"screen (webview): {e}")
    return None


def fit_window(width, height, backend):
    """Hitung ukuran awal jendela. 0 = otomatis: 620x820, tapi maksimal 92% lebar / 88% tinggi
    layar (menyisakan ruang untuk panel/taskbar dan title bar)."""
    width = width or int(os.environ.get("WLM_GOG_WIDTH") or 0) or 620
    height = height or int(os.environ.get("WLM_GOG_HEIGHT") or 0) or 820
    scr = screen_size(backend)
    if scr:
        width = min(width, int(scr[0] * 0.92))
        height = min(height, int(scr[1] * 0.88))
        log(f"layar {scr[0]}x{scr[1]} -> jendela {width}x{height}")
    return max(width, 360), max(height, 420)


def ua_order(backend):
    """Urutan percobaan User-Agent. Chromium: Chrome dulu. WebKit/tidak diketahui: Safari dulu."""
    if backend == "qt":
        return ["chrome", "firefox", "safari"]
    return ["safari", "firefox", "chrome"]


def find_native(window):
    """Objek jendela native. Mendukung pywebview baru (window.native) dan lama (BrowserView.instances)."""
    n = getattr(window, "native", None)
    if n is not None:
        return n
    uid = getattr(window, "uid", None)
    for modname in ("webview.platforms.gtk", "webview.platforms.qt"):
        bv = getattr(sys.modules.get(modname), "BrowserView", None)
        inst = getattr(bv, "instances", None)
        if inst:
            return inst.get(uid) or next(iter(inst.values()), None)
    return None


def _is_uniform(data, w, h, stride):
    """True kalau hampir semua piksel (contoh tiap 4 px) berwarna sama - halaman login asli selalu
    punya teks/logo/tombol, jadi tidak akan lolos ambang ini."""
    if w <= 0 or h <= 0:
        return None
    counts, total = {}, 0
    for y in range(0, h, 4):
        row = y * stride
        for x in range(0, w, 4):
            off = row + x * 4
            px = tuple(v >> 3 for v in data[off:off + 4])
            counts[px] = counts.get(px, 0) + 1
            total += 1
    if total < 500:
        return None
    return max(counts.values()) / total >= 0.998


def snapshot_blank(native, timeout=4.0):
    """True = tampilan WebKit polos satu warna (mis. putih walau DOM berisi), False = ada isinya,
    None = tidak bisa diperiksa. Mendeteksi layar putih akibat rendering yang rusak."""
    view = getattr(native, "webview", None)
    if view is None or not hasattr(view, "get_snapshot"):
        return None
    try:
        from gi.repository import GLib
        WebKit2 = sys.modules.get("gi.repository.WebKit2")
        if WebKit2 is None:
            from gi.repository import WebKit2
    except Exception as e:
        log(f"snapshot: gi/WebKit2 tidak tersedia: {e}")
        return None
    box = {"result": None}
    done = threading.Event()

    def finish(source, res, _data=None):
        try:
            surface = source.get_snapshot_finish(res)
            surface.flush()
            box["result"] = _is_uniform(bytes(surface.get_data()), surface.get_width(),
                                        surface.get_height(), surface.get_stride())
        except Exception as e:
            log(f"snapshot gagal: {e}")
        finally:
            done.set()

    def start():
        try:
            view.get_snapshot(WebKit2.SnapshotRegion.VISIBLE, WebKit2.SnapshotOptions.NONE,
                              None, finish, None)
        except Exception as e:
            log(f"snapshot tidak bisa dimulai: {e}")
            done.set()
        return False

    GLib.idle_add(start)
    done.wait(timeout)
    return box["result"]


def install_gtk_popups(native, on_url, is_done, main_url):
    """Popup asli + anti-blank untuk WebKit2GTK. Dipanggil dari thread watcher; semua pekerjaan
    GTK dijalankan di main thread lewat GLib.idle_add. True jika berhasil terpasang."""
    view = getattr(native, "webview", None)
    if view is None or not hasattr(view, "connect"):
        log(f"gtk: native.webview tidak ditemukan (native={type(native).__name__}); pakai fallback JS")
        return False
    try:
        from gi.repository import Gtk, GLib
        WebKit2 = sys.modules.get("gi.repository.WebKit2")
        if WebKit2 is None:
            from gi.repository import WebKit2
    except Exception as e:
        log(f"gtk: gi/WebKit2 tidak bisa di-import: {e}")
        return False

    result = {"ok": False}
    done = threading.Event()
    rec = {"tries": 0}

    def nick(x):
        return getattr(x, "value_nick", x)

    zoom = {"level": 1.0, "full": False}

    def on_key(_w, ev):
        """Ctrl +/-/0 = zoom halaman (perkecil kalau tombol Sign in terpotong); F11 = layar penuh."""
        try:
            from gi.repository import Gdk
            ctrl = bool(ev.state & Gdk.ModifierType.CONTROL_MASK)
            k = ev.keyval
            if ctrl and k in (0x2b, 0x3d, 0xffab):        # + = KP_Add
                zoom["level"] = min(3.0, zoom["level"] + 0.1)
            elif ctrl and k in (0x2d, 0xffad):            # - KP_Subtract
                zoom["level"] = max(0.3, zoom["level"] - 0.1)
            elif ctrl and k in (0x30, 0xffb0):            # 0 KP_0
                zoom["level"] = 1.0
            elif k == 0xffc8 and _w is view:              # F11 (jendela utama)
                zoom["full"] = not zoom["full"]
                (native.fullscreen if zoom["full"] else native.unfullscreen)()
                return True
            else:
                return False
            _w.set_zoom_level(zoom["level"])
            log(f"zoom {zoom['level']:.1f}")
            return True
        except Exception as e:
            log(f"key handler: {e}")
            return False

    def watch_view(v, tag):
        def on_load_changed(_v, ev):
            log(f"{tag} load-changed: {nick(ev)} {v.get_uri() or ''}")

        def on_load_failed(_v, _ev, uri, err):
            log(f"{tag} LOAD-FAILED {uri}: {getattr(err, 'message', err)}")
            return False

        def on_terminated(_v, reason):
            log(f"{tag} WEB PROCESS TERMINATED ({nick(reason)}) -> reload")
            try:
                v.reload()
            except Exception:
                pass

        def on_policy(_v, decision, dtype):
            # Dipanggil untuk SETIAP navigasi (termasuk redirect) sebelum halaman dimuat.
            if nick(dtype) not in ("navigation-action", "new-window-action"):
                return False
            try:
                uri = decision.get_navigation_action().get_request().get_uri() or ""
            except Exception:
                return False
            log(f"{tag} nav: {uri}")
            if on_url(uri):  # code ditemukan / diblokir -> hentikan pemuatan halaman
                try:
                    decision.ignore()
                except Exception:
                    pass
                return True
            return False

        try:
            v.connect("key-press-event", on_key)
        except Exception as e:
            log(f"{tag}: key-press-event tidak tersedia: {e}")
        for sig, cb in (("load-changed", on_load_changed), ("load-failed", on_load_failed),
                        ("web-process-terminated", on_terminated), ("decide-policy", on_policy)):
            try:
                v.connect(sig, cb)
            except Exception as e:
                log(f"{tag}: sinyal {sig} tidak tersedia: {e}")

    def recover_main(reason):
        if is_done() or rec["tries"] >= 3:
            return False
        rec["tries"] += 1
        log(f"RECOVER ({reason}): memuat ulang halaman login di jendela induk")
        try:
            view.load_uri(main_url)
        except Exception as e:
            log(f"recover gagal: {e}")
        return False

    def on_create(parent_view, _navigation_action):
        popup = WebKit2.WebView.new_with_related_view(parent_view)  # berbagi sesi + window.opener
        popup.set_settings(parent_view.get_settings())
        win = Gtk.Window(title="Sign in")
        win.set_resizable(True)
        pw, ph = 520, 700
        try:
            g = win.get_screen()
            pw, ph = min(pw, int(g.get_width() * 0.92)), min(ph, int(g.get_height() * 0.88))
        except Exception:
            pass
        win.set_default_size(pw, ph)
        win.set_size_request(320, 360)
        try:
            win.set_transient_for(native)
        except Exception:
            pass
        win.add(popup)
        info = {"external": False, "alive": True, "scheduled": False}
        log("popup dibuka")
        watch_view(popup, "popup")

        def on_destroy(_w):
            info["alive"] = False
            log("popup ditutup")
            if is_done():
                return
            uri_at_close = view.get_uri()

            def later():
                if not is_done() and view.get_uri() == uri_at_close:
                    recover_main("popup ditutup, jendela induk tidak bergerak")
                return False
            GLib.timeout_add(3000, later)

        def on_uri(w, _p):
            uri = w.get_uri() or ""
            log(f"popup uri: {uri}")
            on_url(uri)
            host = urllib.parse.urlparse(uri).hostname or ""
            if not host.endswith("gog.com"):
                info["external"] = True
            elif info["external"] and not info["scheduled"]:
                info["scheduled"] = True

                def stuck():
                    if info["alive"] and not is_done():
                        log("popup macet di halaman gog.com -> ditutup")
                        win.destroy()
                    return False
                GLib.timeout_add(5000, stuck)

        popup.connect("close", lambda _w: win.destroy())
        popup.connect("notify::uri", on_uri)
        popup.connect("create", on_create)
        win.connect("destroy", on_destroy)
        win.show_all()
        return popup

    main_info = {"external": False}

    def on_main_uri(w, _p):
        uri = w.get_uri() or ""
        log(f"main uri: {uri}")
        host = urllib.parse.urlparse(uri).hostname or ""
        if not host:
            return
        if not host.endswith("gog.com"):
            main_info["external"] = True  # sedang di Google/Facebook/Steam/Xbox
        elif main_info["external"]:
            def stall():
                if not is_done() and view.get_uri() == uri:
                    recover_main("jendela induk berhenti di halaman gog.com setelah login sosial")
                return False
            GLib.timeout_add(4000, stall)

    def setup():
        try:
            settings = view.get_settings()
            settings.set_javascript_can_open_windows_automatically(True)
            if NO_GPU:
                try:  # tanpa akselerasi GPU sama sekali (popup memakai settings yang sama)
                    settings.set_hardware_acceleration_policy(
                        WebKit2.HardwareAccelerationPolicy.NEVER)
                    log("hardware acceleration: NEVER")
                except Exception as e:
                    log(f"hardware acceleration policy: {e}")
            view.connect("create", on_create)
            view.connect("notify::uri", on_main_uri)
            watch_view(view, "main")
            result["ok"] = True
        except Exception as e:
            log(f"gtk setup gagal: {e}")
        try:  # OAuth lintas-domain butuh cookie pihak ketiga
            view.get_context().get_cookie_manager().set_accept_policy(
                WebKit2.CookieAcceptPolicy.ALWAYS)
        except Exception as e:
            log(f"cookie policy: {e}")
        try:
            view.get_website_data_manager().set_itp_enabled(False)
        except Exception as e:
            log(f"itp: {e}")
        done.set()
        return False

    GLib.idle_add(setup)
    done.wait(3)
    return result["ok"]


def main():
    global _log_fh
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--watch", default="on_login_success",
                    help="potongan path URL yang menandakan login sukses")
    ap.add_argument("--title", default="GOG Login")
    ap.add_argument("--width", type=int, default=0, help="0 = otomatis (muat di layar)")
    ap.add_argument("--height", type=int, default=0, help="0 = otomatis (muat di layar)")
    ap.add_argument("--ua", default="auto", choices=["auto"] + sorted(UA_PROFILES))
    ap.add_argument("--tried", default="", help="(internal) daftar UA yang sudah dicoba")
    ap.add_argument("--render", default="", choices=[""] + sorted(RENDER_PROFILES),
                    help="profil rendering (default: software)")
    ap.add_argument("--render-tried", default="", help="(internal) daftar profil rendering yang sudah dicoba")
    ap.add_argument("--no-retry", action="store_true",
                    help="jangan ulangi dengan UA lain jika diblokir/putih")
    args = ap.parse_args()

    tried = [t for t in args.tried.split(",") if t]
    tried_r = [t for t in args.render_tried.split(",") if t]
    if RENDER not in tried_r:
        tried_r.append(RENDER)
    try:
        _log_fh = open(LOG_PATH, "a" if tried else "w", encoding="utf-8")
    except Exception:
        _log_fh = None

    backend = guess_backend()
    order = ua_order(backend)
    ua_name = order[0] if args.ua == "auto" else args.ua
    if ua_name not in tried:
        tried.append(ua_name)
    log(f"=== mulai (backend~{backend}, ua={ua_name}, render={RENDER}, tried={tried}, "
        f"render_tried={tried_r}) === log: {LOG_PATH}")

    try:
        import webview
    except ImportError:
        emit({"error": "pywebview is not installed (pip install pywebview)."})
        return 2
    try:
        from importlib.metadata import version as _v
        log(f"pywebview {_v('pywebview')}")
    except Exception:
        log("pywebview (versi tidak diketahui)")

    state = {"code": None, "closed": False, "blocked": False, "blank": False, "popups": None}
    args.width, args.height = fit_window(args.width, args.height, backend)
    window = webview.create_window(args.title, args.url, width=args.width, height=args.height,
                                   resizable=True, min_size=(320, 360))
    window.events.closed += lambda: state.update(closed=True)

    def close_window():
        try:
            window.destroy()
        except Exception:
            pass

    def check_url(url):
        """Dipakai untuk jendela induk DAN popup. True jika alur sudah selesai."""
        if state["code"] or state["blocked"]:
            return True
        u = urllib.parse.urlparse(url)
        if args.watch in u.path:  # cek PATH saja: redirect_uri di query tidak ikut terhitung
            code = urllib.parse.parse_qs(u.query).get("code")
            if code:
                state["code"] = code[0]
                log("CODE DITEMUKAN -> menutup jendela")
                close_window()
                return True
            log(f"halaman sukses tanpa code=: {url}")
        elif is_blocked_url(url):
            state["blocked"] = True
            log(f"DIBLOKIR: {url}")
            close_window()
            return True
        return False

    def is_done():
        return bool(state["code"] or state["blocked"] or state["blank"] or state["closed"])

    def probe():
        try:
            r = window.evaluate_js(PROBE_JS)
            return json.loads(r) if r else None
        except Exception as e:
            return {"error": str(e)}

    def watcher():
        native = None
        for _ in range(50):  # tunggu jendela native siap (maks ~5 dtk)
            native = find_native(window)
            if native is not None:
                break
            time.sleep(0.1)
        mods = sorted(m for m in sys.modules if m.startswith("webview.platforms."))
        log(f"modul backend: {mods}; native={type(native).__name__ if native else None}")
        try:
            state["popups"] = install_gtk_popups(native, check_url, is_done, args.url)
        except Exception as e:
            log(f"install_gtk_popups error: {e}")
            state["popups"] = False
        log(f"popup asli/GTK: {state['popups']}")

        last_url, last_inject = None, 0.0
        url_since, last_probe, probed = time.time(), 0.0, set()
        last_snap, snap_ok, snap_tries, blank_hits = 0.0, set(), {}, 0
        while not state["closed"] and not state["code"] and not state["blocked"] and not state["blank"]:
            try:
                url = window.get_current_url() or ""
            except Exception:
                url = ""
            if check_url(url):
                return
            now = time.time()
            if url != last_url:
                url_since = now
                if state["popups"] is not True:
                    log(f"main uri (poll): {url}")
            if state["popups"] is not True and (url != last_url or now - last_inject > 1.0):
                try:  # tanpa popup asli: pakai fallback JS
                    window.evaluate_js(POPUP_FIX)
                except Exception:
                    pass
                last_inject = now
            last_url = url

            # Deteksi halaman putih/kosong: URL sama >= 8 dtk tapi tidak ada teks sama sekali.
            path = urllib.parse.urlparse(url).path
            if (url and not url.startswith("about:") and url not in probed
                    and args.watch not in path and now - url_since >= 8 and now - last_probe >= 2):
                last_probe = now
                info = probe()
                log(f"probe: {info}")
                if isinstance(info, dict) and "error" not in info:
                    if info.get("ready") == "complete" or now - url_since >= 20:
                        probed.add(url)
                        if info.get("len") == 0:
                            state["blank"] = True
                            log("HALAMAN PUTIH/KOSONG terdeteksi -> menutup jendela")
                            close_window()
                            return
                else:
                    probed.add(url)  # evaluate_js gagal: jangan diulang terus

            # Layar putih karena render rusak: DOM berisi teks tapi yang tergambar polos.
            if (state["popups"] is True and native is not None and url and not url.startswith("about:")
                    and args.watch not in path and url not in snap_ok
                    and now - url_since >= 10 and now - last_snap >= 4):
                last_snap = now
                snap_tries[url] = snap_tries.get(url, 0) + 1
                blank_now = snapshot_blank(native)
                log(f"snapshot polos? {blank_now}")
                if blank_now is False or snap_tries[url] >= 6:
                    snap_ok.add(url)
                    blank_hits = 0
                elif blank_now is True:
                    blank_hits += 1
                    if blank_hits >= 2:
                        state["blank"] = True
                        log("TAMPILAN POLOS (render rusak) -> menutup jendela")
                        close_window()
                        return
            time.sleep(0.3)

    try:
        # private_mode bawaan: tidak ada cookie/sesi yang tersimpan di disk
        webview.start(watcher, user_agent=UA_PROFILES[ua_name])
    except Exception as e:
        log(f"webview.start gagal: {e}")
        emit({"error": f"Could not start the browser window: {e}"})
        return 3
    log(f"guilib: {getattr(webview, 'guilib', None)}")

    if state["code"]:
        emit({"code": state["code"]})
    elif state["blocked"] or state["blank"]:
        why = "diblokir" if state["blocked"] else "halaman putih"
        nxt = next((u for u in order if u not in tried), None)
        nxt_r = None if state["blocked"] else next((r for r in RENDER_ORDER if r not in tried_r), None)
        if (nxt or nxt_r) and not args.no_retry:
            use_ua, use_r = nxt or ua_name, nxt_r or RENDER
            log(f"{why} -> mengulang dengan UA {use_ua}, render {use_r}")
            # pywebview tidak bisa start dua kali dalam satu proses -> ulangi sebagai proses baru
            cmd = [sys.executable, os.path.abspath(__file__), args.url,
                   "--watch", args.watch, "--title", args.title,
                   "--width", str(args.width), "--height", str(args.height),
                   "--ua", use_ua, "--tried", ",".join(tried),
                   "--render", use_r, "--render-tried", ",".join(tried_r)]
            env = os.environ.copy()
            for k in _ENV_SET:      # biarkan proses baru menyetel profilnya sendiri
                env.pop(k, None)
            try:
                r = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, env=env)
                lines = r.stdout.strip().splitlines()
                if lines:
                    sys.stdout.write(lines[-1] + "\n")
                    sys.stdout.flush()
                    return 0
            except Exception as e:
                log(f"retry gagal: {e}")
        emit({"error": BLOCKED_HINT if state["blocked"] else BLANK_HINT})
    else:
        log("jendela ditutup pengguna tanpa code")
        emit({"cancelled": True})
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code or 0) 
