# -*- coding: utf-8 -*-
"""微软拼音候选读取：后台线程 UIA 读候选窗 + 全局低级键盘钩子累积拼音。

核心架构（解决卡顿/死机）：
- UIA 读取（约 130ms 的重活）只跑在**独立后台线程**，绝不进 Tk 主线程。
- 主线程通过 poll() 非阻塞取最新结果（微秒级），查词典、触发渲染。
- 打字中每 120ms 读一次；静止时只 300ms 检查一次、不读 UIA（零开销）。

候选窗 UIA 结构（实测）：
  桌面根
    > WindowControl 'Windows 输入体验'   (TextInputHost 的 CoreWindow，根层)
      > MenuControl 'Microsoft 候选项 UI'  (depth=2)
        > ListControl '候选项面板'
          > ListItemControl name='你好'    (候选词，name 即汉字)
          > ButtonControl name='上一页/下一页/...'  (按 ControlType 排除)
"""
import ctypes
import ctypes.wintypes as wt
import threading
import time

import uiautomation as auto

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
VK_BACK = 0x08
VK_SPACE = 0x20
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B

CONTROL_NAMES = {"上一页", "下一页", "表情符号和符号", "搜索建议"}


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD),
                ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class KeyboardTracker:
    """后台线程挂 WH_KEYBOARD_LL，累积拼音字母串，并记录最近按键时间。"""

    def __init__(self):
        self.typed = ""
        self._last_key_time = 0.0
        self._lock = threading.Lock()
        self._hook = None
        self._proc = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]

        HOOKPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)

        def _cb(nCode, wParam, lParam):
            if nCode == 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                ks = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                vk = ks.vkCode
                with self._lock:
                    if 0x41 <= vk <= 0x5A:          # A-Z -> 小写
                        self.typed += chr(vk + 0x20)
                    elif vk == VK_BACK:
                        self.typed = self.typed[:-1]
                    elif vk in (VK_SPACE, VK_RETURN, VK_ESCAPE) or 0x30 <= vk <= 0x39:
                        self.typed = ""              # 选字/取消/结束一轮
                    self._last_key_time = time.time()
            return user32.CallNextHookEx(0, nCode, wParam, lParam)

        self._proc = HOOKPROC(_cb)
        # WH_KEYBOARD_LL 的 hMod 必须为 NULL（回调在本进程内，无需 DLL）
        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        err = ctypes.get_last_error()
        if not self._hook:
            print(f"[KeyboardTracker] SetWindowsHookExW 失败, err={err}", flush=True)

        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def get(self):
        with self._lock:
            return self.typed

    def clear(self):
        with self._lock:
            self.typed = ""

    def active(self, window=2.0):
        """最近 window 秒内是否有按键。"""
        with self._lock:
            return (time.time() - self._last_key_time) < window

    def stale(self, seconds=1.5):
        """typed 非空且超过 seconds 秒无按键 → True（残留键入串，该清空）。"""
        with self._lock:
            return bool(self.typed) and (time.time() - self._last_key_time) > seconds


class MspyReader:
    """后台线程读微软拼音候选窗；主线程 poll() 非阻塞取最新结果。

    静止时**保持**最后一次结果（不清空）——候选窗通常还开着，
    用户只是在思考，清空会误伤。候选窗真正关闭（读到空）时才清空。
    """

    def __init__(self, tracker, timeout=2):
        self.tracker = tracker
        self.timeout = timeout
        auto.SetGlobalSearchTimeout(timeout)
        self._lock = threading.Lock()
        self._latest = ("", [])
        self._had_items = False     # 上次是否有候选：候选消失时要清空，别残留旧拼音
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        """可重入：停止后可再次启动（切换输入法需要反复启停）。"""
        if self._thread is None or not self._thread.is_alive():
            self._running = True
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self):
        self._running = False

    def poll(self):
        """主线程调用：非阻塞取 (拼音, [候选词])，微秒级。"""
        with self._lock:
            return self._latest

    def _loop(self):
        # 后台线程初始化 COM（UIA 是 COM 组件）
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        except Exception:
            pass
        busy_sleep = 0.12     # 打字中：120ms 读一次
        idle_sleep = 0.50     # 有候选但静止：500ms 读一次，仍能及时检测候选窗关闭
        while self._running:
            active = self.tracker.active(window=2.0)
            # 打字中，或上一次有候选（候选窗可能还开着，要持续跟踪到它关闭）才读；
            # 否则不读 UIA（零开销）。
            if active or self._had_items:
                try:
                    items = self._read()
                    comp = self.tracker.get()
                except Exception:
                    items, comp = [], ""
                if items:
                    self._had_items = True
                    with self._lock:
                        self._latest = (comp, items)
                else:
                    # 候选消失（上屏/取消）→ 清空，别残留旧拼音
                    if self._had_items:
                        self._had_items = False
                        self.tracker.clear()
                    with self._lock:
                        self._latest = ("", [])
                time.sleep(busy_sleep if active else idle_sleep)
            else:
                # 完全静止：兜底清空残留键入串（防英文输入时累积拼音）
                if self.tracker.stale(1.5):
                    self.tracker.clear()
                time.sleep(idle_sleep)

    def _read(self):
        """读候选窗，返回 [候选词]；空列表 = 候选窗未开。"""
        try:
            root = auto.GetRootControl()
        except Exception:
            return []

        # 1. 定位宿主窗口（根层顶层窗口）
        host = None
        try:
            for c in root.GetChildren():
                if (c.Name or "") == "Windows 输入体验":
                    host = c
                    break
        except Exception:
            host = None

        # 2. 在宿主子树找候选面板；没找到再回退到全根限深递归
        panel = self._find_panel(host, 0, 3) if host is not None else None
        if panel is None:
            panel = self._find_panel(root, 0, 3)
        if panel is None:
            return []

        items = []
        try:
            for it in panel.GetChildren():
                if it.ControlTypeName != "ListItemControl":
                    continue
                nm = it.Name or ""
                if nm and nm not in CONTROL_NAMES:
                    items.append(nm)
        except Exception:
            pass
        return items

    def _find_panel(self, c, depth, maxdepth):
        if c is None or depth > maxdepth:
            return None
        try:
            kids = c.GetChildren()
        except Exception:
            return None
        for d in kids:
            try:
                nm = d.Name or ""
                ct = d.ControlTypeName
            except Exception:
                continue
            if nm == "候选项面板" and ct == "ListControl":
                return d
            hit = self._find_panel(d, depth + 1, maxdepth)
            if hit:
                return hit
        return None
