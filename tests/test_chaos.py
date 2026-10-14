# -*- coding: utf-8 -*-
"""随机交错压力测试（乱序操作，专治"逐用例全绿也照样漏"）。

跑法：`python tests/test_chaos.py <seed> <次数>`

## 为什么必须有它

第 9 轮那次 `set_volume` 段错误，逐用例测试**全绿**照样漏 —— 因为所有音量用例
用的都是有音轨的 `city.mp4`。是随机交错把"无音轨文件 + 某个时序"这个组合撞出来
的。结论：**用例覆盖不到的状态组合，靠随机交错补。**

（第 11 轮也一样：连续快速拖动导致声音永久消失，逐用例的顺序执行撞不到，
是这里才暴露的。）

## 判据

1. **不崩**：进程活着跑完（真段错误整个进程会消失，退出码非 0）；
2. **播放中不卡**：随机操作间隙里，画面帧间隔 > 400ms 的次数要有限 ——
   但"换文件"（要等新文件装载）与"跳转落地"这两类天然的长间隔要**归属到具体
   操作**上排除掉，不能笼统算成卡顿；
3. **结束时状态干净**：没有待关旧实例、没有在跑的交接线程、没有挂起的重建请求、
   没有残留定时器。

参数说明：`seed` 决定操作序列，`次数` 是操作总数。同一个 seed 的结果可复现。
"""

import os
import random
import sys
import time

import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import (FrameProbe, city_fixture, make_noaudio_fixture,  # noqa: E402
                      pump, run_suite, work_dir)

OPS = ["seek", "speed", "pause", "volume", "mute", "open"]


def body(root, app, fp, s):
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    n_ops = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    rng = random.Random(seed)

    city = city_fixture()
    noaudio = make_noaudio_fixture(
        os.path.join(work_dir("chaos"), "noaudio_3d.mp4"), city, seconds=3.0)
    files = [city, noaudio]

    probe = FrameProbe(app)
    counts = {op: 0 for op in OPS}
    # 每个操作"结束后到下一次操作前"这段时间里的最大帧间隔 —— 这样长间隔能
    # 归属到具体操作上，而不是笼统地算成"卡顿"。
    worst = {op: 0.0 for op in OPS}

    app.open_video(city)
    pump(root, 2.0)

    for i in range(n_ops):
        op = rng.choice(OPS)
        counts[op] += 1
        probe.reset()
        t0 = time.time()

        if op == "seek":
            n = app.total_frames
            app.seek_var.set(app._clamp_seek_frame(rng.uniform(0, n)))
            app.finish_seek()
        elif op == "speed":
            app.speed_var.set(rng.choice(["0.5x", "1.0x", "1.5x", "2.0x"]))
            app.change_speed(None)
        elif op == "pause":
            app.toggle_play()
        elif op == "volume":
            app.change_volume(rng.choice([0.0, 0.15, 0.35, 0.6, 1.0]))
        elif op == "mute":
            app.toggle_mute()
        elif op == "open":
            app.open_video(rng.choice(files))

        # 操作后的观察窗口：够长到能看见"卡一下"，又不能让压力测试跑太久
        pump(root, rng.uniform(0.12, 0.35))
        if app.is_paused:
            # 暂停时本来就不出帧，这段空档不能算卡顿
            probe.reset()
        else:
            st = probe.stats()
            if st["n"]:
                worst[op] = max(worst[op], st["max"])
        if i % 25 == 24:
            print("  [%d] %-6s 播放器=%s 交接挂起=%s 待关旧实例=%s"
                  % (i + 1, op, app.audio_player is not None,
                     app._audio_handoff_pending, app._audio_handoff_old is not None))

    # 【必须先恢复播放再断言】暂停态下：
    #   - "挂起的音频重建请求"**按设计就是留着不动**（暂停时重建毫无意义，
    #     只记住位置、等恢复播放时再兑现 —— 见第 9 轮"拖动卡顿"那次的取舍）；
    #   - 音频交接也会一直等（要等时钟重新走起来才确认新播放器在跑，兜底 3 秒）。
    # 不先按继续，下面两条断言会把"设计如此"误报成缺陷。
    if app.is_paused:
        app.toggle_play()
        pump(root, 1.0)
    pump(root, 3.5)

    s.note("乱序操作 %d 次：%s" % (n_ops, counts))
    s.note("各操作后的最大帧间隔（ms）：%s"
           % {k: round(v) for k, v in worst.items()})

    # ---- 判据 1：没崩 ----
    s.check(True, "跑完 %d 次乱序操作没有崩溃（能到这行就说明进程活着）" % n_ops)

    # ---- 判据 2：除"换文件"外，播放中的长间隔都 <400ms ----
    bad = {k: v for k, v in worst.items() if k != "open" and v > 400}
    s.check(not bad,
            "除『换文件』外，播放中最大帧间隔均 <400ms（超标的：%s）"
            % (bad or "无"))

    # ---- 判据 3：结束时状态干净 ----
    alive = bool(app._audio_handoff_thread is not None
                 and app._audio_handoff_thread.is_alive())
    s.check(not alive, "没有遗留在跑的交接线程")
    s.check(app._audio_handoff_old is None, "没有遗留待关的旧实例（交接已收尾）")
    s.check(app._pending_audio_pos is None,
            "没有挂起未兑现的重建请求（第 11 轮不变量：请求一定会被兑现）")
    s.check(app._audio_volume_resume_timer is None, "没有残留的抢发定时器")
    s.check(not app._audio_suspended_for_seek,
            "没有残留的『为拖动而挂起音频』标记（残留会让真·无音轨文件漏判，"
            "正是第 7 轮『播放变慢』回归的来源）")


if __name__ == "__main__":
    sys.exit(run_suite("乱序压力(seed=%s)" % (sys.argv[1] if len(sys.argv) > 1 else 1),
                       body))
