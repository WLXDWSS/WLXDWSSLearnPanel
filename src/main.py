# -*- coding: utf-8 -*-
"""候选英文学习窗（兼容版）—— 搜狗 / 微软拼音，用户手动选择输入法。

设计：不做自动判断（搜狗守护进程会抵抗 TSF 检测，不可靠），
由用户在设置里选当前使用的输入法，只启动对应的那一条读取路径：
  - 搜狗      ：hookdll 注入 + UDP 收候选（轻量，主线程 poll 即可）
  - 微软拼音  ：后台线程 UIA 读候选窗（重活不进主线程，所以不卡）

主面板用分层窗口（LayeredPanel，无毛边），设置面板用 Tk Toplevel。
"""
import ctypes
import ctypes.wintypes as wt
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cedict import CEDict
from mspy_reader import MspyReader, KeyboardTracker
from sogou_reader import SogouReader
from layered_panel import LayeredPanel

APP_NAME = "WLXDWSSLearnPanel"
BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
DICT_PATH = os.path.join(ROOT, "dict", "cedict.txt")
DATA = os.path.join(os.environ["LOCALAPPDATA"], APP_NAME)
SETTINGS_PATH = os.path.join(DATA, "settings.json")
CACHE_PATH = os.path.join(DATA, "cache.json")

CJK = re.compile(r"[\u4e00-\u9fff]")

# ---------------- 外观 ----------------
THEMES = {
    "dark": dict(name="暗夜", bg="#121418", bar="#1a1e24", border="#3a424e",
                 zh="#f5f7fa", en="#dbe4f0", sub="#9aa5b3", accent="#7fb3ff",
                 sel="#262c35", online="#8fd4a8"),
    "light": dict(name="浅色", bg="#ffffff", bar="#f1f4f8", border="#b9c3d0",
                  zh="#0b0e12", en="#1e2732", sub="#54606d", accent="#0b5cc4",
                  sel="#e3eaf3", online="#1b6b3a"),
    "paper": dict(name="纸黄", bg="#faf3e3", bar="#f0e7d3", border="#d3c5a5",
                  zh="#2b2519", en="#463d2c", sub="#6d6250", accent="#8a5c12",
                  sel="#eae0c8", online="#2b6335"),
    "contrast": dict(name="高对比", bg="#000000", bar="#0a0a0a", border="#4a4a4a",
                     zh="#ffffff", en="#ffd84d", sub="#c8c8c8", accent="#00e0ff",
                     sel="#1c1c1c", online="#7dffb0"),
}
THEME_ORDER = ["dark", "light", "paper", "contrast"]

SIZES = {
    "compact": dict(name="紧凑", zh=14, row_s=34, row_d=54),
    "normal": dict(name="标准", zh=15, row_s=38, row_d=60),
    "large": dict(name="放大", zh=17, row_s=42, row_d=68),
}

RATIOS = [(0.80, "0.8×"), (0.90, "0.9×"), (1.00, "1.0×"), (1.15, "1.15×")]

# 输入法：用户手动选择，程序不自动判断
IME_NAMES = {"mspy": "微软拼音", "sogou": "搜狗"}
IME_ORDER = ["mspy", "sogou"]

DEFAULTS = dict(theme="light", layout="single", size="normal", follow_system=False,
                online="off", baidu_appid="", baidu_key="",
                en_ratio=0.90, numbers=True, autostart=True, transparent=False,
                ime="mspy")


# ---------- DPAPI：用当前 Windows 用户凭据加密密钥，只有本用户能解密 ----------
u32 = ctypes.WinDLL("user32", use_last_error=True)
_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(_DATA_BLOB), wt.LPCWSTR, ctypes.POINTER(_DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_DATA_BLOB)]
_crypt32.CryptProtectData.restype = wt.BOOL
_crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(wt.LPWSTR), ctypes.POINTER(_DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_DATA_BLOB)]
_crypt32.CryptUnprotectData.restype = wt.BOOL
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]


def _dpapi_blob(data):
    buf = ctypes.create_string_buffer(data, max(1, len(data)))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def dpapi_encrypt(text):
    """加密并返回 base64；失败返回 None。"""
    if not text:
        return None
    try:
        blob_in, _buf = _dpapi_blob(text.encode("utf-8"))
        blob_out = _DATA_BLOB()
        if not _crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                         ctypes.byref(blob_out)):
            return None
        try:
            return base64.b64encode(ctypes.string_at(blob_out.pbData, blob_out.cbData)).decode("ascii")
        finally:
            _kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


def dpapi_decrypt(b64):
    """解密 base64 密文；失败返回 None。"""
    if not b64:
        return None
    try:
        blob_in, _buf = _dpapi_blob(base64.b64decode(b64))
        blob_out = _DATA_BLOB()
        if not _crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                           ctypes.byref(blob_out)):
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData).decode("utf-8")
        finally:
            _kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


def load_settings():
    s = dict(DEFAULTS)
    try:
        d = json.load(open(SETTINGS_PATH, "r", encoding="utf-8"))
        if isinstance(d, dict):
            for k in DEFAULTS:
                if k in d:
                    s[k] = d[k]
            # 新格式：密钥加密存储，解密回内存
            enc = d.get("baidu_key_enc")
            if enc:
                dec = dpapi_decrypt(enc)
                if dec is not None:
                    s["baidu_key"] = dec
    except Exception:
        pass
    return s


def save_json(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        json.dump(obj, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def system_is_light():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        v, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
        return bool(v)
    except Exception:
        return True


def set_autostart(on):
    frozen = getattr(sys, "frozen", False)
    cmd = ('"%s"' % sys.executable if frozen
           else '"%s" "%s"' % (sys.executable, os.path.join(BASE, "main.py")))
    flags = 0x08000000
    try:
        if on:
            subprocess.run(["reg", "add", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
                            "/v", APP_NAME, "/t", "REG_SZ", "/d", cmd, "/f"],
                           capture_output=True, creationflags=flags, timeout=15)
        else:
            subprocess.run(["reg", "delete", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
                            "/v", APP_NAME, "/f"],
                           capture_output=True, creationflags=flags, timeout=15)
    except Exception:
        return False
    return True


def get_scale():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop\WindowMetrics")
        dpi, _ = winreg.QueryValueEx(k, "AppliedDPI")
        if dpi:
            return float(dpi) / 96.0
    except Exception:
        pass
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def fmt_gloss(s):
    s = re.sub(r"\([^)]*\)", "", s).strip()
    s = re.sub(r"\[[^\]]*\]", "", s).strip()
    return s


class Toggle(tk.Canvas):
    """iOS 风格开关。"""

    def __init__(self, parent, value, command, theme, scale=1.0):
        self.tw = int(round(42 * scale))
        self.th = int(round(23 * scale))
        super().__init__(parent, width=self.tw, height=self.th, bg=theme["bg"],
                         highlightthickness=0, bd=0, cursor="hand2")
        self.t = theme
        self.on = bool(value)
        self.command = command
        self.pos = 1.0 if self.on else 0.0
        self._draw(self.pos)
        self.bind("<Button-1>", self._click)

    def _draw(self, p):
        self.delete("all")
        w, h = self.tw, self.th
        r = h / 2.0
        track = self.t["accent"] if self.on else self.t["border"]
        self.create_oval(0, 0, h, h, fill=track, outline="")
        self.create_oval(w - h, 0, w, h, fill=track, outline="")
        self.create_rectangle(r, 0, w - r, h, fill=track, outline="")
        pad = h * 0.13
        d = h - pad * 2
        x = pad + p * (w - d - pad * 2)
        self.create_oval(x, pad, x + d, pad + d, fill="#ffffff", outline="")

    def _click(self, _e):
        self.on = not self.on
        start = self.pos
        target = 1.0 if self.on else 0.0
        steps = 6

        def step(i):
            p = start + (target - start) * (i / float(steps))
            self.pos = p
            self._draw(p)
            if i < steps:
                self.after(13, lambda: step(i + 1))

        step(1)
        if self.command:
            self.after(13 * steps + 10, lambda: self.command(self.on))


class App:
    def __init__(self, root):
        self.root = root
        self.dict = CEDict(DICT_PATH)
        self.settings = load_settings()
        # 旧版明文密钥自动迁移为 DPAPI 密文（启动时检测到明文就立即加密落盘）
        if self.settings.get("baidu_key"):
            try:
                self._save_settings()
            except Exception:
                pass
        self.sp = None
        self.last = None
        self._online_timer = None
        self._online_pending = []
        self._inflight = set()
        self._cache_dirty = False
        try:
            self.cache = json.load(open(CACHE_PATH, "r", encoding="utf-8"))
        except Exception:
            self.cache = {}

        if self.settings.get("follow_system"):
            self.settings["theme"] = "light" if system_is_light() else "dark"

        self.t = THEMES[self.settings["theme"]]
        self.sz = SIZES[self.settings["size"]]

        root.withdraw()

        # 候选读取：两条路径按用户选择启动其中一条（不自动判断输入法）
        self.tracker = KeyboardTracker()
        self.mspy = MspyReader(self.tracker)
        self.sogou = SogouReader()
        self._reader = None
        self._reader_ime = None

        self.pos_file = os.path.join(DATA, "pos.json")
        x, y = root.winfo_screenwidth() - 500, 80
        try:
            d = json.load(open(self.pos_file))
            x, y = d["x"], d["y"]
        except Exception:
            pass

        self.lp = LayeredPanel(
            x, y, scale=get_scale(),
            on_close=self._quit,
            on_settings=self._toggle_settings,
            on_move=self._save_pos)

        self.tracker.start()
        self._start_reader(self.settings.get("ime", "mspy"))
        root.after(80, self._tick)

    # ---------- 输入法选择（手动，不自动判断） ----------
    def _start_reader(self, ime):
        """按选择启动一条读取路径（先停旧的）。"""
        if ime not in IME_NAMES:
            ime = "mspy"
        self._stop_reader()
        self._reader_ime = ime
        try:
            if ime == "sogou":
                self.sogou.start()
                self._reader = self.sogou
            else:
                self.mspy.start()
                self._reader = self.mspy
        except Exception:
            self._reader = None

    def _stop_reader(self):
        old = self._reader
        self._reader = None
        if old is None:
            return
        try:
            old.stop()
        except Exception:
            pass
        try:
            self.tracker.clear()      # 换源时清掉旧拼音，避免残留
        except Exception:
            pass

    def _set_ime(self, ime):
        """用户在设置里切换输入法：立即生效、记住选择。"""
        if ime not in IME_NAMES or ime == self._reader_ime:
            return
        self.settings["ime"] = ime
        self._save_settings()
        self._start_reader(ime)
        self.last = None
        self._refresh()

    def _poll_source(self):
        """从当前选中的那条路径取 (拼音, [候选])。非阻塞、微秒级。"""
        r = self._reader
        if r is None:
            return "", []
        try:
            d = r.poll()
        except Exception:
            return "", []
        if not d:
            return "", []
        if isinstance(d, tuple):                 # 微软拼音：poll() -> (comp, items)
            return d[0] or "", list(d[1] or [])
        # 搜狗：poll() -> {"c": 拼音, "i": [候选]}
        # 拼音优先用 KeyboardTracker（输入法处理前的原始键码，和微软路径一致），
        # 钩子没抓到时退回 DLL 读到的组合串
        comp = self.tracker.get() or (d.get("c") or "")
        items = [x for x in (d.get("i", []) or []) if x]
        return comp, items

    def _refresh(self):
        """渲染一次。不注册 after 调度 —— 设置变更等场景调它，不会新增循环链。"""
        comp, items = self._poll_source()
        self._render(comp, items)

    def _tick(self):
        """唯一的调度链：每 80ms 取一次候选并渲染。全是微秒级操作。

        注意：除本函数外，任何地方都不要调用 _tick()，否则会新增一条
        独立的 after 循环链（多次调用 = 多条链并行 = CPU 飙升卡死）。
        """
        try:
            self._refresh()
        except Exception:
            pass
        # 开始打字（出现新候选）→ 自动收起设置窗口
        try:
            if self.sp and self.last and self.last[1]:
                self._close_settings()
        except Exception:
            pass
        # 点击设置窗口外部 → 自动收起
        self._maybe_close_settings_on_click()
        self.root.after(80, self._tick)

    # ---------- 渲染 ----------
    def _render(self, comp, items):
        items = [x for x in items if CJK.search(x) and not x.startswith("(")]
        items = items[:7]
        key = (comp, tuple(items))
        if key == self.last:
            return
        self.last = key

        pairs = []
        need_online = []
        for word in items:
            local = self.dict.gloss(word)
            if local:
                pairs.append((word, fmt_gloss(local)))
            elif self.settings["online"] != "off":
                c = self.cache.get(word)
                if c:
                    pairs.append((word, "≈ " + c))
                else:
                    pairs.append((word, "—"))
                    need_online.append(word)
            else:
                pairs.append((word, "—"))
        self._push_layered(comp, pairs)
        if need_online:
            self._schedule_online(need_online)

    def _push_layered(self, comp, pairs):
        lp = getattr(self, "lp", None)
        if not lp:
            return
        try:
            lp.set_state(
                items=list(pairs), comp=comp or "", theme=self.t,
                size_cfg=self.sz,
                layout=self.settings.get("layout", "single"),
                numbers=bool(self.settings.get("numbers", True)),
                en_ratio=float(self.settings.get("en_ratio", 0.9)),
                transparent=bool(self.settings.get("transparent", False)),
                waiting_text="等待输入…（当前：%s）" % IME_NAMES.get(self._reader_ime or "", ""))
        except Exception:
            pass

    # ---------- 在线翻译 ----------
    def _schedule_online(self, words):
        self._online_pending = [w for w in words if w not in self._inflight]
        if not self._online_pending:
            return
        if self._online_timer:
            try:
                self.root.after_cancel(self._online_timer)
            except Exception:
                pass
        self._online_timer = self.root.after(350, self._kick_online)

    def _kick_online(self):
        self._online_timer = None
        words = list(self._online_pending)
        self._online_pending = []
        if words:
            threading.Thread(target=self._online_worker, args=(words,), daemon=True).start()

    def _translate_mymemory(self, word):
        url = ("https://api.mymemory.translated.net/get?q=%s&langpair=zh-CN|en"
               % urllib.parse.quote(word))
        req = urllib.request.Request(url, headers={"User-Agent": APP_NAME + "/1.0"})
        with urllib.request.urlopen(req, timeout=2.5) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        txt = (d.get("responseData") or {}).get("translatedText") or ""
        if not txt or "MYMEMORY WARNING" in txt.upper():
            return None
        return txt.strip()

    def _translate(self, word):
        p = self.settings.get("online", "off")
        if p == "mymemory":
            return self._translate_mymemory(word)

        if p == "baidu":
            appid = self.settings.get("baidu_appid", "").strip()
            key = self.settings.get("baidu_key", "").strip()
            if not appid or not key:
                return self._translate_mymemory(word)     # 没填 key 就退到 MyMemory
            salt = str(int(time.time() * 1000))
            sign = hashlib.md5((appid + word + salt + key).encode("utf-8")).hexdigest()
            url = ("https://fanyi-api.baidu.com/api/trans/vip/translate"
                   "?q=%s&from=zh&to=en&appid=%s&salt=%s&sign=%s"
                   % (urllib.parse.quote(word), appid, salt, sign))
            with urllib.request.urlopen(url, timeout=2.5) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            res = d.get("trans_result") or []
            if res:
                dst = res[0].get("dst", "").strip()
                # 百度对极短文本常原样返回，视为没翻 → 兜底 MyMemory
                if dst and dst != word:
                    return dst
            return self._translate_mymemory(word)
        return None

    def _online_worker(self, words):
        for w in words:
            if w in self.cache or w in self._inflight:
                continue
            self._inflight.add(w)
            try:
                res = self._translate(w)
            except Exception:
                res = None
            self._inflight.discard(w)
            if res and res != w:
                self.cache[w] = res
                self._cache_dirty = True
                self.root.after(0, self._after_online)
            time.sleep(0.25)

    def _after_online(self):
        if self._cache_dirty:
            self._cache_dirty = False
            save_json(CACHE_PATH, self.cache)
        self.last = None
        self._refresh()          # 只渲染，不注册新循环链

    # ---------- 设置面板 ----------
    def _close_settings(self):
        """统一关闭设置窗口（打字 / 移动 / 点击外部时自动收起）。"""
        sp = getattr(self, "sp", None)
        if sp:
            try:
                sp.destroy()
            except Exception:
                pass
        self.sp = None

    def _maybe_close_settings_on_click(self):
        """鼠标左键点在设置窗口外部 → 自动收起（点齿轮除外，交给 _toggle_settings）。"""
        sp = getattr(self, "sp", None)
        if not sp:
            return
        try:
            if not sp.winfo_exists():
                self.sp = None
                return
            # 左键当前是否按下（bit15）
            if not (u32.GetAsyncKeyState(0x01) & 0x8000):
                return
            pt = wt.POINT()
            u32.GetCursorPos(ctypes.byref(pt))
            sx, sy = sp.winfo_rootx(), sp.winfo_rooty()
            sw, sh = sp.winfo_width(), sp.winfo_height()
            if sx <= pt.x <= sx + sw and sy <= pt.y <= sy + sh:
                return                       # 点在设置窗口内
            # 点在主面板齿轮上 → 不自动关（否则会关掉又被 toggle 重开）
            lp = getattr(self, "lp", None)
            if lp and getattr(lp, "_hwnd", None):
                cx, cy = pt.x - lp.x, pt.y - lp.y
                for (rx, ry, rw, rh, act) in getattr(lp, "_hit_regions", []):
                    if act == "settings" and rx <= cx <= rx + rw and ry <= cy <= ry + rh:
                        return
            self._close_settings()
        except Exception:
            pass

    def _toggle_settings(self):
        try:
            alive = bool(self.sp) and bool(self.sp.winfo_exists())
        except Exception:
            alive = False
        if alive:
            try:
                self.sp.destroy()
            except Exception:
                pass
            self.sp = None
            return
        self._open_settings()

    def _open_settings(self):
        for w in list(self.root.winfo_children()):
            if isinstance(w, tk.Toplevel):
                try:
                    w.destroy()
                except Exception:
                    pass
        self.sp = None
        t = self.t
        top = tk.Toplevel(self.root)
        top.attributes("-alpha", 0.0)   # 全透明，杜绝无边框窗口先按 (0,0) 闪一帧
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg=t["bg"], highlightthickness=1, highlightbackground=t["border"])
        self.sp = top

        wrap = tk.Frame(top, bg=t["bg"], padx=12, pady=10)
        wrap.pack(fill="both", expand=True)

        def title(txt):
            tk.Label(wrap, text=txt, bg=t["bg"], fg=t["sub"],
                     font=("Microsoft YaHei UI", 8), anchor="w").pack(fill="x", pady=(8, 3))

        def toggles(options, current, on_pick):
            bar = tk.Frame(wrap, bg=t["bg"])
            bar.pack(fill="x")
            for key, label in options:
                on = (key == current)
                b = tk.Label(bar, text=label, bg=t["sel"] if on else t["bar"],
                             fg=t["zh"] if on else t["sub"],
                             font=("Microsoft YaHei UI", 9), padx=9, pady=4, cursor="hand2")
                b.pack(side="left", padx=(0, 5))
                b.bind("<Button-1>", lambda e, k=key: on_pick(k))

        title("主题")
        tb = tk.Frame(wrap, bg=t["bg"])
        tb.pack(fill="x")
        for key in THEME_ORDER:
            th = THEMES[key]
            on = (key == self.settings["theme"])
            cell = tk.Frame(tb, bg=th["bg"], width=30, height=24, cursor="hand2",
                            highlightthickness=2,
                            highlightbackground=th["accent"] if on else t["border"])
            cell.pack(side="left", padx=(0, 6))
            cell.pack_propagate(False)
            lab = tk.Label(cell, text="文", bg=th["bg"], fg=th["zh"],
                           font=("Microsoft YaHei UI", 9))
            lab.place(relx=.5, rely=.5, anchor="center")
            lab.bind("<Button-1>", lambda e, k=key: self._set_setting("theme", k))
            cell.bind("<Button-1>", lambda e, k=key: self._set_setting("theme", k))

        title("布局")
        toggles([("single", "单行"), ("double", "双行")],
                self.settings["layout"], lambda k: self._set_setting("layout", k))

        title("字号")
        toggles([(k, SIZES[k]["name"]) for k in ("compact", "normal", "large")],
                self.settings["size"], lambda k: self._set_setting("size", k))

        title("英文字号（相对中文）")
        toggles([(r, lbl) for r, lbl in RATIOS],
                float(self.settings.get("en_ratio", 0.90)),
                lambda k: self._set_setting("en_ratio", float(k)))

        title("输入法（手动选择，程序不自动判断）")
        ib = tk.Frame(wrap, bg=t["bg"])
        ib.pack(fill="x")
        mb = tk.Menubutton(ib, text=IME_NAMES.get(self._reader_ime or "mspy", "微软拼音") + "  ▼",
                           bg=t["bar"], fg=t["zh"], relief="flat",
                           font=("Microsoft YaHei UI", 9), padx=10, pady=4,
                           cursor="hand2", anchor="w", width=12)
        menu = tk.Menu(mb, tearoff=False, bg=t["bar"], fg=t["zh"],
                       activebackground=t["sel"], activeforeground=t["zh"],
                       font=("Microsoft YaHei UI", 9))
        for k in IME_ORDER:
            def _pick(kk=k, btn=mb):
                self._set_ime(kk)
                btn.config(text=IME_NAMES[kk] + "  ▼")
            menu.add_command(label=IME_NAMES[k], command=_pick)
        mb.config(menu=menu)
        mb.pack(side="left")

        title("在线翻译（本地查不到时才查）")
        toggles([("off", "关闭"), ("mymemory", "MyMemory"), ("baidu", "百度")],
                self.settings["online"], lambda k: self._set_online(k))

        if self.settings["online"] == "baidu":
            f = tk.Frame(wrap, bg=t["bg"])
            f.pack(fill="x", pady=(6, 0))
            tk.Label(f, text="appid", bg=t["bg"], fg=t["sub"],
                     font=("Segoe UI", 8)).pack(side="left")
            nv = tk.StringVar(value=self.settings.get("baidu_appid", ""))
            kv = tk.StringVar(value=self.settings.get("baidu_key", ""))
            e1 = tk.Entry(f, textvariable=nv, bg=t["bar"], fg=t["zh"], insertbackground=t["zh"],
                          relief="flat", font=("Segoe UI", 9), width=9)
            e1.pack(side="left", padx=(4, 8))
            tk.Label(f, text="key", bg=t["bg"], fg=t["sub"],
                     font=("Segoe UI", 8)).pack(side="left")
            e2 = tk.Entry(f, textvariable=kv, bg=t["bar"], fg=t["zh"], insertbackground=t["zh"],
                          relief="flat", font=("Segoe UI", 9), width=9, show="•")
            e2.pack(side="left", padx=4)

            # FocusOut 在窗口 destroy 时不触发（实测），改为 StringVar 实时落盘：
            # 每输入一个字符就写入 settings.json，关窗口/崩溃都不丢 key。
            def _sync_creds(*_a):
                self.settings["baidu_appid"] = nv.get().strip()
                self.settings["baidu_key"] = kv.get().strip()
                self._save_settings()
            nv.trace_add("write", _sync_creds)
            kv.trace_add("write", _sync_creds)

        title("其它")

        def switch_row(label, value, on_change):
            r = tk.Frame(wrap, bg=t["bg"])
            r.pack(fill="x", pady=3)
            tk.Label(r, text=label, bg=t["bg"], fg=t["zh"],
                     font=("Microsoft YaHei UI", 9), anchor="w").pack(side="left")
            Toggle(r, value, on_change, t, get_scale()).pack(side="right")

        switch_row("显示候选编号", self.settings.get("numbers", True),
                   lambda v: self._set_setting("numbers", bool(v)))
        switch_row("开机自启", self.settings.get("autostart", True),
                   lambda v: self._set_autostart(bool(v)))
        switch_row("跟随系统深色", self.settings.get("follow_system", False),
                   lambda v: self._set_setting("follow_system", bool(v)))
        switch_row("透明面板（透出桌面）", self.settings.get("transparent", False),
                   lambda v: self._set_setting("transparent", bool(v)))

        top.update_idletasks()
        self._place_settings()
        top.update()                   # 强制几何落定（先定位，再恢复不透明）
        top.attributes("-alpha", 1.0)  # 恢复不透明（此时已在正确位置）
        top.update()

    def _place_settings(self):
        if not (self.sp and self.sp.winfo_exists()):
            return
        self.sp.update_idletasks()
        lp = getattr(self, "lp", None)
        if lp:
            px, py, pw = lp.x, lp.y, getattr(lp, "_w", 0) or 380
        else:
            try:
                px = self.root.winfo_x()
                py = self.root.winfo_y()
                pw = self.root.winfo_width() or 380
            except Exception:
                return
        x = px - 226
        y = py
        if x < 4:
            x = px + pw + 8
        self.sp.geometry(f"+{x}+{y}")

    def _set_setting(self, key, value):
        self.settings[key] = value
        self.t = THEMES[self.settings["theme"]]
        self.sz = SIZES[self.settings["size"]]
        self._save_settings()
        self.last = None
        self._refresh()          # 只渲染，不注册新循环链

    def _set_autostart(self, on):
        self.settings["autostart"] = bool(on)
        set_autostart(bool(on))
        self._save_settings()

    def _set_online(self, value):
        self.settings["online"] = value
        self._save_settings()
        self._open_settings()

    def _save_settings(self):
        data = dict(self.settings)
        key = data.pop("baidu_key", "")        # 不写明文密钥
        data.pop("baidu_key_enc", None)
        if key:
            enc = dpapi_encrypt(key)
            if enc:
                data["baidu_key_enc"] = enc
            else:
                data["baidu_key"] = key        # 加密失败才退回明文（保底，不丢配置）
        save_json(SETTINGS_PATH, data)

    def _save_pos(self, x, y):
        save_json(self.pos_file, {"x": x, "y": y})
        # 拖动主窗口 → 自动收起设置窗口
        self._close_settings()

    def _quit(self):
        os._exit(0)


def main():
    enable_dpi_awareness()
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wt.HANDLE
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    k32.CreateMutexW(None, False, APP_NAME + "Mutex")
    if ctypes.get_last_error() == 183:
        print("已在运行", flush=True)
        sys.exit(0)

    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
