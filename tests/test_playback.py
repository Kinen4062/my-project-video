# -*- coding: utf-8 -*-
"""功能用例套件 —— 覆盖第 1~11 轮修掉的所有播放行为。

跑法：`python tests/test_playback.py`（或 `python run_tests.py`）

分组（共 37 项断言）：
  1. 拖动进度条后音量保持 ← 第 10 轮用户报的 bug
  2. 拖动进度条后静音状态保持
  3. 连续快速拖动 6 次（不堆积交接、不卡）
  4. 暂停态拖动 → 恢复播放
  5. 切倍速 1x→2x→1x（音量不丢、不卡）
  6. 片尾回绕（原来每圈卡 0.5 秒）
  7. 无音轨文件（本程序导出的 3D 视频）：拖动/改音量/静音都不崩
  8. 交接进行中改音量 / 静音（历史崩溃点）
  9. 交接进行中换文件
 10. 稳态播放速度
 11. 冷启动后立刻拖动（抢发窗口来不及开）
"""

import os
import sys
import time

import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import (FrameProbe, Mixer, city_fixture, drag,  # noqa: E402
                      make_noaudio_fixture, measure_speed, measure_speed_window,
                      pump, run_suite, to_mixer_level, work_dir)


def media_pos(app):
    """当前媒体位置（帧）。注意 POS_FRAMES 给的是"下一帧序号"。"""
    return app.cap.get(cv2.CAP_PROP_POS_FRAMES)


def body(root, app, fp, s):
    city = city_fixture()
    noaudio = make_noaudio_fixture(
        os.path.join(work_dir("playback"), "noaudio_3d.mp4"), city, seconds=3.0)
    mixer = Mixer()
    probe = FrameProbe(app)

    def vol_ok(volume_0_1, playing=True):
        """通道级音量断言；探针不可用时放行并留一条说明（免得满屏假 FAIL）"""
        if not mixer.available:
            s.note("（混音通道探针不可用，跳过通道级音量校验）")
            return True
        return bool(mixer.channels_at(to_mixer_level(volume_0_1), playing=playing))

    s.check(mixer.available, "混音通道探针可用（量真实音量的唯一可靠手段）")

    # ---------------------------------------------------------- 1
    s.section("拖动进度条后音量保持（第 10 轮用户报的 bug）")
    app.open_video(city)
    pump(root, 3.0)
    app.change_volume(0.3)
    pump(root, 0.6)
    s.check(vol_ok(0.3), "拖动前：混音器里确实有一个通道是 0.3")
    probe.reset()
    drag(app, root, 0.15, 0.55)
    pump(root, 1.6)
    s.check(mixer.snapshot() is not None,
            "拖动后：混音器没被销毁（销毁后读通道就是 access violation）")
    s.check(vol_ok(0.3) and not vol_ok(1.0),
            "拖动后：**新播放器的通道**也是 0.3（不是默认满音量）← bug 本体")
    st = probe.stats()
    s.check(st["over200"] == 0,
            "拖动后没有 >200ms 卡顿（帧间隔 中位 %.0fms / 最大 %.0fms）"
            % (st["median"], st["max"]))
    s.check(bool(app._audio_calibrated) and app._audio_handoff_old is None,
            "音频已交接回来（时钟已校准、无待关旧实例）")

    # ---------------------------------------------------------- 2
    s.section("拖动进度条后静音状态保持")
    app.change_volume(0.5)
    pump(root, 0.4)
    app.toggle_mute()
    pump(root, 0.5)
    quiet = mixer.audio_channels()
    s.check(quiet is not None and all(v == 0 for _, v in quiet),
            "静音后所有发声通道都是 0")
    drag(app, root, 0.2, 0.6)
    pump(root, 1.6)
    quiet = mixer.audio_channels()
    s.check(quiet is not None and all(v == 0 for _, v in quiet),
            "拖动后仍然是静音（新播放器没有出声）")
    app.toggle_mute()
    pump(root, 0.8)
    s.check(vol_ok(0.5), "取消静音后音量回到 0.5")

    # ---------------------------------------------------------- 3
    s.section("连续快速拖动 6 次")
    app.change_volume(0.4)
    pump(root, 0.5)
    probe.reset()
    for i in range(6):
        a = 0.05 + i * 0.12
        drag(app, root, a, min(0.95, a + 0.1), hold_ms=30)
        pump(root, 0.08)
    pump(root, 2.2)
    st = probe.stats()
    s.check(st["over200"] == 0,
            "连续拖动全程无 >200ms 卡顿（最大 %.0fms）" % st["max"])
    pending = app._audio_handoff_old is not None
    alive = bool(app._audio_handoff_thread is not None
                 and app._audio_handoff_thread.is_alive())
    s.check(not pending and not alive, "没有堆积出多个并行交接（旧实例已交出去）")
    s.check(bool(app._audio_calibrated), "过程结束时音频已回来")
    s.check(vol_ok(0.4), "连续拖动后音量仍是 0.4")

    # ---------------------------------------------------------- 4
    s.section("暂停态拖动 → 恢复播放")
    app.change_volume(0.35)
    pump(root, 0.4)
    app.toggle_play()
    pump(root, 0.4)
    s.check(app.is_paused, "已进入暂停态")
    drag(app, root, 0.25, 0.65)
    pump(root, 1.0)
    # 探针必须在"按继续"的前一刻清零：暂停期间本来就不出帧，
    # 那段空档会被误算成一次大卡顿（踩过，误报 500ms）。
    probe.reset()
    app.toggle_play()
    pump(root, 1.6)
    st = probe.stats()
    s.check(st["over200"] == 0,
            "按继续后立刻开播，无 >200ms 卡顿（最大 %.0fms）" % st["max"])
    speed = measure_speed(app, root, measure_speed_window(app))
    s.check(0.90 <= speed <= 1.06, "恢复后速度正常（%.3fx）" % speed)
    s.check(vol_ok(0.35), "恢复播放后音量是 0.35")

    # ---------------------------------------------------------- 5
    s.section("切倍速（1.0x → 2.0x → 1.0x）")
    app.change_volume(0.45)
    pump(root, 0.4)
    app.speed_var.set("2.0x")
    app.change_speed(None)
    pump(root, 1.0)
    probe.reset()
    pump(root, 1.2)
    st = probe.stats()
    s.check(st["over200"] == 0,
            "切倍速无 >200ms 卡顿（帧间隔 中位 %.0fms）" % st["median"])
    s.check(vol_ok(0.45), "切倍速后音量仍是 0.45")
    app.speed_var.set("1.0x")
    app.change_speed(None)
    pump(root, 1.2)
    s.check(vol_ok(0.45), "切回 1.0x 后音量仍是 0.45")

    # ---------------------------------------------------------- 6
    s.section("片尾回绕")
    app.change_volume(0.3)
    pump(root, 0.4)
    fps = app.video_fps if app.video_fps > 0 else 25.0
    app.seek_var.set(app.total_frames - int(fps * 1.5))
    app.finish_seek()
    probe.reset()
    pump(root, 3.5)
    st = probe.stats()
    s.check(st["over200"] == 0,
            "回绕处不再卡顿（原来每圈卡 0.5 秒；最大 %.0fms）" % st["max"])
    s.check(vol_ok(0.3), "回绕后音量仍是 0.3")

    # ---------------------------------------------------------- 7
    s.section("无音轨文件（本程序导出的 3D 视频）：拖动 + 改音量 + 静音")
    app.open_video(noaudio)
    pump(root, 3.0)
    s.check(bool(app.audio_failed or app._audio_clock_dead),
            "无音轨文件被判定为没有音频")
    probe.reset()
    drag(app, root, 0.2, 0.7)
    pump(root, 0.8)
    app.change_volume(0.2)
    pump(root, 0.3)
    app.toggle_mute()
    pump(root, 0.3)
    app.toggle_mute()
    pump(root, 0.4)
    # 跑得到这里就说明没崩（真段错误的话进程已经没了，整个套件红掉）
    s.check(True, "无音轨文件上拖动/改音量/静音都不崩")
    st = probe.stats()
    s.check(st["over200"] == 0, "无音轨文件也不卡（最大 %.0fms）" % st["max"])
    s.check(app._audio_handoff_old is None, "无音轨文件不留下待关的旧实例")
    s.check(not app._audio_volume_resume,
            "无音轨文件不会被误调 set_volume（混音器已销毁时那是读 NULL）")

    # ---------------------------------------------------------- 8
    s.section("交接进行中改音量 / 静音（历史崩溃点）")
    app.open_video(city)
    pump(root, 2.5)
    app.change_volume(0.25)
    pump(root, 0.5)
    drag(app, root, 0.3, 0.75)
    pump(root, 0.12)                       # 故意停在交接半途
    app.change_volume(0.6)
    pump(root, 0.05)
    app.toggle_mute()
    pump(root, 0.05)
    app.toggle_mute()
    pump(root, 1.8)
    s.check(True, "交接中改音量/静音不崩")
    app.change_volume(0.25)
    app.toggle_mute()
    pump(root, 0.8)
    quiet = mixer.audio_channels()
    s.check(quiet is not None and all(v == 0 for _, v in quiet),
            "静音最终生效")
    app.toggle_mute()
    pump(root, 0.9)
    s.check(vol_ok(0.25), "取消静音后新播放器是 0.25")
    app.toggle_play()
    pump(root, 0.4)
    app.toggle_play()
    pump(root, 0.4)
    s.check(app.play_state == fp.PlayState.PLAYING, "新播放器上暂停/恢复正常")
    p0 = media_pos(app)
    pump(root, 1.2)
    s.check(media_pos(app) != p0, "恢复后画面继续推进")

    # ---------------------------------------------------------- 9
    s.section("交接进行中换文件")
    app.open_video(city)
    pump(root, 2.5)
    drag(app, root, 0.2, 0.8)
    pump(root, 0.12)                       # 交接半途切走
    app.open_video(noaudio)
    pump(root, 2.5)
    s.check(True, "交接中换文件不崩")
    s.check(app._audio_handoff_old is None
            and not (app._audio_handoff_thread is not None
                     and app._audio_handoff_thread.is_alive()),
            "换文件后 state 干净")
    s.check(not app._audio_volume_resume, "换文件后抢发窗口已关")
    s.check(app._audio_volume_resume_timer is None, "换文件后不留抢发定时器")
    p0 = media_pos(app)
    pump(root, 1.2)
    s.check(media_pos(app) != p0, "换文件后能正常播（媒体位置在推进）")

    # ---------------------------------------------------------- 10
    s.section("稳态播放速度（不碰界面）")
    app.open_video(city)
    pump(root, 1.8)
    speed = measure_speed(app, root, measure_speed_window(app))
    s.check(0.90 <= speed <= 1.06, "city.mp4 速度接近实时（%.3fx）" % speed)

    # ---------------------------------------------------------- 11
    s.section("打开后立刻拖动（抢发窗口来不及开）")
    app.open_video(city)
    pump(root, 0.05)                       # 几乎不等：音频还没校准
    app.change_volume(0.5)
    drag(app, root, 0.1, 0.4)
    pump(root, 3.0)
    s.check(vol_ok(0.5), "冷启动后立刻拖动，音量仍被补上")
    s.check(app._audio_volume_resume_timer is None
            and not app._audio_volume_resume, "状态干净（抢发定时器已停）")


if __name__ == "__main__":
    sys.exit(run_suite("功能用例", body))
