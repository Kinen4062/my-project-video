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
 12. 三个按钮合成两个：打开视频 / 转换为3d ★第 22 轮
"""

import os
import sys
import time
import uuid

import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import (FrameProbe, Mixer, city_fixture, drag,  # noqa: E402
                      make_noaudio_fixture, measure_speed, measure_speed_window,
                      pump, run_suite, to_mixer_level, work_dir)

# 【第 22 轮】本次运行的唯一标签。凡是要"写一个文件"的断言都得放在
# `run_<标签>/` 里 —— 本环境的沙箱**覆盖不了已经存在的文件**，第二次跑
# 会红，而且报错落在 shutil.copy 上、看着完全不像权限问题。
# test_subtitles / test_online_subtitles 用的也是这一套。
_RUN_TAG = "%d_%s" % (os.getpid(), uuid.uuid4().hex[:6])


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

    # ---------------------------------------------------------- 12
    s.section("【第 22 轮】三个按钮合成两个：打开视频 / 转换为3d")
    # 这一节**绝不真的跑转换** —— 那要加载 Depth-Anything 模型、几分钟起步。
    # 真转换由 test_audio_mux（补音轨）与人工真机验证覆盖；这里只钉三件事：
    #   ① 底栏真的只剩那两个按钮（旧的两个不见了）；
    #   ② 【转换为3d】到底拿哪个文件当源（这是本轮新增的逻辑，最容易写错）；
    #   ③ 两个按钮**走真实派发路径**（invoke）确实进到了各自的回调里。

    # --- ① 按钮集合变了 ---
    def _btn_texts():
        return [w.cget("text") for w in app.btn_frame.winfo_children()
                if isinstance(w, fp.tk.Button)]

    texts = _btn_texts()
    s.check("打开视频" in texts, "底栏有【打开视频】")
    s.check("转换为3d" in texts, "底栏有【转换为3d】")
    s.check("打开已有3D视频" not in texts, "旧的【打开已有3D视频】已去掉")
    s.check("导入2D视频一键转3D" not in texts, "旧的【导入2D视频一键转3D】已去掉")
    s.check("导出3D视频" not in texts, "旧的【导出3D视频】已去掉（导出并进了转换流程）")
    s.check(app.btn_convert.cget("text") == "转换为3d", "btn_convert 的文案是【转换为3d】")

    # --- ② 转换源怎么选 ---
    app.open_video(city)
    app.session_output_path = None
    s.check(app._conversion_source() == city,
            "当前有视频 → 转换源就是它（不再弹对话框）")

    # 没有当前视频 → 退到文件对话框；用户取消 → 没有可转的源
    saved_current = app.current_video_path
    orig_open = fp.filedialog.askopenfilename
    app.current_video_path = None
    fp.filedialog.askopenfilename = lambda *a, **k: ""
    try:
        s.check(app._conversion_source() is None,
                "没有当前视频 + 用户在对话框里取消 → 没有源（不转）")
    finally:
        fp.filedialog.askopenfilename = orig_open
        app.current_video_path = saved_current

    # 当前播的就是**本次转换的产物** → 不能拿它当源（产物是左右分屏，
    # 再转一次只会得到"四格"画面），而应该问"要另存吗"
    for answer, expect_export, label in ((True, 1, "答「是」→ 走另存"),
                                         (False, 0, "答「否」→ 既不另存也不转")):
        asked, exported = [], []
        orig_ask, orig_export = fp.messagebox.askyesno, app.export_3d_video
        app.session_output_path = city          # 假装 city 就是刚转出来的产物
        fp.messagebox.askyesno = lambda *a, **k: (asked.append(a[0]), answer)[1]
        app.export_3d_video = lambda: exported.append(1)
        try:
            src = app._conversion_source()
        finally:
            fp.messagebox.askyesno = orig_ask
            app.export_3d_video = orig_export
            app.session_output_path = None
        s.check(src is None, "当前播的是产物 → 不拿它当转换源（%s）" % label)
        s.check(len(asked) == 1, "并且问了一句（%s）" % label)
        s.check(len(exported) == expect_export,
                "调用另存的次数 == %d（%s）" % (expect_export, label))

    # --- ③ 另存这份能力没丢：有产物就能存下去 ---
    save_dir = os.path.join(work_dir("convert_buttons"), "run_" + _RUN_TAG)
    os.makedirs(save_dir, exist_ok=True)
    src_file = os.path.join(save_dir, "fake_product.mp4")
    with open(src_file, "wb") as fh:
        fh.write(b"fake-3d-product")
    dst_file = os.path.join(save_dir, "saved_copy.mp4")

    orig_info, orig_warn = fp.messagebox.showinfo, fp.messagebox.showwarning
    orig_save = fp.filedialog.asksaveasfilename
    orig_err = fp.messagebox.showerror
    fp.messagebox.showinfo = lambda *a, **k: None
    fp.messagebox.showerror = lambda *a, **k: None
    fp.messagebox.showwarning = lambda *a, **k: None
    try:
        app.session_output_path = None
        s.check(app.export_3d_video() is False,
                "没有产物时另存 → 返回 False（不再靠 cwd 里有同名文件来判断）")

        app.session_output_path = src_file
        fp.filedialog.asksaveasfilename = lambda *a, **k: ""
        s.check(app.export_3d_video() is False, "另存对话框里取消 → False")

        fp.filedialog.asksaveasfilename = lambda *a, **k: dst_file
        s.check(app.export_3d_video() is True, "有产物 + 选了路径 → True")
        s.check(os.path.exists(dst_file)
                and os.path.getsize(dst_file) == os.path.getsize(src_file),
                "文件真的复制过去了（%d 字节）" % os.path.getsize(src_file))
    finally:
        app.session_output_path = None
        fp.messagebox.showinfo = orig_info
        fp.messagebox.showwarning = orig_warn
        fp.messagebox.showerror = orig_err
        fp.filedialog.asksaveasfilename = orig_save

    # --- ④ 两个按钮都走真实派发路径（invoke），不是直接调函数 ---
    #
    # 【打开视频】：让文件对话框返回 city.mp4，点一下应当真的播起来。
    orig_open = fp.filedialog.askopenfilename
    fp.filedialog.askopenfilename = lambda *a, **k: city
    try:
        open_btn = [w for w in app.btn_frame.winfo_children()
                    if isinstance(w, fp.tk.Button) and w.cget("text") == "打开视频"]
        s.check(len(open_btn) == 1, "底栏恰好有一个【打开视频】按钮")
        if open_btn:
            open_btn[0].invoke()
    finally:
        fp.filedialog.askopenfilename = orig_open
    pump(root, 0.5)
    s.check(app.current_video_path == city, "点【打开视频】→ 选中的文件真的开始播了")

    # 【转换为3d】：把 is_converting 置真，让 start_conversion 走"已在转换中"
    # 那条**提前返回**（只弹个提示），于是能证明"按钮接的确实是它"，
    # 又完全不会启动真正的转换线程。同时把文件对话框也堵上 —— 万一哪天
    # 按钮被接错到 open_video，最坏也只是空返回，不会把套件挂死在弹窗上。
    warned = []
    orig_warn = fp.messagebox.showwarning
    orig_open = fp.filedialog.askopenfilename
    fp.messagebox.showwarning = lambda *a, **k: (warned.append(a[0] if a else ""), None)[1]
    fp.filedialog.askopenfilename = lambda *a, **k: ""
    app.is_converting = True
    try:
        app.btn_convert.invoke()
    finally:
        fp.messagebox.showwarning = orig_warn
        fp.filedialog.askopenfilename = orig_open
        app.is_converting = False
    s.check(warned and warned[0] == "提示",
            "点【转换为3d】真的进到了 start_conversion（走真实 invoke）")
    s.check(app.is_converting is False, "验完把 is_converting 还原（别留给后面的用例）")


if __name__ == "__main__":
    sys.exit(run_suite("功能用例", body))
