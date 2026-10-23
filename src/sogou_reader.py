# -*- coding: utf-8 -*-
"""搜狗路径：注入 hookdll 读 IMM32 候选，UDP 收候选。

复用原版 panel.py 的注入逻辑（CreateRemoteThread LoadLibraryW，32 位走 inject32.exe），
前台检测改用纯轮询（原版用 WinEvent hook，依赖 Tk mainloop）。
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import socket
import subprocess
import threading
import time

APP_NAME = "WLXDWSSLearnPanel"
EVENT_NAME = APP_NAME + "Alive"
PORT = 52380

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
BUILD = os.path.join(ROOT, "build")
DATA = os.path.join(os.environ["LOCALAPPDATA"], APP_NAME)
DLL_PATH = os.path.join(DATA, "hookdll64.dll")
DLL32_PATH = os.path.join(DATA, "hookdll32.dll")
INJ32_PATH = os.path.join(DATA, "inject32.exe")

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32 = ctypes.WinDLL("user32", use_last_error=True)

# 这些进程不注入：面板自己、agent 宿主、资源管理器等
SKIP_EXE = {
    "explorer.exe", "pythonw.exe", "python.exe",
    "wlxdwsslearnpanel.exe", "hermeslearnpanel.exe",
    "searchhost.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe",
}


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD),
                ("hwndActive", wt.HWND), ("hwndFocus", wt.HWND),
                ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND),
                ("hwndMoveSize", wt.HWND), ("hwndCaret", wt.HWND),
                ("rcCaret", wt.RECT)]


u32.GetGUIThreadInfo.restype = wt.BOOL
u32.GetGUIThreadInfo.argtypes = [wt.DWORD, ctypes.POINTER(GUITHREADINFO)]


def focus_pids(hwnd):
    """返回「该注入的 pid」列表，焦点窗口优先。

    WPS 这类双进程应用：前台窗口（OpusApp）在框架进程里，真正接键盘的编辑控件
    （_WwG）在**另一个进程**里。只按前台 pid 注入会注到框架进程，那里既没有
    输入上下文也收不到按键（实测 himc=NULL、零条 WM_KEYDOWN）。
    焦点窗口用 GetGUIThreadInfo(前台线程) 跨进程取到。
    """
    pids = []

    def add(h):
        if not h:
            return
        p = wt.DWORD(0)
        u32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value and p.value not in pids:
            pids.append(p.value)

    if hwnd:
        tid = u32.GetWindowThreadProcessId(hwnd, None)
        if tid:
            gti = GUITHREADINFO()
            gti.cbSize = ctypes.sizeof(GUITHREADINFO)
            if u32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
                add(gti.hwndFocus)      # 真正的编辑控件
        add(hwnd)                       # 框架进程（普通程序两者相同）
    return pids


def ensure_dll():
    os.makedirs(DATA, exist_ok=True)
    pairs = [(os.path.join(BUILD, "hookdll64.dll"), DLL_PATH),
             (os.path.join(BUILD, "hookdll32.dll"), DLL32_PATH),
             (os.path.join(BUILD, "inject32.exe"), INJ32_PATH)]
    for src, dst in pairs:
        if not os.path.exists(src):
            raise SystemExit("找不到 " + src + "，请先编译")
        if os.path.exists(dst) and os.path.getsize(src) == os.path.getsize(dst):
            continue
        try:
            import shutil
            shutil.copy2(src, dst)
        except PermissionError:
            print("[sogou] 无法更新 %s（可能正被已注入的程序占用）。"
                  "请关闭 WPS/记事本等程序后重启面板。" % os.path.basename(dst), flush=True)
    return DLL_PATH


def is32pid(pid):
    k32.IsWow64Process2.restype = wt.BOOL
    k32.IsWow64Process2.argtypes = [wt.HANDLE, ctypes.POINTER(wt.USHORT), ctypes.POINTER(wt.USHORT)]
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    pm, nm = wt.USHORT(0), wt.USHORT(0)
    ok = k32.IsWow64Process2(h, ctypes.byref(pm), ctypes.byref(nm))
    k32.CloseHandle(h)
    return bool(ok) and pm.value == 0x14C


def proc_name(pid):
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(300)
        n = wt.DWORD(300)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return os.path.basename(buf.value)
    except Exception:
        pass
    finally:
        k32.CloseHandle(h)
    return ""


def _inject_once(pid):
    """注入 hookdll 到 pid。返回是否成功。"""
    if is32pid(pid):
        try:
            r = subprocess.run([INJ32_PATH, str(pid), DLL32_PATH],
                               capture_output=True, timeout=8,
                               creationflags=0x08000000)  # CREATE_NO_WINDOW
            return r.returncode == 0
        except Exception:
            return False
    h = k32.OpenProcess(0x1F0FFF, False, pid)
    if not h:
        return False
    try:
        buf = (DLL_PATH + "\0").encode("utf-16-le")
        k32.VirtualAllocEx.restype = ctypes.c_void_p
        k32.VirtualAllocEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD]
        addr = k32.VirtualAllocEx(h, None, len(buf), 0x3000, 0x04)
        if not addr:
            return False
        w = ctypes.c_size_t(0)
        k32.WriteProcessMemory.restype = wt.BOOL
        k32.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        if not k32.WriteProcessMemory(h, addr, buf, len(buf), ctypes.byref(w)):
            return False
        k32.GetModuleHandleW.restype = wt.HMODULE
        k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
        k32.GetProcAddress.restype = ctypes.c_void_p
        k32.GetProcAddress.argtypes = [wt.HMODULE, ctypes.c_char_p]
        load = k32.GetProcAddress(k32.GetModuleHandleW("kernel32.dll"), b"LoadLibraryW")
        k32.CreateRemoteThread.restype = wt.HANDLE
        k32.CreateRemoteThread.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
        th = k32.CreateRemoteThread(h, None, 0, load, addr, 0, None)
        if not th:
            return False
        k32.WaitForSingleObject(th, 3000)
        code = wt.DWORD(0)
        k32.GetExitCodeThread(th, ctypes.byref(code))
        k32.CloseHandle(th)
        return bool(code.value)
    finally:
        k32.CloseHandle(h)


class SogouReader:
    """注入 + UDP 收候选。read() 返回 {'c': 拼音, 'i': [候选]} 或 None。"""

    def __init__(self):
        self.q = queue.Queue()
        self.latest = None
        self.done = set()
        self.tries = {}
        self._hold_event = None
        self._lock = threading.Lock()
        self._running = False
        self._udp_thread = None
        self._inj_thread = None

    def start(self):
        """可重入：停止后可再次启动（切换输入法需要反复启停）。"""
        if self._running:
            return
        self._running = True
        ensure_dll()
        # 存活事件：hookdll 靠它判断面板是否在跑（面板退出后 DLL 自卸载）
        if not self._hold_event:
            k32.CreateEventW.restype = wt.HANDLE
            k32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
            self._hold_event = k32.CreateEventW(None, True, False, EVENT_NAME)
        if self._udp_thread is None or not self._udp_thread.is_alive():
            self._udp_thread = threading.Thread(target=self._udp, daemon=True)
            self._udp_thread.start()
        if self._inj_thread is None or not self._inj_thread.is_alive():
            self._inj_thread = threading.Thread(target=self._inject_loop, daemon=True)
            self._inj_thread.start()

    def stop(self):
        """停收包与注入。已注入宿主里的 hookdll 留着无害（UDP 无人收，包被丢弃）。"""
        self._running = False
        self.latest = None
        self.q = queue.Queue()

    def read(self):
        """非阻塞：返回最新 UDP 候选 dict，无新数据返回上次的。"""
        latest = None
        while True:
            try:
                latest = self.q.get_nowait()
            except queue.Empty:
                break
        if latest is not None:
            self.latest = latest
        return self.latest

    def poll(self):
        """统一接口：与 MspyReader.poll() 同语义，非阻塞返回最新候选或 None。"""
        return self.read()

    def _udp(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except OSError:
            pass
        try:
            s.bind(("127.0.0.1", PORT))
        except OSError:
            # 端口被占（可能原版在跑），仍尝试收
            pass
        s.settimeout(0.3)
        while self._running:
            try:
                data, _ = s.recvfrom(65535)
                self.q.put(json.loads(data.decode("utf-8", "replace")))
            except socket.timeout:
                continue
            except Exception:
                time.sleep(0.1)
        try:
            s.close()
        except Exception:
            pass

    def _inject_loop(self):
        while self._running:
            try:
                fg = u32.GetForegroundWindow()
                if fg:
                    # WPS 是双进程（框架 OpusApp + 文档 _WwG），
                    # 焦点窗口往往在另一个进程里，两个都要注入
                    for pid in focus_pids(fg):
                        self._request(pid)
            except Exception:
                pass
            time.sleep(1.0)

    def _request(self, pid):
        if not pid or pid == os.getpid():
            return
        with self._lock:
            if pid in self.done:
                return
        name = proc_name(pid)
        if name in SKIP_EXE:
            return
        ok = _inject_once(pid)
        with self._lock:
            if ok:
                self.done.add(pid)
            else:
                self.tries[pid] = self.tries.get(pid, 0) + 1
                if self.tries[pid] >= 3:
                    self.done.add(pid)
