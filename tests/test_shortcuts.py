# -*- coding: utf-8 -*-
"""键盘快捷键与逐帧步进套件（第 11 轮新增功能）。

## 为什么逐帧步进要用**像素级比对**来验

"按键之后位置对不对"如果只看帧号，很容易被骗：`cv2.CAP_PROP_POS_FRAMES` 给的
是**下一帧的序号**（比正在显示的那帧大 1），而 `cap.set()` 之后读一个位置又会
再变；再加上暂停态的 `force` 重绘路径。历史上就因为这类差 1 帧的问题返工过。

所以这里的判据是：把 `app.current_raw_frame` 和"用另一个独立 VideoCapture
单独解码第 N 帧"的结果做 `np.array_equal` —— 画面对上了才算数。

## 覆盖

  ← / → 暂停态逐帧（像素比对）、边界不越界、不绕回
  ↑ / ↓ 音量（读混音通道值，不认 get_volume 那个会撒谎的字段）
  M 静音、F 全屏、J / L 快进快退
  焦点落在滑块/下拉框上时方向键必须让路（否则一次按键两个动作）
"""

import os
import sys
import tkinter as tk
from tkinter import ttk

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import (Mixer, city_fixture, pump, run_suite,  # noqa: E402
                      take_keyboard_focus, to_mixer_level)


def reference_frame(path, index):
    """用另一个独立的 VideoCapture 解出第 index 帧（作为像素比对的基准）"""
    cap = cv2.VideoCapture(path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, index)
    ok, frame = cap.read()
    cap.release()
    return frame if ok else None


class KeyEvent(object):
    """退化路径用的假事件（只在环境抢不到键盘焦点时出现）。

    真实投递时 `event.widget` 由 Tk 填写，这里必须自己补上 —— 让路逻辑要读它。
    """

    def __init__(self, keysym, widget=None):
        self.keysym = keysym
        self.widget = widget


def keysym_of(seq):
    """`"<Right>"` → `"Right"`；`"<Key-m>"` → `"m"`（假事件用）"""
    inner = seq.strip("<>")
    if inner.lower().startswith("key-"):
        return inner[4:]
    return inner


def body(root, app, fp, s):
    city = city_fixture()
    mixer = Mixer()

    # 【第 11 轮·修正】投递真实按键的前提：窗口持有 Tk 的键盘焦点窗口。
    # 没有它 Tk 会把键盘事件直接丢掉（-window / when 都救不了），整组会以
    # "按键之后纹丝不动"的形式假失败 —— 后台任务里跑就会这样，前台手跑却正常。
    # 详见 _harness.take_keyboard_focus 的说明。
    delivered = [0]
    degraded = []

    def press(seq, target=None):
        """发一次按键。优先走真实事件投递（连带验证绑定链路），抢不到焦点才退化。

        `target` 指定"让谁持有焦点"（默认主窗口）。每次都得重新摆一次焦点：
        Tk 会在处理 `update()` 时因窗口失活把焦点清空，设一次不管用。
        """
        ok = take_keyboard_focus(root, target)
        if ok:
            root.event_generate(seq)
            delivered[0] += 1
        else:
            degraded.append(seq)
            app._on_key_shortcut(KeyEvent(keysym_of(seq), target))
        pump(root, 0.02)

    def displayed_frame():
        """正在显示的那一帧的序号（POS_FRAMES 给的是下一帧，要减 1）"""
        return int(app.cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1

    app.open_video(city)
    pump(root, 2.5)
    app.toggle_play()                     # 暂停 —— 逐帧步进的主要用法
    pump(root, 0.4)

    s.section("0 绑定注册方式")
    s.check(bool(root.bind_all("<Right>")) and bool(root.bind_all("<Key-m>")),
            "快捷键注册在 bind_all 上（焦点在子控件上时也能收到）")
    s.check(not root.bind("<Right>"),
            "没有同时留在 root 的 widget 绑定上（避免一条按键两条路径）")
    # 让路规则的两条铁律。这两句就是用户那个 bug 的根因，写死在这里，
    # 以后再有人把 Button 塞进"方向键让路"或把下拉框塞进"空格让路"，会立刻红。
    # （拿真控件实例问，而不是查清单 —— 清单本身也可能是错的。
    #   ttk.Combobox 是 ttk.Entry 的子类，这一点必须靠实例才验得出来。）
    probes = {
        "tk按钮": tk.Button(root),
        "ttk按钮": ttk.Button(root),
        "下拉框": ttk.Combobox(root),
        "输入框": tk.Entry(root),
        "滑块": ttk.Scale(root),
    }
    try:
        s.check(not fp.should_yield_shortcut(probes["tk按钮"], "right"),
                "按钮**不让**方向键（实测按钮完全不吃方向键）")
        s.check(not fp.should_yield_shortcut(probes["ttk按钮"], "right"),
                "ttk 按钮**不让**方向键")
        s.check(not fp.should_yield_shortcut(probes["下拉框"], "space"),
                "下拉框**不让**空格（实测只读下拉框对空格毫无反应）")
        s.check(not fp.should_yield_shortcut(probes["下拉框"], "m"),
                "下拉框**不让**字母键（虽然它是 Entry 的子类）")
        s.check(fp.should_yield_shortcut(probes["下拉框"], "down"),
                "下拉框在规则表里**让**方向键")
        s.check(app._shortcut_should_yield(probes["下拉框"], "down"),
                "列表没弹出时 ↓ 仍让给下拉框（实测 ↓ 会弹出候选列表）")
        s.check(not app._shortcut_should_yield(probes["下拉框"], "right")
                and not app._shortcut_should_yield(probes["下拉框"], "up"),
                "列表没弹出时 ←/→/↑ 归播放器（实测它们改不动下拉框的值）")
        s.check(not app._shortcut_should_yield(app.scale, "right"),
                "进度条不让方向键（语义是定位控件，按 ←/→ 应该逐帧步进）")
        s.check(fp.should_yield_shortcut(probes["输入框"], "m"),
                "输入框让字母键（要能打字）")
        s.check(fp.should_yield_shortcut(probes["滑块"], "right"),
                "滑块**让**方向键（实测 ←/→ 调值）")
        s.check(not fp.should_yield_shortcut(probes["滑块"], "m"),
                "滑块**不让**字母键")
    finally:
        for w in probes.values():
            w.destroy()

    # 按钮的空格类绑定必须已经被换掉 —— 否则"点过按钮后按空格"会去激活那个按钮
    # （"打开视频"会重新弹文件框），用户以为空格坏了。
    for cls_name in ("Button", "TButton"):
        script = root.bind_class(cls_name, "<space>") or ""
        s.check("ButtonInvoke" not in script,
                "%s 的空格类绑定已替换成播放/暂停（不再是激活按钮）" % cls_name)
    if take_keyboard_focus(root):
        s.note("窗口可拿到键盘焦点，以下用例走真实事件投递")
    else:
        s.note("抢不到键盘焦点，按键退化为直接调用绑定入口"
               "（绑定链路本身已由上面两条覆盖）")

    s.section("① 暂停态逐帧步进（像素级比对）")
    s.check(app.is_paused, "已经处于暂停态")

    # 先跳到一个确定的帧，作为步进的起点
    start = 100
    app.seek_var.set(start)
    app.finish_seek()
    pump(root, 0.4)
    s.check(displayed_frame() == start,
            "起点落在第 %d 帧（实际 %d）" % (start, displayed_frame()))
    s.check(np.array_equal(app.current_raw_frame, reference_frame(city, start)),
            "起点画面与独立解码的第 %d 帧逐像素一致" % start)

    for step in (1, 2, 3):
        press("<Right>")
        pump(root, 0.25)
        want = start + step
        s.check(displayed_frame() == want,
                "按 → 第 %d 次：位置到第 %d 帧（实际 %d）"
                % (step, want, displayed_frame()))
        s.check(np.array_equal(app.current_raw_frame, reference_frame(city, want)),
                "按 → 第 %d 次：画面就是第 %d 帧（像素一致）" % (step, want))

    press("<Left>")
    pump(root, 0.25)
    s.check(displayed_frame() == start + 2,
            "按 ← 退一帧回到第 %d 帧（实际 %d）" % (start + 2, displayed_frame()))
    s.check(np.array_equal(app.current_raw_frame, reference_frame(city, start + 2)),
            "按 ← 之后画面是第 %d 帧（像素一致）" % (start + 2))

    s.section("② 边界：不越界、不绕回")
    app.seek_var.set(0)
    app.finish_seek()
    pump(root, 0.4)
    press("<Left>")                       # 已经在第 0 帧，再退应当无动作
    pump(root, 0.3)
    s.check(displayed_frame() == 0,
            "在第 0 帧按 ← 停在原地（实际 %d），没有绕到片尾" % displayed_frame())

    last = app.total_frames - 1
    app.seek_var.set(last)
    app.finish_seek()
    pump(root, 0.4)
    pos_before = displayed_frame()
    press("<Right>")                      # 末帧再进应当无动作
    pump(root, 0.3)
    s.check(displayed_frame() >= pos_before,
            "在末帧按 → 不越界（%d → %d）" % (pos_before, displayed_frame()))

    s.section("③ ↑ / ↓ 调音量")
    app.change_volume(0.5)
    pump(root, 0.5)
    s.check(bool(mixer.channels_at(to_mixer_level(0.5), playing=True)),
            "初始音量 0.5 已生效")
    press("<Up>")
    pump(root, 0.5)
    s.check(bool(mixer.channels_at(to_mixer_level(0.55), playing=True)),
            "按 ↑ 之后音量 0.55（通道 %.0f）" % to_mixer_level(0.55))
    press("<Down>")
    press("<Down>")
    pump(root, 0.5)
    s.check(bool(mixer.channels_at(to_mixer_level(0.45), playing=True)),
            "连按两次 ↓ 之后音量 0.45（通道 %.0f）" % to_mixer_level(0.45))
    for _ in range(20):                   # 一路按到底，应当停在 0 而不是变负
        press("<Down>")
    pump(root, 0.5)
    s.check(abs(app.audio_volume) < 1e-9, "连按 ↓ 到底音量停在 0（不会变负）")

    s.section("④ M 静音 / F 全屏")
    app.change_volume(0.5)
    pump(root, 0.4)
    press("<Key-m>")
    pump(root, 0.5)
    chans = mixer.audio_channels()
    s.check(chans is not None and all(v == 0 for _, v in chans), "按 M 静音生效")
    press("<Key-m>")
    pump(root, 0.5)
    s.check(bool(mixer.channels_at(to_mixer_level(0.5), playing=True)),
            "再按 M 取消静音")

    before_fs = app.is_fullscreen
    press("<Key-f>")
    pump(root, 0.3)
    s.check(app.is_fullscreen != before_fs, "按 F 切换了全屏状态")
    press("<Key-f>")
    pump(root, 0.3)
    s.check(app.is_fullscreen == before_fs, "再按 F 切回原状态")

    s.section("⑤ J / L 快进快退")
    app.seek_var.set(100)
    app.finish_seek()
    pump(root, 0.4)
    fps = app.video_fps if app.video_fps > 0 else 25.0
    press("<Key-l>")
    pump(root, 0.4)
    forward = displayed_frame()
    s.check(abs(forward - (100 + 10 * fps)) <= 2,
            "按 L 前进约 10 秒（期望 %d，实际 %d）" % (100 + 10 * fps, forward))
    press("<Key-j>")
    pump(root, 0.4)
    back = displayed_frame()
    s.check(abs(back - 100) <= 3,
            "按 J 退回约 10 秒（期望回到 %d 附近，实际 %d）" % (100, back))

    s.section("⑥ 进度条上的方向键：逐帧步进（不让给滑块）")
    # 进度条虽然是 ttk.Scale，但语义上是"定位控件"：F1 里写的就是"←/→ 上一帧、
    # 下一帧"。让路给 ttk.Scale 的后果是"滑块只挪一小格 + 定位要等 500ms 防抖
    # 才落地"（键盘操作不会触发 ButtonRelease），用户的观感就是"←/→ 没反应"。
    # 所以这里是个**刻意的例外**（见 _on_key_shortcut 里那条 identity 判断）：
    # 只有进度条不让，音量/视差滑块照旧让。
    s.check(fp.should_yield_shortcut(app.scale, "right"),
            "例外是靠 identity 判断实现的：规则表里进度条仍算 Scale（会命中）")
    if take_keyboard_focus(root, app.scale):
        # (a) 暂停态：位置必须精确前进一帧
        #     （播放态下这一帧会被主时钟同步吸收，断不准，所以放在暂停态测）
        app.seek_var.set(100)
        app.finish_seek()
        pump(root, 0.4)
        before = displayed_frame()
        press("<Right>", target=app.scale)
        pump(root, 0.35)
        s.check(displayed_frame() == before + 1,
                "焦点在进度条上按 → 步进一帧（%d → %d）"
                % (before, displayed_frame()))
        press("<Left>", target=app.scale)
        pump(root, 0.35)
        s.check(displayed_frame() == before,
                "焦点在进度条上按 ← 退回一帧（实际 %d）" % displayed_frame())
        # 暂停态下"为拖动挂起"的标记**允许**留着：重建请求已登记，恢复播放时才兑现
        # （这是第 11 轮那条不变量，不是泄漏）。
        s.check(app._pending_audio_pos is not None,
                "暂停态步进后登记了待兑现的音频重建请求（恢复播放时对齐）")

        # (b) 播放态：这条路径上挂起必须**当场**解除，否则声音会一直哑着
        app.toggle_play()
        pump(root, 1.0)
        press("<Right>", target=app.scale)
        pump(root, 0.4)
        s.check(not app._audio_suspended_for_seek,
                "播放态在进度条上步进后，『为拖动而挂起』标记已当场解除")
        app.toggle_play()                     # 收回到暂停态，后面的用例依赖它
        pump(root, 0.8)
    else:
        s.note("（抢不到键盘焦点，跳过「进度条步进」）")
    take_keyboard_focus(root)
    pump(root, 0.2)

    s.section("⑦ 焦点落在子控件上时，不冲突的快捷键依然生效")
    # 这是本轮 bind_all 改造要防的回归：以前热点在任意子控件上（用户点过按钮
    # 之后就是这样），root 的 widget 绑定根本不执行，全盘失灵。
    # video_container 是普通 Frame：不在任何冲突清单里，是理想的"中立焦点"。
    if take_keyboard_focus(root, app.video_container):
        app.change_volume(0.5)
        pump(root, 0.4)
        press("<Key-m>", target=app.video_container)
        pump(root, 0.5)
        chans = mixer.audio_channels()
        s.check(chans is not None and all(v == 0 for _, v in chans),
                "焦点在视频区（Frame）上按 M 仍能静音")
        press("<Key-m>", target=app.video_container)
        pump(root, 0.5)
        s.check(bool(mixer.channels_at(to_mixer_level(0.5), playing=True)),
                "再按 M 取消静音")
    else:
        s.note("（抢不到键盘焦点，跳过）")
    take_keyboard_focus(root)
    pump(root, 0.2)

    s.section("⑧ 让路矩阵：焦点 × 按键（本次缺陷的回归防线）")
    # 用户报的 bug 就是"某类控件拿到焦点后一大片按键全哑"。
    # 所以不能只测"某个键在某个控件上让路"，要把矩阵跑一遍 —— 逐键判断，
    # 因为一个控件可能只吃方向键而不吃字母键（下拉框就是）。
    #
    # 这里用自建的实验台（一个独立 Toplevel），不用 app 自己的控件：
    # 测的是"让路策略"，实验台上的控件动起来不会反过来影响播放状态。
    lab = tk.Toplevel(root)
    lab_hits = []
    lab_btn = tk.Button(lab, text="实验按钮", command=lambda: lab_hits.append(1))
    lab_btn.pack()
    lab_cb = ttk.Combobox(lab, values=["甲", "乙", "丙"], state="readonly")
    lab_cb.pack()
    lab_scale = ttk.Scale(lab, from_=0, to=100, orient=tk.HORIZONTAL)
    lab_scale.pack()
    lab_entry = tk.Entry(lab)
    lab_entry.pack()
    root.update()

    # (焦点控件, 名称, 空格是否让路, 方向键是否让路, 字母键是否让路)
    # 依据全部来自实测：
    #   主窗口/视频区 → 没有任何原生含义，全生效
    #   按钮   → 空格也不让（类绑定已被替换成"播放/暂停"）；方向键/字母一概不吃
    #   下拉框 → 列表**没弹出**时方向键归播放器（弹出时才让给列表选择）；
    #            空格实测毫无反应（不让）
    #   滑块   → ←/→/↑/↓ 调值（方向键让路）；空格/字母不吃（不让）
    #   输入框 → 空格打字、方向键移光标、字母打字（全部让路）
    MATRIX = (
        (root,                "主窗口",      False, False, False),
        (app.video_container, "视频区Frame", False, False, False),
        (lab_btn,             "按钮",        False, False, False),
        (lab_cb,              "下拉框",      False, False, False),
        (lab_scale,           "滑块",        False, True,  False),
        (lab_entry,           "输入框",      True,  True,  True),
    )

    for widget, name, space_yields, arrows_yield, letters_yield in MATRIX:
        if not take_keyboard_focus(root, widget):
            s.note("（抢不到键盘焦点，跳过「%s」这一行）" % name)
            continue
        # 空格 / K：看播放状态是否被切换
        for seq, label, yields in (("<space>", "空格", space_yields),
                                   ("<Key-k>", "K", letters_yield)):
            if app.is_playing:
                app.toggle_play()
            pump(root, 0.25)
            was = app.is_playing
            press(seq, target=widget)
            pump(root, 0.35)
            acted = app.is_playing != was
            s.check(acted != yields,
                    "焦点在%s上时 %s %s（实际%s）"
                    % (name, label, "应让路" if yields else "应生效",
                       "让路" if not acted else "生效"))
        # ←/→：只看画面位置有没有变（不看 seek_var，滑块自己也会改它）
        for seq, label in (("<Right>", "→"), ("<Up>", "↑")):
            if app.is_playing:
                app.toggle_play()
            pump(root, 0.25)
            frame0 = displayed_frame()
            vol0 = app.audio_volume
            press(seq, target=widget)
            pump(root, 0.3)
            acted = (displayed_frame() != frame0) or (abs(app.audio_volume - vol0) > 1e-9)
            s.check(acted != arrows_yield,
                    "焦点在%s上时 %s %s（实际%s）"
                    % (name, label, "应让路" if arrows_yield else "应生效",
                       "让路" if not acted else "生效"))
        # 字母键 M / L（一个改静音、一个改位置，覆盖两类副作用）
        for seq, label in (("<Key-m>", "M"), ("<Key-l>", "L")):
            if app.audio_muted:
                app.toggle_mute()
            app.change_volume(0.5)
            app.seek_var.set(100)
            app.finish_seek()
            pump(root, 0.35)
            muted0 = app.audio_muted
            frame0 = displayed_frame()
            press(seq, target=widget)
            pump(root, 0.35)
            acted = (app.audio_muted != muted0) or (displayed_frame() != frame0)
            s.check(acted != letters_yield,
                    "焦点在%s上时 %s %s（实际%s）"
                    % (name, label, "应让路" if letters_yield else "应生效",
                       "让路" if not acted else "生效"))
        if app.audio_muted:
            app.toggle_mute()
        if app.is_playing:
            app.toggle_play()
        pump(root, 0.15)

    # 一次按键不许干两件事：空格在按钮上只该切换播放，不该同时激活按钮。
    if take_keyboard_focus(root, lab_btn):
        del lab_hits[:]
        if app.is_playing:
            app.toggle_play()
        pump(root, 0.25)
        was = app.is_playing
        press("<space>", target=lab_btn)
        pump(root, 0.35)
        s.check(app.is_playing != was, "焦点在按钮上时空格切换了播放")
        s.check(not lab_hits, "焦点在按钮上时空格**没有**顺便激活按钮（无双重动作）")
    else:
        s.note("（抢不到键盘焦点，跳过「按钮上空格无双重动作」）")
    if app.is_playing:
        app.toggle_play()
    pump(root, 0.2)

    lab.destroy()
    take_keyboard_focus(root)
    pump(root, 0.2)

    s.section("⑨ 点一下画面能把焦点收回来")
    # 让路是刻意保留的取舍，但必须给用户一个"一眼能想到"的恢复动作。
    take_keyboard_focus(root, app.btn_play)      # 焦点先放到按钮上
    pump(root, 0.2)
    app.video_container.event_generate("<Button-1>")
    pump(root, 0.3)
    focus_win = str(root.tk.call("focus"))
    s.check(focus_win in ("", "."),
            "点画面后键盘焦点回到主窗口（实际 %r）" % focus_win)
    if app.is_playing:
        app.toggle_play()
    pump(root, 0.25)
    was = app.is_playing
    press("<Key-k>")
    pump(root, 0.35)
    s.check(app.is_playing != was, "点画面之后 K 恢复生效")

    s.section("⑩ 没有打开文件时按键不崩")
    app._stop_playback_loop()
    app.cap.release()
    app.cap = None
    for seq in ("<Left>", "<Right>", "<Up>", "<Down>",
                "<Key-m>", "<Key-j>", "<Key-k>", "<Key-l>"):
        press(seq)
    pump(root, 0.3)
    s.check(True, "cap 为 None 时所有快捷键都不崩（能到这行即通过）")

    if degraded:
        s.note("本次有 %d 次按键走了退化路径（真实投递 %d 次）"
               % (len(degraded), delivered[0]))


if __name__ == "__main__":
    sys.exit(run_suite("快捷键与逐帧步进", body))
