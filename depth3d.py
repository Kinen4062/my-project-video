# -*- coding: utf-8 -*-
"""SBS（左右分屏）原始帧 → 当前显示模式的帧。

【第 12 轮·重构】从 `final_player.py` 搬出来的第二块：原来这两个东西是
`VideoPlayerApp._process_3d()` 和它专用的 `_get_mesh()`，但它们其实只需要
"一帧 + 几个调节量"，跟窗口、播放器、音频都没关系。

搬过来之后改成显式传参：调节量（display_mode / swap_eyes / parallax）从
app 的字段读出来传进去，网格缓存由 `MeshCache` 自己持有 —— 原来它挂在
app 上的 `_mesh_hw` / `_mesh_x` / `_mesh_y` 三个字段因此消失了。

调用点（`update_frame` / `change_display_mode` / `_refresh_paused_frame`）不用改：
`VideoPlayerApp._process_3d()` 保留为同名薄封装。
"""

import cv2
import numpy as np


class MeshCache:
    """缓存 cv2.remap 用的坐标网格，避免每帧重建。

    网格只跟 (h, w) 有关，而同一段视频的尺寸不会变，所以实际上只会算一次。
    尺寸变了（换文件）才重算。
    """

    def __init__(self):
        self._hw = None
        self._x = None
        self._y = None

    def get(self, h, w):
        if self._hw != (h, w):
            x = np.arange(w, dtype=np.float32)
            y = np.arange(h, dtype=np.float32)
            self._x, self._y = np.meshgrid(x, y)
            self._hw = (h, w)
        return self._x, self._y


def process_3d(frame, display_mode, swap_eyes, parallax, mesh):
    """把 SBS 原始帧处理成当前显示模式的帧。

    frame         —— BGR 的左右分屏原始帧
    display_mode  —— "2D" / "红蓝3D" / "偏振-隔行" / "偏振-隔列" / "偏振-棋盘"
    swap_eyes     —— 是否交换左右眼
    parallax      —— 视差偏移量（0 表示不做 remap）
    mesh          —— MeshCache 实例（只有需要视差调节时才会被用到）

    2D 模式直接返回原帧（连左右切片都不做）—— 这一点很重要，
    `_sync_parallax_visibility` 的注释里说明过：视差滑块在 2D 下本来就不显示。
    """
    if display_mode == "2D":
        return frame

    h, w = frame.shape[:2]
    w_half = w // 2
    if w_half <= 0:
        return frame

    # 【P2 修复】原来这里无条件做两次 cv2.resize(left/right_eye, (w_half, h))：
    #   - w 为偶数时（绝大多数情况）切片宽度正好就是 w_half，
    #     目标尺寸与源尺寸完全相同 → 纯空操作，每帧白做两次全画幅拷贝；
    #   - w 为奇数时右眼比左眼宽 1 像素，这时才真的需要裁到同一宽度。
    # 所以改成"只在宽度不一致时才 resize"。另外切片本身是**非连续视图**
    # （行跨距还是整幅宽度），统一用 ascontiguousarray 交一份连续内存给
    # 后面的 cv2.split / cv2.remap，避免它们内部再偷偷转换。
    left_eye = frame[:, 0:w_half]
    right_eye = frame[:, w_half:w]
    left_eye = (cv2.resize(left_eye, (w_half, h)) if left_eye.shape[1] != w_half
                else np.ascontiguousarray(left_eye))
    right_eye = (cv2.resize(right_eye, (w_half, h)) if right_eye.shape[1] != w_half
                 else np.ascontiguousarray(right_eye))

    # 【新增】交换左右眼
    if swap_eyes:
        left_eye, right_eye = right_eye, left_eye

    # 【新增】实时视差调节
    extra = parallax
    if extra != 0:
        x_base, y_base = mesh.get(h, w_half)
        left_eye = cv2.remap(left_eye, x_base + extra, y_base,
                             cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        right_eye = cv2.remap(right_eye, x_base - extra, y_base,
                              cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

    # 3D 合成（逻辑与原代码一致）
    if display_mode == "红蓝3D":
        b_l, g_l, r_l = cv2.split(left_eye)
        b_r, g_r, r_r = cv2.split(right_eye)
        return cv2.merge((b_l, g_l, r_r))

    elif display_mode == "偏振-隔行":
        combined = np.zeros_like(left_eye)
        combined[0::2, :] = left_eye[0::2, :]
        combined[1::2, :] = right_eye[1::2, :]
        return combined

    elif display_mode == "偏振-隔列":
        combined = np.zeros_like(left_eye)
        combined[:, 0::2] = left_eye[:, 0::2]
        combined[:, 1::2] = right_eye[:, 1::2]
        return combined

    elif display_mode == "偏振-棋盘":
        combined = np.zeros_like(left_eye)
        combined[0::2, 0::2] = left_eye[0::2, 0::2]
        combined[0::2, 1::2] = left_eye[0::2, 1::2]
        combined[1::2, 0::2] = right_eye[1::2, 0::2]
        combined[1::2, 1::2] = left_eye[1::2, 1::2]
        return combined

    return frame
