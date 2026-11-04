# -*- coding: utf-8 -*-
"""macOS Quartz/CGWindowList 窗口枚举实现。

与 windows.py 接口同构（enumerate_windows / window_info /
resolve_window / process_name_by_hwnd），由 screen_pilot.py 在
darwin 平台通过 sys.modules 注入替换，下游惰性 import 零感知。

hwnd 位承载 CGWindowID（int）。className 在 macOS 无对应概念，
恒为空串，过滤条件中的 class_name 仅参与评分不参与过滤。
"""

from .errors import ScreenPilotError
from .geometry import Rect


def _cg_window_list(all_windows=False):
    """返回 CGWindow 信息字典列表。"""
    import Quartz

    if all_windows:
        opts = Quartz.kCGWindowListOptionAll
    else:
        opts = (Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements)
    return list(Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or [])


def _pid_set_on_screen():
    """当前在屏窗口的 pid 集合，用于最小化判定。"""
    on_screen = set()
    for info in _cg_window_list(all_windows=False):
        pid = info.get("kCGWindowOwnerPID")
        if pid is not None:
            on_screen.add(int(pid))
    return on_screen


def process_name_by_hwnd(hwnd):
    """根据 CGWindowID 获取所属进程名。"""
    try:
        import Quartz

        cid = int(hwnd)
        for info in _cg_window_list(all_windows=True):
            if int(info.get("kCGWindowNumber", -1)) == cid:
                pid = int(info.get("kCGWindowOwnerPID", -1))
                if pid < 0:
                    return None
                try:
                    import psutil
                    return psutil.Process(pid).name()
                except Exception:
                    return info.get("kCGWindowOwnerName")
    except Exception:
        return None
    return None


def window_info(hwnd):
    """读取单个窗口的稳定元数据（字段与 windows.window_info 对齐）。"""
    import Quartz

    cid = int(hwnd)
    target = None
    for info in _cg_window_list(all_windows=True):
        if int(info.get("kCGWindowNumber", -1)) == cid:
            target = info
            break
    if target is None:
        raise ScreenPilotError(
            "INVALID_HWND",
            f"CGWindowID:{cid} 不是有效窗口。",
            retryable=True,
        )

    bounds = target.get("kCGWindowBounds") or {}
    pid = int(target.get("kCGWindowOwnerPID", -1))
    owner = target.get("kCGWindowOwnerName") or f"CGWindow:{cid}"
    title = target.get("kCGWindowName") or owner

    on_screen = any(
        int(i.get("kCGWindowNumber", -1)) == cid
        for i in _cg_window_list(all_windows=False)
    )

    return {
        "hwnd": cid,
        "title": title,
        "processId": pid,
        "processName": owner,
        "className": "",
        "visible": bool(on_screen),
        "minimized": not bool(on_screen),
        "windowRect": Rect.from_ltrb(
            int(bounds.get("X", 0)),
            int(bounds.get("Y", 0)),
            int(bounds.get("X", 0)) + int(bounds.get("Width", 0)),
            int(bounds.get("Y", 0)) + int(bounds.get("Height", 0)),
        ).to_dict(),
    }


def enumerate_windows(*, title=None, process_name=None, pid=None,
                      class_name=None, visible_only=True, min_size=1):
    """枚举并筛选顶层窗口，返回按匹配质量和面积排序的候选。

    评分逻辑与 windows.py 复刻对齐；class_name 无 macOS 对应，
    仅在提供时按 owner 名包含关系给低权重分。
    """
    title_query = str(title or "").casefold()
    process_query = str(process_name or "").casefold()
    class_query = str(class_name or "").casefold()
    target_pid = int(pid) if pid not in (None, "") else None
    candidates = []

    process_pids = set()
    exact_process_pids = set()
    if process_query:
        try:
            import psutil
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    name = str(proc.info.get("name") or "")
                    folded = name.casefold()
                    if process_query in folded:
                        process_pids.add(int(proc.info["pid"]))
                        if folded == process_query:
                            exact_process_pids.add(int(proc.info["pid"]))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except ImportError:
            pass

    on_screen_ids = set()
    if not visible_only:
        for info in _cg_window_list(all_windows=False):
            cid = info.get("kCGWindowNumber")
            if cid is not None:
                on_screen_ids.add(int(cid))

    for info in _cg_window_list(all_windows=not visible_only):
        try:
            cid = int(info.get("kCGWindowNumber", -1))
            if cid < 0:
                continue
            layer = int(info.get("kCGWindowLayer", 99))
            if layer != 0:
                continue  # 只收普通层级窗口，排除菜单栏/Dock/桌面部件

            pid = int(info.get("kCGWindowOwnerPID", -1))
            owner = str(info.get("kCGWindowOwnerName") or "")
            actual_title = str(info.get("kCGWindowName") or "") or owner

            if target_pid is not None and pid != target_pid:
                continue
            if title_query and title_query not in actual_title.casefold():
                continue
            if process_query:
                if process_pids:
                    if pid not in process_pids:
                        continue
                elif process_query not in owner.casefold():
                    continue

            bounds = info.get("kCGWindowBounds") or {}
            left = int(bounds.get("X", 0))
            top = int(bounds.get("Y", 0))
            width = int(bounds.get("Width", 0))
            height = int(bounds.get("Height", 0))
            rect = Rect.from_ltrb(left, top, left + width, top + height)
            if rect.width < min_size or rect.height < min_size:
                continue

            score = 0
            reasons = []
            if title_query:
                if actual_title.casefold() == title_query:
                    score += 100
                    reasons.append("exactTitle")
                else:
                    score += 60
                    reasons.append("containsTitle")
            if process_query:
                if pid in exact_process_pids:
                    score += 100
                    reasons.append("exactProcessName")
                else:
                    score += 60
                    reasons.append("containsProcessName")
            if target_pid is not None:
                score += 100
                reasons.append("exactPid")
            if class_query:
                if class_query in owner.casefold():
                    score += 40
                    reasons.append("containsClassNameLike")

            candidates.append({
                "hwnd": cid,
                "title": actual_title or f"CGWindow:{cid}",
                "processId": pid,
                "processName": owner,
                "className": "",
                "visible": True if visible_only else cid in on_screen_ids,
                "minimized": (not visible_only) and cid not in on_screen_ids,
                "windowRect": rect.to_dict(),
                "area": rect.width * rect.height,
                "score": score,
                "matchReasons": reasons,
            })
        except Exception:
            continue

    candidates.sort(key=lambda item: (item["score"], item["area"]), reverse=True)
    return candidates


def resolve_window(args, *, required=False):
    """按 hwnd → title → processName → pid/className 解析窗口。

    逻辑与 windows.resolve_window 对齐，调用本模块的枚举实现。
    """
    normalized = {str(key).lower(): value for key, value in dict(args or {}).items()}
    hwnd = normalized.get("hwnd")
    if hwnd not in (None, ""):
        selected = window_info(int(hwnd))
        selected["selectionReason"] = "explicitHwnd"
        return selected, [selected]

    title = (
        normalized.get("windowtitle")
        or normalized.get("window_title")
        or normalized.get("title")
    )
    process_name = (
        normalized.get("processname")
        or normalized.get("process_name")
        or normalized.get("process")
    )
    pid = normalized.get("pid") or normalized.get("processid")
    class_name = normalized.get("classname") or normalized.get("class_name")

    if not any(value not in (None, "") for value in (title, process_name, pid, class_name)):
        if required:
            raise ScreenPilotError(
                "WINDOW_SELECTOR_REQUIRED",
                "必须提供 hwnd、windowTitle、processName、pid 或 className。",
            )
        return None, []

    candidates = enumerate_windows(
        title=title,
        process_name=process_name,
        pid=pid,
        class_name=class_name,
        min_size=1,
    )
    if not candidates:
        raise ScreenPilotError(
            "WINDOW_NOT_FOUND",
            "未找到符合条件的窗口。",
            retryable=True,
            details={
                "windowTitle": title,
                "processName": process_name,
                "pid": pid,
                "className": class_name,
            },
        )

    selected = dict(candidates[0])
    selected["selectionReason"] = "+".join(
        selected.get("matchReasons") or ["largestArea"]
    )
    return selected, candidates