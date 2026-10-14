# -*- coding: utf-8 -*-
"""第 18 轮：双语字幕（中文 + 英文）。

要防的那件事：
    视频旁边躺着 `电影.srt`（或者 `电影.zh.srt` + `电影.en.srt`），播放器却
    一个字都不显示 —— 要么界面里得有"字幕"这回事。

需求拆成一句可验的话：
    支持中文字幕与英文字幕的**双语字幕**，其它语言留扩展位、暂不做。

本套件的分层（从下到上，下面绿了上面才不会假绿）：
    ① `subtitle.parse`：SRT / VTT 的解析与各种脏数据
    ② 编码嗅探：UTF-8 / BOM / GBK / UTF-16，乱码与二进制垃圾都不许抛
    ③ 认语种（两条路）：文件名标签、内容嗅探；日文不许被当成中文
    ④ 找同名字幕：同目录、subs 子目录、只认自己那份、优先级
    ⑤ 组装：一中一英两个文件、单文件双语（一个 Track 占两个槽）、显式指定
    ⑥ 时间轴查表：左右闭开区间、空隙、重叠字幕的回退、片尾之后
    ⑦ 渲染层：文字真画出来了（有白字也有黑描边）、按内容缓存、折行
    ⑧ 端到端（app）：打开视频自动认领 + **切换模式时画面像素真的变了**
    ⑨ 降级：关掉总开关 / 关掉字幕 / 没有字幕文件 / 字幕文件是垃圾

⑧ 那一节是本套件最要紧的一层，而且它**不查变量、只查像素**：单元测试全绿、
画面上一个字都没有，是完全可能发生的（算了半天 rows 却没接到渲染那一步），
只有像素能证明它真的上屏了。
"""

import os
import shutil
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import (PROJ, city_fixture, run_suite,  # noqa: E402
                      take_keyboard_focus, work_dir)

if PROJ not in sys.path:
    sys.path.insert(0, PROJ)

import numpy as np  # noqa: E402

import subtitle as sub  # noqa: E402

# 本次进程专属的目录后缀：pid + 随机串，保证**永不与上一次跑重名**
# （理由见 `_fresh` —— 沙箱里删不掉旧文件，只能靠换名字避免覆盖）。
_RUN_TAG = "%d_%s" % (os.getpid(), uuid.uuid4().hex[:6])

# 三种素材：纯中文、纯英文、单文件双语（字幕组最常见的那种）。
SRT_ZH = ("1\n00:00:01,000 --> 00:00:20,000\n"
          "这是一句中文字幕\n")
SRT_EN = ("1\n00:00:01,000 --> 00:00:20,000\n"
          "This is an English subtitle line\n")
SRT_BILINGUAL = ("1\n00:00:01,000 --> 00:00:20,000\n"
                 "这是一句中文字幕\n"
                 "This is an English subtitle line\n")
# 时间轴专用的素材：中间留空隙，并且故意让一条短字幕插在长字幕里面。
SRT_TIMELINE = ("1\n00:00:01,000 --> 00:00:02,000\n你好\n\n"
                "2\n00:00:04,000 --> 00:00:09,000\n长字幕\n\n"
                "3\n00:00:05,000 --> 00:00:06,000\n短字幕\n")


def _fresh(name):
    """给一节一个**全新的**子目录。

    ⚠️ 为什么是"每次换新名字"而不是"先清空固定目录"（本轮踩过）：
    开发沙箱里**删除已存在的文件是被拒绝的**（`os.remove` / `shutil.rmtree`
    直接抛 `[WinError 5] 拒绝访问`），于是"开跑前 rmtree 一下"会静默失败
    （`ignore_errors=True`），上一轮跑剩下的 `movie.mp4` 还在，紧接着
    `shutil.copy` 想覆盖它又是 WinError 5 —— 症状是**同一个套件第一次跑绿、
    第二次跑红**（实测复现）。

    换成"每次跑一个带 pid + 随机串的新目录"就完全没有这个问题：
    全程只创建新文件、一个字节都不覆盖，宿主机和沙箱行为一致。
    目录本身由 `run_tests.py` 收尾时统一清（清不掉也无所谓，都在 %TEMP% 里）。
    """
    path = os.path.join(work_dir("subtitles"), "run_" + _RUN_TAG, name)
    os.makedirs(path, exist_ok=True)
    return path


def _write(path, text, encoding="utf-8"):
    with open(path, "w", encoding=encoding) as fh:
        fh.write(text)
    return path


def _touch(path, data=b"not a real video"):
    with open(path, "wb") as fh:
        fh.write(data)
    return path


class _Key:
    """伪造一个 Tk 键盘事件（只需要 keysym 和 widget 两个字段）。"""

    def __init__(self, keysym, widget=None):
        self.keysym = keysym
        self.widget = widget


def body(root, app, fp, s):
    city = city_fixture()

    # ============================================================
    s.section("① 解析 SRT / VTT")
    # ============================================================
    basic = ("1\r\n00:00:01,000 --> 00:00:03,500\r\n你好，世界\r\n\r\n"
             "2\r\n00:00:03,500 --> 00:00:06,000\r\n第一行\r\n第二行\r\n")
    cues = sub.parse(basic)
    s.check(len(cues) == 2, "两条字幕都读出来了（实测 %d 条）" % len(cues))
    s.check(abs(cues[0]["start"] - 1.0) < 1e-6 and abs(cues[0]["end"] - 3.5) < 1e-6,
            "时间轴解析正确（1.000 → 3.500）")
    s.check(cues[0]["lines"] == ["你好，世界"], "文字行原样读出")
    s.check(cues[1]["lines"] == ["第一行", "第二行"],
            "多行字幕两行都保留（CRLF 也认）")

    vtt = ("WEBVTT\n\n"
           "cue-1\n00:01.000 --> 00:03.000 align:middle line:90%\nHello there\n\n"
           "00:04.000 --> 00:05.000\nShort one\n")
    cues = sub.parse(vtt)
    s.check(len(cues) == 2, "VTT 也认（含 cue 名字与 align / line 设置项）")
    s.check(abs(cues[0]["start"] - 1.0) < 1e-6,
            "省略小时的 MM:SS.mmm 时间轴也认")

    messy = ("1\n不是时间轴的一行\n00:00:10,000 --> 00:00:12,000\n后面的\n\n"
             "2\n00:00:05,000 --> 00:00:07,000\n前面的\n\n"
             "3\n00:00:20,000 --> 00:00:19,000\n时间写反了\n")
    cues = sub.parse(messy)
    s.check([c["start"] for c in cues] == [5.0, 10.0, 20.0],
            "乱序的按开始时间排好、前面那行废话被跳过")
    s.check(cues[-1]["end"] == cues[-1]["start"],
            "结束时间早于开始 → 当成零长保留，不整条丢掉")

    tagged = ("1\n00:00:00,000 --> 00:00:02,000\n"
              "<i>斜体中文</i>\n{\\an8}Line with {\\pos(1,2)}tags\n\n"
              "2\n00:00:02,000 --> 00:00:03,000\n第一行\\N第二行\n")
    cues = sub.parse(tagged)
    s.check(cues[0]["lines"] == ["斜体中文", "Line with tags"],
            "HTML 标签与 ASS 覆盖标签都被剥掉")
    s.check(cues[1]["lines"] == ["第一行 第二行"], r"ASS 的 \N 换成空格")

    s.check(sub.parse("") == [] and sub.parse("随便写点什么\n第二行") == [],
            "空文本 / 不是字幕的文本 → 空表，不抛异常")

    # ============================================================
    s.section("② 编码嗅探")
    # ============================================================
    d = _fresh("encoding")
    written = {}
    for name, encoding in (("u8.srt", "utf-8"), ("u8bom.srt", "utf-8-sig"),
                           ("gbk.srt", "gbk"), ("u16.srt", "utf-16")):
        written[encoding] = _write(os.path.join(d, name), SRT_ZH, encoding)
    for encoding, path in written.items():
        text, used = sub.read_text(path)
        s.check("这是一句中文字幕" in text,
                "%s 读出来不是乱码（实际认出 %s）" % (encoding, used))

    u16le = os.path.join(d, "u16le.srt")
    with open(u16le, "wb") as fh:
        fh.write(SRT_ZH.encode("utf-16-le"))
    text, used = sub.read_text(u16le)
    s.check("这是一句中文字幕" in text,
            "没有 BOM 的 UTF-16LE 也认（靠隔一个字节一个 0 的特征，认出 %s）" % used)

    garbage = os.path.join(d, "garbage.srt")
    with open(garbage, "wb") as fh:
        fh.write(bytes(range(256)) * 4)
    text, used = sub.read_text(garbage)
    s.check(isinstance(text, str), "二进制垃圾不抛异常（硬解成 %s）" % used)
    s.check(sub.parse(text) == [], "垃圾内容解析出 0 条字幕")

    s.check(sub.read_text(os.path.join(d, "根本不存在.srt")) == ("", ""),
            "文件不存在 → 返回空，不抛")

    # ============================================================
    s.section("③ 认语种（文件名 / 内容）")
    # ============================================================
    name_cases = [
        ("电影.srt", None), ("电影.zh.srt", "zh"), ("电影.chs.srt", "zh"),
        ("电影.cht.srt", "zh"), ("电影.sc.srt", "zh"), ("电影.简体中文.srt", "zh"),
        ("电影.中文.srt", "zh"),
        ("The.Show.2024.en.srt", "en"), ("movie.eng.srt", "en"),
        ("movie.English.srt", "en"),
        ("movie.中英.srt", "both"), ("movie.chs&eng.srt", "both"),
        ("movie.zh-en.srt", "both"),
        ("Enchanted.srt", None), ("movie.mp4", None),
    ]
    wrong = []
    for filename, expect in name_cases:
        got = sub.language_from_name(filename)
        if got != expect:
            wrong.append("%s→%s(应 %s)" % (filename, got, expect))
    s.check(not wrong, "文件名认语种 %d 例全对（错的是 %s）"
            % (len(name_cases), wrong or "无"))
    s.check(sub.language_from_name("Enchanted.srt") is None,
            "标题里含 en 不会被误认（只认整段 token）")

    s.check(sub.sniff_language("你好，这是一段中文字幕，用来判断语言") == "zh",
            "纯中文内容 → zh")
    s.check(sub.sniff_language("This is an English subtitle line for testing") == "en",
            "纯英文内容 → en")
    s.check(sub.sniff_language("你好，世界\nHello, world\n这是第二句中文") == "both",
            "中英混排内容 → both")
    s.check(sub.sniff_language("これは日本語の字幕です、ありがとうございます") is None,
            "日文（含假名）不会被当成中文 —— 宁可看不懂，也不糊到画面上")
    s.check(sub.sniff_language("OK") is None, "内容太短就不下结论")

    zh, en = sub.split_by_language(["你好，世界", "Hello, world", "♪",
                                    "おはよう", "这个 CPU 很强"])
    s.check(zh == ["你好，世界", "这个 CPU 很强"],
            "逐行拆分：含汉字的行算中文（夹英文单词也算）")
    s.check(en == ["Hello, world"], "逐行拆分：纯英文行算英文")
    s.check(len(zh) + len(en) == 3, "符号行与日文行两边都不收（直接丢掉）")

    # ============================================================
    s.section("④ 找同名字幕")
    # ============================================================
    d = _fresh("discover")
    video = _touch(os.path.join(d, "movie.mp4"))
    _write(os.path.join(d, "movie.srt"), SRT_ZH)
    _write(os.path.join(d, "movie.zh.srt"), SRT_ZH)
    _write(os.path.join(d, "movie.en.srt"), SRT_EN)
    _write(os.path.join(d, "other.zh.srt"), SRT_ZH)          # 别的视频的
    _write(os.path.join(d, "movie.txt"), SRT_ZH)             # 扩展名不对
    os.makedirs(os.path.join(d, "subs"), exist_ok=True)
    _write(os.path.join(d, "subs", "movie.zh.srt"), SRT_ZH)  # 子目录里的

    found = sub.discover(video)
    paths = [p for p, _ in found]
    rel = sorted(os.path.relpath(p, d).replace("\\", "/") for p in paths)
    s.check(rel == ["movie.en.srt", "movie.srt", "movie.zh.srt", "subs/movie.zh.srt"],
            "找齐了 4 份、其余不认领（实测 %s）" % rel)
    s.check(os.path.relpath(paths[0], d).replace("\\", "/") == "movie.srt",
            "与视频**完全同名**的排最前（最可能是正经配套的那份）")
    s.check("other.zh.srt" not in [os.path.basename(p) for p in paths],
            "别的视频的字幕不会被认领")
    s.check(not any(p.endswith(".txt") for p in paths), "扩展名不对的不认")

    d2 = _fresh("discover_fullname")
    video2 = _touch(os.path.join(d2, "movie.mp4"))
    _write(os.path.join(d2, "movie.mp4.srt"), SRT_EN)
    s.check(len(sub.discover(video2)) == 1, "整名当初缀的 movie.mp4.srt 也认")

    d3 = _fresh("discover_none")
    video3 = _touch(os.path.join(d3, "movie.mp4"))
    s.check(sub.discover(video3) == [], "旁边没有字幕 → 空表")
    s.check(sub.discover(None) == [], "没给视频路径 → 空表，不抛")

    # ============================================================
    s.section("⑤ 组装：一中文一英文 / 单文件双语 / 显式指定")
    # ============================================================
    d = _fresh("only_en")
    video = _touch(os.path.join(d, "movie.mp4"))
    _write(os.path.join(d, "movie.en.srt"), SRT_EN)
    built = sub.build(video)
    s.check(built.available and built.en is not None and built.zh is None,
            "只有英文字幕文件 → 只占英文那个槽")
    s.check([r[0] for r in built.lines_for(5.0, "both")] == ["en"],
            "选「中英双语」而只有英文 → 照样显示英文（不硬凑、也不空屏）")
    s.check(built.lines_for(5.0, "zh") == [], "切到「中文」→ 什么都没有（诚实）")

    d = _fresh("two_files")
    video = _touch(os.path.join(d, "movie.mp4"))
    _write(os.path.join(d, "movie.zh.srt"), SRT_ZH)
    _write(os.path.join(d, "movie.en.srt"), SRT_EN)
    built = sub.build(video)
    s.check(built.zh is not None and built.en is not None and built.zh is not built.en,
            "一中一英两个文件 → 各占一个槽（是两个不同的 Track）")
    rows = built.lines_for(5.0, "both")
    s.check([r[0] for r in rows] == ["zh", "en"], "双语模式：中文在上、英文在下")
    s.check("中文" in built.describe() and "English" in built.describe(),
            "describe 把两种语言都说清楚（%s）" % built.describe())

    d = _fresh("one_bilingual")
    video = _touch(os.path.join(d, "movie.mp4"))
    _write(os.path.join(d, "movie.中英.srt"), SRT_BILINGUAL)
    built = sub.build(video)
    s.check(built.zh is not None and built.zh is built.en,
            "单文件双语 → **同一个 Track 占两个槽**（这样上层不用分情况讨论）")
    s.check([r[0] for r in built.lines_for(5.0, "both")] == ["zh", "en"],
            "同一文件里的中英两行，双语模式下一起显示")
    s.check(sub.language_from_name("movie.中英.srt") == "both",
            "文件名里的「中英」被认成双语")

    d = _fresh("explicit")
    video = _touch(os.path.join(d, "movie.mp4"))
    _write(os.path.join(d, "movie.zh.srt"), SRT_ZH)          # 自动能找着的
    manual = _write(os.path.join(d, "1.srt"), SRT_EN)        # 名字毫不相干
    built = sub.build(video, explicit_path=manual)
    s.check(built.en is not None and built.zh is None,
            "显式指定时**不再自动找**（用户的意图优先）")
    s.check("1.srt" in built.describe(), "describe 说的是手选那份（%s）" % built.describe())

    empty = sub.SubtitleSet()
    s.check(not empty.available and empty.describe() == "没有字幕",
            "什么都没有 → available 假、describe 说「没有字幕」")
    s.check(empty.lines_for(1.0, "both") == [], "查表返回空表，不抛")

    # ============================================================
    s.section("⑥ 时间轴查表")
    # ============================================================
    d = _fresh("timeline")
    path = _write(os.path.join(d, "t.zh.srt"), SRT_TIMELINE)
    track = sub.load_track(path)
    s.check(track is not None and len(track.cues) == 3, "读出 3 条")
    cases = [
        (0.5, [], "第一条开始之前 → 什么都不显示"),
        (1.0, ["你好"], "开始那一刻就显示（左闭）"),
        (1.999, ["你好"], "结束前一瞬还在"),
        (2.0, [], "结束那一刻就撤掉（右开）"),
        (3.0, [], "两条之间的空隙 → 不显示"),
        (4.0, ["长字幕"], "第二条开始"),
        (5.5, ["短字幕"], "嵌套在里面那条短的优先（离得近的赢）"),
        (7.0, ["长字幕"], "短的那条过去之后 → 退回外面那条长的（不黑屏）"),
        (9.0, [], "长的那条也过完 → 撤掉"),
        (100.0, [], "播到片尾之后 → 空，不越界"),
    ]
    wrong = []
    for sec, expect, why in cases:
        got = track.lines_at(sec)[0]
        if got != expect:
            wrong.append("%ss: %s(应 %s)" % (sec, got, expect))
    s.check(not wrong, "时间轴 %d 个边界点全对（错的：%s）" % (len(cases), wrong or "无"))

    # ============================================================
    s.section("⑦ 渲染层（文字真的画出来了 + 缓存）")
    # ============================================================
    font = sub.find_font()
    s.note("找到的中文字体：%s" % font)
    if font is None:
        s.note("本机没有中文字体，渲染相关的断言跳过（程序会静默不画字幕）")
    else:
        renderer = sub.SubtitleRenderer(font)
        rows = [("zh", "你好，世界"), ("en", "Hello, world")]
        layer = renderer.layer(rows, 32, 600)
        # ⚠️ `layer.size` 是个 (宽, 高) 二元组，`%` 会把它当成多个参数去填 ——
        #    必须自己包成单元素元组，否则报 "not all arguments converted"。
        s.check(layer is not None and layer.size[1] > 20,
                "画出了图层（尺寸 %s）" % ((layer.size if layer else None),))
        arr = np.asarray(layer)
        alpha = arr[:, :, 3]
        s.check(int(alpha.max()) == 255, "图层里有完全不透明的像素")
        bright = arr[:, :, :3].max(axis=2)
        s.check(bool(((bright > 200) & (alpha > 0)).any()), "有白字（填充是白色）")
        s.check(bool(((bright < 60) & (alpha > 0)).any()),
                "有黑描边 —— 没有它，白字压在明亮画面上就看不见了")

        again = renderer.layer(rows, 32, 600)
        s.check(again is layer, "同样的内容再要一次 → 同一个对象（缓存命中）")
        s.check(renderer.hits == 1 and renderer.misses == 1,
                "命中 1 次、未命中 1 次（实测 %d / %d）"
                % (renderer.hits, renderer.misses))
        s.check(renderer.layer(rows, 33, 601) is layer,
                "字号/宽度做了量化 → 差 1 像素不会白撑出一张新图")
        s.check(renderer.layer([("zh", "换了一句话")], 32, 600) is not layer,
                "内容变了就是新图层")
        s.check(renderer.layer([], 32, 600) is None, "没有行 → None（调用方直接跳过）")
        s.check(sub.SubtitleRenderer(None).layer(rows, 32, 600) is None,
                "没有字体 → None，绝不抛异常")

        long_text = "很长很长的一行中文字幕文字" * 4
        wide = renderer.layer([("zh", long_text)], 32, 300)
        s.check(wide is not None and wide.size[0] <= 300,
                "超长行会折行，图层宽度不超出可用宽度（实测 %d ≤ 300）"
                % (wide.size[0] if wide else -1))
        s.check(wide is not None and wide.size[1] > 32 * 1.5,
                "折行之后确实变高了（两行以上）")

    # ============================================================
    s.section("⑧ 端到端：打开视频自动认领 + 画面像素真的变了")
    # ============================================================
    d = _fresh("app")
    video = os.path.join(d, "movie.mp4")
    shutil.copy(city, video)
    _write(os.path.join(d, "movie.中英.srt"),
           "1\n00:00:00,000 --> 00:02:00,000\n这是一句中文字幕\n"
           "This is an English subtitle line\n")

    s.check(app.open_video(video), "打开视频成功")
    s.check(app.subtitle_set.available,
            "自动认领了同名字幕（%s）" % app.subtitle_set.describe())
    s.check(app.subtitle_mode == "both", "默认模式就是中英双语")
    s.check(app.subtitle_var.get() == "中英双语", "下拉框显示当前模式")
    s.check("movie.中英.srt" in app.playlist_status.cget("text"),
            "状态行把字幕的来路说清楚了（%s）" % app.playlist_status.cget("text"))
    s.check(len(app._subtitle_rows()) == 2, "此刻这一帧有两行字幕（中英各一）")

    def snap(mode):
        app.subtitle_mode = mode
        app.render_current_frame()
        return np.asarray(app._last_display_image).astype(int)

    def diff(a, b):
        return np.abs(a - b).sum(axis=2) > 30

    def top_row(mask):
        rows = np.where(mask.any(axis=1))[0]
        return int(rows[0]) if len(rows) else -1

    # ⚠️ 先把四周那圈模糊背景**冻住**再做像素对比。
    #    它是 `viewfit.LetterboxBgCache`，每 3 次渲染就从当前视频帧重算一次
    #    （`BG_REFRESH_INTERVAL`）—— 重算那一刻整圈黑边都会换掉，于是差异像素里
    #    混进一大块跟字幕毫无关系的东西（实测：冻住前 69358 个差异像素里只有
    #    50097 个在字幕带内；冻住后 13606 个全在带内）。背景动不动是渲染自己的
    #    事，不该让字幕的断言来背 —— 把 interval 设成够大，它第一次算完就不再有
    #    机会重算，剩下的对比就只剩字幕这一个变量。
    app._bg.refresh_interval = 10 ** 9
    app.render_current_frame()

    off = snap("off")
    zh = snap("zh")
    en = snap("en")
    both = snap("both")

    d_zh, d_en, d_both = diff(off, zh), diff(off, en), diff(off, both)
    s.check(int(d_zh.sum()) > 0, "选「中文」→ 画面像素真的变了（%d 个像素）"
            % int(d_zh.sum()))
    s.check(int(d_en.sum()) > 0, "选「英文」→ 画面像素真的变了（%d 个像素）"
            % int(d_en.sum()))
    band = slice(off.shape[0] * 2 // 3, None)
    s.check(int(d_both[band].sum()) == int(d_both.sum()),
            "变化全在画面下三分之一 —— 字幕就该在那儿（%d / %d）"
            % (int(d_both[band].sum()), int(d_both.sum())))
    s.check(int(d_both.sum()) > int(d_zh.sum()) and int(d_both.sum()) > int(d_en.sum()),
            "双语比单语画得更多（%d > %d, %d）"
            % (int(d_both.sum()), int(d_zh.sum()), int(d_en.sum())))
    s.check(top_row(d_both) < top_row(d_zh),
            "双语时中文被顶高一截（英文插在它下面）—— 实测 %d < %d"
            % (top_row(d_both), top_row(d_zh)))

    app.subtitle_mode = "both"
    app.render_current_frame()
    shot = app._screenshot_frame()
    s.check(np.array_equal(shot, np.asarray(app._last_display_image, dtype=np.uint8)[:, :, [2, 1, 0]]),
            "截图帧 = 上屏那一屏（BGR 通道顺序正确）")
    s.check(not np.array_equal(shot, np.asarray(off, dtype=np.uint8)[:, :, [2, 1, 0]]),
            "截图里确实带着字幕（与关掉字幕的同一屏不同）")

    app.subtitle_mode = "both"
    take_keyboard_focus(root)
    seen = []
    for _ in range(4):
        app._on_key_shortcut(_Key("V", widget=root))
        seen.append(app.subtitle_mode)
    s.check(seen == ["off", "zh", "en", "both"],
            "V 键按顺序循环四种模式（实测 %s）" % seen)
    s.check(app.subtitle_var.get() == "中英双语", "循环一圈之后下拉框也对得上")
    s.check(app.subtitle_mode == "both" and len(app._subtitle_rows()) == 2,
            "循环回「中英双语」后字幕又在画了")

    app.subtitle_combo.set("中文")
    app.subtitle_var.set("中文")
    app.on_subtitle_mode_change()
    s.check(app.subtitle_mode == "zh", "下拉框选「中文」也生效")
    app.subtitle_combo.set("中英双语")
    app.subtitle_var.set("中英双语")
    app.on_subtitle_mode_change()
    s.check(app.subtitle_mode == "both", "改回「中英双语」")

    # ============================================================
    s.section("⑨ 降级：任何「没有字幕」的情况都不许影响播放")
    # ============================================================
    d = _fresh("degrade")
    bare = os.path.join(d, "bare.mp4")
    shutil.copy(city, bare)
    s.check(app.open_video(bare), "没有字幕文件时视频照常打开")
    s.check(not app.subtitle_set.available, "字幕集是空的")
    s.check(app._subtitle_rows() == [], "这一帧没有字幕行")
    s.check("没有找到字幕" in app._subtitle_status_text(),
            "状态行解释了为什么没字幕（%s）" % app._subtitle_status_text())
    app.subtitle_mode = "both"
    app.render_current_frame()
    s.check(app._last_display_image is not None, "没有字幕也照常渲染一帧（不抛）")
    s.check(int(np.asarray(app._last_display_image).size) > 0, "那一帧是有内容的")

    app.subtitle_mode = "off"
    app.subtitle_enabled = False
    app._load_subtitles(video)
    s.check(not app.subtitle_set.available, "总开关关掉 → 什么都不加载")
    s.check(app._subtitle_rows() == [], "总开关关掉 → 也什么都不画")
    app.subtitle_enabled = True
    app._load_subtitles(video)
    s.check(app.subtitle_set.available, "总开关打开 → 又能加载了")

    app.subtitle_mode = "both"
    app.subtitle_auto = False
    app._load_subtitles(video)
    s.check(not app.subtitle_set.available, "自动发现关掉 → 只打开视频时不加载字幕")
    manual = _write(os.path.join(d, "手选的字幕.srt"), SRT_ZH)
    app._load_subtitles(video, explicit_path=manual)
    s.check(app.subtitle_set.available and app.subtitle_set.zh is not None,
            "但手选的字幕文件**照样认**（用户点名了就该认，与自动开关无关）")
    app.subtitle_auto = True

    junk = _write(os.path.join(d, "junk.srt"), "这压根不是字幕文件\n随便写几句中文\n")
    app._load_subtitles(video, explicit_path=junk)
    s.check(not app.subtitle_set.available,
            "手选了一个不是字幕的文件 → 空集（调用方会弹提示，不崩）")
    s.check(app.open_video(video), "字幕坏掉之后视频还是照常能打开")
    s.check(app.subtitle_set.available, "重新打开时又正常认领了字幕")

    app.subtitle_mode = "off"
    app.render_current_frame()
    s.check(app._subtitle_rows() == [], "模式=关闭 → 一行都不取")
    app.subtitle_mode = "both"


if __name__ == "__main__":
    sys.exit(run_suite("字幕（第 18 轮）", body, subtitles=True))
