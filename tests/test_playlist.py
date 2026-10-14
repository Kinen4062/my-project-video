# -*- coding: utf-8 -*-
"""播放列表与拖放套件（第 14 轮新增功能）。

覆盖四层，从里到外：
  ① 两个新模块的纯函数（`playlist_store` 的落盘、`filedrop` 的路径整理）——
     不碰 Tk，跑得飞快，坏了最好定位；
  ② 面板本身：增删清空后"列表 ↔ 界面 ↔ 磁盘"三者一致；
  ③ 播放行为：打开视频会记进列表、双击播放、上一个/下一个、**片尾自动续播**；
  ④ 拖放：拿一个**真的 WM_DROPFILES 消息**（自己造的 HDROP）打给窗口，
     走完整的"消息 → WNDPROC → 取路径 → 回 Tk 队列 → 进列表并播放"链路。

第 ④ 层为什么不"直接调 _on_files_dropped 就算了"：那样只能证明回调本身没写错，
证不了 ctypes 那一层（句柄宽度、WNDPROC 转发、DragQueryFileW 取值）是通的 ——
而那一层恰恰是这种功能唯一会崩的地方。合成 HDROP 是本套件里最值钱的几行。

⚠️ 本套件的 app 用的是**临时播放列表文件**（见 _harness.make_playlist_path），
不会碰用户真实的 %APPDATA% 列表。
"""

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import (PROJ, city_fixture, make_noaudio_fixture,  # noqa: E402
                      pump, run_suite, work_dir)

sys.path.insert(0, PROJ)
import filedrop        # noqa: E402
import playlist_store  # noqa: E402


# ============================================================
# 测试侧的"假拖放源"：造一个真的 HDROP 结构
# ============================================================
class DROPFILES(ctypes.Structure):
    """Win32 的 DROPFILES 结构：pFiles 是"文件列表相对本结构头的偏移"，
    fWide=True 表示后面是 UTF-16 字符串，每个路径以 \\0 结尾，最后再补一个 \\0。"""
    _fields_ = [("pFiles", wintypes.DWORD), ("pt", wintypes.POINT),
                ("fNC", wintypes.BOOL), ("fWide", wintypes.BOOL)]


def _k32():
    dll = ctypes.WinDLL("kernel32")
    # ⚠️ 句柄必须声明成 64 位，否则 GlobalAlloc 的返回值会被截断成 32 位
    dll.GlobalAlloc.restype = ctypes.c_void_p
    dll.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    dll.GlobalLock.restype = ctypes.c_void_p
    dll.GlobalLock.argtypes = [ctypes.c_void_p]
    dll.GlobalUnlock.argtypes = [ctypes.c_void_p]
    return dll


def make_hdrop(paths):
    """把一组路径打包成 HDROP（就是资源管理器拖放时放在剪贴板里的那种东西）"""
    k32 = _k32()
    blob = b"".join(p.encode("utf-16-le") + b"\x00\x00" for p in paths) + b"\x00\x00"
    head = DROPFILES()
    head.pFiles = ctypes.sizeof(DROPFILES)
    head.fWide = True
    total = ctypes.sizeof(DROPFILES) + len(blob)
    handle = k32.GlobalAlloc(0x0042, total)          # GMEM_MOVEABLE|GMEM_ZEROINIT
    ptr = k32.GlobalLock(handle)
    ctypes.memmove(ptr, ctypes.byref(head), ctypes.sizeof(DROPFILES))
    ctypes.memmove(ptr + ctypes.sizeof(DROPFILES), blob, len(blob))
    k32.GlobalUnlock(handle)
    return handle


def send_drop(app, paths):
    """把一次"拖放"投给主窗口（等同于用户真的把文件拖了进来）"""
    u32 = ctypes.WinDLL("user32")
    u32.SendMessageW.restype = ctypes.c_longlong
    u32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM]
    handle = make_hdrop(paths)
    hwnds = filedrop._window_chain(app.root)
    for hwnd in hwnds:
        u32.SendMessageW(wintypes.HWND(hwnd), filedrop.WM_DROPFILES,
                         wintypes.WPARAM(handle), 0)
    return len(hwnds)


def touch(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def pump_until(root, predicate, timeout=6.0):
    """泵 Tk 事件循环直到条件成立，返回是否成立（比死等固定时长稳得多）"""
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        time.sleep(0.01)
        if predicate():
            return True
    return False


def body(root, app, fp, s):
    city = city_fixture()

    # ==========================================================
    # ① 纯函数：播放列表落盘
    # ==========================================================
    s.section("① playlist_store：落盘 / 读回 / 坏文件容错")
    d = work_dir("plist_store")
    p = os.path.join(d, "pl.json")

    s.check(playlist_store.default_path().endswith("playlist.json"),
            "默认落盘位置在用户配置目录（%s）"
            % os.path.basename(os.path.dirname(playlist_store.default_path())))

    s.check(playlist_store.save([city, "D:/no/such.mp4"], p), "save() 返回成功")
    back = playlist_store.load(p)
    s.check(back == [city, "D:/no/such.mp4"],
            "读回的列表与写入一致（顺序保持不变）")
    s.check(playlist_store.load(os.path.join(d, "never_written.json")) == [],
            "文件不存在时 load() 返回空列表（不抛）")
    s.check(not [f for f in os.listdir(d) if f.startswith(".playlist-")],
            "原子写没有留下 .playlist-*.tmp 残渣")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "items": ["a.mp4", "", None, "a.MP4", "b.mp4"]}, fh)
    s.check(playlist_store.load(p) == ["a.mp4", "b.mp4"],
            "坏条目（空串/非字符串）与重复项（Windows 大小写不敏感）都被清掉")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("{ 这不是 json")
    s.check(playlist_store.load(p) == [], "JSON 损坏时 load() 返回空列表（不抛）")

    # ==========================================================
    # ② 纯函数：拖进来的路径怎么整理
    # ==========================================================
    s.section("② filedrop.expand_dropped_paths：过滤 / 递归 / 去重 / 上限")
    tree = work_dir("drop_tree")
    a = touch(os.path.join(tree, "a.mp4"))
    b = touch(os.path.join(tree, "b.MP4"))          # 大写扩展名也要认
    touch(os.path.join(tree, "c.txt"))              # 不是视频
    sub = touch(os.path.join(tree, "season2", "d.mkv"))

    got = filedrop.expand_dropped_paths([tree])
    s.check(got == [a, b, sub],
            "目录会被递归展开：认 .mp4/.MP4/.mkv，丢掉 .txt，每个目录内按名排序")
    s.check(filedrop.expand_dropped_paths([a, os.path.join(tree, "c.txt")]) == [a],
            "直接拖单个文件：非视频扩展名被丢掉")
    s.check(filedrop.expand_dropped_paths([a, a]) == [a], "同一路径拖两次只算一次")
    s.check(len(filedrop.expand_dropped_paths([tree, tree])) == 3,
            "同一个目录拖两次不会重复展开")
    s.check(len(filedrop.expand_dropped_paths([tree], max_files=2)) == 2,
            "max_files 上限生效（防止手滑把整个盘拖进来）")
    s.check(filedrop.is_video_file("X:/a/b/c.MKV") and not filedrop.is_video_file("x.jpg"),
            "is_video_file 对扩展名大小写不敏感")

    # ==========================================================
    # ③ 面板：列表 ↔ 界面 ↔ 磁盘 三者一致
    # ==========================================================
    s.section("③ 播放列表面板：增 / 删 / 清空 / 计数 / 落盘")
    s.check(app.playlist == [] and app.playlist_box.size() == 0,
            "新开的窗口列表是空的（用的是隔离后的临时列表文件）")
    s.check(app._dnd_enabled, "文件拖放已在窗口上挂好（Windows）")
    s.check(app.playlist_frame.grid_info().get("column") == 1,
            "列表栏挂在第 1 列（视频区右侧）")

    extra = [touch(os.path.join(work_dir("plist_add"), n)) for n in ("x1.mp4", "x2.mp4")]
    added = app._playlist_add([city, extra[0]])
    s.check(added == 2, "加入 2 个新文件")
    s.check(app._playlist_add([city]) == 0, "重复加入同一个文件返回 0（不产生重复项）")
    s.check(app.playlist_box.size() == 2, "界面上的条目数跟着列表走")
    s.check(app.playlist_box.get(0) == os.path.basename(city),
            "列表项显示文件名（完整路径在选中时显示到状态行）")
    s.check("2" in app.playlist_title.cget("text"), "标题里的计数已更新")
    s.check(playlist_store.load(app._playlist_path) == app.playlist,
            "每次改动都立刻落盘（这三条路径：列表/界面/磁盘 一致）")

    app.playlist_box.selection_clear(0, "end")
    app.playlist_box.selection_set(0)
    app.playlist_remove_selected()
    s.check(app.playlist == [extra[0]] and app.playlist_box.size() == 1,
            "移除选中项：列表与界面同步")
    s.check(playlist_store.load(app._playlist_path) == app.playlist, "移除也会落盘")

    app._playlist_add([city])
    app.playlist_clear(confirm=False)
    s.check(app.playlist == [] and app.playlist_box.size() == 0, "清空后列表与界面都空")
    s.check(playlist_store.load(app._playlist_path) == [], "清空也会落盘")

    # ==========================================================
    # ④ 打开视频 → 自动记进列表
    # ==========================================================
    s.section("④ 播放的视频会被记录进列表（需求原文）")
    app.open_video(city)
    pump(root, 0.3)
    s.check(city in app.playlist, "用【打开已有3D视频】播放的文件进了列表")
    s.check(app.playlist_box.curselection() and
            app.playlist_box.get(app.playlist_box.curselection()[0]) == os.path.basename(city),
            "当前正在播的那一项被高亮出来")
    s.check(playlist_store.load(app._playlist_path) == [city], "落盘了")

    # 转换产物（临时文件）不该进列表
    n_before = len(app.playlist)
    app.open_video(city, record=False)
    pump(root, 0.3)
    s.check(len(app.playlist) == n_before,
            "record=False（转换产物）不会被记进列表 —— 它有 %TEMP% 里会被删掉")

    # ==========================================================
    # ⑤ 双击 / 上一个 / 下一个
    # ==========================================================
    s.section("⑤ 双击播放、上一个/下一个（两端循环）")
    short_a = make_noaudio_fixture(
        os.path.join(work_dir("plist_play"), "na_a.mp4"), city, seconds=0.6)
    short_b = make_noaudio_fixture(
        os.path.join(work_dir("plist_play"), "na_b.mp4"), city, seconds=0.6)
    app.playlist_clear(confirm=False)
    app._playlist_add([short_a, short_b])

    app.playlist_box.selection_clear(0, "end")
    app.playlist_box.selection_set(1)
    app.playlist_play_selected()
    pump(root, 0.3)
    s.check(os.path.normcase(app.current_video_path) == os.path.normcase(short_b),
            "双击第 2 项 → 真的开始播它")

    app.playlist_next()
    pump(root, 0.3)
    s.check(os.path.normcase(app.current_video_path) == os.path.normcase(short_a),
            "【下一个】在列表末尾回到第 1 项")
    app.playlist_prev()
    pump(root, 0.3)
    s.check(os.path.normcase(app.current_video_path) == os.path.normcase(short_b),
            "【上一个】在列表开头回到最后一项")

    # ==========================================================
    # ⑥ 片尾自动续播
    # ==========================================================
    s.section("⑥ 片尾自动续播（列表尾回到第 1 项）")
    app.playlist_clear(confirm=False)
    app._playlist_add([short_a, short_b])
    app.open_video(short_a)
    s.check(app._advance_playlist() is not None,
            "列表里有两个时，片尾能算出下一项")
    ok = pump_until(root, lambda: os.path.normcase(app.current_video_path)
                    == os.path.normcase(short_b), timeout=8.0)
    s.check(ok, "第一个播完自动切到第二个（真实短片，不是调函数糊出来的）")

    app.playlist_clear(confirm=False)
    app._playlist_add([short_a])
    app.open_video(short_a)
    s.check(app._advance_playlist() is None,
            "列表里只剩当前这一项时返回 None —— 老用法走原来的无缝回绕，不重开文件")

    app.autoplay_next = False
    app._playlist_add([short_b])
    s.check(app._advance_playlist() is None,
            "关掉 autoplay_next 后即使列表有两个也返回 None（老套件用的就是这个）")
    app.autoplay_next = True

    app.playlist_clear(confirm=False)
    app.open_video(city, record=False)
    s.check(app._advance_playlist() is None, "列表为空时返回 None（不崩）")

    # ==========================================================
    # ⑦ 拖放：真的投一条 WM_DROPFILES 给窗口
    # ==========================================================
    s.section("⑦ 拖放：整条链路（合成 HDROP → 窗口消息 → 进列表并播放）")
    app.playlist_clear(confirm=False)
    n_hwnd = send_drop(app, [city, os.path.join(PROJ, "run_tests.py")])
    s.check(n_hwnd >= 1, "窗口链上至少挂了一个拖放目标（实际 %d 个）" % n_hwnd)
    ok = pump_until(root, lambda: city in app.playlist, timeout=5.0)
    s.check(ok, "拖进来的 .mp4 进了播放列表")
    s.check(not any(p.endswith("run_tests.py") for p in app.playlist),
            "同一个拖放里的非视频文件被过滤掉了")
    ok = pump_until(root, lambda: os.path.normcase(app.current_video_path)
                    == os.path.normcase(city), timeout=5.0)
    s.check(ok, "拖进来之后自动开始播放它（需求原文）")
    s.check(len(app.playlist) == 1, "拖进来的两个文件只留下 1 个视频条目")

    app.playlist_clear(confirm=False)
    junk = touch(os.path.join(work_dir("drop_junk"), "readme.txt"), b"not a video")
    send_drop(app, [junk])
    pump(root, 0.3)
    s.check(app.playlist == [] and "没有能播的" in app.playlist_status.cget("text"),
            "拖进非视频：不加入列表，状态行给出说明（不弹模态框）")

    # ==========================================================
    # ⑧ 播放列表上的快捷键让路
    # ==========================================================
    s.section("⑧ 焦点在播放列表上时的按键归属")
    s.check(fp.should_yield_shortcut(app.playlist_box, "left"),
            "方向键让给列表（它的原生含义就是移动选中）")
    s.check(fp.should_yield_shortcut(app.playlist_box, "space"),
            "空格让给列表（选中当前项）")
    s.check(not fp.should_yield_shortcut(app.playlist_box, "m"),
            "M 仍然归播放器（列表对字母键毫无反应，让路就成哑巴了）")
    s.check(not fp.should_yield_shortcut(app.playlist_box, "k"),
            "K（播放/暂停）也归播放器")

    # ==========================================================
    # ⑨ 列表栏显隐
    # ==========================================================
    s.section("⑨ 列表栏显隐")
    app.toggle_playlist()
    root.update()
    s.check(not app._playlist_visible and not app.playlist_frame.winfo_ismapped(),
            "隐藏后列表栏不再显示")
    s.check(int(app.root.grid_columnconfigure(1)["minsize"]) == 0,
            "隐藏时把第 1 列的 minsize 归零（否则视频区右边留一条空带）")
    app.toggle_playlist()
    root.update()
    s.check(app._playlist_visible and app.playlist_frame.winfo_ismapped(),
            "再点一次恢复显示")
    s.check(int(app.root.grid_columnconfigure(1)["minsize"]) == fp.PLIST_MIN_WIDTH,
            "恢复后列宽也还原")

    # ==========================================================
    # ⑩ 边界：列表里的文件被删了
    # ==========================================================
    s.section("⑩ 列表里的文件不在了（自动续播要能跳过它）")
    # 刻意**不创建**这个文件：模拟"列表记着，但文件已经被删/挪走"
    gone = os.path.join(work_dir("plist_gone"), "vanished.mp4")
    app.playlist = [short_a, gone]
    app._refresh_playlist_ui()
    app.open_video(short_a)
    s.check(app._advance_playlist() is None,
            "下一个条目文件不存在 → 跳过它；没有别的可播就返回 None（不会卡死重试）")
    s.check(gone in app._playlist_failed,
            "不存在的条目被记进「播不动」名单，下次不再重试")


if __name__ == "__main__":
    # 本套件是唯一打开"片尾自动续播"的套件（默认关，见 _harness.load_app）
    sys.exit(run_suite("播放列表与拖放", body, autoplay_next=True))
