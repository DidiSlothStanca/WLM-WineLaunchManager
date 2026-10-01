"""game_covers.py - tampilan library bergambar (grid kartu ala GOG Store) untuk launcher.py.

Isi modul:
- CoverStore   : cari cover berdasarkan JUDUL game di katalog GOG (kalau ada internet), cocokkan
                 dengan kemiripan judul, lalu simpan di ~/wlm/covers/<nama>.jpg. Semua akses
                 jaringan BLOCKING dan harus dipanggil dari background thread.
- LibraryGrid  : widget Tk berisi kartu cover yang bisa di-scroll. Cover dimuat lazy (hanya kartu
                 yang terlihat), jadi library besar tetap ringan.
- load/save_view_config : pilihan tampilan (Grid/List) dan ukuran kartu.

Aturan cover:
1. File cover yang sudah ada (diunduh sebelumnya atau dipilih manual) selalu dipakai lebih dulu.
2. Kalau belum ada dan ada internet: cari judul di GOG, ambil hasil yang paling mirip. Kalau
   kemiripannya di bawah MATCH_THRESHOLD, dianggap tidak ketemu (dicoba lagi 7 hari kemudian).
3. Kalau offline: tidak dianggap gagal - dicoba lagi lain kali. Sementara itu kartu memakai ikon
   game (kalau ada) atau huruf pertama judul.
Hanya butuh library standar + Pillow (sudah dipakai launcher.py).
"""
import difflib
import io
import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageTk

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) WineLaunchManager"
MATCH_THRESHOLD = 0.72            # 0-1; makin tinggi makin ketat
MISS_RETRY_SECS = 7 * 24 * 3600   # judul yang tidak ketemu dicoba lagi setelah 1 minggu
OFFLINE_BACKOFF_SECS = 60         # setelah gagal konek, jangan coba jaringan lagi selama ini
COVER_RATIO = 0.5625              # tinggi/lebar kartu (16:9, seperti tile GOG)
CARD_SIZES = {"Small": 160, "Medium": 200, "Large": 260}
_MAX_BYTES = 8 * 1024 * 1024
GOG_COLOR = "#b06cff"

_RESAMPLE = getattr(getattr(Image, "Resampling", Image), "LANCZOS")


class OfflineError(Exception):
    """Tidak ada koneksi / timeout (bukan berarti judulnya tidak ada)."""


class FetchError(Exception):
    """Server menjawab tapi dengan error (HTTP 4xx/5xx, JSON rusak, gambar rusak)."""


# --------------------------------------------------------------------------- view config

def load_view_config(path):
    cfg = {"view": "list", "card_width": CARD_SIZES["Medium"]}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("view") in ("grid", "list"):
            cfg["view"] = data["view"]
        w = int(data.get("card_width", cfg["card_width"]))
        cfg["card_width"] = max(120, min(400, w))
    except Exception:
        pass
    return cfg


def save_view_config(path, cfg):
    try:
        Path(path).write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[WLM] Could not save {path}: {e}")


# --------------------------------------------------------------------------- title matching

_EDITION_RE = re.compile(
    r"\b(goty|game of the year( edition)?|definitive edition|complete edition|deluxe edition|"
    r"enhanced edition|special edition|ultimate edition|gold edition|collector'?s edition|"
    r"remastered|remaster|gog edition|director'?s cut|anniversary edition)\b")
_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8",
          "ix": "9", "x": "10"}


def normalize_title(text):
    t = unicodedata.normalize("NFKD", text or "").lower()
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[\u2122\u00ae\u00a9]", "", t).replace("&", " and ")
    t = re.sub(r"[\(\[].*?[\)\]]", " ", t)          # (GOG), [v1.2] dsb.
    t = _EDITION_RE.sub(" ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _number_tokens(norm):
    toks = norm.split()
    nums = {_ROMAN.get(t, t) for t in toks if t.isdigit() or t in _ROMAN}
    # 'v' sendirian di tengah kata lain bisa berarti huruf biasa; hanya hitung kalau bukan kata pertama
    if toks and toks[0] in _ROMAN and len(toks) > 1:
        nums.discard(_ROMAN[toks[0]])
    return nums


def title_similarity(a, b):
    """0-1. Judul yang sama setelah dinormalisasi = 1.0. Angka sekuel yang berbeda
    ('Doom' vs 'Doom 3', 'X II' vs 'X III') menurunkan skor supaya tidak salah cover."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    score = difflib.SequenceMatcher(None, na, nb).ratio()
    if _number_tokens(na) != _number_tokens(nb):
        score *= 0.6
    return score


# --------------------------------------------------------------------------- network

def _fetch(url, timeout=10, limit=_MAX_BYTES):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                "Accept": "application/json, image/*;q=0.9, */*;q=0.5"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(limit + 1)
    except urllib.error.HTTPError as e:            # harus sebelum URLError (HTTPError turunannya)
        raise FetchError(f"HTTP {e.code} for {url}")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise OfflineError(str(e))
    if len(data) > limit:
        raise FetchError("response too large")
    return data


def _search_catalog(title):
    """Katalog GOG (dipakai situs gog.com). List {'title','horizontal','vertical'}."""
    qs = urllib.parse.urlencode({
        "limit": 10, "order": "desc:score", "productType": "in:game,pack",
        "query": "like:" + title, "page": 1, "countryCode": "US",
        "locale": "en-US", "currencyCode": "USD"})
    data = json.loads(_fetch("https://catalog.gog.com/v1/catalog?" + qs, limit=2 * 1024 * 1024))
    out = []
    for p in data.get("products") or []:
        h, v = p.get("coverHorizontal") or "", p.get("coverVertical") or ""
        if p.get("title") and (h or v):
            out.append({"title": p["title"], "horizontal": h, "vertical": v})
    return out


def _search_embed(title):
    """Cadangan: endpoint lama embed.gog.com (field 'image' = URL dasar tanpa ekstensi)."""
    qs = urllib.parse.urlencode({"mediaType": "game", "search": title})
    data = json.loads(_fetch("https://embed.gog.com/games/ajax/filtered?" + qs, limit=2 * 1024 * 1024))
    out = []
    for p in data.get("products") or []:
        img = (p.get("image") or "").strip()
        if not img or not p.get("title"):
            continue
        if img.startswith("//"):
            img = "https:" + img
        elif not img.startswith("http"):
            img = "https://images.gog.com/" + img.lstrip("/")
        url = re.sub(r"\.(?:jpe?g|png|webp)$", "", img) + "_product_tile_398.jpg"
        out.append({"title": p["title"], "horizontal": url, "vertical": url})
    return out


def find_cover_url(title, threshold=MATCH_THRESHOLD):
    """(url, judul_yang_cocok, skor) atau (None, None, skor_terbaik). Raise OfflineError kalau
    tidak ada internet."""
    queries = [title]
    simple = normalize_title(title)
    if simple and simple != title.lower():
        queries.append(simple)
    best = (None, None, 0.0)
    last_err = None
    for q in queries:
        for search in (_search_catalog, _search_embed):
            try:
                results = search(q)
            except OfflineError:
                raise
            except Exception as e:                 # FetchError / JSON rusak -> coba sumber lain
                last_err = e
                continue
            for r in results:
                score = title_similarity(title, r["title"])
                url = r["horizontal"] or r["vertical"]
                if url and score > best[2]:
                    best = (url, r["title"], score)
            if best[2] >= threshold:
                return best
            if results:
                break                              # sumber ini menjawab; tidak perlu cadangan
    if best[2] < threshold and last_err is not None and best[0] is None:
        print(f"[WLM] Cover search for '{title}' failed: {last_err}")
    return None, None, best[2]


# --------------------------------------------------------------------------- store

class CoverStore:
    def __init__(self, data_dir):
        self.dir = Path(data_dir) / "covers"
        self.index_path = self.dir / "index.json"
        self._lock = threading.Lock()
        self._offline_until = 0.0
        self._index = None

    # -- index (nama -> {'src','matched','score','ts'} atau {'miss': ts})
    def _load(self):
        if self._index is None:
            try:
                self._index = json.loads(self.index_path.read_text(encoding="utf-8"))
            except Exception:
                self._index = {}
        return self._index

    def _save(self):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.index_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._index, indent=1), encoding="utf-8")
            tmp.replace(self.index_path)
        except Exception as e:
            print(f"[WLM] Could not save cover index: {e}")

    def path(self, name):
        return self.dir / (re.sub(r"[\\/]", "_", name) + ".jpg")

    def has(self, name):
        return self.path(name).is_file()

    def offline(self):
        return time.time() < self._offline_until

    def _mark_offline(self):
        self._offline_until = time.time() + OFFLINE_BACKOFF_SECS

    def clear_offline(self):
        self._offline_until = 0.0

    def _recent_miss(self, name):
        with self._lock:
            ts = (self._load().get(name) or {}).get("miss")
        return bool(ts) and time.time() - ts < MISS_RETRY_SECS

    def clear_miss(self, name):
        with self._lock:
            idx = self._load()
            if "miss" in (idx.get(name) or {}):
                del idx[name]
                self._save()

    def _record(self, name, entry):
        with self._lock:
            self._load()[name] = entry
            self._save()

    def _save_image(self, name, img):
        img = img.convert("RGB")
        img.thumbnail((640, 640), _RESAMPLE)
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.path(name)
        tmp = p.with_name(p.name + ".tmp")
        img.save(tmp, "JPEG", quality=88)
        tmp.replace(p)

    def set_manual(self, name, image_path):
        img = Image.open(image_path)
        img.load()
        self._save_image(name, img)
        self._record(name, {"src": "manual", "ts": time.time()})

    def forget(self, name):
        self.path(name).unlink(missing_ok=True)
        with self._lock:
            if self._load().pop(name, None) is not None:
                self._save()

    def rename(self, old, new):
        try:
            if self.has(old) and not self.has(new):
                self.path(old).rename(self.path(new))
            else:
                self.path(old).unlink(missing_ok=True)
        except OSError as e:
            print(f"[WLM] Could not rename cover: {e}")
        with self._lock:
            entry = self._load().pop(old, None)
            if entry and "miss" not in entry:
                self._load()[new] = entry
            self._save()

    def ensure(self, name, force=False):
        """Pastikan cover untuk 'name' ada di disk. True kalau ada (sudah ada / baru diunduh).
        False kalau judul tidak ketemu/tidak mirip. Raise OfflineError kalau tidak ada internet.
        Cover manual tidak pernah ditimpa kecuali force=True."""
        if not force:
            if self.has(name):
                return True
            if self._recent_miss(name):
                return False
        if self.offline():
            raise OfflineError("offline (backoff)")
        try:
            url, matched, score = find_cover_url(name)
            if not url:
                self._record(name, {"miss": time.time(), "score": round(score, 2)})
                return False
            img = Image.open(io.BytesIO(_fetch(url, timeout=15)))
            img.load()
        except OfflineError:
            self._mark_offline()
            raise
        except (FetchError, OSError, ValueError) as e:
            print(f"[WLM] Cover download for '{name}' failed: {e}")
            return self.has(name)
        self._save_image(name, img)
        self._record(name, {"src": "auto", "matched": matched, "score": round(score, 2),
                            "ts": time.time()})
        return True


# --------------------------------------------------------------------------- grid

class LibraryGrid(tk.Frame):
    """Grid kartu game: cover + judul, bisa di-scroll.

    on_select(name)   : dipanggil saat kartu diklik (launcher menyinkronkan Treeview-nya)
    on_deselect()     : klik kartu yang sudah terpilih -> batal pilih
    on_activate(name) : double-click (mis. langsung PLAY)
    """

    def __init__(self, parent, get_colors, fonts, icon_dir, store, card_width,
                 on_select, on_deselect, on_activate, on_status=None, source_of=None):
        self.get_colors = get_colors
        super().__init__(parent, bg=get_colors()["primary"])
        self.fonts, self.icon_dir, self.store = fonts, Path(icon_dir), store
        self.on_select, self.on_deselect = on_select, on_deselect
        self.on_activate, self.on_status = on_activate, on_status
        self.source_of = source_of      # nama -> "gog" | "other" (None = tanpa penanda)
        self.card_w = max(120, min(400, int(card_width)))
        self.card_h = int(self.card_w * COVER_RATIO)

        c = get_colors()
        self.canvas = tk.Canvas(self, bg=c["primary"], highlightthickness=0, bd=0,
                                yscrollincrement=60)
        self.vscroll = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_yscroll)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vscroll.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.inner = tk.Frame(self.canvas, bg=c["primary"])
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self._names = ()
        self._cards = {}
        self._selected = None
        self._photos = {}      # nama -> PhotoImage (referensi harus disimpan)
        self._pending = set()
        self._gen = 0          # naik tiap kartu dibangun ulang; hasil thread lama diabaikan
        self._layout_key = None
        self._t_layout = self._t_load = self._t_retry = None
        self._retry = set()    # kartu tanpa cover karena offline; dicoba lagi otomatis
        self._pool = ThreadPoolExecutor(max_workers=3)
        self._placeholder = None
        self._menu = tk.Menu(self, tearoff=0)

        self.canvas.bind("<Configure>", lambda _e: self._schedule_layout())
        self.bind("<Destroy>", self._on_destroy)
        self._bind_wheel(self.canvas)
        self._bind_wheel(self.inner)

    def _fit(self, img):
        return ImageOps.fit(img.convert("RGB"), (self.card_w, self.card_h), method=_RESAMPLE)

    def _blank(self, title=None):
        c = self.get_colors()
        img = Image.new("RGB", (self.card_w, self.card_h), c.get("secondary", "#16213e"))
        if title:
            try:
                d = ImageDraw.Draw(img)
                try:
                    font = ImageFont.load_default(size=max(16, self.card_h // 2))
                except TypeError:
                    font = ImageFont.load_default()
                d.text((self.card_w // 2, self.card_h // 2), (title.strip()[:1] or "?").upper(),
                       fill=c.get("text_secondary", "#b0b0b0"), font=font, anchor="mm")
            except Exception:
                pass
        return img

    def _icon_card(self, name):
        """Ikon game (persegi) ditaruh di tengah latar tema, tidak dipotong."""
        p = self.icon_dir / f"{name}.png"
        if not p.is_file():
            return None
        try:
            ic = Image.open(p).convert("RGBA")
            ic.thumbnail((self.card_h - 8, self.card_h - 8), _RESAMPLE)
            bg = self._blank().convert("RGBA")
            bg.alpha_composite(ic, ((self.card_w - ic.width) // 2, (self.card_h - ic.height) // 2))
            return bg.convert("RGB")
        except Exception:
            return None

    def _compose(self, name):
        """(PIL.Image atau None, ada_cover: bool)."""
        if self.store.has(name):
            try:
                img = Image.open(self.store.path(name))
                img.load()
                return self._fit(img), True
            except Exception:
                self.store.path(name).unlink(missing_ok=True)   # rusak -> unduh ulang nanti
        return self._icon_card(name), False

    def _bind_wheel(self, w):
        w.bind("<MouseWheel>", lambda e: self._scroll(-1 if e.delta > 0 else 1), add="+")
        w.bind("<Button-4>", lambda _e: self._scroll(-1), add="+")
        w.bind("<Button-5>", lambda _e: self._scroll(1), add="+")

    def _scroll(self, n):
        if self.inner.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(n, "units")

    def _on_yscroll(self, first, last):
        self.vscroll.set(first, last)
        self._schedule_load()

    def _on_destroy(self, e):
        if e.widget is self:
            try:
                self._pool.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                self._pool.shutdown(wait=False)

    def _later(self, attr, delay, fn):
        old = getattr(self, attr)
        if old:
            try:
                self.after_cancel(old)
            except Exception:
                pass
        setattr(self, attr, self.after(delay, fn))

    def _schedule_layout(self, delay=30):
        self._later("_t_layout", delay, self._layout)

    def _schedule_load(self, delay=60):
        self._later("_t_load", delay, self._load_visible)

    def _say(self, text, level="info"):
        if self.on_status:
            try:
                self.on_status(text, level)
            except Exception:
                pass

    def set_items(self, names, force=False):
        names = tuple(names)
        if names == self._names and not force:
            return
        self._names = names
        self._rebuild()

    def _rebuild(self):
        self._gen += 1
        for card in self._cards.values():
            card["frame"].destroy()
        self._cards = {}
        self._pending.clear()
        self._layout_key = None
        self._placeholder = ImageTk.PhotoImage(self._blank())
        for n in self._names:
            self._cards[n] = self._make_card(n)
        if self._selected not in self._cards:
            self._selected = None
        self.highlight(self._selected)
        self._schedule_layout()

    def recolor(self):
        """Dipanggil setelah ganti tema."""
        c = self.get_colors()
        self.configure(bg=c["primary"])
        self.canvas.configure(bg=c["primary"])
        self.inner.configure(bg=c["primary"])
        self._photos.clear()      # placeholder/ikon memakai warna tema
        self._rebuild()

    def set_card_width(self, width):
        width = max(120, min(400, int(width)))
        if width == self.card_w:
            return
        self.card_w, self.card_h = width, int(width * COVER_RATIO)
        self._photos.clear()
        self._rebuild()

    def highlight(self, name):
        """Tandai kartu terpilih TANPA memanggil callback (dipakai launcher saat pilihan berubah)."""
        c = self.get_colors()
        prev = self._cards.get(self._selected)
        if prev:
            prev["frame"].config(highlightbackground=c["card_bg"], highlightcolor=c["card_bg"])
        self._selected = name
        card = self._cards.get(name)
        if card:
            hl = c.get("highlight", "#e94560")
            card["frame"].config(highlightbackground=hl, highlightcolor=hl)
            self._ensure_visible(card)

    def _ensure_visible(self, card):
        try:
            self.update_idletasks()
            y, h = card["frame"].winfo_y(), card["frame"].winfo_height()
            top, ch = self.canvas.canvasy(0), self.canvas.winfo_height()
            total = max(1, self.inner.winfo_reqheight())
            if h and (y < top or y + h > top + ch):
                self.canvas.yview_moveto(max(0.0, (y - 10) / total))
        except tk.TclError:
            pass

    def _make_card(self, name):
        c = self.get_colors()
        bg, fg = c["card_bg"], c["text"]
        f = tk.Frame(self.inner, bg=bg, highlightthickness=2, highlightbackground=bg,
                     highlightcolor=bg, cursor="hand2")
        photo = self._photos.get(name) or self._placeholder
        img = tk.Label(f, image=photo, bg=bg, bd=0)
        img.pack(padx=4, pady=(4, 2))
        title = tk.Label(f, text=name, bg=bg, fg=fg, font=self.fonts["small"],
                         wraplength=self.card_w, justify="center", anchor="n", height=2)
        title.pack(padx=4)
        widgets = [f, img, title]
        if self.source_of is not None:
            is_gog = self.source_of(name) == "gog"
            tag = tk.Label(f, text="\u25cf GOG" if is_gog else "\u25cb Non-GOG", bg=bg,
                           fg=GOG_COLOR if is_gog else c.get("text_secondary", "#b0b0b0"),
                           font=self.fonts["small"])
            tag.pack(padx=4, pady=(0, 4))
            widgets.append(tag)
        else:
            title.pack_configure(pady=(0, 4))
        for w in widgets:
            w.bind("<Button-1>", lambda _e, n=name: self._click(n))
            w.bind("<Double-1>", lambda _e, n=name: self._double(n))
            w.bind("<Button-3>", lambda e, n=name: self._popup(e, n))
            self._bind_wheel(w)
        return {"name": name, "frame": f, "img": img, "lower": name.lower()}

    def _click(self, name):
        if name == self._selected:
            self.on_deselect()
        else:
            self.highlight(name)
            self.on_select(name)

    def _double(self, name):
        self.highlight(name)
        self.on_select(name)
        self.on_activate(name)

    def _popup(self, event, name):
        self.highlight(name)
        self.on_select(name)
        c = self.get_colors()
        m = self._menu
        m.delete(0, tk.END)
        m.configure(bg=c["card_bg"], fg=c["text"], activebackground=c["highlight"],
                    activeforeground=c.get("button_text", "#ffffff"))
        m.add_command(label="Download cover again", command=lambda: self.refresh(name, force=True))
        m.add_command(label="Choose cover image...", command=lambda: self._choose(name))
        m.add_command(label="Remove cover", command=lambda: self._remove(name),
                      state=tk.NORMAL if self.store.has(name) else tk.DISABLED)
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _choose(self, name):
        path = filedialog.askopenfilename(
            parent=self.winfo_toplevel(), title=f"Cover for {name}",
            filetypes=[("Image Files", "*.png *.jpg *.jpeg *.webp *.bmp"), ("All Files", "*.*")])
        if not path:
            return
        try:
            self.store.set_manual(name, path)
        except Exception as e:
            self._say(f"Could not use that image: {e}", "danger")
            return
        self.refresh(name)
        self._say(f"Cover set for {name}", "success")

    def _remove(self, name):
        self.store.forget(name)
        self.refresh(name)
        self._say(f"Cover removed: {name}", "warning")

    def _layout(self):
        self._t_layout = None
        if not self.winfo_exists():
            return
        width = self.canvas.winfo_width()
        if width <= 1:
            self._schedule_layout(50)
            return
        cols = max(1, width // (self.card_w + 26))
        key = (cols, self._names, self._gen)
        if key != self._layout_key:
            self._layout_key = key
            for c in self._cards.values():
                c["frame"].grid_forget()
            for i, n in enumerate(self._names):
                self._cards[n]["frame"].grid(row=i // cols, column=i % cols, padx=6, pady=6, sticky="n")
            self.inner.update_idletasks()
        iw, ih = self.inner.winfo_reqwidth(), self.inner.winfo_reqheight()
        self.canvas.coords(self._win, 0, 0)
        self.canvas.configure(scrollregion=(0, 0, max(width, iw), max(ih, 1)))
        self._schedule_load()

    def _load_visible(self):
        self._t_load = None
        if not self.winfo_exists():
            return
        top = self.canvas.canvasy(0)
        bottom = top + self.canvas.winfo_height()
        margin = self.card_h
        for n in self._names:
            f = self._cards[n]["frame"]
            y, h = f.winfo_y(), f.winfo_height()
            if y + h < top - margin or y > bottom + margin:
                continue
            if n in self._pending or n in self._photos:
                continue
            self._submit(n)

    def _submit(self, name, force=False):
        self._pending.add(name)
        self._pool.submit(self._worker, name, self._gen, force)

    def _worker(self, name, gen, force):
        """Thread: pakai cover yang ada, atau unduh kalau online. Hasil dikirim ke main thread."""
        try:
            if force or not self.store.has(name):
                try:
                    if force:
                        self.store.clear_miss(name)
                        self.store.clear_offline()
                    self.store.ensure(name, force=force)
                except OfflineError:
                    if force:
                        self.after(0, lambda: self._say("No internet connection - cover not downloaded.", "warning"))
                except Exception as e:
                    print(f"[WLM] Cover for '{name}' failed: {e}")
            img, has_cover = self._compose(name)
        except Exception as e:
            print(f"[WLM] Cover render for '{name}' failed: {e}")
            img, has_cover = None, False
        try:
            self.after(0, lambda: self._on_cover(name, gen, img, has_cover))
        except Exception:
            pass

    def _on_cover(self, name, gen, img, has_cover):
        try:
            if gen != self._gen or not self.winfo_exists():
                return
            self._pending.discard(name)
            card = self._cards.get(name)
            if not card:
                return
            photo = ImageTk.PhotoImage(img if img is not None else self._blank(name))
            self._photos[name] = photo
            card["img"].config(image=photo)
            if not has_cover and self.store.offline():
                self._retry.add(name)
                self._later("_t_retry", int((OFFLINE_BACKOFF_SECS + 5) * 1000), self._retry_offline)
        except tk.TclError:
            pass

    def _retry_offline(self):
        self._t_retry = None
        names, self._retry = self._retry, set()
        for n in names:
            if n in self._cards and not self.store.has(n):
                self._photos.pop(n, None)
        self._schedule_load()

    def refresh(self, name, force=False):
        """Muat ulang gambar satu kartu (force=True: unduh ulang dari internet)."""
        self._photos.pop(name, None)
        if name in self._cards and name not in self._pending:
            self._submit(name, force=force)

    def fetch_missing(self):
        """Coba unduh cover untuk semua game yang belum punya (mis. setelah tersambung internet).
        Return jumlah game yang dicoba."""
        self.store.clear_offline()
        todo = [n for n in self._names if not self.store.has(n)]
        for n in todo:
            self.store.clear_miss(n)
            self._photos.pop(n, None)
            if n not in self._pending:
                self._submit(n)
        return len(todo)
