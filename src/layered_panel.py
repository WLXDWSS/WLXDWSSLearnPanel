"""分层窗口面板：WS_EX_LAYERED + PIL 渲染 + UpdateLayeredWindow。

为什么不用 Tk：Tk 的 -transparentcolor 会让文字边缘的抗锯齿像素变成彩色毛边。
这里把整块面板用 FreeType 渲染成 RGBA 位图（alpha 正确），再整窗贴到分层窗口上，
背景 alpha=0（透出桌面）、文字 alpha=255（实体清晰），没有毛边。

对外接口（全部在主线程调用）：
    p = LayeredPanel(x, y, on_close, on_settings, on_pick)
    p.set_state(items=[("你好", "hello; hi"), ...], comp="nihao", theme=THEMES["light"],
                size_cfg=..., layout="single"|"double", numbers=True,
                en_ratio=0.9, transparent=False, waiting_text="等待输入")
    p.move(x, y)
    p.destroy()

线程模型：窗口与消息循环跑在独立线程；主线程通过 PostMessage(WM_USER_REFRESH) 触发重绘。
"""
import ctypes
import ctypes.wintypes as wt
import threading
import time

from PIL import Image, ImageDraw, ImageFont

u32 = ctypes.WinDLL("user32", use_last_error=True)
g32 = ctypes.WinDLL("gdi32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WM_PAINT = 0x000F
WM_DESTROY = 0x0002
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEMOVE = 0x0200
WM_USER_REFRESH = 0x0400 + 1
ULW_ALPHA = 0x02
AC_SRC_ALPHA = 0x01

LRESULT = ctypes.c_longlong


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


# ---- 64 位下 GDI 句柄仍是 32 位值，ctypes 设 c_void_p 会符号扩展，统一 mask ----
def _h(v):
    return (v or 0) & 0xFFFFFFFF


u32.CreateWindowExW.restype = wt.HWND
u32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
u32.UpdateLayeredWindow.restype = wt.BOOL
u32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT),
                                    ctypes.POINTER(wt.SIZE), wt.HDC, ctypes.POINTER(wt.POINT),
                                    wt.COLORREF, ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
u32.DefWindowProcW.restype = LRESULT
u32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, ctypes.c_ulonglong, ctypes.c_longlong]

# 光标 API：类光标为 NULL 时 DefWindowProc 不设置光标，会继承系统上一次的
# 忙碌光标（鼠标移上来显示沙漏）。这里显式用箭头光标。
u32.LoadCursorW.restype = wt.HANDLE
u32.LoadCursorW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
u32.SetCursor.restype = wt.HANDLE
u32.SetCursor.argtypes = [wt.HANDLE]
IDC_ARROW = 32512
u32.GetDC.restype = ctypes.c_void_p
u32.GetDC.argtypes = [wt.HWND]
u32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
u32.SetCapture.argtypes = [wt.HWND]
u32.ReleaseCapture.argtypes = []
u32.InvalidateRect.argtypes = [wt.HWND, ctypes.c_void_p, wt.BOOL]
u32.PostMessageW.restype = wt.BOOL
u32.PostMessageW.argtypes = [wt.HWND, wt.UINT, ctypes.c_ulonglong, ctypes.c_longlong]
u32.DestroyWindow.argtypes = [wt.HWND]
u32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
u32.GetWindowLongW.restype = ctypes.c_longlong
u32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
u32.SetWindowPos.restype = wt.BOOL
u32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, wt.UINT]
u32.RegisterClassW.restype = wt.ATOM
u32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
k32.GetModuleHandleW.restype = wt.HMODULE
k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
g32.CreateCompatibleDC.restype = ctypes.c_void_p
g32.CreateCompatibleDC.argtypes = [wt.HDC]
g32.CreateDIBSection.restype = ctypes.c_void_p
g32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT,
                                 ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
g32.SelectObject.restype = ctypes.c_void_p
g32.SelectObject.argtypes = [wt.HDC, wt.HANDLE]
g32.DeleteObject.argtypes = [wt.HANDLE]
g32.DeleteDC.argtypes = [wt.HDC]

_FONT_CACHE = {}


def _font(path, size, bold=False):
    key = (path, size, bold)
    if key not in _FONT_CACHE:
        try:
            _FONT_CACHE[key] = ImageFont.truetype(path, size)
        except Exception:
            _FONT_CACHE[key] = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", size)
    return _FONT_CACHE[key]


ZH_FONT = "C:/Windows/Fonts/msyhbd.ttc"
EN_FONT = "C:/Windows/Fonts/msyh.ttc"
SYMBOL_FONT = "C:/Windows/Fonts/seguisym.ttf"   # 微软雅黑没有 ⚙✕ 字形，符号用 Segoe UI Symbol


def _rgb(hexstr):
    hexstr = hexstr.lstrip("#")
    return tuple(int(hexstr[i:i + 2], 16) for i in (0, 2, 4))


class LayeredPanel:
    _cls_registered = False

    def __init__(self, x, y, on_close=None, on_settings=None, on_pick=None,
                 on_move=None, scale=1.0):
        self.scale = scale
        self.on_close = on_close
        self.on_settings = on_settings
        self.on_pick = on_pick
        self.on_move = on_move
        self.x, self.y = x, y
        self._lock = threading.Lock()
        self._state = dict(items=[], comp="", waiting=True, theme=None,
                           size_cfg=dict(zh=15, en=13, row_s=36, row_d=58),
                           layout="single", numbers=True, en_ratio=0.9, transparent=False)
        self._w = self._h = 1
        self._hwnd = None
        self._ready = threading.Event()
        self._drag = None
        self._hit_regions = []          # [(x, y, w, h, action)]
        self._proc = WNDPROC(self._wndproc)
        cls = "WLXDWSSPanelWnd"
        if not LayeredPanel._cls_registered:
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._proc
            wc.hInstance = k32.GetModuleHandleW(None)
            wc.lpszClassName = cls
            wc.hCursor = u32.LoadCursorW(None, IDC_ARROW)   # 显式箭头，避免继承沙漏
            u32.RegisterClassW(ctypes.byref(wc))
            LayeredPanel._cls_registered = True
        self._cls = cls
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(5)

    # ---------- 线程 ----------
    def _run(self):
        inst = k32.GetModuleHandleW(None)
        self._hwnd = u32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW,
            self._cls, "", WS_POPUP, int(self.x), int(self.y), 600, 420,
            None, None, inst, None)
        u32.ShowWindow(self._hwnd, 5)
        self._ready.set()
        self._render_and_blit()
        msg = wt.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == 0x0020:                       # WM_SETCURSOR：强制箭头，别继承沙漏
            u32.SetCursor(u32.LoadCursorW(None, IDC_ARROW))
            return 1
        if msg == WM_PAINT:
            self._render_and_blit()
            return 0
        if msg == WM_USER_REFRESH:
            self._render_and_blit()
            return 0
        if msg == WM_LBUTTONDOWN:
            mx, my = self._unpack_xy(lparam)
            action = self._hit_test(mx, my)
            if action == "close" and self.on_close:
                self.on_close()
            elif action == "settings" and self.on_settings:
                self.on_settings()
            elif isinstance(action, str) and action.startswith("pick:") and self.on_pick:
                self.on_pick(int(action.split(":")[1]))
            elif action is None:
                self._drag = (mx, my, self.x, self.y)
                u32.SetCapture(hwnd)
            return 0
        if msg == WM_MOUSEMOVE and self._drag:
            mx, my = self._unpack_xy(lparam)
            sx, sy, ox, oy = self._drag
            self.move(self.x + (mx - sx), self.y + (my - sy))
            return 0
        if msg == WM_LBUTTONUP and self._drag:
            self._drag = None
            u32.ReleaseCapture()
            return 0
        if msg == WM_DESTROY:
            u32.PostQuitMessage(0)
            return 0
        return u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    @staticmethod
    def _unpack_xy(lparam):
        v = lparam & 0xFFFFFFFF
        x = ctypes.c_short(v & 0xFFFF).value
        y = ctypes.c_short((v >> 16) & 0xFFFF).value
        return x, y

    def _hit_test(self, mx, my):
        for (x, y, w, h, act) in self._hit_regions:
            if x <= mx <= x + w and y <= my <= y + h:
                return act
        return None

    # ---------- 对外 ----------
    def set_state(self, **kw):
        with self._lock:
            self._state.update(kw)
        if self._hwnd:
            u32.PostMessageW(self._hwnd, WM_USER_REFRESH, 0, 0)

    def move(self, x, y):
        self.x, self.y = int(x), int(y)
        if self._hwnd:
            # 分层窗口的内容会随窗口移动，拖动时只需要挪位置，不必每帧重绘
            u32.SetWindowPos(self._hwnd, 0, self.x, self.y, 0, 0, 0x0001 | 0x0004 | 0x0010)
        if self.on_move:
            try:
                self.on_move(self.x, self.y)
            except Exception:
                pass

    def destroy(self):
        if self._hwnd:
            u32.DestroyWindow(self._hwnd)

    # ---------- 渲染 ----------
    def _layout(self):
        s = self.scale
        st = self._state
        theme = st["theme"] or {}
        size_cfg = st["size_cfg"]
        items = st["items"]
        numbers = st["numbers"]
        layout = st["layout"]
        ratio = st["en_ratio"]

        zh_pt = size_cfg["zh"]
        px = lambda pt: max(8, int(round(pt * 1.3333 * s)))
        zh_px = px(zh_pt)
        en_px = max(8, int(round(zh_px * ratio)))
        f_zh = _font(ZH_FONT, zh_px)
        f_en = _font(EN_FONT, en_px)

        W = int(round(450 * s))
        bar_h = int(round(30 * s))
        pad_x = int(round(10 * s))

        # 汉字列宽：本屏最长候选
        zh_w = 0
        for zh, _ in items:
            b = f_zh.getbbox(zh or "")
            zh_w = max(zh_w, b[2] - b[0])
        num_w = 0
        if numbers and items:
            num_w = int(round(18 * s))

        if layout == "double":
            row_h = int(round(size_cfg["row_d"] * s))
        else:
            row_h = max(int(round(size_cfg["row_s"] * s)), zh_px + int(round(10 * s)))

        rows = len(items) if items else 1
        H = bar_h + rows * row_h + int(round(8 * s))
        return dict(W=W, H=H, bar_h=bar_h, row_h=row_h, pad_x=pad_x,
                    zh_w=zh_w, num_w=num_w, zh_px=zh_px, en_px=en_px,
                    f_zh=f_zh, f_en=f_en, layout=layout, numbers=numbers)

    def _render_image(self):
        with self._lock:
            st = dict(self._state)
        theme = st["theme"] or {}
        L = self._layout()
        W, H = L["W"], L["H"]
        s = self.scale
        transparent = st.get("transparent", False)

        # 透明模式下给文字加深色描边，保证任何桌面背景上都看得清（字色仍跟随主题）
        stroke_w = max(1, int(round(s))) if transparent else 0
        stroke_f = (0, 0, 0)

        def dt(xy, text, font, fill, anchor=None, stroke=None):
            kw = dict(font=font, fill=fill + (255,))
            if anchor:
                kw["anchor"] = anchor
            sw = stroke_w if stroke is None else stroke
            if sw:
                kw["stroke_width"] = sw
                kw["stroke_fill"] = stroke_f + (255,)
            d.text(xy, text, **kw)

        bg = _rgb(theme.get("bg", "#ffffff"))
        border = _rgb(theme.get("border", "#d6dbe3"))
        zh_c = _rgb(theme.get("zh", "#1f2937"))
        en_c = _rgb(theme.get("en", "#2b3540"))
        sub_c = _rgb(theme.get("sub", "#8b93a1"))
        accent = _rgb(theme.get("accent", "#5b8def"))

        # 注意：分层窗口 alpha=0 的像素会让点击穿透到下层（按钮/拖动就点不到了），
        # 所以透明模式的背景用 alpha=1：视觉上几乎全透明，但仍可命中鼠标。
        alpha_bg = 1 if transparent else 255
        img = Image.new("RGBA", (W, H), bg + (alpha_bg,))
        d = ImageDraw.Draw(img)

        # 边框：透明模式下必须留一圈，否则看不出窗口边界
        d.rectangle([0, 0, W - 1, H - 1], outline=border + (255,), width=max(1, int(round(s))))

        pad = L["pad_x"]
        bar_h = L["bar_h"]

        # ---- 顶栏 ----
        comp = st.get("comp") or ""
        if comp:
            # 键入串用强调色、稍大字号；描边比正文细一档，避免把字盖成黑色
            thin = max(1, int(round(s)) - 1) if transparent else 0
            dt((pad, int(bar_h * 0.18)), comp, _font(EN_FONT, L["en_px"] + 2),
               accent, stroke=thin)

        # 右上角两个按钮（符号用 Segoe UI Symbol，微软雅黑会渲染成方块）
        btn = int(round(22 * s))
        gap = int(round(6 * s))
        cy = int(round((bar_h - btn) / 2))
        close_x = W - pad - btn
        gear_x = close_x - btn - gap
        dt((gear_x + btn / 2, bar_h / 2), "⚙", _font(SYMBOL_FONT, int(btn * 0.85)), sub_c, anchor="mm")
        dt((close_x + btn / 2, bar_h / 2), "✕", _font(SYMBOL_FONT, int(btn * 0.85)), sub_c, anchor="mm")
        self._hit_regions = [
            (gear_x, cy, btn, btn, "settings"),
            (close_x, cy, btn, btn, "close"),
        ]

        # ---- 候选 ----
        items = st["items"]
        if not items:
            dt((pad, bar_h + int(10 * s)), st.get("waiting_text", "等待输入"), L["f_en"], sub_c)
            return img

        for i, (zh, en) in enumerate(items):
            y = bar_h + i * L["row_h"]
            self._hit_regions.append((0, y, W, L["row_h"], "pick:%d" % i))
            x = pad
            if L["numbers"]:
                dt((x, y + int(4 * s)), str(i + 1), L["f_en"], sub_c)
                x += L["num_w"]
            if st["layout"] == "double":
                dt((x, y + int(2 * s)), zh, L["f_zh"], zh_c)
                dt((x, y + L["zh_px"] + int(6 * s)), en, L["f_en"], en_c)
            else:
                dt((x, y + int((L["row_h"] - L["zh_px"]) / 2) - int(2 * s)), zh, L["f_zh"], zh_c)
                dt((x + L["zh_w"] + int(14 * s), y + int((L["row_h"] - L["en_px"]) / 2)),
                   en, L["f_en"], en_c)
        return img

    def _render_and_blit(self):
        img = self._render_image()
        w, h = img.size
        self._w, self._h = w, h
        # 候选变多导致窗口变高/变宽时，把它挪回屏幕内，避免底部候选跑到屏幕外
        sw, sh = u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)
        nx, ny = self.x, self.y
        if nx + w > sw:
            nx = max(0, sw - w)
        if ny + h > sh:
            ny = max(0, sh - h)
        if (nx, ny) != (self.x, self.y):
            self.x, self.y = nx, ny
            if self._hwnd:
                u32.SetWindowPos(self._hwnd, 0, nx, ny, 0, 0, 0x0001 | 0x0004 | 0x0010)
            if self.on_move:
                try:
                    self.on_move(nx, ny)
                except Exception:
                    pass
        self._blit(img, w, h)

    def _blit_only(self):
        # 拖动时只挪位置，重用上一帧内容（简单起见重新渲染一次，成本可接受）
        img = self._render_image()
        self._blit(img, img.size[0], img.size[1])

    def _blit(self, img, w, h):
        # UpdateLayeredWindow 的 AC_SRC_ALPHA 要求「预乘 alpha」：RGB 必须乘以 alpha。
        # 不预乘的话 alpha=0 的像素会按原始 RGB 显示（白底面板透不出桌面）。
        # PIL 的 "RGBa" 模式不能直接 tobytes("BGRA")，用 ImageChops.multiply 手动预乘。
        from PIL import ImageChops
        alpha = img.getchannel("A")
        r, g, b = img.convert("RGB").split()
        pre = Image.merge("RGBA", (ImageChops.multiply(r, alpha),
                                   ImageChops.multiply(g, alpha),
                                   ImageChops.multiply(b, alpha),
                                   alpha))
        bgra = pre.tobytes("raw", "BGRA")
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        bits = ctypes.c_void_p()
        screen = _h(u32.GetDC(None))
        mem = _h(g32.CreateCompatibleDC(screen))
        hbmp = _h(g32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), 0, 0))
        try:
            g32.SelectObject(mem, hbmp)
            if bits.value:
                ctypes.memmove(bits.value, bgra, len(bgra))
            pt_dst = wt.POINT(int(self.x), int(self.y))
            size = wt.SIZE(w, h)
            pt_src = wt.POINT(0, 0)
            bf = BLENDFUNCTION(0, 0, 255, AC_SRC_ALPHA)
            u32.UpdateLayeredWindow(self._hwnd, screen, ctypes.byref(pt_dst),
                                    ctypes.byref(size), mem, ctypes.byref(pt_src),
                                    0, ctypes.byref(bf), ULW_ALPHA)
        finally:
            if hbmp:
                g32.DeleteObject(hbmp)
            if mem:
                g32.DeleteDC(mem)
            if screen:
                u32.ReleaseDC(None, screen)
