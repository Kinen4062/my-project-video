# -*- coding: utf-8 -*-
"""取景计算：把一帧画面"完整地"放进任意尺寸的窗口。

【第 12 轮·重构】从 `final_player.py` 搬出来的第三块。这里只有两件事：
  ① min 缩放的几何计算（前景该多大、用哪种插值）；
  ② 四周那圈模糊暗背景的计算与缓存。
两者都是纯 numpy/cv2 运算 —— 不碰控件、不碰定时器，跟窗口尺寸之外的世界无关。

原来模糊背景的缓存挂在 app 上（`_bg_cache` / `_bg_cache_size` / `_bg_age`
三个字段 + `BG_REFRESH_INTERVAL` 常量），现在收进 `LetterboxBgCache` 自己持有，
app 那边只剩一个 `self._bg`。
"""

import cv2


# 【P2 修复】render_current_frame 里最贵的一步是"四周那圈模糊背景"：
# 要把整幅放大到 cover 尺寸、降采样、模糊、再放大回窗口大小。
# 它只是变暗后的陪衬（alpha=0.45），画面变化极慢，没必要每帧重算。
# 这里每 N 帧重算一次、其余帧复用缓存。
BG_REFRESH_INTERVAL = 3


def fit_min(w, h, win_w, win_h):
    """算出"完整显示"（min 缩放）后的前景尺寸与插值方式。

    返回 `(fg_w, fg_h, interp)`。缩小用 INTER_AREA（缩小时质量更好），
    放大用 INTER_LINEAR —— 和原来 render_current_frame 里的判断完全一致。

    min 而不是 max：宁可四周留空（由模糊背景填），也要保证画面**完整**看得到。
    """
    scale = min(win_w / w, win_h / h)
    fg_w = max(1, int(w * scale))
    fg_h = max(1, int(h * scale))
    interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    return fg_w, fg_h, interp


class LetterboxBgCache:
    """生成"铺满整个窗口的模糊暗背景"（带缓存）。

    【P2 修复】原来每渲染一帧就要：把整幅放大到 cover 尺寸（可能比窗口
    还大）→ 降采样 → 高斯模糊 → 再放大回窗口大小 → 整体压暗。视频每帧
    都在变，但这层背景是 0.45 倍压暗 + 强模糊的陪衬，肉眼几乎看不出
    逐帧差异，所以按 refresh_interval 帧重算一次、其余帧复用即可。

    缓存以窗口尺寸为键：窗口一变立刻重算（否则会在旧尺寸的图上取错区域）。
    **返回的数组归缓存所有，调用方不要再往里写**（需要写请先 copy）——
    `render_current_frame` 里就是把 `.copy()` 交出去的。
    """

    def __init__(self, refresh_interval=BG_REFRESH_INTERVAL):
        self.refresh_interval = refresh_interval
        self._size = None
        self._cache = None
        self._age = 0

    def get(self, frame, win_w, win_h):
        size = (win_w, win_h)
        if self._size != size:
            self._size = size
            self._cache = None
            self._age = 0        # 尺寸变了：立刻重算

        if self._cache is None or self._age >= self.refresh_interval:
            h, w = frame.shape[:2]
            bg_scale = max(win_w / w, win_h / h)
            bg_w = max(1, int(w * bg_scale))
            bg_h = max(1, int(h * bg_scale))
            bg = cv2.resize(frame, (bg_w, bg_h), interpolation=cv2.INTER_LINEAR)

            # 居中裁剪到窗口大小
            bx = max(0, (bg_w - win_w) // 2)
            by = max(0, (bg_h - win_h) // 2)
            bg = bg[by:by + win_h, bx:bx + win_w]

            # 降采样模糊，提高性能
            small_w = max(1, win_w // 8)
            small_h = max(1, win_h // 8)
            bg_small = cv2.resize(bg, (small_w, small_h), interpolation=cv2.INTER_AREA)
            bg_small = cv2.GaussianBlur(bg_small, (0, 0), sigmaX=3, sigmaY=3)
            bg = cv2.resize(bg_small, (win_w, win_h), interpolation=cv2.INTER_LINEAR)
            bg = cv2.convertScaleAbs(bg, alpha=0.45, beta=0)

            # 保证与窗口同尺寸（浮点舍入可能差 1 像素）
            if bg.shape[0] != win_h or bg.shape[1] != win_w:
                bg = cv2.resize(bg, (win_w, win_h), interpolation=cv2.INTER_LINEAR)
            self._cache = bg
            self._age = 0
        else:
            self._age += 1

        return self._cache
