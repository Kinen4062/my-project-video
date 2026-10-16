# -*- coding: utf-8 -*-
"""半宽 SBS 套件（第 25 轮）。

要防的那件事：
    转换产物动辄 32:9（比如 2560×720），全屏时上下各留一大条 ——「左右两份
    各占半个屏幕、正好铺满」这个效果怎么做都做不出来。

做法：转换改成输出**半宽 SBS**（每半横向压一半，整幅 = 源尺寸 = 16:9），
播放时按同一个约定把每半拉回 2 倍再合成。**两件事必须同时成立** ——
只改转换就是「转出来是扁的」，只改播放就是「没转的也被拉长」。

本套件的分层（从下往上，下面绿了上面才不会假绿）：
    ① settings_store：往返、坏数据降级、白名单
    ② depth3d 几何：半宽源合成出来的尺寸 == 全宽源
    ③ depth3d 内容：两条路径**逐像素**比 —— 尺寸对，不代表内容对
    ④ 边上行为：2D 模式不受影响、交换左右眼、视差用对网格
    ⑤ 转换输出：真跑一次转换，量产物的宽度
    ⑥ 接线：勾选框 invoke / H 键真派发 / 绑定真的存在（第 19 轮 V 键的教训）
    ⑦ 重绘：改完设置，**画面本身**的尺寸跟着变

③ 和 ⑦ 是这套的骨头：前者证明「真的拉对了」，后者证明「真的重画了」。
两条都不查变量 —— 变量对了而画面没变，是完全可能发生的事。
"""

import os
import sys
import uuid

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import (PROJ, city_fixture, make_noaudio_fixture,  # noqa: E402
                      make_settings_path, pump, run_suite,
                      take_keyboard_focus, work_dir)

# 本节要单独测 settings_store（不经过 app），所以得自己把项目根塞进 sys.path ——
# 不能指望 load_app 已经跑过（它在 run_suite 里才被调用）。
sys.path.insert(0, PROJ)
import settings_store  # noqa: E402

# 转换产物要落到磁盘，而沙箱**覆盖不了已存在的文件**（症状是套件第一次绿、
# 第二次红，报错点还看着不像权限问题）→ 每次跑都用带 pid + 随机串的新名字。
_RUN_TAG = "%d_%s" % (os.getpid(), uuid.uuid4().hex[:6])


def _smooth_image(w, h, seed=0):
    """一张**低频**测试图：横向渐变 + 几块纯色。

    为什么不用随机噪声：半宽路径有一次「缩小 → 放大」的往返，高频内容在
    这一步必然掉信息（这是抽样的物理事实，不是 bug）。用低频图，往返误差
    小到可以拿「逐像素几乎一致」当判据 —— 而判据本身仍然是像素。
    """
    rng = np.random.RandomState(seed)
    base = np.tile(np.linspace(0, 255, w, dtype=np.float32), (h, 1))
    img = np.dstack([base, np.flipud(base), (base + 60.0) % 256.0]).astype(np.uint8)
    for _ in range(4):
        cx, cy = rng.randint(0, max(1, w // 2)), rng.randint(0, max(1, h // 2))
        bw, bh = rng.randint(w // 5, max(w // 5 + 1, w // 3)), rng.randint(h // 5, max(h // 5 + 1, h // 3))
        img[cy:cy + bh, cx:cx + bw] = rng.randint(40, 220, 3)
    return img


def _make_pair(w=320, h=180):
    """造一对「左眼 / 右眼」图（内容不同 —— 不然交换左右眼验不出来）。"""
    return _smooth_image(w, h, seed=0), _smooth_image(w, h, seed=7)


def _sources(left, right):
    """同样的两只眼，排成两种源：全宽（每半原样）与半宽（每半压一半）。"""
    h, w = left.shape[:2]
    full = np.hstack([left, right])
    half = np.hstack([cv2.resize(left, (w // 2, h), interpolation=cv2.INTER_AREA),
                      cv2.resize(right, (w // 2, h), interpolation=cv2.INTER_AREA)])
    return full, half


def section_settings(s):
    s.section("① settings_store：白名单 + 坏数据降级")

    path = make_settings_path()
    s.check(settings_store.load(path)["half_sbs"] is True,
            "文件不存在 → 用默认值（半宽默认开）")

    s.check(settings_store.save({"half_sbs": False}, path) is True, "save 返回 True")
    s.check(settings_store.load(path)["half_sbs"] is False,
            "往返：存成关之后读回来就是关的")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{这不是 JSON")
    s.check(settings_store.load(path)["half_sbs"] is True,
            "坏 JSON → 退回默认值，不抛（配置文件坏了不该让播放器起不来）")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"half_sbs": 1}')
    s.check(settings_store.load(path)["half_sbs"] is True,
            "类型不对（1 而不是 true/false）→ 丢掉用默认值"
            "（用 type() 判而不是 isinstance，否则 1 会被当成布尔收下）")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write('["根本不是字典"]')
    s.check(settings_store.load(path)["half_sbs"] is True, "不是字典 → 退回默认值")

    settings_store.save({"half_sbs": False, "没人认识的键": 123}, path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    s.check("half_sbs" in text and "没人认识的键" not in text,
            "落盘只写白名单里的键（多余的一律不进文件）")

    s.check(settings_store.load()["half_sbs"] in (True, False),
            "不传路径时走用户配置目录，且读得到合法值（不抛）")


def section_geometry(s, fp):
    s.section("② 几何：半宽源合成出来的尺寸 == 全宽源")

    left, right = _make_pair()
    h, w = left.shape[:2]
    full, half = _sources(left, right)
    mesh = fp.depth3d.MeshCache()

    out_full = fp.depth3d.process_3d(full, "红蓝3D", False, 0, mesh)
    out_half = fp.depth3d.process_3d(half, "红蓝3D", False, 0, mesh, source_half=True)

    s.check(out_full.shape[:2] == (h, w),
            "全宽源：合成画面就是一只眼的尺寸 (%d, %d)" % (h, w))
    s.check(out_half.shape[:2] == (h, w),
            "半宽源：合成出来**也是**一只眼的尺寸 —— 每半真的被拉回 2 倍了")

    wrong = fp.depth3d.process_3d(half, "红蓝3D", False, 0, mesh)
    s.check(wrong.shape[:2] == (h, w // 2),
            "对照：忘了拉伸时只有半幅宽 %d —— 与正确路径差了整整一倍"
            % (w // 2,))


def section_content(s, fp):
    s.section("③ 内容：两条路径逐像素比（尺寸对 ≠ 内容对）")

    left, right = _make_pair()
    h, w = left.shape[:2]
    full, half = _sources(left, right)
    mesh = fp.depth3d.MeshCache()

    out_full = fp.depth3d.process_3d(full, "红蓝3D", False, 0, mesh)
    out_half = fp.depth3d.process_3d(half, "红蓝3D", False, 0, mesh, source_half=True)

    diff = np.abs(out_half.astype(np.int16) - out_full.astype(np.int16))
    mean_diff = float(diff.mean())
    s.check(mean_diff < 3.0,
            "同一份内容，半宽路径与全宽路径合成出来几乎一模一样"
            "（平均差 %.2f < 3.0；差值只来自「缩小再放大」这一次有损往返）"
            % mean_diff)

    # 判据为什么是 3.0：低频图「缩小 → 放大」一次往返实测 ≈0.6。真正的错误做法
    # （忘了拉伸）尺寸就已经不对了，会被 ② 挡下；所以这里的 3.0 只需要拦住
    # 「拉伸了，但拉的不是同一只眼」这类错误。
    #
    # ⚠️ 对**红蓝模式**而言，「先拉伸每半再合成」和「先合成再整体拉伸」在数学上
    # 是等价的（合成是逐像素的通道操作，resize 对每个通道独立）—— 所以别指望
    # 用「顺序颠倒」当反例来验它。下面的交叉验证才是能真正分开两条实现的那把尺。
    manual = np.hstack([
        cv2.resize(half[:, :w // 2], (w, h), interpolation=cv2.INTER_LINEAR),
        cv2.resize(half[:, w // 2:], (w, h), interpolation=cv2.INTER_LINEAR)])
    manual_out = fp.depth3d.process_3d(manual, "红蓝3D", False, 0, mesh)
    s.check(np.array_equal(manual_out, out_half),
            "交叉验证：`source_half=True` 与「手工把源恢复成正常比例、再走普通路径」"
            "逐像素完全相同 —— 切片位置和插值方式都没有跑偏")


def section_edges(s, fp):
    s.section("④ 边上行为：2D 模式 / 交换左右眼 / 视差")

    left, right = _make_pair()
    h, w = left.shape[:2]
    full, half = _sources(left, right)
    mesh = fp.depth3d.MeshCache()
    base = fp.depth3d.process_3d(half, "红蓝3D", False, 0, mesh, source_half=True)

    out2d = fp.depth3d.process_3d(half, "2D", False, 0, mesh, source_half=True)
    s.check(out2d.shape == half.shape and np.array_equal(out2d, half),
            "2D 模式不受半宽影响：原帧原样返回 —— 两份压扁的画面并排铺满，"
            "这正是半宽 SBS 该有的样子（不戴眼镜看时就是它）")

    swapped = fp.depth3d.process_3d(half, "红蓝3D", True, 0, mesh, source_half=True)
    s.check(swapped.shape == base.shape and not np.array_equal(swapped, base),
            "半宽下交换左右眼仍然有效（说明拉伸发生在交换**之前**）")

    for mode in ("偏振-隔行", "偏振-隔列", "偏振-棋盘"):
        out = fp.depth3d.process_3d(half, mode, False, 0, mesh, source_half=True)
        s.check(out.shape[:2] == (h, w), "%s：半宽下尺寸也正确" % mode)

    parallax = fp.depth3d.process_3d(half, "红蓝3D", False, 5, mesh, source_half=True)
    s.check(parallax.shape[:2] == (h, w),
            "半宽 + 视差：尺寸不变（视差网格用的是**拉伸后**的宽度 %d，"
            "用错成 %d 会直接抛错）" % (w, w // 2))
    s.check(not np.array_equal(parallax, base), "半宽 + 视差：画面真的动了")


def section_conversion(s, root, app):
    s.section("⑤ 转换输出：真跑一次转换，量产物宽度")

    src = make_noaudio_fixture(
        os.path.join(work_dir("_halfsbs"), "src_%s.mp4" % _RUN_TAG),
        src=city_fixture(), seconds=0.2)
    cap = cv2.VideoCapture(src)
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    s.note("源素材 %d×%d（city.mp4 取 0.2 秒）。第一次转换要顺带加载深度模型，"
           "这段会慢几秒到十几秒 —— 属于一次性成本。" % (src_w, src_h))

    for want_half in (True, False):
        label = "半宽" if want_half else "全宽"
        app.half_sbs = want_half
        app.half_sbs_var.set(want_half)

        dst = os.path.join(work_dir("_halfsbs"), "out_%s_%s.mp4" % (label, _RUN_TAG))
        # 产物路径由调用方钉死：一是拿得到产物（真实路径是 after 回调里才传出来的），
        # 二是避免测试去猜 %TEMP% 里的文件名。**不能 pump 事件** —— 那会把
        # on_conversion_done 排的回调放出来弹"要另存吗"，套件就挂住了。
        app._make_temp_output_path = lambda p=dst: p
        app.process_video_to_3d(src)

        s.check(os.path.isfile(dst), "%s：转换真的产出了文件" % label)
        if not os.path.isfile(dst):
            s.note("产物没生成 —— 多半是深度模型加载失败（本机上模型应在 "
                   "%USERPROFILE%/.cache/huggingface 下）。")
            continue

        out_cap = cv2.VideoCapture(dst)
        out_w = int(out_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        out_h = int(out_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        out_cap.release()

        expect_w = src_w if want_half else src_w * 2
        s.check(out_w == expect_w and out_h == src_h,
                "%s：产物是 %d×%d（期望 %d×%d）%s"
                % (label, out_w, out_h, expect_w, src_h,
                   "—— 整幅跟源一样宽，全屏天然铺满" if want_half
                   else "—— 每半原样并排，还是老行为"))
        if want_half:
            s.check(out_w % 2 == 0 and (out_w // 2) * 2 == out_w,
                    "半宽产物宽度是偶数（每半等宽，不会被截出 1 像素的差）")

    # 收尾：把转换状态恢复成"没在转换"，别把后面几节带偏
    app.half_sbs = True
    app.half_sbs_var.set(True)


def section_wiring(s, root, app):
    s.section("⑥ 接线：勾选框 / H 键 / Tk 绑定真的存在")

    app.half_sbs_check.invoke()
    now = app.half_sbs
    s.check(app.half_sbs_var.get() is now, "点勾选框 → 变量与状态一致")
    s.check(settings_store.load(app._settings_path)["half_sbs"] is now,
            "点勾选框 → 立刻落盘（不落盘的话下次启动就回去了）")
    app.half_sbs_check.invoke()
    s.check(app.half_sbs is not now, "再点一次能切回来")

    s.check(bool(root.bind_all("<Key-h>")),
            "H 键**真的绑到了 Tk**（第 19 轮 V 键的教训：只在表里、没绑上"
            "＝真机无反应而套件全绿）")

    # H 键走真实派发链 —— `_on_key_shortcut` 开头有 `cap is None` 的提前返回，
    # 所以必须先真打开一个视频，否则这条测的是"没反应"而不是"切换了"。
    app.open_video(city_fixture())
    pump(root, 0.4)
    app.half_sbs = True
    app.half_sbs_var.set(True)
    take_keyboard_focus(root)
    root.update()
    root.event_generate("<Key-h>")
    root.update()
    s.check(app.half_sbs is False, "H 键（真实 Tk 派发）把半宽切成了关")
    # ⚠️ 用 `var.get()` 而不是 `checkbutton.select()`：Tk 的 `select()` 是
    #    **动作**（把勾选框置为选中），不是查询 —— 实测它恒返回 None，
    #    写成 `select() == 0` 会永远失败（这一条正是这么被套件自己抓出来的）。
    s.check(app.half_sbs_var.get() is False,
            "H 键与勾选框变量同步（两条路走的是同一个动作，不会各改各的）")
    root.event_generate("<Key-h>")
    root.update()
    s.check(app.half_sbs is True, "再按一次 H 键切回来")


def section_repaint(s, root, app):
    s.section("⑦ 重绘：改完设置，画面本身的尺寸跟着变")

    app.mode_var.set("红蓝3D")
    app.change_display_mode()
    pump(root, 0.3)
    s.check(app.current_raw_frame is not None, "有原始帧可用（打开视频之后）")

    app.half_sbs = False
    app.half_sbs_var.set(False)
    app.current_frame_image = app._process_3d(app.current_raw_frame)
    w_before = app.current_frame_image.shape[1]

    app.half_sbs_var.set(True)
    app.on_half_sbs_toggle()            # 这一步应当自己重绘
    w_after = app.current_frame_image.shape[1]

    s.check(w_after == 2 * w_before,
            "勾上之后**画面本身**从 %d 宽变成 %d 宽（2 倍）—— 证明真的重画了，"
            "而不是只改了变量" % (w_before, w_after))
    s.check(app._last_display_image is not None
            and app._last_display_image.size[0] > 0,
            "上屏用的那一屏也重算过了（截图/显示取的就是它）")


def body(root, app, fp, s):
    section_settings(s)
    section_geometry(s, fp)
    section_content(s, fp)
    section_edges(s, fp)
    section_conversion(s, root, app)
    section_wiring(s, root, app)
    section_repaint(s, root, app)


if __name__ == "__main__":
    sys.exit(run_suite("半宽 SBS", body))
