# -*- coding: utf-8 -*-
"""macOS Accessibility (AX) 树桥接。

为 win32_compat 注入的 uiautomation 垫片提供实现：AXNode 包装
AXUIElementRef，暴露上游 uia.py 消费的属性/方法面（ControlTypeName
角色映射、GetChildren、BoundingRectangle RECT 仿真、五类 Pattern、
GetRuntimeId），上游 walk/resolve/action 逻辑零改动。

坐标语义：AXPosition/AXSize 为全局 display 坐标（points，左上原点），
与 CGWindowBounds、CGEvent 同坐标系，内部链路一致。

Electron 应用默认不暴露完整 AX 树，ControlFromHandle 时设置
AXManualAccessibility / AXEnhancedUserInterface 激活。
"""

import threading

from .errors import ScreenPilotError

_LOCK = threading.Lock()

# ============================================================
# pyobjc AX API
# ============================================================

def _ax():
    from ApplicationServices import (
        AXUIElementCreateApplication,
        AXUIElementCopyAttributeValue,
        AXUIElementCopyAttributeNames,
        AXUIElementCopyActionNames,
        AXUIElementPerformAction,
        AXUIElementSetAttributeValue,
    )
    return {
        "create_app": AXUIElementCreateApplication,
        "copy_attr": AXUIElementCopyAttributeValue,
        "attr_names": AXUIElementCopyAttributeNames,
        "action_names": AXUIElementCopyActionNames,
        "perform": AXUIElementPerformAction,
        "set_attr": AXUIElementSetAttributeValue,
    }


def _attr(ref, name):
    """读取 AX 属性；不存在返回 None。"""
    try:
        err, value = _ax()["copy_attr"](ref, name, None)
        if err != 0:
            return None
        return value
    except Exception:
        return None


def _attr_names(ref):
    try:
        err, value = _ax()["attr_names"](ref, None)
        return list(value) if err == 0 and value else []
    except Exception:
        return []


def _action_names(ref):
    try:
        err, value = _ax()["action_names"](ref, None)
        return list(value) if err == 0 and value else []
    except Exception:
        return []


def _perform(ref, action):
    try:
        return _ax()["perform"](ref, action)
    except Exception:
        return -1


def _set_attr(ref, name, value):
    try:
        return _ax()["set_attr"](ref, name, value)
    except Exception:
        return -1


# ============================================================
# 角色映射：AXRole → uiautomation ControlTypeName
# ============================================================

_ROLE_MAP = {
    "AXButton": "ButtonControl",
    "AXTextField": "EditControl",
    "AXTextArea": "EditControl",
    "AXSearchField": "EditControl",
    "AXStaticText": "TextControl",
    "AXCheckBox": "CheckBoxControl",
    "AXRadioButton": "RadioButtonControl",
    "AXMenuButton": "MenuItemControl",
    "AXMenuItem": "MenuItemControl",
    "AXMenuBarItem": "MenuItemControl",
    "AXPopUpButton": "ComboBoxControl",
    "AXComboBox": "ComboBoxControl",
    "AXPopUp": "ComboBoxControl",
    "AXLink": "HyperlinkControl",
    "AXImage": "ImageControl",
    "AXGroup": "GroupControl",
    "AXSlider": "SliderControl",
    "AXScrollBar": "ScrollBarControl",
    "AXMenuBar": "MenuBarControl",
    "AXMenu": "MenuControl",
    "AXToolBar": "ToolBarControl",
    "AXTabGroup": "TabItemControl",
    "AXList": "ListControl",
    "AXListItem": "ListItemControl",
    "AXTable": "TableControl",
    "AXRow": "ListItemControl",
    "AXTree": "TreeControl",
    "AXTreeItem": "TreeItemControl",
    "AXOutline": "ListControl",
    "AXWindow": "WindowControl",
    "AXSplitGroup": "PaneControl",
    "AXScrollArea": "PaneControl",
    "AXProgressIndicator": "ProgressBarControl",
    "AXValueIndicator": "ProgressBarControl",
    "AXPopover": "PaneControl",
    "AXSheet": "PaneControl",
    "AXIncrementor": "SpinnerControl",
    "AXSwitch": "CheckBoxControl",
    "AXDisclosureTriangle": "CheckBoxControl",
    "AXHeading": "TextControl",
    "AXLabel": "TextControl",
}


# ============================================================
# RECT 仿真：上游 element_info 消费 rect.left/.top 属性与
# rect.width()/.height() 方法
# ============================================================

class _Rect:
    def __init__(self, left, top, width, height):
        self.left = int(left)
        self.top = int(top)
        self._w = int(width)
        self._h = int(height)
        self.right = self.left + self._w
        self.bottom = self.top + self._h

    def width(self):
        return self._w

    def height(self):
        return self._h


def _axvalue_to_tuple(value):
    """AXValue 封装对象 → (x, y) / (w, h) 元组。

    pyobjc 绑定形态（实测背书）：AXValueGetValue(value, type, None)
    的 out 参数必须传 None，数据直接从返回值携带——传结构体会
    ValueError。兼容已解包的 tuple/list 形态。
    """
    if value is None:
        return None
    if isinstance(value, (tuple, list)):
        return tuple(value)
    from ApplicationServices import (
        AXValueGetValue, AXValueGetType,
        kAXValueCGPointType, kAXValueCGSizeType, AXValueGetTypeID,
    )
    from CoreFoundation import CFGetTypeID

    try:
        if CFGetTypeID(value) != AXValueGetTypeID():
            return None
        vtype = AXValueGetType(value)
        if vtype == kAXValueCGPointType:
            ok, out = AXValueGetValue(value, kAXValueCGPointType, None)
            return (out.x, out.y) if ok else None
        if vtype == kAXValueCGSizeType:
            ok, out = AXValueGetValue(value, kAXValueCGSizeType, None)
            return (out.width, out.height) if ok else None
    except Exception:
        pass
    try:
        return tuple(value)
    except Exception:
        return None




def _rect_of(ref):
    pos = _axvalue_to_tuple(_attr(ref, "AXPosition"))
    size = _axvalue_to_tuple(_attr(ref, "AXSize"))
    if not pos or not size:
        return None
    try:
        x, y = float(pos[0]), float(pos[1])
        w, h = float(size[0]), float(size[1])
        if w <= 0 or h <= 0:
            return None
        return _Rect(x, y, w, h)
    except Exception:
        return None


# ============================================================
# Pattern 仿真
# ============================================================

class _InvokePattern:
    def __init__(self, ref, action):
        self._ref = ref
        self._action = action

    def Invoke(self):
        if _perform(self._ref, self._action) != 0:
            raise RuntimeError(f"AX action {self._action} 执行失败")


class _LegacyPattern:
    def __init__(self, ref, actions):
        self._ref = ref
        self._actions = actions

    def DoDefaultAction(self):
        if self._actions:
            _perform(self._ref, self._actions[0])


class _ValuePattern:
    def __init__(self, ref):
        self._ref = ref

    @property
    def Value(self):
        return _attr(self._ref, "AXValue")

    def SetValue(self, value):
        if _set_attr(self._ref, "AXValue", value) != 0:
            raise RuntimeError("AXValue 设置失败（属性可能不可写）")


class _TogglePattern:
    def __init__(self, ref, action):
        self._ref = ref
        self._action = action

    def Toggle(self):
        _perform(self._ref, self._action)


class _ExpandCollapsePattern:
    def __init__(self, ref, expand_action, collapse_action):
        self._ref = ref
        self._expand = expand_action
        self._collapse = collapse_action

    def Expand(self):
        if self._expand:
            _perform(self._ref, self._expand)

    def Collapse(self):
        if self._collapse:
            _perform(self._ref, self._collapse)


class _SelectionItemPattern:
    def __init__(self, ref):
        self._ref = ref

    def Select(self):
        if _set_attr(self._ref, "AXSelected", True) != 0:
            _perform(self._ref, "AXPick")


# ============================================================
# AXNode：上游 uia.py 视角的 control 对象
# ============================================================

class AXNode:
    """包装 AXUIElementRef，属性面与 uiautomation.Control 对齐。"""

    def __init__(self, ref, pid):
        self._ref = ref
        self._pid = int(pid or 0)
        self._actions = None

    # ---- 基础属性 ----

    @property
    def Name(self):
        for key in ("AXTitle", "AXDescription", "AXValue"):
            value = _attr(self._ref, key)
            if isinstance(value, str) and value:
                return value
        return ""

    @property
    def ControlTypeName(self):
        role = _attr(self._ref, "AXRole")
        return _ROLE_MAP.get(role, "CustomControl")

    @property
    def AutomationId(self):
        value = _attr(self._ref, "AXIdentifier")
        return str(value) if value else ""

    @property
    def ClassName(self):
        return str(_attr(self._ref, "AXRole") or "")

    @property
    def FrameworkId(self):
        return "AXManualAccessibility"

    @property
    def ProcessId(self):
        return self._pid

    @property
    def NativeWindowHandle(self):
        return 0

    @property
    def IsEnabled(self):
        return bool(_attr(self._ref, "AXEnabled"))

    @property
    def IsOffscreen(self):
        return _rect_of(self._ref) is None

    @property
    def IsKeyboardFocusable(self):
        return "AXFocused" in _attr_names(self._ref)

    @property
    def HasKeyboardFocus(self):
        return bool(_attr(self._ref, "AXFocused"))

    @property
    def HelpText(self):
        return str(_attr(self._ref, "AXHelp") or "")

    @property
    def AccessKey(self):
        return ""

    @property
    def AcceleratorKey(self):
        return ""

    @property
    def BoundingRectangle(self):
        return _rect_of(self._ref)

    # ---- 树与身份 ----

    def GetChildren(self):
        children = _attr(self._ref, "AXChildren") or []
        return [AXNode(child, self._pid) for child in children]

    def GetRuntimeId(self):
        rect = _rect_of(self._ref)
        role = _attr(self._ref, "AXRole") or ""
        title = self.Name
        if rect:
            pos_key = f"{int(rect.left)},{int(rect.top)},{rect.width()},{rect.height()}"
        else:
            pos_key = "none"
        digest = hash((self._pid, role, title, pos_key))
        return [self._pid, abs(digest) % 0x7FFFFFFF]

    def SetFocus(self):
        _set_attr(self._ref, "AXFocused", True)
        _perform(self._ref, "AXRaise")

    # ---- Pattern 面 ----

    def _ensure_actions(self):
        if self._actions is None:
            self._actions = _action_names(self._ref)
        return self._actions

    def GetInvokePattern(self):
        actions = self._ensure_actions()
        for candidate in ("AXPress", "AXConfirm", "AXPick", "AXOpen"):
            if candidate in actions:
                return _InvokePattern(self._ref, candidate)
        return None

    def GetLegacyIAccessiblePattern(self):
        actions = self._ensure_actions()
        if actions:
            return _LegacyPattern(self._ref, actions)
        return None

    def GetValuePattern(self):
        names = _attr_names(self._ref)
        if "AXValue" in names:
            settable = _attr(self._ref, "AXValue") is not None
            # 可写性由 set 实测决定，这里仅要求属性存在且为值型角色
            role = _attr(self._ref, "AXRole") or ""
            if role in ("AXTextField", "AXTextArea", "AXSearchField",
                        "AXComboBox", "AXSlider", "AXIncrementor"):
                return _ValuePattern(self._ref)
        return None

    def GetTogglePattern(self):
        role = _attr(self._ref, "AXRole")
        if role in ("AXCheckBox", "AXSwitch", "AXDisclosureTriangle"):
            actions = self._ensure_actions()
            action = "AXPress" if "AXPress" in actions else (
                actions[0] if actions else None)
            if action:
                return _TogglePattern(self._ref, action)
        return None

    def GetExpandCollapsePattern(self):
        actions = self._ensure_actions()
        expand = next((a for a in ("AXExpand", "AXDisclose", "AXOpen")
                       if a in actions), None)
        collapse = next((a for a in ("AXCollapse", "AXClose")
                         if a in actions), None)
        if expand or collapse:
            return _ExpandCollapsePattern(self._ref, expand, collapse)
        return None

    def GetSelectionItemPattern(self):
        names = _attr_names(self._ref)
        if "AXSelected" in names or "AXPick" in self._ensure_actions():
            return _SelectionItemPattern(self._ref)
        return None


# ============================================================
# 窗口解析：CGWindowID → AX 根节点
# ============================================================

_ACTIVATED_PIDS = set()


def _activate_ax_for_app(app_ref, pid=None):
    """激活 Electron/Chromium 应用的 AX 树（否则只有稀疏骨架）。

    AXManualAccessibility 对 Chromium>=91 生效但树展开有秒级延迟；
    AXEnhancedUserInterface 需持续持有引用。首次激活后短轮询等待
    AXWebArea 出现，超时放弃（非 Electron 应用无此需求）。
    """
    key = int(pid or 0)
    if key and key in _ACTIVATED_PIDS:
        return
    _set_attr(app_ref, "AXManualAccessibility", True)
    _set_attr(app_ref, "AXEnhancedUserInterface", True)
    if key:
        _ACTIVATED_PIDS.add(key)
    # 短轮询等待树展开（最多2.5s，非Electron应用立即通过）
    import time
    for _ in range(5):
        err, wins = _ax()["copy_attr"](app_ref, "AXWindows", None)
        if err == 0 and wins:
            probe = _ax()["copy_attr"](wins[0], "AXChildren", None)
            if probe[0] == 0 and probe[1]:
                deep = _ax()["copy_attr"](probe[1][0], "AXChildren", None)
                if deep[0] == 0 and deep[1]:
                    return
        time.sleep(0.5)


def _window_bounds(cid):
    """从 CGWindowList 拿窗口 title/bounds 用于 AX 匹配。"""
    import Quartz

    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID) or []
    for info in infos:
        if int(info.get("kCGWindowNumber", -1)) == int(cid):
            b = info.get("kCGWindowBounds") or {}
            return {
                "pid": int(info.get("kCGWindowOwnerPID", -1)),
                "title": str(info.get("kCGWindowName") or ""),
                "owner": str(info.get("kCGWindowOwnerName") or ""),
                "bounds": (int(b.get("X", 0)), int(b.get("Y", 0)),
                           int(b.get("Width", 0)), int(b.get("Height", 0))),
            }
    return None


def get_control_from_window(hwnd):
    """CGWindowID → AXNode（窗口级根节点）。

    匹配策略：pid → app AX element → AXWindows 按 title 与 bounds
    双重校验；全不命中退回 app 元素本身（walk 从 app 层开始）。
    """
    meta = _window_bounds(hwnd)
    if meta is None or meta["pid"] < 0:
        raise ScreenPilotError(
            "UIA_WINDOW_UNAVAILABLE",
            f"无法从窗口:{hwnd} 获取 AX 根元素（窗口不存在）。",
            retryable=True,
        )

    with _LOCK:
        app_ref = _ax()["create_app"](meta["pid"])
        _activate_ax_for_app(app_ref, meta["pid"])
        err, windows = _ax()["copy_attr"](app_ref, "AXWindows", None)
        if err != 0 or not windows:
            # 退回 app 层，walk 仍可用（children 含菜单栏等）
            return AXNode(app_ref, meta["pid"])

        best = None
        best_score = -1
        for win_ref in windows:
            score = 0
            title = _attr(win_ref, "AXTitle") or ""
            if meta["title"] and title == meta["title"]:
                score += 100
            rect = _rect_of(win_ref)
            if rect:
                bx, by, bw, bh = meta["bounds"]
                if (abs(rect.left - bx) <= 8 and abs(rect.top - by) <= 8
                        and abs(rect.width() - bw) <= 8
                        and abs(rect.height() - bh) <= 8):
                    score += 100
            if score > best_score:
                best_score = score
                best = win_ref

        if best is None:
            best = windows[0]
        return AXNode(best, meta["pid"])


def get_root_control():
    """系统级根：返回自身进程 app 元素（上游未实际消费，占位）。"""
    import os
    app_ref = _ax()["create_app"](os.getpid())
    return AXNode(app_ref, os.getpid())