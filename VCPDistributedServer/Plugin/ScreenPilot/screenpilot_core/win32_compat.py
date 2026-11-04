# -*- coding: utf-8 -*-
"""darwin 平台的 win32 兼容垫片。

在 screen_pilot.py 入口经 sys.modules 注入后，上游代码中的
import win32gui / win32ui / win32con / win32api / win32process /
dxcam / uiautomation 全部拿到功能等价的 darwin 实现，
上游模块与主调度零改动。

hwnd 位承载 CGWindowID。
"""

import os
import time

# ============================================================
# win32con：常量字典
# ============================================================

WIN32CON = {
    "SW_RESTORE": 9,
    "SW_SHOW": 5,
    "WM_PRINT": 0x317,
    "PRF_CHILDREN": 0x2,
    "PRF_CLIENT": 0x4,
    "PRF_ERASEBKGND": 0x8,
    "SM_XVIRTUALSCREEN": 76,
    "SM_YVIRTUALSCREEN": 77,
    "SM_CXVIRTUALSCREEN": 78,
    "SM_CXSCREEN": 0,
    "SM_CYSCREEN": 1,
    "HWND_DESKTOP": 0,
    "IMAGE_CURSOR": 2,
}

# 注入器按同名约定取 globals()，常量字典补挂标准名
win32con = WIN32CON

# ============================================================
# 内部工具
# ============================================================

def _quartz():
    import Quartz
    return Quartz


def _window_infos(all_windows=False):
    """CGWindowList 信息字典列表（layer==0 普通窗口）。"""
    q = _quartz()
    if all_windows:
        opts = q.kCGWindowListOptionAll
    else:
        opts = (q.kCGWindowListOptionOnScreenOnly
                | q.kCGWindowListExcludeDesktopElements)
    infos = list(q.CGWindowListCopyWindowInfo(opts, q.kCGNullWindowID) or [])
    return [i for i in infos if int(i.get("kCGWindowLayer", 99)) == 0]


def _find_info(cid):
    for info in _window_infos(all_windows=True):
        if int(info.get("kCGWindowNumber", -1)) == int(cid):
            return info
    return None


def _bounds_of(info):
    b = info.get("kCGWindowBounds") or {}
    l = int(b.get("X", 0)); t = int(b.get("Y", 0))
    w = int(b.get("Width", 0)); h = int(b.get("Height", 0))
    return l, t, l + w, t + h


def _pid_of(info):
    return int(info.get("kCGWindowOwnerPID", -1))


def _owner_of(info):
    return str(info.get("kCGWindowOwnerName") or "")


def _title_of(info):
    return str(info.get("kCGWindowName") or "") or _owner_of(info)


def _is_on_screen(cid):
    return any(int(i.get("kCGWindowNumber", -1)) == int(cid)
               for i in _window_infos(all_windows=False))


def _raise_invalid(hwnd):
    raise OSError(f"无效窗口: CGWindowID:{hwnd}（窗口可能已关闭）")


# ============================================================
# win32gui 功能等价
# ============================================================

class win32gui:
    """命名空间类：静态方法模拟 win32gui 模块接口。"""

    @staticmethod
    def IsWindow(hwnd):
        return _find_info(hwnd) is not None

    @staticmethod
    def IsWindowVisible(hwnd):
        info = _find_info(hwnd)
        return info is not None and _is_on_screen(hwnd)

    @staticmethod
    def IsIconic(hwnd):
        info = _find_info(hwnd)
        if info is None:
            return False
        return not _is_on_screen(hwnd)

    @staticmethod
    def GetWindowText(hwnd):
        info = _find_info(hwnd)
        return _title_of(info) if info else ""

    @staticmethod
    def GetClassName(hwnd):
        # macOS 无窗口类概念，返回进程名作为稳定替代标识
        info = _find_info(hwnd)
        return _owner_of(info) if info else ""

    @staticmethod
    def GetWindowRect(hwnd):
        info = (info if (info := _find_info(hwnd)) is not None
                else _raise_invalid(hwnd))
        return _bounds_of(info)

    @staticmethod
    def GetWindowDC(hwnd):
        return 0  # darwin 无 DC 概念，占位句柄

    @staticmethod
    def ReleaseDC(hwnd, dc):
        return None

    @staticmethod
    def DeleteObject(obj):
        return None

    @staticmethod
    def ShowWindow(hwnd, cmd):
        """SW_RESTORE：激活最小化窗口（AppleScript 激活进程）。"""
        if cmd != 9:  # 只处理 SW_RESTORE
            return None
        info = _find_info(hwnd)
        if info is None:
            return None
        _activate_pid(_pid_of(info))
        return None

    @staticmethod
    def SetForegroundWindow(hwnd):
        info = _find_info(hwnd)
        if info is None:
            return None
        _activate_pid(_pid_of(info))
        return None

    @staticmethod
    def ScreenToClient(hwnd, point):
        """darwin 窗口坐标即全局坐标（CG 坐标系无 client 区别），
        恒等映射。"""
        return (int(point[0]), int(point[1]))

    @staticmethod
    def ChildWindowFromPointEx(hwnd, point, flags=0):
        """darwin 无子窗口概念，命中检测退化为：点在窗口 bounds 内
        返回 hwnd，否则返回 0（桌面）。"""
        info = _find_info(hwnd)
        if info is None:
            return 0
        l, t, r, b = _bounds_of(info)
        x, y = int(point[0]), int(point[1])
        if l <= x < r and t <= y < b:
            return int(hwnd)
        return 0

    @staticmethod
    def EnumWindows(callback, extra=None):
        """按 win32 语义枚举顶层窗口：callback(hwnd, extra)，
        返回 False 中止。"""
        for info in _window_infos(all_windows=False):
            cid = int(info.get("kCGWindowNumber", -1))
            if cid < 0:
                continue
            if callback(cid, extra) is False:
                break
        return None

    @staticmethod
    def SendMessage(hwnd, msg, wparam=0, lparam=0):
        return 0  # darwin 无 WM_ 消息泵，占位

    @staticmethod
    def GetWindowThreadProcessId(hwnd):
        """返回 (thread_id, pid)；darwin 无线程句柄概念，thread=pid。"""
        info = _find_info(hwnd)
        if info is None:
            return (0, 0)
        return (_pid_of(info), _pid_of(info))

    @staticmethod
    def GetDC(hwnd):
        return 0

    @staticmethod
    def SetProcessDPIAware():
        return None  # macOS Retina 由系统管理，无需声明


# ============================================================
# win32api / win32process / win32ui
# ============================================================

class win32api:
    @staticmethod
    def OpenProcess(*a, **k):
        raise OSError("darwin 无 OpenProcess；进程名经 psutil 获取")

    @staticmethod
    def CloseHandle(h):
        return None


class win32process:
    @staticmethod
    def GetWindowThreadProcessId(hwnd):
        info = _find_info(hwnd)
        if info is None:
            return (0, 0)
        pid = _pid_of(info)
        return (pid, pid)

    @staticmethod
    def GetModuleFileNameEx(handle, module):
        return ""  # 进程路径经 psutil，此路径上游仅作 fallback


class win32ui:
    """GDI 设备上下文：darwin 无对应概念。

    上游 _capture_window_gdi 的 DC/Bitmap 链路在 darwin 走
    capture_window_single 替代（主调度 darwin 分支已接线），
    此类仅保证 import 与属性访问不炸。"""

    class CreateDCFromHandle:
        def __init__(self, handle):
            self._handle = handle

        def CreateCompatibleDC(self):
            return _FakeDC()

    @staticmethod
    def CreateBitmap():
        return _FakeBitmap()


class _FakeDC:
    def __init__(self):
        self.GetSafeHdc = lambda: 0
        self.CreateCompatibleDC = lambda: None
        self.SelectObject = lambda *a: None
        self.DeleteDC = lambda: None
        self.BitBlt = lambda *a, **k: None


class _FakeBitmap:
    def __init__(self):
        self.GetHandle = lambda: 0
        self.CreateCompatibleBitmap = lambda *a: None
        self.GetBitmapBits = lambda: b""


def _activate_pid(pid):
    """激活目标进程主窗口（osascript 前台化）。"""
    import subprocess

    try:
        import psutil
        name = psutil.Process(int(pid)).name()
    except Exception:
        name = None
    app_name = (name or "").removesuffix(".app")
    if app_name:
        subprocess.run(
            ["osascript", "-e",
             f'tell application id "com.apple.{app_name}" to activate'],
            capture_output=True, timeout=3,
        )
        return
    subprocess.run(
        ["osascript", "-e",
         f'tell application "System Events" to set frontmost of '
         f'(first process whose unix id is {int(pid)}) to true'],
        capture_output=True, timeout=3,
    )


# ============================================================
# dxcam：区域截图直通 Quartz
# ============================================================

class _DXCamCamera:
    def __init__(self):
        pass

    def grab(self, region=None):
        from . import capture_darwin
        if region:
            rect = {"x": region[0], "y": region[1],
                    "width": region[2] - region[0],
                    "height": region[3] - region[1]}
            image = capture_darwin._region_image(rect)
        else:
            image, _, _ = capture_darwin.capture_full_virtual_desktop()
        import numpy as np
        return np.asarray(image)[:, :, ::-1]  # RGB→BGR 对齐 dxcam 语义

    def stop(self):
        return None


class dxcam:
    @staticmethod
    def create(output_idx=0, output_color="RGB"):
        return _DXCamCamera()


# ============================================================
# uiautomation：AX 树桥接（T4 完整实现，此处先立骨架）
# ============================================================

class uiautomation:
    """AX 树桥接骨架。GetRootControl/GetControlFromWindow 等由
    uia_darwin 模块在 darwin 上提供完整实现后接入。"""

    @staticmethod
    def GetRootControl(*a, **k):
        from . import uia_darwin
        return uia_darwin.get_root_control()

    @staticmethod
    def GetControlFromWindow(hwnd, *a, **k):
        from . import uia_darwin
        return uia_darwin.get_control_from_window(int(hwnd))


# ============================================================
# 注入器
# ============================================================

INJECTED = ("win32gui", "win32ui", "win32con", "win32api",
            "win32process", "dxcam", "uiautomation")


def install():
    """将兼容模块注入 sys.modules。幂等。

    每个注入名生成独立 ModuleType：调用方 import win32gui 后以
    win32gui.IsWindow 形式访问——IsWindow 必须是模块级属性。
    类的静态方法经 getattr 平铺为模块函数；win32con 字典逐键平铺。
    """
    import sys
    import types

    for name in INJECTED:
        source = globals()[name]
        module = types.ModuleType(f"win32_compat.{name}")
        if isinstance(source, dict):
            for key, value in source.items():
                setattr(module, key, value)
        else:
            for key in dir(source):
                if key.startswith("_"):
                    continue
                setattr(module, key, getattr(source, key))
        sys.modules[name] = module
    return None


def get_virtual_screen_rect_patched():
    """geometry.get_virtual_screen_rect 的 darwin 替代。"""
    from . import capture_darwin
    return capture_darwin.get_virtual_screen_rect()


def patch_geometry():
    """把 geometry.get_virtual_screen_rect 替换为 darwin 实现。"""
    from . import geometry as geo
    geo.get_virtual_screen_rect = get_virtual_screen_rect_patched
    return None