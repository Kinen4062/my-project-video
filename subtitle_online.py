# -*- coding: utf-8 -*-
"""在线字幕匹配 —— 去 OpenSubtitles 要一份现成的字幕（第 20 轮）。

## 这个模块管什么、不管什么

管：算文件指纹 → 搜索 → 挑候选 → 下载 → 解压 → **落到视频旁边**。
不管：解析字幕、渲染、碰 Tk。落盘之后就交给 `subtitle.py` 那套现成的
`discover()` / `build()`，一个字都不用重写 —— 这也是为什么本模块只往外吐
**文件路径**，不往外吐 `SubtitleSet`。

## 为什么 HTTP 层要能注入

`OpenSubtitles(session=...)` 那个参数是为了测试。搜索 → 挑候选 → 下载 →
解压 → 落盘这条链路里有大量**容易写错又不容易发现**的东西（字段名、排序、
重名让路、ZIP 解压……），它们全都不该依赖"此刻能不能连上外网"才能验证。
把网络收敛成一个可替换的 `session` 之后，整条链路可以在测试里跑通，
网络本身成了唯一的不可测部分 —— 而不是把逻辑和网络搅在一起谁也测不了。

## 关于 API key（话说在前面）

OpenSubtitles 要求"一个应用一个 key"，而且**不鼓励把用户的 key 填进应用**。
本模块的做法是把 key 放在用户自己的配置目录（`%APPDATA%/final_player/online.json`）
里，绝不写进代码、绝不进版本库。这个播放器是单机自用工具，你自己申请、
自己用，用的是你自己的每日配额。

免费额度：**不登录每 IP 每天 5 次下载**；把 consumer 设成 "Under Development"
可以到 100 次/天。搜索**不计次**（官方原话：搜索没有限制，只有下载有）。
"""

import io
import json
import os
import re
import struct
import tempfile
import zipfile

try:
    import requests
    HTTP_AVAILABLE = True
except Exception:                      # 环境里没装 requests 也不能让播放器打不开
    requests = None
    HTTP_AVAILABLE = False


API_BASE = "https://api.opensubtitles.com/api/v1"

# 官方明确要求 User-Agent 能认出应用（"MyApp v1.0" 这种写法），
# 文档原话是"通用或第三方库的默认 UA 会被封"。别改成 requests 默认值。
USER_AGENT = "final_player v1.0"

# 一次搜索要哪些语言。**必须按字母序**（文档要求），否则接口可能直接报错。
#
# OpenSubtitles 的中文分了三种，这里全要：
#   zh-cn 简体        zh-tw 繁体        ze 中英双语（"Chinese bilingual"）
# `ze` 是最值钱的那个 —— 字幕组做好的中英对照，一次下载就让播放器的
# 「中英双语」模式有内容，不用下两份、不浪费配额。
SEARCH_LANGUAGES = "en,ze,zh-cn,zh-tw"

# OpenSubtitles 的语言代码 → 本播放器的语言槽（见 subtitle.SUPPORTED_LANGS）。
# `ze` 同时算中文和英文两槽 —— 因为它一个文件里两种语言都有。
LANG_SLOT = {
    "zh-cn": ("zh",),
    "zh-tw": ("zh",),
    "ze": ("zh", "en"),
    "en": ("en",),
}

# 落盘时用的文件名后缀。**这份后缀决定播放器认成什么语种**（见
# subtitle.language_from_name）：文件名里的语种标签优先级高于内容嗅探，
# 所以双语字幕**不能**落成 `电影.zh.srt` —— 那样它只会显示中文那一行，
# 英文行白白浪费掉。
SLOT_SUFFIX = {
    "ze": "双语",
    "zh-cn": "zh",
    "zh-tw": "zh",
    "en": "en",
}

# 挑候选时每种语言的优先级。简体优先于繁体是按目标用户的默认习惯；
# `ze` 最高是因为它一份就能满足双语需求。
LANG_RANK = {"ze": 3, "zh-cn": 2, "zh-tw": 1, "en": 0}

# 候选"年份是否相符"的打分（第 21 轮）。**这是修掉"配到别的片子"的主力。**
#
# 为什么"差 ≥2 年"要重罚：同系列的续集年份必然不同（《黑客帝国》1999 /
# 《黑客帝国2》2003 /《黑客帝国3》2003），而续集的片名**完整包含**正主片名
# （`The Matrix` ⊂ `The Matrix Revolutions`），靠片名根本分不开 —— 只能靠年份。
# 实测：搜 `The Matrix` 返回的 50 条里**一条正主都没有**，全是续集和纪录片，
# 而续集靠 28.8 万下载量排在最前面（正主只有 1 万多）。
#
# 为什么"差 1 年"只 +60 而不是也重罚：**发行年 ≠ 制作年**是真实存在的。
# 实测样本：The Matrix Revisited 的字幕 release 写着 "(2002)"，而
# feature_details.year 是 2001 —— 同一部片，年份差 1。当成"不同片子"剔除，
# 会把正主自己剔掉。
#
# 为什么是 -500 而不是 -1000：指纹匹配（+1000）是**比年份更硬**的证据 ——
# 它意味着"有人拿一模一样这个文件配过字幕"，而 feature_details 的年份是
# 库里的元数据、本身可能填错。所以年份不符要能压过语言偏好（±600）、
# 盖过下载量（≤100），但**不能盖过指纹匹配**（+1000-500 仍是正分）。
YEAR_MATCH_BONUS = 300
YEAR_NEAR_BONUS = 60
YEAR_MISMATCH_PENALTY = -500

REQUEST_TIMEOUT = 20

# HTTP 状态码 → 给用户看的一句话。401/403 都是 key 的问题（实测过：用一个
# 不存在的 key 请求，接口网关回的就是 403 + "You cannot consume this service"，
# 意思是"不认这个 consumer"），406/429 是额度/频率问题。
HTTP_HINTS = {
    400: "请求被接口拒绝",
    401: "API key 无效（或已被官方作废）",
    403: "接口不认这个 API key（检查有没有填错、或这个 consumer 已被删除）",
    406: "下载额度用完了",
    429: "请求太频繁，过一会儿再试",
}


# ============================================================
# 文件名分析
# ============================================================

# 发布命名里的技术标记。命中它就说明"片名到此为止"。
#
# 为什么用这么长一张表而不是"取前 N 个词"：片名长度毫无规律（《Up》一个词，
# 《Borat Subsequent Moviefilm》三个词），而技术标记是**发布规范的固定词汇**，
# 从第一个技术标记往回切是唯一稳的做法。
_NOISE_RE = re.compile(
    r"""\b(?:
        2160p|1440p|1080p|1080i|720p|576p|480p|4k
      | x\.?264|x\.?265|h\.?264|h\.?265|hevc|avc|xvid|divx|av1
      | bluray|blu-ray|brrip|bdrip|bdremux|remux
      | webrip|web-dl|webdl|web|hdtv|hdrip|dvdrip|dvdscr|dvd
      | aac|ac3|eac3|ddp?5|dts|truehd|flac|mp3|atmos
      | hdr10|hdr|dovi|dv|sdr|10bit|8bit|hi10p
      | proper|repack|extended|unrated|imax|internal|limited|complete
      | multi|subs?|chs|cht|eng|chinese|bilingual|gb|big5
    )\b""",
    re.I | re.X,
)

# S01E02 / S1.E2 / 1x02 三种写法都要认 —— 剧集靠它才能搜准。
_SEASON_EPISODE_RE = re.compile(r"\bS(\d{1,2})[\s._-]*E(\d{1,3})\b", re.I)
_ALT_EPISODE_RE = re.compile(r"\b(\d{1,2})x(\d{2})\b")
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")

# 分隔符统一成空格。⚠️ 连字符**保留**：`Spider-Man`、`WEB-DL` 都要它。
# 全角括号必须一起收 —— 中文资源极爱用 `【电影名】.2020.1080p` 这种写法，
# 漏掉它就会拿 "【电影名】 2020" 去搜，平白多两个字面量字符。
_SEPARATORS_RE = re.compile(r"[._\[\](){}【】（）｛｝［］〔〕]+")


def analyze_filename(path):
    """文件名 → `{"query": 搜索词, "season": None|int, "episode": None|int, "year": None|int}`。

    `The.Matrix.1999.1080p.BluRay.x264-GROUP.mkv`  →  query="The Matrix 1999"
    `Show.S02E05.720p.HDTV.mkv`                    →  query="Show", season=2, episode=5
    `2012.2009.1080p.mkv`                          →  query="2012 2009", year=**2009**

    取"**第一段非技术标记的内容**"而不是"第一个技术标记之前的内容"：
    因为发布名常常以噪音开头（`[Subs] Show.S01E03.1080p`），
    按后者会切出一个空串。
    """
    stem = os.path.splitext(os.path.basename(path or ""))[0]
    stem = _SEPARATORS_RE.sub(" ", stem)

    season = episode = None
    match = _SEASON_EPISODE_RE.search(stem) or _ALT_EPISODE_RE.search(stem)
    if match:
        season, episode = int(match.group(1)), int(match.group(2))

    # 按技术标记切成若干段，取第一段有内容的
    parts = [p.strip(" -") for p in _NOISE_RE.split(stem)]
    query = next((p for p in parts if p), "")

    # 剧集标记不该留在查询词里（它有专门的参数），去掉
    query = _SEASON_EPISODE_RE.sub(" ", query)
    query = _ALT_EPISODE_RE.sub(" ", query)
    query = re.sub(r"\s+", " ", query).strip(" -")

    # ⚠️ 取**最后**一个年份，不是第一个。一个文件名里能出现两个年份：
    # `2012.2009.1080p.mkv` 是电影《2012》—— 它 2009 年上映，第一个 2012 是
    # **片名里的数字**，第二个才是发行年。取第一个会拿 2012 去搜，实测搜出来
    # 是 Coldplay 演唱会那一类 2012 年的东西；取最后一个才命中正主
    # （`2012` + year=2009 → 正主《2012》出现，带 imdb=1190080）。
    # 只有一个年份时两种取法结果相同，所以这条改动不会伤到常规命名
    # （`The.Matrix.1999.1080p` 两种取法都是 1999）。
    years = _YEAR_RE.findall(query)
    year = int(years[-1]) if years else None
    if not years and not query:
        # 第一段被切空了（极端命名），退回"去掉扩展名"的原始形态，
        # 总比拿空字符串去搜强
        fallback = re.sub(r"\s+", " ", stem).strip()
        query = _SEASON_EPISODE_RE.sub(" ", fallback).strip(" -")

    return {"query": query, "season": season, "episode": episode, "year": year}


def movie_hash(path, chunk_size=1 << 16):
    """OpenSubtitles 的 moviehash：**文件大小 + 头 64KB + 尾 64KB** 的 64 位累加和。

    为什么不直接用 md5：字幕要精确对上"这个压制版本"，而两小时的 4K 片子
    读一遍要几十秒。头尾取样既能区分不同发布，又能瞬间算完 —— 这是
    全世界字幕库通用的那个算法，改了就跟整个数据库对不上了。

    ⚠️ 文件小于 128KB 时头尾两块**会重叠**，官方参考实现也是这么做的，
    这是有意为之，不要"修"它。
    """
    size = os.path.getsize(path)
    total = size
    mask = 0xFFFFFFFFFFFFFFFF
    count = chunk_size // 8

    with open(path, "rb") as fh:
        for _ in range(count):
            raw = fh.read(8)
            if len(raw) < 8:
                break
            total = (total + struct.unpack("<Q", raw)[0]) & mask
        fh.seek(max(0, size - chunk_size))
        for _ in range(count):
            raw = fh.read(8)
            if len(raw) < 8:
                break
            total = (total + struct.unpack("<Q", raw)[0]) & mask

    return "%016x" % total


# ============================================================
# 异常
# ============================================================

class OnlineError(Exception):
    """本模块所有**可预期**的失败：网络、配额、没结果、文件坏了……

    调用方只需要 catch 这一个类型。真·意外（代码写错）才会漏出去 ——
    这样"网络出问题"和"我写错了"在日志里能一眼分开。
    """


class NoAPIKey(OnlineError):
    """还没配置 API key。UI 要拿这个类型去提示用户"去哪配"。"""


class QuotaExceeded(OnlineError):
    """今天的下载额度用完了。"""


# ============================================================
# 客户端
# ============================================================

class OpenSubtitles:
    """一个很薄的 OpenSubtitles 客户端。

    `session` 可注入（测试用）；`session.request(method, url, ...)` 是它唯一
    需要满足的接口，返回对象要有 `.status_code` / `.json()` / `.content`。
    """

    def __init__(self, api_key, session=None, timeout=REQUEST_TIMEOUT,
                 user_agent=USER_AGENT):
        if not api_key or not str(api_key).strip():
            raise NoAPIKey("还没有配置 OpenSubtitles 的 API key")
        self.api_key = str(api_key).strip()
        self.timeout = timeout
        self.user_agent = user_agent
        if session is None:
            # ⚠️ 判据用 `requests is None` 而不是模块级的 `HTTP_AVAILABLE`：
            # 后者是 import 那一刻定下的常量，运行时环境变了它不会跟着变。
            # 直接看对象在不在，才是"此刻到底能不能联网"的真话。
            if requests is None:
                raise OnlineError("本机没有 requests 库，没法联网匹配字幕")
            session = requests.Session()
        self.session = session
        # 上一次 `search` 的原始条数，只用于状态行显示"找到几条"
        self.last_total = 0

    # ---------- 底层 ----------

    def _headers(self):
        return {
            "Api-Key": self.api_key,
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Content-Type": "application/json",
        }

    def _call(self, method, url, **kwargs):
        """发请求并统一把失败翻译成 `OnlineError`。

        集中在一个地方做的原因：网络异常、超时、HTTP 错误码、返回体不是
        JSON —— 这四种失败在上层看来是同一件事（"这次没成，原因这样说"），
        散在各处会出现四套写法。
        """
        try:
            response = self.session.request(
                method, url, headers=self._headers(),
                timeout=self.timeout, **kwargs)
        except Exception as exc:
            raise OnlineError("网络请求失败：%s" % exc) from exc

        status = int(getattr(response, "status_code", 200) or 200)
        try:
            payload = response.json()
        except Exception:
            payload = None

        if status >= 400:
            detail = ""
            if isinstance(payload, dict):
                errors = payload.get("errors") or []
                if isinstance(errors, list):
                    detail = " ".join(str(e) for e in errors)
                detail = detail or str(payload.get("message") or "")
            hint = HTTP_HINTS.get(status, "接口返回 HTTP %d" % status)
            if detail:
                hint = "%s（%s）" % (hint, detail)
            if status in (406, 429):
                raise QuotaExceeded(hint)
            raise OnlineError(hint)

        if payload is None:
            raise OnlineError("接口返回了看不懂的内容（不是 JSON）")
        return payload

    # ---------- 搜索 ----------

    def search(self, query=None, moviehash=None, moviehash_match=None,
               season=None, episode=None, year=None, imdb_id=None,
               parent_imdb_id=None, page=1):
        """搜字幕 → 一串**拍平了的候选字典**（见 `parse_results`）。

        搜索**不计入下载配额**（官方：搜索没有限制），所以上层可以放心地
        先按指纹精确搜一遍、没结果再按片名宽松搜一遍。

        `year` / `imdb_id` / `parent_imdb_id` 是本模块准确率的**全部秘密**
        （第 21 轮补的，都实测过）：

        · **`year`** —— 最便宜的那个。`query="The Matrix"` 返回的前 12 条
          **一条正主都没有**（4 条纪录片《Revisited》+ 8 条续集《Revolutions》，
          续集靠 28.8 万下载量把正主整个挤掉了）；同一个 query 加上
          `year=1999`，返回的 50 条**全是 The Matrix(1999)**。就这一个参数。
        · **`imdb_id`** —— 按"这部片"精确搜。实测 `imdb_id=133093` 的 50 条
          全是 The Matrix(1999)，零杂片。剧集也管用：单集的 `imdb_id`
          返回的正好是那一集（实测 23 条全是 Breaking Bad S1E1）。
          接口要**纯数字**，带 `tt` 前缀的先过 `_normalize_imdb`。
        · **`parent_imdb_id`** —— 剧集专用，**必须配 season/episode**：
          只给 parent 会返回整部剧混着的 50 条（实测 36 集），
          配上季集号才是那一集（实测 23 条 1 集）。
        """
        params = {
            "languages": SEARCH_LANGUAGES,
            # 机翻和 AI 翻译默认排除 —— 它们的时间轴常常整体偏移，
            # 而且中文质量普遍不如人工。真需要时用户自己去网页找。
            "machine_translated": "exclude",
            "ai_translated": "exclude",
            "order_by": "download_count",
            "order_direction": "desc",
            "page": _int_or_none(page) or 1,
        }
        if query:
            params["query"] = query
        if moviehash:
            params["moviehash"] = moviehash
            # `only` = 只要"有人拿这个一模一样的文件配过"的结果
            params["moviehash_match"] = moviehash_match or "include"
        if season is not None and episode is not None:
            params["type"] = "episode"
            params["season_number"] = int(season)
            params["episode_number"] = int(episode)

        # 下面三个只在真拿到数字时才传。传 None 或空串会被接口当成一个
        # 真实的筛选条件，结果是"一条都搜不到"这种最难查的失败。
        if _int_or_none(year):
            params["year"] = _int_or_none(year)
        imdb = _normalize_imdb(imdb_id)
        if imdb:
            params["imdb_id"] = imdb
        parent = _normalize_imdb(parent_imdb_id)
        if parent:
            params["parent_imdb_id"] = parent

        payload = self._call("GET", API_BASE + "/subtitles", params=params)
        self.last_total = _int_or_none(payload.get("total_count")) or 0
        return parse_results(payload)

    # ---------- 下载 ----------

    def download(self, file_id):
        """`/download` 换临时直链 → 取字节。返回 `(bytes, meta)`。

        为什么要把 `meta` 带回来：**官方只有在下载这一刻才告诉你还剩几次**
        （`remaining`）。这是唯一能对用户说"今天还剩 N 次"的机会，丢了就没了。
        """
        payload = self._call("POST", API_BASE + "/download",
                             json={"file_id": int(file_id)})
        link = payload.get("link")
        if not link:
            raise OnlineError("接口没有返回下载地址")

        meta = {
            "remaining": payload.get("remaining"),
            "requests": payload.get("requests"),
            "reset_time_utc": payload.get("reset_time_utc"),
            "file_name": payload.get("file_name") or "",
            "message": payload.get("message") or "",
        }

        # 直链是临时 CDN 地址，**不带 Api-Key**：它不认识我们、也不需要。
        try:
            blob = self.session.request("GET", link, timeout=self.timeout)
        except Exception as exc:
            raise OnlineError("下载字幕文件失败：%s" % exc) from exc

        status = int(getattr(blob, "status_code", 200) or 200)
        if status >= 400:
            raise OnlineError("下载字幕文件失败（HTTP %d）" % status)
        content = getattr(blob, "content", b"") or b""
        if not content:
            raise OnlineError("下载回来的文件是空的")
        return content, meta


# ============================================================
# 结果处理
# ============================================================

def _int_or_none(value):
    """接口给的数字 → int；给不出数字（None / "" / 怪值）→ None。

    接口在这件事上很不一致：同一个字段这次是 int、下次是字符串
    （实测 `ratings` 返回的是 `0.0` 这样的浮点），再下次干脆没有。
    全部收敛到这里，外面就不用每个字段各写一遍 try/except ——
    那也正是原来 `votes` / `ratings` 两颗地雷的来源。

    ⚠️ `bool` 要单独挡掉：它是 `int` 的子类，不挡的话 `True` 会变成 1，
    于是"这个字段有没有"和"这个字段的值是 1"就分不开了。
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError 是给 `Infinity` 准备的：JSON 标准里没有这个值，
        # 但 `json.loads` **默认就认它**（一个历史遗留的非标准扩展），
        # 所以接口真可能回一个出来，而 `int(inf)` 抛的正是 OverflowError。
        return None


def _normalize_imdb(value):
    """`tt0133093` / `" 133093 "` / `133093` → `133093`；给不出 → None。

    接口要的是**纯数字**（实测 `imdb_id=133093` 有效），但用户从网页上
    抄下来的通常是带 `tt` 前缀的那种写法，所以两种都得认。
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if text.startswith("tt"):
        text = text[2:]
    return _int_or_none(text)


def parse_results(payload):
    """API 的 `data[]` → 一串扁平的候选字典。

    拍平的理由：后面挑候选要**反复取字段排序**，嵌套着取（
    `entry["attributes"]["files"][0]["file_id"]`）又长又容易 `KeyError`；
    拍平之后所有字段都有默认值，API 少给一个字段也不会炸。
    顺带这里成了 API 结构变化时**唯一**要改的地方。

    【第 21 轮】新增一整组 `feature_*` / `imdb_id` / `parent_*` 字段，
    来自接口的 `attributes.feature_details` —— 那是**"这条字幕属于哪部片"
    的唯一权威依据**，也是本轮修掉"配到别的片子"的关键。三条实测结论：

    · 它**一直都有**，之前只是没被读。`{"feature_id": 518726,
      "feature_type": "Movie", "year": 2001, "title": "The Matrix Revisited",
      "imdb_id": 295432, "tmdb_id": 14543}`。
    · `title` **永远是英文原名**：《让子弹飞》→ `Let the Bullets Fly`，
      《阿凡达》→ `Avatar`。所以**不能拿用户的中文文件名去比标题** ——
      这条实测过，是本轮放弃"片名比对"、改走"年份 + 编号"的原因。
    · 剧集多给 `season_number` / `episode_number` / `parent_*`，
      `title` 是**单集标题**（`Pilot`），片名在 `parent_title` 里。
    """
    out = []
    for entry in (payload.get("data") or []):
        if not isinstance(entry, dict):
            continue
        attributes = entry.get("attributes") or {}
        files = attributes.get("files") or []
        first = files[0] if files and isinstance(files[0], dict) else {}
        file_id = _int_or_none(first.get("file_id"))
        if not file_id:
            continue                    # 没有可下载的文件，这条没法用

        details = attributes.get("feature_details")
        if not isinstance(details, dict):
            details = {}

        try:
            fps = float(attributes.get("fps") or 0.0)
        except (TypeError, ValueError):
            fps = 0.0

        out.append({
            "file_id": file_id,
            "file_name": str(first.get("file_name") or ""),
            "cds": len(files),
            "language": str(attributes.get("language") or "").lower(),
            "downloads": _int_or_none(attributes.get("download_count")) or 0,
            "fps": fps,
            "trusted": bool(attributes.get("from_trusted")),
            "machine": bool(attributes.get("machine_translated")),
            "ai": bool(attributes.get("ai_translated")),
            "hi": bool(attributes.get("hearing_impaired")),
            "release": str(attributes.get("release") or ""),
            "votes": _int_or_none(attributes.get("votes")) or 0,
            "ratings": _int_or_none(attributes.get("ratings")) or 0,
            # 官方论坛会提到这个字段，但 OpenAPI 规格里没写 —— 一律用 .get
            "hash_match": bool(attributes.get("moviehash_match")),
            # ---- "这条属于哪部片"（第 21 轮）----
            "feature_id": _int_or_none(details.get("feature_id")),
            "feature_year": _int_or_none(details.get("year")),
            "feature_title": str(details.get("title") or ""),
            "feature_type": str(details.get("feature_type") or ""),
            "imdb_id": _int_or_none(details.get("imdb_id")),
            "tmdb_id": _int_or_none(details.get("tmdb_id")),
            "season": _int_or_none(details.get("season_number")),
            "episode": _int_or_none(details.get("episode_number")),
            "parent_imdb_id": _int_or_none(details.get("parent_imdb_id")),
            "parent_title": str(details.get("parent_title") or ""),
        })
    return out


def score_candidate(candidate, video_fps=0.0, expected_year=None):
    """给候选打分。**"自动匹配准不准"全在这一个函数里**，改它要谨慎。

    这么排的理由，按权重从大到小：

    1. **哈希精确匹配 +1000** —— 官方说法是"有人拿这个一模一样的文件配过
       字幕"。这是唯一能保证时间轴对得上的信号，压过其他一切。
    2. **年份相符 +300 / 差 1 年 +60 / 差 ≥2 年 -500** —— 见
       `YEAR_MATCH_BONUS` 上面那段：候选属于哪部片靠的是接口给的
       `feature_details.year`，这是**唯一**能把同系列拆开的信号。
       拿不到年份的候选不奖不罚（"不知道"不等于"不符"）。
    3. **语言 ze > zh-cn > zh-tw > en** —— 双语字幕一份顶两份。
    4. **帧率相符 +120 / 明显不符 -100** —— 字幕不同步最常见的来源就是
       帧率不同（23.976 vs 25 差 4%，两小时的片子末尾能差 5 分钟）。
       这里不是"锦上添花"，是**防事故**。
    5. 可信来源 +150、非机翻 +80。
    6. 单片（非 CD1/CD2） +40 —— 分片字幕要手工拼，麻烦。
    7. 听障字幕 -30 —— 会多出 `[敲门声]` 这类描述。
    8. 下载量：封顶后按千分之一计入 —— 让它反映口碑，但**不能盖过质量判断**。
    """
    score = 0.0
    score += LANG_RANK.get(candidate.get("language"), -1) * 200

    if candidate.get("hash_match"):
        score += 1000

    if expected_year:
        feature_year = candidate.get("feature_year")
        if feature_year:
            gap = abs(feature_year - expected_year)
            if gap == 0:
                score += YEAR_MATCH_BONUS
            elif gap == 1:
                score += YEAR_NEAR_BONUS
            else:
                score += YEAR_MISMATCH_PENALTY

    if candidate.get("trusted"):
        score += 150
    if not candidate.get("machine") and not candidate.get("ai"):
        score += 80

    fps = candidate.get("fps") or 0.0
    if video_fps and fps:
        diff = abs(fps - video_fps)
        if diff < 0.05:
            score += 120
        elif diff < 0.2:
            score += 40
        else:
            score -= 100

    if candidate.get("cds") == 1:
        score += 40
    if candidate.get("hi"):
        score -= 30

    score += min(candidate.get("downloads") or 0, 200000) / 2000.0
    return score


def rank_candidates(candidates, video_fps=0.0, expected_year=None):
    """按得分从高到低排（同分时下载量多的在前）。"""
    return sorted(candidates,
                  key=lambda c: (score_candidate(c, video_fps, expected_year),
                                 c.get("downloads") or 0),
                  reverse=True)


# ============================================================
# 下载内容的落地
# ============================================================

def extract_payload(blob):
    """下载回来的东西可能是**裸 srt**，也可能是**zip**（里面才是 srt）。

    判断用 ZIP 的魔数 `PK` 而不是 Content-Type —— 头部会骗人，魔数不会；
    社区里这两种返回都出现过，所以都得认。返回 `(bytes, 原始文件名)`。

    优先取 `.srt` / `.vtt`：一个压缩包里可能同时躺着 `.srt` 和说明文件、
    封面图，随便取第一个会取到不该取的。
    """
    if blob[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                names = [n for n in archive.namelist() if not n.endswith("/")]
                if not names:
                    raise OnlineError("下载回来的压缩包是空的")
                chosen = None
                for extension in (".srt", ".vtt", ".sub", ".txt"):
                    for name in names:
                        if name.lower().endswith(extension):
                            chosen = name
                            break
                    if chosen:
                        break
                if chosen is None:
                    chosen = names[0]
                return archive.read(chosen), os.path.basename(chosen)
        except zipfile.BadZipFile as exc:
            raise OnlineError("下载回来的压缩包坏了（%s）" % exc) from exc
    return blob, ""


def target_path(video_path, language, directory=None):
    """给这份字幕挑一个**绝不会覆盖已有文件**的落点。

    ⚠️ 为什么必须让路而不是直接覆盖：视频旁边可能已经有用户辛苦校对过的
    字幕（甚至是上一轮"记忆播放位置"正记着的那份）。自动匹配是锦上添花，
    把用户自己的文件冲掉是不可接受的事故。所以重名就退到
    `电影.zh.1.srt`、`电影.zh.2.srt`…… 宁可文件名丑一点。

    命名沿用第 18 轮 `subtitle.discover()` 认得的写法（`电影.zh.srt`），
    但**双语字幕用 `双语` 而不是 `zh`**：文件名里的语种标签优先级高于内容
    嗅探，标成 `zh` 会让英文那一行永远显示不出来。
    """
    folder = directory or os.path.dirname(os.path.abspath(video_path)) or "."
    stem = os.path.splitext(os.path.basename(video_path))[0]
    suffix = SLOT_SUFFIX.get(language, "sub")

    candidate = os.path.join(folder, "%s.%s.srt" % (stem, suffix))
    index = 1
    while os.path.exists(candidate):
        candidate = os.path.join(folder, "%s.%s.%d.srt" % (stem, suffix, index))
        index += 1
        if index > 999:                 # 简直病态了，认输
            break
    return candidate


def write_subtitle(blob, path):
    """把字节写到 `path`（走「临时文件 + 原子替换」）。

    内容优先**转成 UTF-8** 再写：官方说下载回来就是 UTF-8，但历史上也见过
    别的编码，统一成 UTF-8 能让用户用记事本打开时也不乱码。

    ⚠️ 两个容易踩的点，都在这几行里：

    1. `subtitle.decode_bytes()` 返回的是 **`(文本, 编码名)` 二元组**，
       不是纯文本。当纯文本用会抛 AttributeError —— 而这个函数外面包着
       `except`，于是「转 UTF-8」会**静默失效**、永远原样写。这种"功能不
       生效且不报错"是最难发现的，所以下面显式解包。
    2. 探测结果若是 `utf-8(replace)`，说明原内容本来就不干净（是带替换字符
       硬解出来的）。这种"猜出来的文本"**不能拿回去覆盖原始字节** ——
       宁可原样保存，也别把用户的文件改坏。
    """
    data = blob
    try:
        import subtitle as _subtitle
        text, encoding = _subtitle.decode_bytes(blob)
        if encoding != "utf-8(replace)":
            data = text.encode("utf-8")
    except Exception:
        data = blob

    folder = os.path.dirname(os.path.abspath(path)) or "."
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(dir=folder, prefix=".subtitle-",
                                         suffix=".tmp")
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
        return path
    except Exception as exc:
        if temporary:
            try:
                os.remove(temporary)
            except Exception:
                pass
        raise OnlineError("字幕写不进 %s：%s" % (folder, exc)) from exc


# ============================================================
# 配置（API key）
# ============================================================

CONFIG_NAME = "online.json"
FORMAT_VERSION = 1


def default_config_path():
    """跟播放列表、播放位置放同一个目录（见 playlist_store.default_path）。"""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = (os.environ.get("XDG_CONFIG_HOME")
                or os.path.join(os.path.expanduser("~"), ".config"))
    return os.path.join(base, "final_player", CONFIG_NAME)


def load_config(path=None):
    """读配置。任何异常都返回 `{}`（**不抛**）—— 配置坏了不该拦住播放。"""
    path = path or default_config_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    api_key = data.get("api_key")
    return {"api_key": api_key.strip() if isinstance(api_key, str) else ""}


def save_config(config, path=None):
    """原子写。成功 True、失败 False（**不抛**）。"""
    path = path or default_config_path()
    payload = {
        "version": FORMAT_VERSION,
        "api_key": (config or {}).get("api_key", "") or "",
    }
    folder = os.path.dirname(path) or "."
    temporary = None
    try:
        os.makedirs(folder, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=folder, prefix=".online-",
                                         suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
        return True
    except Exception:
        if temporary:
            try:
                os.remove(temporary)
            except Exception:
                pass
        return False


# ============================================================
# 高层：从视频到候选列表
# ============================================================

def pick_anchor(candidates, info):
    """从候选里挑出"这是哪部片"，返回能代表它的那条；挑不出返回 `None`。

    **判据是"片子身份"里最硬的那个，不是片名。** 为什么不比片名 ——
    接口返回的标题**永远是英文原名**（《让子弹飞》→ `Let the Bullets Fly`、
    《阿凡达》→ `Avatar`），拿中文文件名去比必然对不上（实测过）。

    于是分两种情况，各有各的硬判据：

    · **剧集**（文件名解析出季集号）：比 `season` / `episode`。实测
      `Breaking Bad` S1E1 按片名搜出来 24 条是 **4 集不同的剧**（混进了
      《Breaking Bad: Original Minisodes》和《The Bad Guys: Breaking In》），
      按季集号筛完再聚类才干净。剧集不需要年份 —— 文件名里通常也没有。
    · **电影**：比 `feature_year`，容差 1 年 —— 因为**发行年 ≠ 制作年**是
      真实存在的（实测 The Matrix Revisited 的字幕 release 写 "(2002)"，
      而 `feature_details.year` 是 2001。当"不同片子"剔除会把正主自己剔掉）。

    两种情况都还要求**挑出来的那簇足够突出**（≥ 第二簇的 2 倍条数），
    否则说明分不清，宁可不锚 —— 不锚只是排序差一点，锚错了会把另一部片
    的字幕硬塞给用户。

    ⚠️ 季集号和年份都不知道时**必须返回 None**。「条数最多的那一簇」在
    无年份时是危险的：实测「阿凡达.mkv」32 条是《阿凡达 2》(2020)、
    18 条才是《阿凡达》(2009)，按条数挑会挑成*第二部*；
    「The.Matrix.mkv」更极端 —— 返回的 50 条是续集和纪录片，
    **一条正主都没有**，怎么挑都错。
    """
    want_season = info.get("season")
    want_episode = info.get("episode")
    want_year = info.get("year")

    if want_season is not None and want_episode is not None:
        pool = [c for c in candidates
                if c.get("season") == want_season
                and c.get("episode") == want_episode]
    elif want_year:
        pool = [c for c in candidates
                if c.get("feature_year")
                and abs(c["feature_year"] - want_year) <= 1]
    else:
        return None

    if not pool:
        return None

    groups = {}
    for candidate in pool:
        key = candidate.get("feature_id") or candidate.get("imdb_id")
        if key is None:
            continue
        groups.setdefault(key, []).append(candidate)
    if not groups:
        return None

    ordered = sorted(groups.values(), key=len, reverse=True)
    best = ordered[0]
    if len(ordered) > 1 and len(best) < 2 * len(ordered[1]):
        return None
    return best[0]


# 自动挑候选时，"同一部片的条目占多少"才算定下来了（见 looks_ambiguous）。
# 0.8 这个数是拿实测样本卡出来的：
#   「黑客帝国.mkv」49/50 = 98%  → 该自动下（正主占绝对多数）
#   「阿凡达.mkv」  32/50 = 64%  → 该让人挑（那 64% 是*第二部*）
#   「The.Matrix.mkv」32/50 = 64% → 该让人挑（正主一条都没有）
# 把阈值放在两者中间，64% 那两种会被拦下来，98% 那种放过去。
AUTO_PICK_SHARE = 0.8


def looks_ambiguous(candidates, info):
    """这批候选能不能定下来？`True` = 定不下来，该请用户自己挑。

    定得下来的三种情形：

    1. `pick_anchor` 认出了这是哪部片（年份或季集号对得上）→ 能精确锁定；
    2. 候选**全都属于同一部片** —— 片名在库里只对应一部片，没什么好挑的
       （实测「盗梦空间.mkv」50 条全是 Inception）；
    3. 同一部片的条目占压倒多数（`AUTO_PICK_SHARE`）—— 零星混进来的
       长尾条目不影响判断（实测「黑客帝国.mkv」49 条正主 + 1 条续集）。

    定不下来时**不能硬下**：下一份错的等于白花一次当天配额（未登录只有
    5 次），而且用户还以为配成功了。实测无年份的常见片名（阿凡达、
    The Matrix）就落在这里，占我们手头样本的 2/5 —— 不是罕见情况。
    """
    if not candidates:
        # 空候选不是"定不下来"，是"压根没搜到" —— 那是另一回事，
        # 由 `fetch` 走"没找到"的报错路径，别混进这里。
        return False
    if pick_anchor(candidates, info) is not None:
        return False

    counts = {}
    for candidate in candidates:
        key = candidate.get("feature_id") or candidate.get("imdb_id")
        if key is None:
            continue
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        # 一条编号都没有（很老的片子）→ 没有"是哪部片"这种信息可用，
        # 也就无从判断，交给打分排序走，不拦。
        return False
    if len(counts) <= 1:
        return False
    return max(counts.values()) < AUTO_PICK_SHARE * len(candidates)


def _anchor_search(client, representative, info):
    """按"这部片的编号"再搜一遍，把同系列其他部彻底排除。搜不到返回 `[]`。

    两条路，按可靠性排序（都实测过）：

    · `imdb_id` —— **电影和剧集单集都用它**。实测 `imdb_id=133093` 的 50 条
      全是 The Matrix(1999)；剧集单集 `imdb_id=959621` 的 23 条全是
      Breaking Bad S1E1。最简单也最准，所以排第一。
    · `parent_imdb_id` + 季集号 —— 剧集的备用路。实测「剧集编号 + S1E1」
      得到 23 条 1 集。**必须带季集号**：只给剧集编号会返回整部剧混着的
      50 条（实测 36 集）。

    搜不出东西不是错误（可能这一簇的候选恰好都没编号），交给上层退回原候选。
    """
    season = representative.get("season")
    if season is None:
        season = info.get("season")
    episode = representative.get("episode")
    if episode is None:
        episode = info.get("episode")

    if representative.get("imdb_id"):
        try:
            got = client.search(imdb_id=representative["imdb_id"])
            if got:
                return got
        except OnlineError:
            pass

    if (representative.get("parent_imdb_id")
            and season is not None and episode is not None):
        try:
            got = client.search(parent_imdb_id=representative["parent_imdb_id"],
                                season=season, episode=episode)
            if got:
                return got
        except OnlineError:
            pass

    return []


def find_candidates(client, video_path, video_fps=0.0):
    """搜出这部片子的候选，排好序返回 `(候选列表, 文件名分析结果)`。

    四轮，按"证据从硬到软"排：

      1. 按 `moviehash` **只找精确匹配**的 —— 有人拿这个文件配过字幕，
         时间轴基本不会错。命中就到此为止，不用挑。
      2. 按片名 + **年份**搜。年份是第 21 轮补上的（原来漏了）：实测
         `query="The Matrix"` 一条正主都没有，加上 `year=1999` 全是正主。
      3. 还没结果就把首词去掉再试 —— 有些发布名以括号包着的组名开头
         （`[Group]Movie.2020`），那个词会把结果挤没。
      4. **锚定**：从候选里认出"这是哪部片"（`pick_anchor`），再用它的
         编号精确重搜一遍（`_anchor_search`），把同系列其他部清干净。

    第 4 轮是"配到别的片子"的正解。它能把第 2 轮里那些靠下载量挤上来的
    续集/纪录片全部换掉 —— 实测「让子弹飞」那轮搜出 10 部片混在一起，
    锚定之后 18 条全是 Let the Bullets Fly。

    搜索不花配额（官方：搜索没有限制），所以这几轮随便打。
    返回的 `info` 里会多一个 `anchor` 键：认出片子时是那条代表候选，
    否则是 `None` —— 状态行拿它告诉用户"锁定到哪部片了"。
    """
    info = analyze_filename(video_path)
    info["anchor"] = None
    candidates = []

    try:
        fingerprint = movie_hash(video_path)
    except Exception:
        fingerprint = ""                # 文件读不了就算不出指纹，走片名搜

    if fingerprint:
        try:
            candidates = client.search(
                moviehash=fingerprint, moviehash_match="only",
                season=info["season"], episode=info["episode"])
        except OnlineError:
            candidates = []

    if candidates:
        # 指纹命中 = 有人拿一模一样这个文件配过字幕，这是最硬的证据，
        # 不再锚定（锚定会把它换成"同片的其他版本"，反而丢了时间轴保证）。
        return rank_candidates(candidates, video_fps), info

    if not info["query"]:
        raise OnlineError("从文件名里读不出片名，没法搜 —— 试试【载入字幕】手动指定")
    candidates = client.search(
        query=info["query"], year=info["year"],
        season=info["season"], episode=info["episode"])

    # 第三轮（只在上面一无所获时才走）。要求剩余仍有 ≥2 个词，
    # 别把 "Up 2009" 削成 "2009"。
    if not candidates and len(info["query"].split()) >= 3:
        candidates = client.search(
            query=" ".join(info["query"].split()[1:]), year=info["year"],
            season=info["season"], episode=info["episode"])

    # 第四轮：锚定
    if candidates:
        anchor = pick_anchor(candidates, info)
        if anchor is not None:
            anchored = _anchor_search(client, anchor, info)
            if anchored:
                candidates = anchored
                info["anchor"] = anchor

    return rank_candidates(candidates, video_fps, info["year"]), info


def describe_candidate(candidate):
    """一句话描述一个候选（给状态行、候选列表和"换一个"用）。

    【第 21 轮】开头加上了**这属于哪部片**。理由很实在：状态行写着
    「已匹配 The Matrix Revolutions (2003) · 中英双语」，用户一眼就知道
    配错了 —— 这比等字幕画到屏幕上、发现对不上要早得多。
    片名是接口给的英文原名（中文名它不提供，实测过）；剧集显示成
    「Breaking Bad S01E01」比只给单集标题「Pilot」有用。
    """
    parts = []

    title = candidate.get("feature_title") or ""
    parent = candidate.get("parent_title") or ""
    season = candidate.get("season")
    episode = candidate.get("episode")
    if parent and season is not None and episode is not None:
        title = "%s S%02dE%02d" % (parent, season, episode)
    if title:
        year = candidate.get("feature_year")
        parts.append("%s (%s)" % (title, year) if year else title)

    language = candidate.get("language", "?")
    names = {"zh-cn": "简体中文", "zh-tw": "繁体中文",
             "ze": "中英双语", "en": "英文"}
    parts.append(names.get(language, language))

    if candidate.get("hash_match"):
        parts.append("指纹匹配")
    if candidate.get("trusted"):
        parts.append("可信来源")
    if candidate.get("cds", 1) > 1:
        parts.append("%d 张盘" % candidate["cds"])
    if candidate.get("hi"):
        parts.append("听障版")
    fps = candidate.get("fps") or 0.0
    if fps:
        # ⚠️ 这里**必须**保留小数。`%.3g` 会把 23.976 显示成 "24"，
        # 看着像整帧 24fps —— 而 23.976 恰恰是最常见的那个，
        # 也恰恰是"字幕不同步"的高发区。显示错了会让人做出错误判断。
        parts.append("%g fps" % round(fps, 3))

    downloads = candidate.get("downloads") or 0
    if downloads >= 10000:
        parts.append("下载 %.1f 万" % (downloads / 10000.0))
    elif downloads:
        parts.append("下载 %d" % downloads)

    return " · ".join(parts)


def download_candidate(client, video_path, candidate, directory=None):
    """下载**指定**的候选 → 解压 → 落到视频旁边。

    和 `fetch` 拆开，是为了候选列表那条路：用户在列表里挑好了一条，
    这时不需要再搜、也不用再打分，直接下他点的那条。
    顺带把一条规矩写进代码里 —— **用户的选择永远优先于我们的打分**。
    打分再精细也只是猜，用户看一眼片名和年份就知道对不对。
    """
    blob, meta = client.download(candidate["file_id"])
    content, _inner_name = extract_payload(blob)

    path = target_path(video_path, candidate.get("language") or "", directory)
    write_subtitle(content, path)

    return {
        "path": path,
        "candidate": candidate,
        "description": describe_candidate(candidate),
        "meta": meta,
        "ambiguous": False,
        "total_found": 0,
        "info": None,
    }


def _no_result_hint(info, video_path):
    """一条都搜不到时，把"为什么"和"接下来怎么办"说清楚。

    第 21 轮分出两种说法，来自实测：文件名里没年份时，接口返回的候选里
    **正主压根不会出现**（搜 "The Matrix" 的 50 条全是续集和纪录片）——
    那不算"没搜到"，而是"搜到的全是别的片、正主不在里面"。提示语必须
    不一样，否则用户会一直点【在线匹配】却永远配不对。
    """
    name = info.get("query") or os.path.basename(video_path)
    if not info.get("year"):
        return ("没找到《%s》的字幕。\n"
                "文件名里没有年份 —— 同样的片名会搜出很多别的片子。"
                "点【搜索…】可以自己填片名和年份再挑。" % name)
    return "没找到《%s》的字幕 —— 冷门片或新片常常还没有" % name


def fetch(client, video_path, video_fps=0.0, tried=(), directory=None):
    """完整走一遍：搜 → 挑 → 下载 → 解压 → 落盘。

    返回一个字典，**有两种形态**，靠 `ambiguous` 这个键区分：

    · `ambiguous` 为假 —— 匹配成功。有 `path` / `candidate` / `description`
      / `meta` / `total_found` / `info`。
    · `ambiguous` 为真 —— **没下载**，因为定不下来是哪部片（见
      `looks_ambiguous`）。这时有 `candidates` / `info`，调用方应该把候选
      列表摆出来让用户自己挑。**这一支一份配额都没花** —— 搜索不计次，
      只有下载计次。

    为什么要多出第二种返回：下一份错的花的是当天那 5 次配额里的一次，
    而且用户当场看不出来（字幕画上去才发现对不上），那比"多问一句"
    糟糕得多。实测无年份的常见片名（阿凡达、The Matrix）都落在这里。

    `tried` 是**已经下过的 file_id 集合**：用户对匹配结果不满意时会再点一次，
    这时候要往前推进到下一条，而不是把同一条又下一遍（那样既浪费配额、
    又永远拿到同一份）。这是"再点一次 = 换一条"的实现方式。
    """
    ranked, info = find_candidates(client, video_path, video_fps)

    if not ranked:
        raise OnlineError(_no_result_hint(info, video_path))

    if looks_ambiguous(ranked, info):
        return {
            "ambiguous": True,
            "candidates": ranked,
            "info": info,
            "path": None,
            "candidate": None,
            "description": "",
            "meta": {},
            "total_found": len(ranked),
        }

    usable = [c for c in ranked if c["file_id"] not in set(tried)]
    if not usable:
        raise OnlineError(
            "找到 %d 条，但都已经试过了。点【载入字幕】可以从已有文件里挑"
            % len(ranked))

    result = download_candidate(client, video_path, usable[0], directory)
    result["total_found"] = len(ranked)
    result["info"] = info
    return result


def group_by_feature(candidates):
    """把候选按"属于哪部片"聚在一起，同一部片的连着排。

    为什么要动顺序：实测「阿凡达.mkv」的 50 条候选里，《阿凡达》(2009) 和
    《阿凡达 2》(2020) 的条目**交替出现** —— 用户很难看清"这里面其实是两部
    不同的片子"。聚在一起之后，"这部 18 条、那部 32 条"一眼就能看出来。

    组与组之间按条目数从多到少，组内保持传进来的顺序（调用方已经按打分排过）。
    认片子用的键跟 `pick_anchor` 一致（缺 `feature_id` 就退到 `imdb_id`）。
    """
    groups = {}
    order = []
    for candidate in candidates:
        key = candidate.get("feature_id") or candidate.get("imdb_id")
        if key is None:
            key = ("__unknown__", len(order))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(candidate)

    order.sort(key=lambda k: len(groups[k]), reverse=True)
    out = []
    for key in order:
        out.extend(groups[key])
    return out


def search_candidates(client, query=None, year=None, season=None, episode=None,
                      video_fps=0.0):
    """手动搜索：把原始候选排好序返回。**故意不锚定。**

    与 `find_candidates` 的区别就在最后一步。用户打开候选列表就是想
    "看见所有可能"，而锚定会把不属于某一部片的候选全滤掉 —— 那恰好
    正是用户想看的那些（比如他就是想看看到底有哪些片子跟这个名字重名）。
    """
    if not query and not (season is not None and episode is not None):
        raise OnlineError("请先填片名")
    candidates = client.search(query=query, year=year,
                               season=season, episode=episode)
    return group_by_feature(rank_candidates(candidates, video_fps, year))
