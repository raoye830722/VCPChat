# -*- coding: utf-8 -*-
"""macOS CGEvent 后台输入通道。

替代 Windows PostMessage 后台链路：事件定向投递到目标 pid
（CGEventPostToPid），不劫持物理键鼠焦点，属半后台——部分应用
（尤其游戏/特权进程）对合成事件有信任门槛，前台模式
（pyautogui，CGEventPostToPSN 全局）始终可用作兜底。

坐标语义：全局 display 坐标（points），与 AXPosition /
kCGWindowBounds 同系，无 client 区换算。
"""

import time

_BUTTON_MAP = {
    "left": (1, 2),      # kCGEventLeftMouseDown/Up
    "right": (3, 4),     # kCGEventRightMouseDown/Up
    "middle": (5, 6),    # kCGEventOtherMouseDown/Up
}

_MOUSE_BUTTON = {"left": 0, "right": 1, "middle": 2}


def post_click(pid, x, y, button="left", clicks=1):
    """向 pid 投递鼠标点击（含移动事件先行，对齐上游 hot/hover 语义）。"""
    import Quartz

    pid = int(pid)
    x, y = int(x), int(y)
    down_type, up_type = _BUTTON_MAP.get(button) or _BUTTON_MAP["left"]
    mouse_button = _MOUSE_BUTTON.get(button, 0)

    def _event(event_type):
        ev = Quartz.CGEventCreateMouseEvent(
            None, event_type, Quartz.CGPointMake(x, y), mouse_button
        )
        Quartz.CGEventPostToPid(pid, ev)

    # 先投移动事件，让目标更新 hover 态（对齐 _post_click 的 WM_MOUSEMOVE）
    _event(Quartz.kCGEventMouseMoved)

    for i in range(int(clicks)):
        _event(down_type)
        time.sleep(0.02)
        _event(up_type)
        if i < int(clicks) - 1:
            time.sleep(0.05)
    return None


def post_scroll(pid, x, y, direction="down", amount=3):
    """向 pid 投递滚轮事件。amount 为滚动格数（上游语义：行数）。"""
    import Quartz

    pid = int(pid)
    x, y = int(x), int(y)
    delta = -int(amount) if str(direction).lower() == "down" else int(amount)
    # 移动到目标位置再滚（滚轮事件作用于光标下的视图）
    move = Quartz.CGEventCreateMouseEvent(
        None, Quartz.kCGEventMouseMoved, Quartz.CGPointMake(x, y), 0
    )
    Quartz.CGEventPostToPid(pid, move)
    ev = Quartz.CGEventCreateScrollWheelEvent(
        None, Quartz.kCGScrollEventUnitLine, 1, delta
    )
    Quartz.CGEventPostToPid(pid, ev)
    return None


def post_text(pid, text):
    """向 pid 投递 Unicode 文本（逐事件，支持全 Unicode/中文）。

    CGEventKeyboardSetUnicodeString 单事件上限约 20 字符，
    按块拆分投递；每块之间投 Return 无必要，块间仅 sleep。
    """
    import Quartz

    pid = int(pid)
    text = str(text or "")
    if not text:
        return None
    chunk_size = 20
    for start in range(0, len(text), chunk_size):
        chunk = text[start:start + chunk_size]
        ev = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
        Quartz.CGEventKeyboardSetUnicodeString(ev, len(chunk), chunk)
        Quartz.CGEventPostToPid(pid, ev)
        up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
        Quartz.CGEventKeyboardSetUnicodeString(up, len(chunk), chunk)
        Quartz.CGEventPostToPid(pid, up)
        time.sleep(0.01)
    return None


def post_key(pid, key):
    """向 pid 投递单个虚拟键码事件（Enter/Tab/Esc 等控制键）。

    key 接受 'enter'/'return'/'tab'/'escape'/'delete' 或直接数字键码。
    """
    import Quartz

    keycodes = {
        "enter": 36, "return": 36, "tab": 48, "escape": 53, "esc": 53,
        "delete": 51, "backspace": 51, "space": 49, "up": 126,
        "down": 125, "left": 123, "right": 124,
    }
    normalized = str(key or "").lower()
    code = keycodes.get(normalized)
    if code is None:
        try:
            code = int(normalized)
        except ValueError:
            raise ValueError(f"未知键名: {key}")
    pid = int(pid)
    down = Quartz.CGEventCreateKeyboardEvent(None, code, True)
    Quartz.CGEventPostToPid(pid, down)
    up = Quartz.CGEventCreateKeyboardEvent(None, code, False)
    Quartz.CGEventPostToPid(pid, up)
    return None


def pid_of_hwnd(hwnd):
    """CGWindowID → pid（供主调度从窗口选择器取投递目标）。"""
    import Quartz

    cid = int(hwnd)
    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID) or []
    for info in infos:
        if int(info.get("kCGWindowNumber", -1)) == cid:
            return int(info.get("kCGWindowOwnerPID", -1))
    return -1