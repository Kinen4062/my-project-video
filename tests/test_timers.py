# -*- coding: utf-8 -*-
"""定时器泄漏与"挂起请求一定会被兑现"套件。

为什么单列一条套件：本程序的音频交接 / 跳转防抖 / 抢发窗口全是 `root.after`
定时器，而 `after` 回调**不会**随窗口销毁自动失效 —— 忘停一个就是
"invalid command name" 报错 + 幽灵回调。历史上真漏过一次（第 17 条，防抖
回调泄漏导致多渲染一帧）。

另外这里守一条**不变量**：任何登记的音频重建请求（`_pending_audio_pos`）
最终都必须被兑现 —— 第 11 轮就是靠它发现"连续快速拖动后声音永久消失"的。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import (city_fixture, drag, make_noaudio_fixture,  # noqa: E402
                      pump, run_suite, work_dir)

# 关窗时需要取消的定时器（与 on_close 的取消列表保持一致）
TIMER_ATTRS = ("after_id", "resize_after_id", "seek_timer", "_hide_controls_id",
               "_audio_restart_timer", "_audio_handoff_poll_id",
               "_audio_volume_resume_timer")


def lingering_timers(app):
    return {a: getattr(app, a, None) for a in TIMER_ATTRS
            if getattr(app, a, None) is not None}


def body(root, app, fp, s):
    city = city_fixture()
    noaudio = make_noaudio_fixture(
        os.path.join(work_dir("timers"), "noaudio_3d.mp4"), city, seconds=3.0)

    s.section("① 播放态拖动一次，等交接走完")
    app.open_video(city)
    pump(root, 3.0)
    app.change_volume(0.35)
    pump(root, 0.5)
    drag(app, root, 0.15, 0.55)
    pump(root, 1.8)
    s.check(bool(app._audio_calibrated), "新播放器音频已校正")
    s.check(not app._audio_volume_resume, "抢发窗口已关闭")
    s.check(app._audio_volume_resume_timer is None, "抢发定时器已清空（不泄漏）")
    s.check(app._audio_handoff_old is None, "没有待关的旧实例")
    s.check(app._audio_handoff_poll_id is None, "交接轮询定时器已清空")
    s.check(app._pending_audio_pos is None, "没有未兑现的重建请求")

    s.section("② 暂停态拖动")
    app.toggle_play()
    pump(root, 0.4)
    drag(app, root, 0.2, 0.7)
    pump(root, 1.0)
    s.check(not app._audio_volume_resume, "暂停态拖动不开抢发窗口")
    s.check(app._audio_volume_resume_timer is None, "暂停态拖动不留定时器")
    app.toggle_play()
    pump(root, 1.5)
    s.check(app.play_state == fp.PlayState.PLAYING, "恢复播放成功")
    s.check(app._audio_volume_resume_timer is None, "恢复后也不留定时器")
    s.check(app._pending_audio_pos is None, "恢复后挂起请求已兑现")

    s.section("③ 连续快速拖动 —— 挂起请求必须被兑现（第 11 轮 bug）")
    # 这是本轮修掉的真 bug：交接"等校准"那段里，新到的重建请求会被丢掉，
    # 而音频已被 set_pause 压住 → 永久无声、还会被误判成无音轨文件。
    app.change_volume(0.4)
    pump(root, 0.5)
    for i in range(6):
        a = 0.05 + i * 0.12
        drag(app, root, a, min(0.95, a + 0.1), hold_ms=30)
        pump(root, 0.08)
    pump(root, 2.5)
    s.check(app._pending_audio_pos is None, "挂起的重建请求已被兑现（没有丢掉）")
    s.check(not app._audio_suspended_for_seek, "拖动挂起标记已解除")
    s.check(bool(app._audio_calibrated), "音频时钟重新校准（声音回来了）")
    s.check(not app.audio_failed and not app._audio_clock_dead,
            "没有被误判成无音轨文件（audio_failed / 时钟判死均为 False）")
    s.check(app._audio_volume_resume_timer is None, "抢发定时器不残留")
    s.check(app._audio_handoff_old is None and app._audio_handoff_poll_id is None,
            "交接状态已收干净")

    s.section("④ 交接进行中立刻换文件")
    drag(app, root, 0.3, 0.8)
    pump(root, 0.12)                     # 故意停在交接半途
    app.open_video(noaudio)
    pump(root, 2.5)
    s.check(not app._audio_volume_resume, "换文件后抢发窗口已关")
    s.check(app._audio_volume_resume_timer is None, "换文件后定时器已清空")
    s.check(app._audio_handoff_old is None, "换文件后无待关旧实例")
    s.check(bool(app.audio_failed), "无音轨文件被判定为无音频（不该调 set_volume）")

    s.section("⑤ 停止播放后不应残留任何挂起定时器")
    app._stop_playback_loop()
    pump(root, 0.2)
    left = lingering_timers(app)
    s.check(not left, "停播 + 收尾后无残留定时器（%s）" % (left or "干净"))

    s.section("⑥ 关窗")
    app._cancel_audio_handoff(timeout=1.0)
    app._close_audio_player()
    try:
        app.cap.release()
    except Exception:
        pass
    root.destroy()
    try:
        root.update()
        s.check(True, "destroy 后 update 没有抛异常（定时器都取消干净了）")
    except Exception as e:
        s.check(False, "destroy 后 update 抛了 %r" % (e,))


if __name__ == "__main__":
    sys.exit(run_suite("定时器与挂起请求", body))
