# -*- coding: utf-8 -*-
"""字幕 —— 解析、找文件、认语种、画到画面上（第 18 轮·双语字幕）。

## 这一轮要什么

需求：**支持中文字幕与英文字幕的双语字幕**，其它语言留着扩展位、暂不做。

"双语字幕"在中文圈子里有两种形态，两种都得认：

  ① **两个字幕文件**：`电影.zh.srt` + `电影.en.srt`，各管一种语言；
  ② **一个字幕文件里两种语言**：同一段时间轴上，中文一行、英文一行 ——
     这是字幕组最常见的形态，文件名常叫 `电影.中英.srt` / `电影.chs&eng.srt`。

所以本模块的核心结构是"**每条字幕（cue）自己按行分语种**"：一条 cue 进来，
无论它原本是单语还是双语，出去时都是 `(中文行列表, 英文行列表)`。
上层的"显示哪一种"就退化成一个筛选动作，不用再关心字幕文件是怎么组织的。

## 与 app 的分工（和其它模块一个路子）

本模块**不知道播放器存在**：不读 app 状态、不碰任何控件。什么时候加载、
哪一种模式显示、画在画面的什么位置，全部由 `final_player` 决定。
这里只有纯函数，外加一个"只装缓存"的 `SubtitleRenderer`。

## 为什么渲染要缓存"整块文字图层"

一帧画面里字幕文字通常连续几十帧不变，而带描边的文字要画 2 遍（描边 + 填充）。
每帧重画是纯浪费，所以把"若干行文字"渲染成一张透明 RGBA 图层缓存起来，
每帧只做一次 `paste`。键里带内容，所以字幕一换自然就是新的图层。

## 语种扩展点（需求明确要求留的）

只动三处就能加一种语言：① `SUPPORTED_LANGS` 加代码；② `LANG_NAMES` /
`NAME_TAGS` / `line_language` 里补上它的显示名、文件名标签、逐行判据；
③ `final_player.SUBTITLE_MODE_LABELS` 补一个显示名。
找文件、拆句、查表、绘制这些地方全是按表转的，一行都不用改。
"""

import bisect
import codecs
import os
import re

from PIL import Image, ImageDraw, ImageFont

# ============================================================
# 语言表 —— 唯一的扩展点
# ============================================================
# 目前只做中文 + 英文。加语言请只动这三张表，别在下面的函数里写死判断。
SUPPORTED_LANGS = ("zh", "en")

LANG_NAMES = {"zh": "中文", "en": "English"}

# 文件名里的语种标签。**只认整段 token**（按 . - _ 空格 括号 切开），
# 所以 `The.2024.EN.srt` 认得出 en，而 `Enchanted.srt` 不会因为含 "en" 被误认。
NAME_TAGS = {
    "zh": ("zh", "zho", "chi", "chs", "cht", "sc", "tc", "cn", "chinese",
           "mandarin", "simplified", "traditional",
           "中文", "简体", "繁体", "简中", "繁中", "简体中文", "繁体中文"),
    "en": ("en", "eng", "english", "英文", "英语"),
}

# "这份文件里两种语言都有"的标签。只写**单个 token 就能表达"双语"**的那些；
# `zh-en` / `chs&eng` 这类由分隔符切开的，靠下面"两种语言各命中一次"自动得出。
NAME_TAGS_BILINGUAL = ("bilingual", "dual", "中英", "双语", "雙語", "中英双语")

# 支持的字幕格式（按扩展名）。SRT 是中文圈的事实标准；VTT 解析规则几乎一样，
# 顺手就支持了。ASS/SSA 有样式、卡拉OK 等一整套语义，不在这轮的范围内。
SUBTITLE_EXTS = (".srt", ".vtt")

# 放字幕的子目录名（很多人习惯单独建一个目录）
SUBTITLE_DIRS = ("subs", "sub", "subtitles", "subtitle", "字幕")

# 判"这行/这篇是什么语言"所需的最少字符数。太少说明不了问题：
# 一行 "OK" 不能说明这是英文字幕，"3" 更不能。
SNIFF_MIN_CHARS = 8

# 一条 cue 最多保留几行（折行之后）。真实字幕极少超过 2 行，
# 有的机翻字幕会甩出一大坨，全画出来会糊满半个屏幕。
MAX_LINES_PER_ROW = 2


# ============================================================
# 字素统计：中文 / 日文假名 / 拉丁字母
# ============================================================
# 判"是不是中文"只能靠汉字，而汉字在中文和日文里长得一样 —— 所以必须先看
# 假名：假名出现得够多就说明这是日文，**宁可看不懂不显示，也不能把日文
# 当中文糊在画面上**。这条判据同时也就是"其它语言留的扩展位"的兜底。
_CJK_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF))
_KANA_RANGES = ((0x3040, 0x30FF), (0x31F0, 0x31FF))


def _count_scripts(text):
    """返回 `(汉字数, 假名数, 拉丁字母数)`。"""
    cjk = kana = latin = 0
    for ch in text:
        code = ord(ch)
        if any(lo <= code <= hi for lo, hi in _CJK_RANGES):
            cjk += 1
        elif any(lo <= code <= hi for lo, hi in _KANA_RANGES):
            kana += 1
        elif ch.isascii() and ch.isalpha():
            latin += 1
    return cjk, kana, latin


def _is_japanese(cjk, kana):
    """假名够多就当是日文（本程序不支持的语言，返回真 = 别把它当中文）。"""
    return kana >= 3 and kana * 3 >= cjk


def line_language(line):
    """一行文字属于哪种语言：`"zh"` / `"en"` / `None`（都不是）。

    含汉字的行一律算中文 —— 中文行里夹几个英文单词很正常（"这个 CPU 很强"），
    把它算成英文会串味。假名够多则整体判为日文，返回 None。
    """
    cjk, kana, latin = _count_scripts(line)
    if _is_japanese(cjk, kana):
        return None
    if cjk > 0:
        return "zh"
    if latin > 0:
        return "en"
    return None


def split_by_language(lines):
    """把一条 cue 的若干行拆成 `(中文行, 英文行)`。"""
    out = {"zh": [], "en": []}
    for line in lines:
        lang = line_language(line)
        if lang in out:
            out[lang].append(line)
    return out["zh"], out["en"]


def sniff_language(text):
    """整篇内容是什么语言：`"zh"` / `"en"` / `"both"` / `None`。

    只在**文件名看不出语种**时才会被用到（比如 `电影.srt`）。
    """
    cjk, kana, latin = _count_scripts(text)
    if _is_japanese(cjk, kana):
        return None
    has_zh = cjk >= SNIFF_MIN_CHARS
    has_en = latin >= SNIFF_MIN_CHARS
    if has_zh and has_en:
        return "both"
    if has_zh:
        return "zh"
    if has_en:
        return "en"
    return None


# ============================================================
# 文件名认语种
# ============================================================
_TOKEN_SPLIT = re.compile(r"[.\-_\[\](){}·+&◇ ]+")


def _tokens(base):
    return [t for t in _TOKEN_SPLIT.split(base.lower()) if t]


def language_from_name(filename):
    """从文件名猜语种：`"zh"` / `"en"` / `"both"` / `None`（看不出来）。

    这是**优先级最高**的判据：字幕组命名通常很规矩，比内容嗅探可靠得多
    （一部片子里英文台词多与少，跟字幕本身是什么语言没关系）。
    """
    base = os.path.splitext(os.path.basename(filename))[0]
    tokens = _tokens(base)
    if not tokens:
        return None
    for token in tokens:
        if token in NAME_TAGS_BILINGUAL:
            return "both"
    hits = [lang for lang in SUPPORTED_LANGS
            if any(token in NAME_TAGS.get(lang, ()) for token in tokens)]
    if len(hits) >= 2:
        return "both"
    return hits[0] if hits else None


# ============================================================
# 读文件：编码嗅探
# ============================================================
# 中文字幕文件的编码是个真问题：字幕组的产出 UTF-8 居多，但老片子/论坛附件
# 里 GBK、BIG5、UTF-16 都不少见。读错的后果不是报错，而是**满屏乱码**，
# 而且用户会以为是程序坏了。所以逐个试。
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
# ⚠️ 顺序有意义：UTF-8 必须排在 GBK 前面（GBK 几乎能吃下任意字节序列，
#    排前面会把 UTF-8 的东西解成乱码而不报错）；GBK 排在 BIG5 前面是因为
#    简体内容远比繁体常见。
_FALLBACK_ENCODINGS = ("utf-8", "gbk", "big5", "cp1252")


def decode_bytes(raw):
    """字节 → `(文本, 实际用的编码名)`。任何输入都不抛异常。"""
    if not raw:
        return "", "utf-8"
    for bom, enc in _BOMS:
        if raw.startswith(bom):
            try:
                return raw.decode(enc), enc
            except Exception:
                break
    # 没有 BOM 的 UTF-16：特征是"每隔一个字节就是 0"。放在 fallback 之前判，
    # 否则会被 cp1252 解成一串带 \x00 的垃圾。
    if len(raw) > 8 and raw[1:2] == b"\x00" and raw[0:1] != b"\x00":
        try:
            return raw.decode("utf-16-le"), "utf-16-le"
        except Exception:
            pass
    for enc in _FALLBACK_ENCODINGS:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    # 到这儿说明连 cp1252 都失败了（几乎不可能）—— 带替换字符硬解，别抛。
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def read_text(path):
    """读一个字幕文件 → `(文本, 编码名)`；打不开就返回 `("", "")`。"""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except Exception:
        return "", ""
    return decode_bytes(raw)


# ============================================================
# 解析 SRT / VTT
# ============================================================
# 时间轴行。刻意写得宽容：小时可省略（VTT 允许 `MM:SS.mmm`）、毫秒分隔符
# 逗号/句点/全角句点都收、VTT 那串 `align:middle line:90%` 设置项直接无视
# （正则只匹配前缀，剩下的不管）。
_TIME_RE = re.compile(
    r"^\s*(?:(\d{1,3}):)?(\d{1,2}):(\d{1,2})(?:[,.\uff0e](\d{1,3}))?\s*-->\s*"
    r"(?:(\d{1,3}):)?(\d{1,2}):(\d{1,2})(?:[,.\uff0e](\d{1,3}))?"
)

# ASS 覆盖标签 `{\an8}` / `{\pos(10,20)}` 和 HTML 标签 `<i>` / `<font ...>`。
# 它们在 SRT 里也经常出现（从 ASS 转过来的字幕尤其多）。
_ASS_TAG_RE = re.compile(r"\{[^{}]*\}")
_HTML_TAG_RE = re.compile(r"</?[^<>]{1,40}>")


def _to_seconds(hh, mm, ss, ms):
    total = int(hh or 0) * 3600 + int(mm or 0) * 60 + int(ss)
    frac = int((ms or "0").ljust(3, "0")) / 1000.0 if ms else 0.0
    return total + frac


def strip_tags(line):
    """去掉样式标签，返回干净的一行文字。"""
    line = _ASS_TAG_RE.sub("", line)
    line = _HTML_TAG_RE.sub("", line)
    line = (line.replace("\\N", " ").replace("\\n", " ")
                .replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">"))
    return line.strip()


def parse(text):
    """解析 SRT / VTT → `[{start, end, lines}, ...]`（按开始时间排好序）。

    坏数据的处理原则是"**丢那一条，不丢整份**"：时间轴读不出来就跳过这一条，
    下一条照常。字幕文件是手改得最随意的东西，一整份因为它中间夹了一行错字
    就全废掉，用户会觉得这功能根本不能用。
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues = []
    i = 0
    while i < len(lines):
        match = _TIME_RE.match(lines[i])
        if not match:
            i += 1                       # 序号行 / WEBVTT 头 / 空行都从这里过
            continue
        start = _to_seconds(*match.group(1, 2, 3, 4))
        end = _to_seconds(*match.group(5, 6, 7, 8))
        i += 1
        body = []
        while i < len(lines) and lines[i].strip() and not _TIME_RE.match(lines[i]):
            body.append(lines[i])
            i += 1
        body = [line for line in (strip_tags(b) for b in body) if line]
        if end < start:                  # 时间轴写反了：当"零长"处理，别丢
            end = start
        if body:
            cues.append({"start": start, "end": end, "lines": body})
    cues.sort(key=lambda c: c["start"])
    return cues


# ============================================================
# 找同名字幕
# ============================================================
def _name_matches(base, stem, full_name):
    """字幕文件（去掉扩展名的那部分）是不是配这个视频的。

    认这几种：
      `电影.srt`      —— 完全同名（最常见）
      `电影.zh.srt`   —— 同名 + `.-_ 空格` 分隔的标签
      `电影.mp4.srt`  —— 有人把整个文件名当初前缀
    """
    low = base.lower()
    if low == stem.lower() or low == full_name.lower():
        return True
    if low.startswith(stem.lower() + ".") or low.startswith(stem.lower() + " "):
        return True
    if low.startswith(stem.lower() + "-") or low.startswith(stem.lower() + "_"):
        return True
    return False


def discover(video_path):
    """找出配这个视频的字幕文件 → `[(路径, 文件名猜出的语种), ...]`。

    顺序就是优先级：**与视频同名（`电影.srt`）的排最前**，其次同在视频目录的，
    再次子目录里的。同一个位置有多个候选时，先来的先占坑（见 `SubtitleSet.add`）。
    """
    if not video_path:
        return []
    directory = os.path.dirname(os.path.abspath(video_path))
    stem = os.path.splitext(os.path.basename(video_path))[0]
    full_name = os.path.basename(video_path)

    found = []
    for sub in ("",) + SUBTITLE_DIRS:
        folder = os.path.join(directory, sub) if sub else directory
        if not os.path.isdir(folder):
            continue
        try:
            names = os.listdir(folder)
        except OSError:
            continue                   # 没有权限 / 目录刚被删：当作没有字幕
        for name in names:
            if os.path.splitext(name)[1].lower() not in SUBTITLE_EXTS:
                continue
            base = os.path.splitext(name)[0]
            if _name_matches(base, stem, full_name):
                found.append(os.path.join(folder, name))

    def rank(path):
        base = os.path.splitext(os.path.basename(path))[0].lower()
        return (0 if base == stem.lower() else 1,             # 完全同名最优先
                0 if os.path.dirname(path) == directory else 1,  # 视频同目录优先
                os.path.basename(path).lower())               # 最后按名字定序（稳定）

    found.sort(key=rank)
    out, seen = [], set()
    for path in found:
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        out.append((path, language_from_name(path)))
    return out


# ============================================================
# 轨道 / 字幕集
# ============================================================
class Track:
    """一个字幕文件解析出来的东西（内容 + 语种拆分结果）。"""

    def __init__(self, path, lang, cues, declared_lang=None, encoding=""):
        self.path = path
        self.lang = lang                 # "zh" / "en" / "both" / None
        self.declared_lang = declared_lang   # 文件名给出的（可能是 None）
        self.encoding = encoding
        self.cues = cues
        self.starts = [cue["start"] for cue in cues]
        # 逐条把行拆好语种，查表时就不用再判 —— 这是本模块的核心形态。
        for cue in cues:
            cue["zh"], cue["en"] = split_by_language(cue["lines"])

    @property
    def has_zh(self):
        return any(cue["zh"] for cue in self.cues)

    @property
    def has_en(self):
        return any(cue["en"] for cue in self.cues)

    def lines_at(self, sec, lookback=4):
        """这一时刻该显示哪些行 → `(中文行, 英文行)`。

        用二分找"开始时间 ≤ sec 的最后一条"。往前再扫几条是为了兜住
        **时间轴重叠**的情况（一条长字幕中间插一条短的）：短的那条一旦过去，
        要能退回外面那条长的，而不是黑屏。
        """
        index = bisect.bisect_right(self.starts, sec) - 1
        if index < 0:
            return [], []
        for k in range(index, max(-1, index - lookback), -1):
            cue = self.cues[k]
            if cue["start"] <= sec < cue["end"]:
                return cue["zh"], cue["en"]
        return [], []


def load_track(path, declared_lang=None):
    """读并解析一个字幕文件；读不出任何字幕返回 None。"""
    text, encoding = read_text(path)
    if not text:
        return None
    cues = parse(text)
    if not cues:
        return None
    lang = declared_lang
    if lang is None:
        # 文件名看不出来才嗅内容，而且是**所有 cue 的所有行一起嗅**。
        # ⚠️ 这里踩过一次：最初只取了每条 cue 的第一行去嗅，于是
        #    "中文一行 + 英文一行"的双语字幕被判成纯中文（第一条永远是中文行），
        #    `SubtitleSet` 只占中文槽，选【中英双语】时英文那一行根本不出现。
        joined = "\n".join("\n".join(cue["lines"]) for cue in cues)
        lang = sniff_language(joined)
    return Track(path, lang, cues, declared_lang=declared_lang, encoding=encoding)


class SubtitleSet:
    """当前视频可用的字幕：最多一个中文来源 + 一个英文来源。

    `zh` / `en` 两个槽就是全部状态。一个"中英都有"的文件会**同时占两个槽**
    （同一个 Track 对象），于是"显示中文/英文/双语"退化成一个筛选动作，
    不用再分情况讨论"字幕是搁在一个文件里还是两个文件里"。
    """

    def __init__(self):
        self.zh = None
        self.en = None

    @property
    def available(self):
        return self.zh is not None or self.en is not None

    def track_for(self, lang):
        return self.zh if lang == "zh" else self.en

    def add(self, track):
        """按 track 的语言往空槽里放；一个槽都没放进返回 False。"""
        if track is None:
            return False
        slots = {"zh": ("zh",), "en": ("en",), "both": ("zh", "en")}.get(track.lang, ())
        placed = False
        for slot in slots:
            if getattr(self, slot) is None:      # 已有来源就不覆盖（先到先得）
                setattr(self, slot, track)
                placed = True
        return placed

    def lines_for(self, sec, mode="both"):
        """这一时刻要画的行 → `[(语种, 文字), ...]`，顺序即显示顺序（中在上）。"""
        if mode == "off":
            return []
        rows = []
        for lang in SUPPORTED_LANGS:
            if mode != "both" and mode != lang:
                continue
            track = self.track_for(lang)
            if track is None:
                continue
            zh_lines, en_lines = track.lines_at(sec)
            for text in (zh_lines if lang == "zh" else en_lines):
                rows.append((lang, text))
        return rows

    def languages(self):
        """实际能显示的语种（用于界面上提示"这份字幕是什么"）。"""
        out = []
        for lang in SUPPORTED_LANGS:
            track = self.track_for(lang)
            if track is not None and (track.has_zh if lang == "zh" else track.has_en):
                out.append(lang)
        return out

    def describe(self):
        """给人看的一句话，例如 `中文/English(电影.中英.srt)`。"""
        by_path = {}
        for lang in SUPPORTED_LANGS:
            track = self.track_for(lang)
            if track is None:
                continue
            by_path.setdefault(track.path, []).append(LANG_NAMES[lang])
        if not by_path:
            return "没有字幕"
        return " + ".join("%s(%s)" % ("/".join(names), os.path.basename(path))
                          for path, names in by_path.items())


def build(video_path=None, explicit_path=None):
    """组装当前视频的字幕集。

    `explicit_path` 是用户手选的字幕文件 —— 给了它就只看它，不再自动找
    （用户的意图优先，而且这样能救那些名字完全不搭的字幕文件）。
    """
    result = SubtitleSet()
    if explicit_path:
        track = load_track(explicit_path, language_from_name(explicit_path))
        result.add(track)
        return result
    if not video_path:
        return result
    for path, declared in discover(video_path):
        track = load_track(path, declared)
        if track is not None:
            result.add(track)
    return result


# ============================================================
# 字体
# ============================================================
# 中文字幕需要一份带汉字的字体 —— PIL 的内置位图字体画不出汉字（会变成方块
# 或者直接报错）。找不到字体就**不画字幕**，绝不因为字幕把播放搞崩。
FONT_ENV = "FINAL_PLAYER_SUBTITLE_FONT"
_FONT_FILES = ("msyh.ttc", "msyhbd.ttc", "msyhl.ttc",      # Windows 微软雅黑
               "msjh.ttc", "simhei.ttf", "Deng.ttf", "simsun.ttc",
               "PingFang.ttc", "Hiragino Sans GB.ttc",     # macOS
               "NotoSansCJK-Regular.ttc", "NotoSansSC-Regular.otf",
               "wqy-microhei.ttc", "wqy-zenhei.ttc")       # Linux
_FONT_DIRS = (
    os.path.join(os.environ.get("WINDIR") or "C:/Windows", "Fonts"),
    "/System/Library/Fonts", "/Library/Fonts", os.path.expanduser("~/Library/Fonts"),
    "/usr/share/fonts/opentype/noto", "/usr/share/fonts/truetype/noto",
    "/usr/share/fonts/truetype/wqy", "/usr/share/fonts", os.path.expanduser("~/.fonts"),
)


def find_font(override=None):
    """找一份能画中文的字体文件；找不到返回 None。"""
    candidates = [override, os.environ.get(FONT_ENV)]
    for filename in _FONT_FILES:
        for folder in _FONT_DIRS:
            candidates.append(os.path.join(folder, filename))
    for path in candidates:
        try:
            if path and os.path.isfile(path):
                return path
        except Exception:
            continue
    return None


_FONT_CACHE = {}
_FONT_CACHE_MAX = 24


def load_font(path, size):
    """读取并缓存字体对象（尺寸变了要新对象，所以按 `(路径, 字号)` 缓存）。"""
    if not path:
        return None
    key = (path, int(size))
    font = _FONT_CACHE.get(key)
    if font is not None:
        return font
    try:
        font = ImageFont.truetype(path, int(size))
    except Exception:
        return None
    if len(_FONT_CACHE) >= _FONT_CACHE_MAX:
        _FONT_CACHE.clear()
    _FONT_CACHE[key] = font
    return font


# ============================================================
# 排版 + 图层渲染
# ============================================================
# 文字颜色是刻意放在这里的一张表：想改"中文白、英文黄"这种搭配，改这一处就行。
ROW_STYLE = {
    "zh": {"fill": (255, 255, 255, 255), "scale": 1.0},
    "en": {"fill": (255, 255, 255, 255), "scale": 0.86},
}
OUTLINE_COLOR = (0, 0, 0, 255)
STROKE_RATIO = 0.075      # 描边粗细 = 字号 × 这个比例
STROKE_MAX = 4
LINE_GAP_RATIO = 0.20     # 行距 = 字号 × 这个比例

# 折行的切分：连续的 ASCII 字母数字算一个词，其它字符一个算一个
# （中文没有词边界，按字折最稳妥）。
_WRAP_RE = re.compile(r"[A-Za-z0-9'\u2019\-]+|\s+|.", re.S)


def wrap_text(text, font, max_width, max_lines=MAX_LINES_PER_ROW):
    """按像素宽度折行。`max_lines` 超了就截断（保留最前面几行）。"""
    text = (text or "").strip()
    if not text:
        return []
    if max_width <= 0:
        return [text]
    lines, current = [], ""
    for token in _WRAP_RE.findall(text):
        candidate = current + token
        if current and font.getlength(candidate) > max_width:
            lines.append(current.rstrip())
            if len(lines) >= max_lines:
                return lines
            current = token.lstrip()
        else:
            current = candidate
    if current.strip():
        lines.append(current.rstrip())
    return lines[:max_lines]


class SubtitleRenderer:
    """把「若干行文字」渲染成一张带描边的 RGBA 图层，并按内容缓存。

    缓存键 = `(行内容, 字号, 可用宽度)`。内容进键这一点很重要：字幕一换就是
    新图层，不需要任何手动失效。窗口拖动时字号/宽度会连续变化，所以两者都
    做了**量化**（字号取 2 的倍数、宽度取 16 的倍数）—— 不然一次拖拽就能
    塞出几十个只差 1 像素的图层，把缓存冲干净。
    """

    def __init__(self, font_path=None, max_layers=48):
        self.font_path = font_path
        self.max_layers = max_layers
        self._layers = {}
        self.hits = 0
        self.misses = 0

    def clear(self):
        self._layers.clear()

    @property
    def cached(self):
        return len(self._layers)

    def layer(self, rows, size, max_width):
        """`rows = [(语种, 文字), ...]` → PIL RGBA 图层；画不出来返回 None。"""
        if not rows or not self.font_path:
            return None
        size = max(8, int(size) // 2 * 2)
        max_width = max(32, int(max_width) // 16 * 16)
        key = (tuple(rows), size, max_width)
        cached = self._layers.get(key)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1
        layer = self._render(rows, size, max_width)
        if layer is not None:
            if len(self._layers) >= self.max_layers:
                self._layers.clear()
            self._layers[key] = layer
        return layer

    def _render(self, rows, size, max_width):
        stroke = max(1, min(STROKE_MAX, int(round(size * STROKE_RATIO))))
        gap = max(2, int(size * LINE_GAP_RATIO))

        entries = []                 # [(文字, 字体, 语种)]
        for lang, text in rows:
            style = ROW_STYLE.get(lang, ROW_STYLE["zh"])
            font_size = max(8, int(size * style["scale"]))
            font = load_font(self.font_path, font_size)
            if font is None:
                return None
            for line in wrap_text(text, font, max_width - 2 * stroke):
                entries.append((line, font, lang))
        if not entries:
            return None

        measured = []
        for line, font, lang in entries:
            ascent, descent = font.getmetrics()
            width = int(font.getlength(line)) + 2 * stroke
            measured.append((line, font, lang, width, ascent + descent + 2 * stroke + gap))

        total_w = max(item[3] for item in measured)
        total_h = sum(item[4] for item in measured)
        layer = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)

        y = 0
        for line, font, lang, width, height in measured:
            style = ROW_STYLE.get(lang, ROW_STYLE["zh"])
            draw.text(((total_w - width) // 2 + stroke, y + stroke), line,
                      font=font, fill=style["fill"],
                      stroke_width=stroke, stroke_fill=OUTLINE_COLOR)
            y += height
        return layer
