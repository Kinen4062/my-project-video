# -*- coding: utf-8 -*-
"""第 16 轮：音轨探测 —— 让"没有音轨的文件"从一开始就不建音频播放器。

要防的那件事（用户报的）：
    关掉播放器之后总会冒出一行
    `Failed to open file '...' or configure filtergraph`。

它的真相（本套件就是围绕这个事实搭的）：
    · 那行**产生于打开无音轨文件的那一刻** —— 给没有音频流的文件建
      MediaPlayer，ffpyplayer 的读线程必然配不起 filtergraph；
    · 但 Windows 上 stderr 不是终端时是**块缓冲**，这几十个字节填不满 4KB，
      于是一直扣在 C 运行时的缓冲里，直到进程退出（os._exit 会 flush）才冲出来
      —— 表现出来就成了"一关播放器就报打开失败"，看着像关闭动作把播放搞坏了。
    · 所以想让它彻底不出现，唯一的办法是**别建那个播放器**（它本来也没用）。

本套件的分层：
    ① 探测函数本身的三态语义（True / False / None）
    ② 无音轨文件：不建播放器，但必须立刻能拿到墙钟主时钟
    ③ 有音轨文件：一切照旧（不能为了修一个噪音把正常音频搞坏）
    ④ 缓存：同一文件不重复探测、文件变了要失效
    ⑤ 无音轨文件上不再做无用的音频交接 / 重建
    ⑥ 启动预热真的把列表文件探好了
    ⑦ 探测不了（没有 ffmpeg）时**必须**照旧建播放器 —— 宁可白建，不误静音
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import (PROJ, city_fixture, make_noaudio_fixture,  # noqa: E402
                      pump, run_suite, work_dir)


def body(root, app, fp, s):
    import media_tools

    ffmpeg = fp.find_ffmpeg()
    s.check(ffmpeg is not None, "本机找得到 ffmpeg（找不到的话后面几条会走保守分支）")

    city = city_fixture()
    noaudio = make_noaudio_fixture(
        os.path.join(work_dir("audio_probe"), "noaudio.mp4"))

    # ==========================================================
    s.section("① 探测函数的三态语义")
    # ==========================================================
    s.check(media_tools.probe_audio_stream(ffmpeg, city) is True,
            "有音轨的文件 → True")
    s.check(media_tools.probe_audio_stream(ffmpeg, noaudio) is False,
            "无音轨的文件 → False")
    s.check(media_tools.probe_audio_stream(None, city) is None,
            "没给 ffmpeg 路径 → None（不猜）")
    s.check(media_tools.probe_audio_stream(ffmpeg, "") is None,
            "空路径 → None")
    s.check(media_tools.probe_audio_stream(
                ffmpeg, os.path.join(work_dir("audio_probe"), "根本没有这个.mp4")) is False,
            "文件打不开 → False（ffmpeg 报的错里没有 Audio:）")
    # 老接口的二态语义必须原样保留：它服务的是"要不要补音轨"，判断不了就别动
    s.check(media_tools.has_audio_stream(None, city) is False,
            "has_audio_stream 保持二态：探测不了也算 False（mux 场景的默认值）")
    s.check(media_tools.has_audio_stream(ffmpeg, city) is True,
            "has_audio_stream 对真音轨仍然报 True")

    # 中文 + 空格路径（用户那个文件就是中文名，Windows 下这是真实风险点）
    zh = make_noaudio_fixture(
        os.path.join(work_dir("audio_probe"), "测试 合成3D.mp4"))
    s.check(media_tools.probe_audio_stream(ffmpeg, zh) is False,
            "中文 + 空格路径也能正确探测（Windows 路径编码）")

    # ==========================================================
    s.section("② 无音轨文件：不建音频播放器，但要立刻能用墙钟主时钟")
    # ==========================================================
    app.open_video(noaudio, record=False)
    s.check(app.current_has_audio is False, "探测结论记下来了（False）")
    s.check(app.audio_player is None, "**没有**创建音频播放器（那行报错的来源就断了）")
    s.check(app.audio_failed is True,
            "audio_failed 同时置上 —— 它是墙钟主时钟的准入条件，漏了会退化成盲调度")
    s.check(app._audio_clock_dead is False,
            "拿到墙钟靠的是「已经知道没音轨」，不是靠「等 1.5 秒判死时钟」")

    # ⚠️ open_video() 结束时状态已经是 PLAYING（见它的结尾），这里**不要**再
    #    toggle_play() —— 那会把画面暂停，而暂停时主时钟按设计返回 None。
    pump(root, 1.2)
    clock = app._master_clock_media_seconds()
    s.check(clock is not None,
            "无音轨文件能在播放中拿到墙钟主时钟（原来要白等 1.5 秒判死时钟）")
    s.check(clock is not None and clock > 0.0, "墙钟主时钟读数是有效正数")
    pump(root, 1.0)
    later = app._master_clock_media_seconds()
    s.check(later is not None and clock is not None and later > clock,
            "墙钟主时钟确实在推进（不是一个冻住的读数）")

    # ==========================================================
    s.section("③ 有音轨文件：一切照旧")
    # ==========================================================
    app.open_video(city, record=False)
    s.check(app.current_has_audio is True, "探测结论 True")
    s.check(app.audio_player is not None, "照常创建音频播放器")
    s.check(app.audio_failed is False, "audio_failed 是干净的 False")
    pump(root, 2.5)
    s.check(app._audio_calibrated, "有音轨的照旧能校准上音频主时钟")

    # ==========================================================
    s.section("④ 探测缓存：同文件不重复探、文件变了要失效；代价有上界")
    # ==========================================================
    calls = []
    orig_probe = media_tools.probe_audio_stream

    def counting(ff, path):
        calls.append(path)
        return orig_probe(ff, path)

    media_tools.probe_audio_stream = counting
    try:
        app._audio_probe_cache.clear()
        app.open_video(noaudio, record=False)
        n1 = len(calls)
        s.check(n1 == 1, "第一次打开：探测了一次（实际 %d 次）" % n1)

        app.open_video(noaudio, record=False)
        s.check(len(calls) == n1, "第二次打开同一个文件：**没有**再探（缓存命中）")

        # 改动文件的时间戳 → 指纹变了 → 缓存必须失效
        old = os.path.getmtime(noaudio)
        os.utime(noaudio, (old + 5.0, old + 5.0))
        app.open_video(noaudio, record=False)
        s.check(len(calls) == n1 + 1,
                "文件指纹（大小/时间）变了之后重新探测（缓存不会拿旧结论骗人）")
    finally:
        media_tools.probe_audio_stream = orig_probe

    # 探测本身的代价必须有上界 —— 它是本轮**新加进 open_video 路径**的开销，
    # 不量的话下次有人把它拖成 1 秒也没人知道。
    app._audio_probe_cache.clear()
    t0 = time.perf_counter()
    app.open_video(noaudio, record=False)
    first_ms = (time.perf_counter() - t0) * 1000.0
    t0 = time.perf_counter()
    app.open_video(noaudio, record=False)
    cached_ms = (time.perf_counter() - t0) * 1000.0
    s.check(first_ms < 600,
            "首次打开（含起一个 ffmpeg 子进程探测）< 600ms，实测 %.0fms" % first_ms)
    s.check(cached_ms < 200,
            "缓存命中后再打开 < 200ms，实测 %.0fms（探测那一笔应该是 0）" % cached_ms)

    # ==========================================================
    s.section("⑤ 无音轨文件上不再做无用的音频交接 / 重建")
    # ==========================================================
    app.open_video(noaudio, record=False)
    app._begin_audio_handoff(0.0)
    s.check(app._audio_handoff_thread is None and app._audio_handoff_old is None,
            "请求音频交接 → 直接被挡掉（没有音频可交接，也就不用付那 0.5 秒）")
    # 拖动 / 切倍速这两条重建路径也不该动
    # （open_video 之后已经是 PLAYING，这里不能再 toggle_play，那会变成暂停态，
    #   而暂停态切倍速只会登记、根本不走重建，就测不到要测的东西了）
    pump(root, 0.8)
    app.speed_var.set("1.5x")
    app.change_speed(None)
    pump(root, 0.6)
    s.check(app.audio_player is None, "无音轨文件切倍速之后依然没有音频播放器")
    app.finish_seek()
    pump(root, 0.8)
    s.check(app.audio_player is None, "无音轨文件拖动之后依然没有音频播放器")
    s.check(app.cap is not None and app.current_video_path == noaudio,
            "拖动/切倍速之后视频本身照常在工作（挡掉的只是音频那一侧）")

    # ==========================================================
    s.section("⑥ 启动预热：趁空闲把列表里的文件先探好")
    # ==========================================================
    app._audio_probe_cache.clear()
    app.playlist = []
    app._playlist_add([city, noaudio])      # 真实存在的两个文件
    app._audio_probe_warm_enabled = True
    app._warm_audio_probe_cache()
    deadline = time.time() + 10.0
    while time.time() < deadline and len(app._audio_probe_cache) < 2:
        time.sleep(0.05)
        root.update()
    s.check(len(app._audio_probe_cache) >= 2,
            "预热把列表里两个文件的结论都算好了（%d 条）" % len(app._audio_probe_cache))
    # 预热不是"顺手全探一遍"：列表只有一个文件时不值得起线程
    app._audio_probe_cache.clear()
    app.playlist = []
    app._playlist_add([city])
    app._warm_audio_probe_cache()
    time.sleep(0.3)
    s.check(len(app._audio_probe_cache) == 0,
            "列表里只有 1 个文件时不预热（一个文件的探测成本反正躲不掉）")
    app._audio_probe_warm_enabled = False
    app.playlist = []
    app._playlist_add([city, noaudio])
    app._warm_audio_probe_cache()
    time.sleep(0.3)
    s.check(len(app._audio_probe_cache) == 0, "开关关掉后预热完全不启动")
    app._audio_probe_warm_enabled = True
    app.playlist = []

    # ==========================================================
    s.section("⑦ 探测不了时不许误静音")
    # ==========================================================
    app._audio_probe_cache.clear()
    saved = (fp._FFMPEG_PATH, fp._FFMPEG_SEARCHED)
    fp._FFMPEG_PATH, fp._FFMPEG_SEARCHED = None, True     # 伪造"本机没有 ffmpeg"
    try:
        s.check(app._probe_audio_track(city) is None, "没有 ffmpeg → 探测结果是 None")
        app.open_video(city, record=False)
        s.check(app.current_has_audio is None, "结论记成 None（未知）")
        s.check(app.audio_player is not None,
                "**仍然**建了播放器 —— 判断不了就按有音轨处理，绝不把正常文件搞成静音")
    finally:
        fp._FFMPEG_PATH, fp._FFMPEG_SEARCHED = saved
    app._audio_probe_cache.clear()
    app.open_video(city, record=False)
    s.check(app.audio_player is not None and app.current_has_audio is True,
            "恢复 ffmpeg 之后一切照常")

    # 收尾：把播放停掉，免得带着定时器退场
    app._stop_playback_loop()


if __name__ == "__main__":
    sys.exit(run_suite("音轨探测（不建无用播放器）", body))
