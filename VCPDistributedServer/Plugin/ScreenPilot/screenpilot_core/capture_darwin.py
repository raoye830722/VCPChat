# -*- coding: utf-8 -*-
"""macOS Quartz 截图后端。

与 capture.py 接口同构（capture_dxgi / capture_virtual_desktop_region /
capture_window_fallback / capture_full_virtual_desktop /
reset_capture_backend），由 screen_pilot.py 在 darwin 平台注入。
另提供 get_virtual_screen_rect 的 darwin 实现（替代 geometry.py 的
user32.GetSystemMetrics 版本）与 capture_window_single（替代 GDI 截窗）。

坐标系说明：CG display bounds 与 kCGWindowBounds 同为左上原点全局
坐标，与上游 Rect 语义一致，无需翻转。
"""

from .geometry import Rect, clipped_capture_rect


def get_virtual_screen_rect(user32=None):
    """darwin 版虚拟桌面矩形：所有在线 display 的包围盒。

    签名兼容 geometry.get_virtual_screen_rect（user32 参数忽略）。
    """
    import Quartz

    _, ids, _ = Quartz.CGGetOnlineDisplayList(16, None, None)
    count = len(ids) if ids else 0
    if count == 0:
        return Rect(0, 0, 0, 0)
    left = top = None
    right = bottom = None
    for did in ids[:count]:
        b = Quartz.CGDisplayBounds(did)
        l, t = int(b.origin.x), int(b.origin.y)
        r, bt = l + int(b.size.width), t + int(b.size.height)
        left = l if left is None else min(left, l)
        top = t if top is None else min(top, t)
        right = r if right is None else max(right, r)
        bottom = bt if bottom is None else max(bottom, bt)
    return Rect(left, top, right, bottom)


def _region_image(rect):
    """截取全局坐标区域内在屏窗口的合成图像，返回 PIL Image。"""
    import Quartz
    from PIL import Image

    target = Rect.from_mapping(rect)
    cg_rect = Quartz.CGRectMake(
        target.left, target.top, target.width, target.height
    )
    img_ref = Quartz.CGWindowListCreateImage(
        cg_rect,
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
        Quartz.kCGWindowImageDefault,
    )
    if img_ref is None:
        raise RuntimeError("CGWindowListCreateImage 未返回图像。")
    return _cgimage_to_pil(img_ref)


def _cgimage_to_pil(img_ref):
    """CGImage → PIL Image（BGRA 原始字节搬运，避免额外依赖）。"""
    import Quartz
    from PIL import Image

    width = Quartz.CGImageGetWidth(img_ref)
    height = Quartz.CGImageGetHeight(img_ref)
    if width <= 0 or height <= 0:
        raise ValueError("CGImage 尺寸无效。")
    bytes_per_row = Quartz.CGImageGetBytesPerRow(img_ref)
    bpc = Quartz.CGImageGetBitsPerPixel(img_ref) // 8
    data = Quartz.CGDataProviderCopyData(
        Quartz.CGImageGetDataProvider(img_ref)
    )
    buf = bytes(data)
    expected = bytes_per_row * height
    if len(buf) < expected:
        raise RuntimeError(f"CGImage 数据不足: {len(buf)} < {expected}")
    if bytes_per_row == width * bpc:
        row_data = buf
    else:
        row_data = b"".join(
            buf[y * bytes_per_row:y * bytes_per_row + width * bpc]
            for y in range(height)
        )
    if bpc == 4:
        image = Image.frombytes("RGBA", (width, height), row_data)
        image = image.convert("RGB")
    elif bpc == 3:
        image = Image.frombytes("RGB", (width, height), row_data)
    else:
        raise RuntimeError(f"不支持的 CGImage 位深: {bpc * 8}bpp")
    return image


def capture_window_single(hwnd, *, include_behind=False):
    """按 CGWindowID 截取单窗口图像，返回 PIL Image。

    include_behind=True 时包含被遮挡部分（含窗口完整内容）。
    """
    import Quartz

    cid = int(hwnd)
    # kCGWindowListOptionIncludingBehindWindows = 1<<3，pyobjc 未绑定常量时数值兜底
    behind_opt = getattr(Quartz, "kCGWindowListOptionIncludingBehindWindows", 1 << 3)
    options = (behind_opt if include_behind
               else Quartz.kCGWindowListOptionOnScreenOnly)
    img_ref = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull,
        options,
        cid,
        Quartz.kCGWindowImageBoundsIgnoreFraming,
    )
    if img_ref is None:
        raise RuntimeError(f"CGWindowID:{cid} 截图失败（窗口可能已关闭或无画面）。")
    return _cgimage_to_pil(img_ref)


def capture_dxgi(rect, debug_log=None):
    """darwin 版区域截图（接口对齐 capture.capture_dxgi）。"""
    target = Rect.from_mapping(rect)
    if target.width <= 0 or target.height <= 0:
        raise ValueError("截图区域为空。")
    virtual = get_virtual_screen_rect()
    if target.intersect(virtual) != target:
        raise ValueError("截图区域超出虚拟桌面范围。")
    if debug_log:
        debug_log(f"Quartz 区域截图 {target.width}x{target.height}")
    return _region_image(target)


def capture_virtual_desktop_region(rect, debug_log=None):
    """截取虚拟桌面区域，返回 (image, capture_rect, )——注意返回二元组。

    与上游差异：上游返回 (image, capture) 二元组，保持一致。
    """
    target = Rect.from_mapping(rect)
    virtual = get_virtual_screen_rect()
    capture = target.intersect(virtual)
    if capture.width <= 0 or capture.height <= 0:
        raise ValueError("目标区域完全位于虚拟桌面之外。")

    full_image = _region_image(virtual)
    crop_box = (
        capture.left - virtual.left,
        capture.top - virtual.top,
        capture.right - virtual.left,
        capture.bottom - virtual.top,
    )
    image = full_image.crop(crop_box)
    if debug_log:
        debug_log(
            f"虚拟桌面裁剪成功 {image.size}: "
            f"({capture.left},{capture.top})→({capture.right},{capture.bottom})"
        )
    return image, capture


def capture_window_fallback(window_rect, *, prefer_dxgi=True, debug_log=None):
    """捕获窗口可见区域，返回 (image, capture_rect, backend)。

    darwin 下 Quartz 区域截图即原生路径，无需 DXGI/ImageGrab 双轨。
    """
    window = Rect.from_mapping(window_rect)
    virtual = get_virtual_screen_rect()
    capture = clipped_capture_rect(window, virtual)
    if capture.width <= 0 or capture.height <= 0:
        raise ValueError("窗口完全位于虚拟桌面之外，无法截图。")

    image = _region_image(capture)
    return image, capture, "quartz_region"


def capture_full_virtual_desktop(debug_log=None):
    """截取完整虚拟桌面，返回 (image, rect, backend)。"""
    virtual = get_virtual_screen_rect()
    image = _region_image(virtual)
    if debug_log:
        debug_log(
            f"完整虚拟桌面截图成功 {image.size}: "
            f"({virtual.x},{virtual.y} {virtual.width}×{virtual.height})"
        )
    return image, virtual, "quartz_full_desktop"


def reset_capture_backend():
    """darwin 后端无会话状态，空实现保持接口对齐。"""
    return None