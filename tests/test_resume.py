# -*- coding: utf-8 -*-
"""第 17 轮：记忆播放位置 —— 关掉播放器 / 切走之后再点回来，能接着上次看。

要防的那件事：
    一个片子看到一半关了播放器（或者切去看别的），过一会儿再点开它，
    **只能从头再放一遍**。用户得自己拖进度条找回去 —— 而他要找的那个位置
    恰恰是他上一次主动离开的地方。

需求拆成一句可验的话：
    关闭播放器后、或播放过别的视频后，再点这个**没有播完**的视频时，
    可以选择「从头播放」还是「接着上次的位置」。

本套件的分层（从下到上，下面绿了上面才不会假绿）：
    ① 时间格式化的边界（纯函数）
    ② `resume_store` 的存取与坏数据（纯存储，不碰 Tk）
    ③ 记录时机：切走时记什么、什么时候**不**记
    ④ 三种选择的落地：继续 / 从头 / 取消
    ⑤ 片尾自动连播那条路径：静默续播，不许弹窗
    ⑥ 播到片尾 → 记录被清掉（下次点开不再问）
    ⑦ 记录失效：文件被换掉、转换产物不该进这套体系
    ⑧ 关掉播放器再打开，记录还在（落盘 + 读回）
    ⑨ `ResumeDialog` 本身：三个返回值都能拿到，`show()` 会等、也会返回
"""

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import (PROJ, city_fixture, make_noaudio_fixture,  # noqa: E402
                      make_playlist_path, pump, run_suite, teardown, work_dir)

import cv2  # noqa: E402


def _findpos(app):
    """当前视频位置（秒）—— 与程序内部同一套换算（POS_FRAMES / fps）。"""
    fps = app.video_fps if app.video_fps > 0 else 25.0
    return app.cap.get(cv2.CAP_PROP_POS_FRAMES) / fps


def _jump_to(app, root, sec):
    """把播放位置拨到 `sec` 秒并渲染一帧（模拟「用户看到这里就离开了」）。

    走 `update_frame(force=True)` 而不是直接改 cap：程序内部的
    `_last_media_sec`、进度条读数都是在那一步同步的。
    """
    app.cap.set(cv2.CAP_PROP_POS_FRAMES, int(sec * app.video_fps))
    app.update_frame(force=True)
    root.update()


def body(root, app, fp, s):
    import resume_store

    work = work_dir("resume")
    city = city_fixture()
    other = os.path.join(work, "other.mp4")
    if not os.path.exists(other) or os.path.getsize(other) == 0:
        shutil.copy(city, other)
    # 14 秒的**无音轨**素材：既够长（有「既不在片头也不在片尾」的中间位置），
    # 又没有音频线程来搅时序 —— ⑥ 节要真的把它播到片尾，用无音轨的走墙钟
    # 主时钟，帧调度干净、跑得快。
    quiet = make_noaudio_fixture(os.path.join(work, "quiet14.mp4"), seconds=14.0)

    key_city = resume_store.make_key(city)
    key_other = resume_store.make_key(other)
    key_quiet = resume_store.make_key(quiet)

    def reset_records():
        """每节开始前清空内存记录 + 落盘文件，节与节之间不串味。"""
        app._resume_positions.clear()
        app._save_resume()

    def clean_state():
        app.autoplay_next = False
        app._resume_ignored.clear()
        app._ask_resume_choice = lambda p, s_, d_: "resume"
        reset_records()

    # ==========================================================
    s.section("① 时间格式化（续播提示要给人看的时间）")
    # ==========================================================
    s.check(fp.format_hms(0) == "0:00", "0 秒 → 0:00")
    s.check(fp.format_hms(59) == "0:59", "59 秒 → 0:59")
    s.check(fp.format_hms(60) == "1:00", "60 秒进到 1:00")
    s.check(fp.format_hms(3599) == "59:59", "3599 秒 → 59:59")
    s.check(fp.format_hms(3600) == "1:00:00", "3600 秒才出现小时位")
    s.check(fp.format_hms(3725) == "1:02:05", "3725 秒 → 1:02:05")
    s.check(fp.format_hms(-5) == "0:00", "负数按 0 处理（不打印 -1:-1）")
    s.check(fp.format_hms("x") == "0:00", "非数字按 0 处理（不抛异常）")

    # ==========================================================
    s.section("② resume_store：存取与坏数据")
    # ==========================================================
    store_path = os.path.join(work, "store_roundtrip.json")
    if os.path.exists(store_path):
        os.remove(store_path)
    s.check(resume_store.load(store_path) == {}, "文件不存在 → 空表（不是异常）")

    zh = os.path.join(work, "测试 视频.mp4")
    resume_store.save({resume_store.make_key(zh): {"path": zh, "sec": 12.5,
                                                   "dur": 600.0, "at": 1.0}},
                      store_path)
    back = resume_store.load(store_path)
    s.check(resume_store.make_key(zh) in back,
            "中文 + 空格路径能存能取（Windows 路径编码）")
    s.check(abs(back[resume_store.make_key(zh)]["sec"] - 12.5) < 1e-6,
            "秒数原样回来")

    with open(store_path, "w", encoding="utf-8") as fh:
        fh.write("{ 这不是合法的 JSON")
    s.check(resume_store.load(store_path) == {}, "文件被写坏 → 空表（不抛）")

    bad_path = os.path.join(work, "store_bad.json")
    with open(bad_path, "w", encoding="utf-8") as fh:
        fh.write('{"version": 1, "positions": ['
                 '{"path": "", "sec": 1, "dur": 10},'          # 空路径
                 '{"path": 123, "sec": 5, "dur": 10},'         # 路径不是字符串
                 '{"path": "a.mp4", "sec": "x", "dur": 10},'   # 秒数不是数字
                 '{"path": "b.mp4", "sec": 5, "dur": 0},'      # 时长为 0
                 '{"path": "c.mp4", "sec": -3, "dur": 10},'    # 负位置
                 '{"path": "d.mp4", "sec": 5, "dur": 10, "size": true},'  # bool 当数值
                 '"这一条根本不是对象",'
                 '{"path": "ok.mp4", "sec": 5, "dur": 10}]}')
    cleaned = resume_store.load(bad_path)
    s.check(len(cleaned) == 2
            and resume_store.make_key("ok.mp4") in cleaned
            and resume_store.make_key("d.mp4") in cleaned,
            "坏条目一律丢弃，只留 2 条合法的（不猜默认值）")
    s.check("size" not in cleaned[resume_store.make_key("d.mp4")],
            "bool 不能当数值用 → 只丢掉那一个字段，不牵连整条记录")
    s.check(cleaned[resume_store.make_key("ok.mp4")]["sec"] == 5.0
            and cleaned[resume_store.make_key("ok.mp4")]["dur"] == 10.0,
            "合法那条的数字原样保留")

    many = {}
    for i in range(resume_store.MAX_ENTRIES + 25):
        p = os.path.join(work, "cap_%03d.mp4" % i)
        many[resume_store.make_key(p)] = {"path": p, "sec": 1.0, "dur": 10.0,
                                          "at": float(i)}
    cap_path = os.path.join(work, "store_cap.json")
    resume_store.save(many, cap_path)
    kept = resume_store.load(cap_path)
    s.check(len(kept) == resume_store.MAX_ENTRIES,
            "条数封顶在 %d（文件不会无人看管地长大）" % resume_store.MAX_ENTRIES)
    s.check(resume_store.make_key(os.path.join(work, "cap_%03d.mp4"
                                               % (resume_store.MAX_ENTRIES + 24))) in kept,
            "封顶时淘汰最旧的、留下最新的")
    s.check(len([f for f in os.listdir(work) if f.startswith(".positions-")]) == 0,
            "原子写不留临时文件残渣")

    # ==========================================================
    s.section("③ 记录时机：什么时候记、什么时候不记")
    # ==========================================================
    clean_state()
    app.open_video(city)
    _jump_to(app, root, 8.0)
    s.check(app._resume_positions.get(key_city) is None,
            "还在播的时候**不写盘**（只在离开那一刻结算）")

    app.open_video(other)                      # 切走 → 结算 city
    rec = app._resume_positions.get(key_city)
    s.check(rec is not None, "切走时把上个视频的位置记下来了")
    s.check(rec is not None and abs(rec["sec"] - 8.0) < 1.0,
            "记下的位置≈8 秒（实测 %.2f）" % (rec["sec"] if rec else -1))
    s.check(rec is not None and abs(rec["dur"] - 20.0) < 0.5,
            "连总时长一起记（下次才能判断还剩多少）")

    # 太靠前：点开看一眼就关，不该在下次拦人问一句
    reset_records()
    app.open_video(city)
    _jump_to(app, root, 0.5)
    app.open_video(other)
    s.check(key_city not in app._resume_positions,
            "停在片头 %.0f 秒内不记（等同于从头，不值得问）"
            % fp.RESUME_MIN_POSITION_SEC)

    # 贴着片尾：按「看完了」处理，而且要把**旧记录**也清掉
    reset_records()
    app.open_video(city)
    _jump_to(app, root, 8.0)
    app.open_video(other)                      # 先产生一条记录
    s.check(key_city in app._resume_positions, "（前置）记录已存在")
    app.open_video(city)                       # 切回来
    _jump_to(app, root, app.total_frames / app.video_fps - 1.0)
    app.open_video(other)
    s.check(key_city not in app._resume_positions,
            "停在离片尾 %.0f 秒以内 → 记录被清掉（不算没看完）"
            % fp.RESUME_TAIL_SEC)

    # ==========================================================
    s.section("④ 三种选择：继续 / 从头 / 取消")
    # ==========================================================
    # --- 继续 ---
    clean_state()
    app._remember_resume(city, 8.0, 20.0)
    asked = []
    app._ask_resume_choice = lambda p, s_, d_: (asked.append((s_, d_)), "resume")[1]
    app.open_video(city)
    s.check(len(asked) == 1, "有记录时会问一句（问了一次）")
    s.check(asked and abs(asked[0][0] - 8.0) < 1e-6,
            "问的时候把上次的位置一并给了对话框（好显示「上次看到 8:00」）")
    s.check(abs(_findpos(app) - 8.0) < 1.5,
            "选「继续」→ 起播位置≈8 秒（实测 %.2f）" % _findpos(app))
    s.check(app.play_state == fp.PlayState.PLAYING, "起播后处于播放态")

    # --- 从头 ---
    clean_state()
    reset_records()
    app._remember_resume(city, 8.0, 20.0)
    app._ask_resume_choice = lambda p, s_, d_: "restart"
    app.open_video(city)
    s.check(_findpos(app) < 1.0,
            "选「从头」→ 位置回到片头（实测 %.2f）" % _findpos(app))
    s.check(key_city not in app._resume_positions,
            "选「从头」时把旧记录清掉（否则下次又问、又续）")

    # --- 取消 ---
    clean_state()
    app.open_video(other)                      # 先让 other 成为「正在播的」
    _jump_to(app, root, 6.0)
    app._remember_resume(city, 8.0, 20.0)
    app._ask_resume_choice = lambda p, s_, d_: None
    cur_path = app.current_video_path
    cur_state = app.play_state
    cur_pos = _findpos(app)
    ok = app.open_video(city)
    s.check(ok is False, "取消 → open_video 返回 False（调用方据此不做后续动作）")
    s.check(app.current_video_path == cur_path,
            "取消时**当前正在播的视频没有被换掉**（询问放在拆旧之前）")
    s.check(app.play_state == cur_state, "取消时播放状态原样不动")
    s.check(abs(_findpos(app) - cur_pos) < 1.5, "取消时播放位置也没有被拨动")

    # ==========================================================
    s.section("⑤ 片尾自动连播：静默续播，不许弹窗打断连播")
    # ==========================================================
    clean_state()
    # 先把"当前正在播的"切成 city。为什么必须先切：
    # `open_video` 开头那次结算记的是 **current_video_path**，如果当前就是
    # other，它会先把 other 现在的位置写进去，把下面刚塞的 7.0 覆盖掉 ——
    # 第一版就是这么写的，实测续播到了 6.08 秒（=当时 other 的真实位置）而不是
    # 7.0，看着像"没读记录"，其实是记录被覆盖了。
    app.open_video(city)
    app._remember_resume(other, 7.0, 20.0)
    called = []
    app._ask_resume_choice = lambda p, s_, d_: (called.append(1), "restart")[1]
    app.open_video(other, ask=False)
    s.check(not called, "ask=False 时**根本不问**（连播途中不该弹窗）")
    s.check(abs(_findpos(app) - 7.0) < 0.5,
            "仍然接着上次的位置播（请求 7.0 秒，实测 %.2f）" % _findpos(app))
    s.check(key_other in app._resume_positions,
            "静默续播不当成「用户选了从头」，那条记录还在")

    # ==========================================================
    s.section("⑥ 播到片尾 → 记录被清掉")
    # ==========================================================
    clean_state()
    app.autoplay_next = False
    app.open_video(quiet)
    app._remember_resume(quiet, 8.0, 14.0)
    s.check(key_quiet in app._resume_positions, "（前置）手工塞了一条「没看完」的记录")
    # 拨到离片尾 6 帧，让它自己播过去
    app.cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, app.total_frames - 6))
    pump(root, 2.0)
    s.check(key_quiet not in app._resume_positions,
            "真的播到片尾之后记录被清掉（下次点开就是从头，不再问一句）")

    # ==========================================================
    s.section("⑦ 记录失效：文件被换掉 / 转换产物不进这套体系")
    # ==========================================================
    clean_state()
    # 怎么造"指纹对不上"：**直接改记录里的指纹**，不去改文件本体。
    # 两个原因：① 正在播的文件在 Windows 上被持有句柄，改它内容会
    # PermissionError（实测）；② 我们要测的本来就是 `_lookup_resume` 的校验
    # 分支，直接喂一个对不上的指纹，比"真去动文件、再期望程序发现"更精确。
    real_size = float(os.path.getsize(city))
    real_mtime = float(os.path.getmtime(city))
    key_probe = resume_store.make_key(city)

    def forged(size=None, mtime=None, sec=7.0, dur=20.0):
        app._resume_positions[key_probe] = {
            "path": city, "sec": sec, "dur": dur,
            "size": real_size if size is None else size,
            "mtime": real_mtime if mtime is None else mtime}

    forged()
    s.check(app._lookup_resume(city) is not None, "（前置）指纹对得上 → 记录有效")
    forged(mtime=real_mtime + 120)
    s.check(app._lookup_resume(city) is None,
            "修改时间对不上 → 记录作废（这个路径已经是别的东西了）")
    s.check(key_probe not in app._resume_positions, "作废的记录被就地清掉")
    forged(size=real_size + 12345)
    s.check(app._lookup_resume(city) is None, "大小对不上 → 记录同样作废")
    forged(sec=1.0)
    s.check(app._lookup_resume(city) is None,
            "记录里的位置太靠前 → 当没有记录（不拿它去问一句）")
    forged(sec=19.0)
    s.check(app._lookup_resume(city) is None,
            "记录里的位置快贴片尾 → 同样不当「没看完」处理")

    clean_state()
    app.open_video(city, record=False)         # 模拟转换临时产物
    _jump_to(app, root, 9.0)
    app.open_video(other)
    s.check(key_city not in app._resume_positions,
            "record=False 的文件（%TEMP% 转换产物）不记位置 —— 记了也是死条目")
    app._resume_ignored.clear()

    # ==========================================================
    s.section("⑧ 落盘：关掉播放器再打开，记录还在")
    # ==========================================================
    clean_state()
    app.open_video(city)
    _jump_to(app, root, 9.0)
    app.open_video(other)                      # 结算 → 写盘
    on_disk = resume_store.load(app._resume_path)
    s.check(key_city in on_disk, "记录已经落到磁盘上（不是只在内存里）")
    s.check(abs(on_disk[key_city]["sec"] - 9.0) < 1.0, "落盘的秒数正确")

    # 新开一个 app 指向同一份 JSON —— 等价于「用户重开了播放器」。
    # 为什么可以同进程再建一个 Tk：这里已经到套件最后了，先把上一个 app 的
    # 播放/音频全部收干净（否则两套 SDL 音频设备并存正是段错误的老来源），
    # 而且新 app 只读记录、不播任何东西，不碰音频。
    app._stop_playback_loop()
    app._cancel_audio_handoff(timeout=1.0)
    app._close_audio_player()
    if app.cap is not None:
        app.cap.release()
        app.cap = None
    app.play_state = fp.PlayState.IDLE

    import tkinter as tk
    root2 = tk.Tk()
    root2.geometry("900x600")
    root2.update()
    app2 = fp.VideoPlayerApp(root2, playlist_path=make_playlist_path(),
                             resume_path=app._resume_path)
    try:
        s.check(key_city in app2._resume_positions,
                "新开的播放器读到了上次留下的记录（关闭→重开，进度没丢）")
        s.check(app2._lookup_resume(city) is not None,
                "并且这条记录通过校验（能立刻用来续播）")
    finally:
        teardown(root2, app2)

    # ==========================================================
    s.section("⑨ ResumeDialog：三个返回值 + show() 会等也会返回")
    # ==========================================================
    d = fp.ResumeDialog(root, city, 12.5, 600.0)
    s.check(d.choice is None, "刚建出来还没选（choice 是 None）")
    s.check(int(d.top.winfo_exists()) == 1, "窗口真的建出来了")
    s.check(abs(float(d.top.winfo_reqwidth())) > 0, "控件都摆好了（有请求尺寸）")
    s.check(d.btn_resume.cget("text") == "继续播放"
            and d.btn_restart.cget("text") == "从头播放"
            and d.btn_cancel.cget("text") == "取消",
            "三个按钮的文字就是三个动作本身（不去猜「是/否」是哪一个）")
    d._pick("resume")
    s.check(d.choice == "resume", "点「继续播放」→ choice 是 resume")
    s.check(int(d.top.winfo_exists()) == 0, "选完窗口自己关掉")
    d._pick("restart")
    s.check(int(d.top.winfo_exists()) == 0, "重复 _pick 不抛异常（窗口已没了）")

    for want in ("restart", None):
        dd = fp.ResumeDialog(root, city, 1.0, 10.0)
        root.after(60, lambda w=want, ww=dd: ww._pick(w))
        got = dd.show()                        # 会阻塞到 after 把它点掉
        s.check(got == want or (got is None and want is None),
                "show() 等用户点完再返回，返回的就是那个选择（%r）" % (want,))

    s.note("本套件用同进程新开的 app 模拟「关掉再打开」，没有真的关进程 —— "
           "走的是同一条落盘 / 读回代码路径。")


if __name__ == "__main__":
    sys.exit(run_suite("记忆播放位置", body, resume_enabled=True))
