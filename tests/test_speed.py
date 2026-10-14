# -*- coding: utf-8 -*-
"""稳态播放速度套件。

## 为什么必须每次都跑

第 7 轮出过一次真实回归：用户报"同一个从 2D 转 3D 生成的视频播放速度异常慢"。
根因是新增的"音频主时钟"对**无音轨文件**采信了恒为 0.0 的 `get_pts()`，
等待量随播放进度线性增长（越播越慢，6 秒只推进 0.88 秒 = 0.147x）。
当时之所以没发现，就是因为端到端只测了**有音轨**的 `city.mp4`。

所以这里的规则是：**有音轨与无音轨两种素材都要量**，而且无音轨那一档必须用
`cv2.VideoWriter` 造出来的产物（和程序自己转换产物同源）—— 不能拿别的无声
视频顶替，否则测不出这条路径。

## 测量口径（两个坑）

1. 必须用**媒体位置推进量 / 墙钟**。用渲染帧数算出来的是显示帧率，丢帧时会低估。
2. 测量窗口必须**按片长自适应**。曾经拿固定的 4 秒窗口去量一个 4 秒的素材，
   整个窗口穿过片尾回绕，结果误报成 0.010x。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import (city_fixture, make_noaudio_fixture, measure_speed,  # noqa: E402
                      measure_speed_window, pump, run_suite, work_dir)


def body(root, app, fp, s):
    city = city_fixture()
    noaudio = make_noaudio_fixture(
        os.path.join(work_dir("speed"), "noaudio_3d.mp4"), city, seconds=4.0)

    for label, path in (("有音轨 city.mp4", city),
                        ("无音轨 3D 产物（cv2.VideoWriter）", noaudio)):
        app.open_video(path)
        pump(root, 1.8)                       # 起播 + 音频预热 + 首次校准
        margin = 0.4
        window = measure_speed_window(app, margin=margin)
        fps = app.video_fps if app.video_fps > 0 else 25.0
        duration = (app.total_frames / fps) if app.total_frames else 0.0
        speed = measure_speed(app, root, window)
        s.check(0.90 <= speed <= 1.06,
                "%s：速度 %.3fx（片长 %.1fs，窗口 %.1fs）"
                % (label, speed, duration, window))

    # 无音轨文件的"死时钟"判据本身也要在：它是墙钟主时钟启用的前提
    # （见 _silent_clock_media_seconds），少了它就没有兜底时钟可用。
    app.open_video(noaudio)
    pump(root, 3.0)
    s.check(bool(app.audio_failed or app._audio_clock_dead),
            "无音轨文件被正确判定为『没有音频时钟』")


if __name__ == "__main__":
    sys.exit(run_suite("稳态速度", body))
