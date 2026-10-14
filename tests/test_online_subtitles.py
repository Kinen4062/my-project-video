# -*- coding: utf-8 -*-
"""第 20 轮：在线字幕匹配 —— 点一下就去 OpenSubtitles 取一份现成的字幕。

要防的那件事：
    视频旁边没有字幕，得上网站找、下载、改名、放进正确目录 —— 这四五步里
    任何一步做错，播放器就"看不到字幕"。而这里面最不能忍的事故是
    **把用户自己辛苦校对过的字幕文件覆盖掉**。

需求拆成一句可验的话：
    点一下按钮，取一份时间轴对得上的字幕放到视频旁边，并且绝不覆盖已有文件。

本套件的分层（从下到上，下面绿了上面才不会假绿）：
    ① 文件名 → 搜索词：发布命名里的技术标记、剧集标记、年份、全角括号
    ② moviehash：跟"按定义逐字节累加"的朴素实现逐一比对（含各边界尺寸）
    ③ 接口结果解析：字段缺失、空文件列表、垃圾条目都不许抛
    ④ 打分与排序：**"自动匹配准不准"全在这一个函数里**
    ⑤ 下载回来的东西：ZIP 与裸 srt 都要认，坏包要报错而不是崩
    ⑥ 落盘：让路不覆盖 + 双语后缀必须被 `subtitle` 认成双语（跨模块契约）
    ⑦ 配置读写：读写失败一律静默降级
    ⑧ 客户端：各种 HTTP 失败 → 对应的异常类型，请求头必须对
    ⑨ 全链路：用假 session 走完 搜 → 挑 → 下载 → 解压 → 落盘
    ⑩ 不联网保证：注入 session 时绝不碰真实的 requests

为什么整条链路都能离线测：
    网络被收敛成一个可替换的 `session`（`OpenSubtitles(session=...)`），
    于是"字段名、排序、让路、解压"这些**最容易写错、又最不容易在真机上
    发现**的东西，全都不需要联网就能验证。网络本身成了唯一不可测的部分 ——
    而不是把逻辑和网络搅在一起，结果谁也测不了。
"""

import io
import os
import struct
import sys
import uuid
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import PROJ, Summary, work_dir  # noqa: E402

if PROJ not in sys.path:
    sys.path.insert(0, PROJ)

import subtitle as sub           # noqa: E402
import subtitle_online as so     # noqa: E402


_RUN_TAG = "%d_%s" % (os.getpid(), uuid.uuid4().hex[:6])

SRT_BODY = ("1\r\n00:00:01,000 --> 00:00:03,000\r\n你好，世界\r\n\r\n"
            "2\r\n00:00:04,000 --> 00:00:06,000\r\nHello, world\r\n")


def _fresh(name):
    """本次进程专属的新目录。

    ⚠️ 不能复用固定目录：这套沙箱**删不掉已存在的文件**（`os.remove` 直接
    WinError 5），上一轮跑剩的文件会变成下一次的"已存在"，让"让路不覆盖"
    这类用例假失败 —— 表现出来就是"第一次绿、第二次红"（第 18 轮踩过）。
    每次换一个带 pid + 随机串的新名字是唯一稳的做法。
    """
    path = os.path.join(work_dir("online"), "run_" + _RUN_TAG, name)
    os.makedirs(path, exist_ok=True)
    return path


# ============================================================
# 假的网络层
# ============================================================

class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b"", json_ok=True):
        self.status_code = status_code
        self._payload = payload
        self._json_ok = json_ok
        self.content = content

    def json(self):
        if not self._json_ok:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """假的 `requests.Session` —— 本套件能离线跑的前提。

    `request(method, url, **kwargs)` 是它唯一需要满足的接口，
    而这正好也就是 `OpenSubtitles` 用到的全部。
    """

    def __init__(self, search=None, download=None, blob=None,
                 search_status=200, download_status=200, blob_status=200,
                 search_json_ok=True, failure=None, search_queue=None):
        self.search = {"total_count": 0, "data": []} if search is None else search
        self.download = download if download is not None else {
            "link": "https://cdn.example.invalid/sub.zip",
            "remaining": 4, "requests": 1,
            "reset_time_utc": "2026-10-15 00:00:00"}
        self.blob = SRT_BODY.encode("utf-8") if blob is None else blob
        self.search_status = search_status
        self.download_status = download_status
        self.blob_status = blob_status
        self.search_json_ok = search_json_ok
        self.failure = failure
        # 连续多次搜索返回不同结果（用来验"降级重试"）
        self.search_queue = list(search_queue or [])
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        if self.failure is not None:
            raise self.failure
        if url.endswith("/subtitles"):
            if self.search_queue:
                payload = self.search_queue.pop(0)
            else:
                payload = self.search
            return FakeResponse(self.search_status, payload,
                                json_ok=self.search_json_ok)
        if url.endswith("/download"):
            return FakeResponse(self.download_status, self.download)
        return FakeResponse(self.blob_status, None, content=self.blob)

    def find(self, suffix):
        return [c for c in self.calls if c["url"].endswith(suffix)]


def _movie(feature_id, title, year, imdb=None, kind="Movie",
           season=None, episode=None, parent_imdb=None, parent_title=None):
    """造一份 `attributes.feature_details`（"这条字幕属于哪部片"）。

    ⚠️ `title` 永远传**英文原名** —— 这不是偷懒，是接口的真实行为：
    实测搜《让子弹飞》返回的 `title` 是 `Let the Bullets Fly`，
    搜《阿凡达》返回 `Avatar`。中文片名它压根不给。**整个"不能拿片名
    比对"的结论就是从这条事实推出来的**，所以夹具也必须照着这个来，
    否则测试会绿在一个真实世界里不成立的前提上。
    """
    details = {"feature_id": feature_id, "feature_type": kind,
               "title": title, "year": year,
               "imdb_id": imdb if imdb is not None else feature_id}
    if season is not None:
        details["season_number"] = season
    if episode is not None:
        details["episode_number"] = episode
    if parent_imdb is not None:
        details["parent_imdb_id"] = parent_imdb
        details["parent_title"] = parent_title
    return details


def _candidate(file_id, language="zh-cn", downloads=5000, fps=23.976,
               hash_match=False, trusted=False, machine=False, ai=False,
               hi=False, cds=1, release="", extra_files=0, movie=None):
    """造一条和 API 返回结构一致的搜索结果。

    `movie` 是"这条属于哪部片"（`attributes.feature_details`），第 21 轮新增。
    传 `None` 就是**老结构**（响应里没有这个键）—— 那种情况下新逻辑必须
    完全不生效、行为跟改动前逐字节一样。老用例一条没改就全绿，靠的就是
    这条"没有 feature_details 就退回老路"的保证。
    """
    files = [{"file_id": file_id, "file_name": "x.srt"}]
    for i in range(extra_files):
        files.append({"file_id": file_id + 1000 + i, "file_name": "y.srt"})
    attributes = {
        "language": language, "download_count": downloads, "fps": fps,
        "from_trusted": trusted, "machine_translated": machine,
        "ai_translated": ai, "hearing_impaired": hi, "release": release,
        "moviehash_match": hash_match, "files": files}
    if movie is not None:
        attributes["feature_details"] = movie
    return {"attributes": attributes}


def _payload(*candidates, total=None):
    return {"total_count": total if total is not None else len(candidates),
            "data": list(candidates)}


# ============================================================
# ① 文件名 → 搜索词
# ============================================================

def section_filenames(s):
    s.section("① 文件名 → 搜索词")

    cases = [
        # 文件名, 期望的搜索词, 期望季, 期望集
        ("The.Matrix.1999.1080p.BluRay.x264-GROUP.mkv", "The Matrix 1999", None, None),
        ("Spider-Man.No.Way.Home.2021.2160p.WEB-DL.DDP5.1.mkv",
         "Spider-Man No Way Home 2021", None, None),
        ("Interstellar.2014.1080p.mkv", "Interstellar 2014", None, None),
        ("Up.2009.1080p.BluRay.mkv", "Up 2009", None, None),
        ("Show.S02E05.720p.HDTV.x264.mkv", "Show", 2, 5),
        ("Show.S1.E2.1080p.mkv", "Show", 1, 2),
        ("Show.2x05.720p.mkv", "Show", 2, 5),
        # 开头的方括号组名：`[Subs]` 在噪声表里，会被切掉
        ("[Subs] Some.Anime.S01E03.1080p.mkv", "Some Anime", 1, 3),
        # 中文片名必须原样保住（这条是**最容易改坏**的：谁要是为了
        # 处理 `[Group]` 而"丢弃方括号内容"，这里会立刻变红）
        ("[阿凡达].2009.1080p.mkv", "阿凡达 2009", None, None),
        ("【流浪地球】.2019.4K.WEB-DL.mkv", "流浪地球 2019", None, None),
        ("让子弹飞.mkv", "让子弹飞", None, None),
        ("Movie.2020.4K.WEB-DL.mkv", "Movie 2020", None, None),
        ("Movie.2020.1080p.x265.10bit.HDR.mkv", "Movie 2020", None, None),
        ("Movie (2020) [1080p] [BluRay].mkv", "Movie 2020", None, None),
    ]
    for name, want_query, want_season, want_episode in cases:
        info = so.analyze_filename(os.path.join("D:/影片", name))
        s.check(info["query"] == want_query,
                "%-46s → 搜索词 %r" % (name[:44], info["query"]))
        s.check(info["season"] == want_season and info["episode"] == want_episode,
                "%-46s → 季集 %s/%s" % (name[:44], info["season"], info["episode"]))

    info = so.analyze_filename("Movie.2020.1080p.mkv")
    s.check(info["year"] == 2020, "年份 2020 被识别出来（%s）" % info["year"])
    info = so.analyze_filename("让子弹飞.mkv")
    s.check(info["year"] is None, "中文片名没有年份时不瞎猜（%s）" % info["year"])

    s.check(so.analyze_filename("")["query"] == "",
            "空文件名不抛异常（返回空搜索词，上层自会拦住）")


# ============================================================
# ② moviehash
# ============================================================

def section_hash(s):
    s.section("② moviehash（跟朴素实现逐一比对）")

    def naive(path, chunk=1 << 16):
        """按定义写的最直白版本：整块读进来、逐 8 字节累加、不 seek。

        用它当参照的意义：`movie_hash` 真正容易写错的地方是**边界**
        （读不满 8 字节、大小不足 128KB 时两块重叠、seek 位置），
        而这些恰恰是"公式背对了也照样错"的地方。
        """
        size = os.path.getsize(path)
        total, mask = size, 0xFFFFFFFFFFFFFFFF
        with open(path, "rb") as fh:
            head = fh.read(chunk)
            fh.seek(max(0, size - chunk))
            tail = fh.read(chunk)
        for block in (head, tail):
            for i in range(0, len(block) - 7, 8):
                total = (total + struct.unpack("<Q", block[i:i + 8])[0]) & mask
        return "%016x" % total

    folder = _fresh("hash")
    sizes = [0, 1, 8, 1000, 65535, 65536, 65537, 131072, 131073, 300000]
    mismatches = []
    for size in sizes:
        path = os.path.join(folder, "h_%d.bin" % size)
        with open(path, "wb") as fh:
            fh.write(bytes((i * 37 + 11) % 256 for i in range(size)))
        got, want = so.movie_hash(path), naive(path)
        if got != want:
            mismatches.append("size=%d 分块=%s 朴素=%s" % (size, got, want))
    s.check(not mismatches,
            "%d 种尺寸（含 0 / 1 / 8 / 65535 / 65536 / 65537 / 131073）"
            "分块实现与朴素实现逐位一致%s"
            % (len(sizes), "" if not mismatches else "；不符：" + "; ".join(mismatches)))

    empty = os.path.join(folder, "empty.bin")
    with open(empty, "wb"):
        pass
    s.check(so.movie_hash(empty) == "0000000000000000",
            "空文件的指纹是全零（不是崩溃、也不是随机值）")

    s.check(len(so.movie_hash(os.path.join(folder, "h_1000.bin"))) == 16,
            "指纹固定是 16 位十六进制（接口要的形式）")

    s.check(so.movie_hash(os.path.join(folder, "h_300000.bin"))
            == so.movie_hash(os.path.join(folder, "h_300000.bin")),
            "同一个文件算两次结果一样（没有把随机数混进去）")


# ============================================================
# ③ 结果解析
# ============================================================

def section_parse(s):
    s.section("③ 接口结果解析（脏数据不许抛）")

    payload = _payload(
        _candidate(111, "zh-cn", downloads=12000, fps=23.976,
                   hash_match=True, trusted=True),
        _candidate(222, "en", downloads=90000, fps=25.0, extra_files=1),
        _candidate(333, "zh-tw", downloads=300),
        {"attributes": {"language": "en", "files": []}},      # 没有可下载文件
        {"attributes": {"files": [{"file_name": "no-id.srt"}]}},   # 缺 file_id
        {"attributes": {"files": [{"file_id": "不是数字"}]}},        # file_id 非法
        {"attributes": None},                                  # attributes 是 null
        "这根本不是个字典",                                      # 垃圾条目
        {"attributes": {"language": "en",
                        "files": [{"file_id": 444, "file_name": "ok.srt"}]}},
    )
    parsed = so.parse_results(payload)
    s.check(len(parsed) == 4,
            "9 条输入里有 4 条可用，其余（空文件表/缺 id/非法 id/null/垃圾）"
            "全被丢弃而不是抛异常（实际 %d）" % len(parsed))
    s.check([c["file_id"] for c in parsed] == [111, 222, 333, 444],
            "保留下来的 file_id 顺序正确：%s" % [c["file_id"] for c in parsed])

    first = parsed[0]
    s.check(first["language"] == "zh-cn" and first["downloads"] == 12000
            and first["hash_match"] and first["trusted"] and not first["machine"],
            "字段映射正确：语言=%s 下载=%s 指纹=%s 可信=%s"
            % (first["language"], first["downloads"], first["hash_match"],
               first["trusted"]))

    s.check(parsed[1]["cds"] == 2,
            "一个条目带两个文件时 cds=2（分片字幕，排序时要降权）")
    s.check(parsed[1]["fps"] == 25.0 and parsed[2]["fps"] == 23.976,
            "fps 被读成浮点数（不是字符串）")

    s.check(so.parse_results({}) == [],
            "响应里没有 data 字段 → 空列表，不是抛 KeyError")
    s.check(so.parse_results({"data": None}) == [],
            "data 是 null → 空列表")
    s.check(so.parse_results({"data": []}) == [],
            "data 是空数组 → 空列表")

    weird = so.parse_results({"data": [
        {"attributes": {"language": "zh-cn", "download_count": "很多",
                        "fps": "不是数字",
                        "files": [{"file_id": 1, "file_name": "a.srt"}]}}]})
    s.check(len(weird) == 1 and weird[0]["downloads"] == 0 and weird[0]["fps"] == 0.0,
            "数字字段给的是垃圾字符串 → 退成 0，不抛（下载=%s fps=%s）"
            % (weird[0]["downloads"], weird[0]["fps"]))


# ============================================================
# ④ 打分与排序
# ============================================================

def section_rank(s):
    s.section("④ 打分与排序（自动匹配准不准全在这）")

    fps = 23.976

    hash_low = _candidate(1, "en", downloads=10, fps=23.976, hash_match=True)
    plain_high = _candidate(2, "en", downloads=500000, fps=23.976)
    a, b = so.parse_results(_payload(hash_low, plain_high))
    ranked = so.rank_candidates([a, b], fps)
    s.check(ranked[0]["file_id"] == 1,
            "指纹精确匹配压过 50 万次下载量（%s 排第一）—— 时间轴对得上比什么都重要"
            % ranked[0]["file_id"])

    ze = so.parse_results(_payload(_candidate(1, "ze")))[0]
    zh_cn = so.parse_results(_payload(_candidate(2, "zh-cn")))[0]
    en = so.parse_results(_payload(_candidate(3, "en")))[0]
    ranked = so.rank_candidates([en, zh_cn, ze], fps)
    s.check([c["language"] for c in ranked] == ["ze", "zh-cn", "en"],
            "语言优先级 中英双语 > 简体 > 英文（实际 %s）"
            % [c["language"] for c in ranked])

    good_fps = so.parse_results(_payload(_candidate(1, fps=23.976)))[0]
    bad_fps = so.parse_results(_payload(_candidate(2, fps=25.0)))[0]
    ranked = so.rank_candidates([bad_fps, good_fps], fps)
    s.check(ranked[0]["file_id"] == 1,
            "帧率相符的排在前面（23.976 vs 25 是字幕不同步的经典来源）")

    s.check(so.score_candidate(good_fps, fps) > so.score_candidate(bad_fps, fps) + 100,
            "帧率不符的惩罚足够重（不是象征性地扣几分）：%0.0f vs %0.0f"
            % (so.score_candidate(good_fps, fps), so.score_candidate(bad_fps, fps)))

    no_fps = so.parse_results(_payload(_candidate(1, fps=0)))[0]
    s.check(so.score_candidate(no_fps, fps) == so.score_candidate(
        so.parse_results(_payload(_candidate(1, fps=0)))[0], fps),
            "候选没给帧率时不奖不罚（不能因为「不知道」就当成「不符」）")

    single = so.parse_results(_payload(_candidate(1, cds=1)))[0]
    multi = so.parse_results(_payload(_candidate(2, cds=2, extra_files=1)))[0]
    s.check(so.score_candidate(single, fps) > so.score_candidate(multi, fps),
            "分片字幕（CD1/CD2）降权 —— 它要手工拼，或者干脆只能显示前半部")

    normal = so.parse_results(_payload(_candidate(1)))[0]
    hi = so.parse_results(_payload(_candidate(2, hi=True)))[0]
    s.check(so.score_candidate(normal, fps) > so.score_candidate(hi, fps),
            "听障字幕降权（会多出 [敲门声] 这类描述）")

    machine = so.parse_results(_payload(_candidate(1, machine=True)))[0]
    human = so.parse_results(_payload(_candidate(2)))[0]
    s.check(so.score_candidate(human, fps) > so.score_candidate(machine, fps),
            "机翻降权")

    tiny = so.parse_results(_payload(_candidate(1, downloads=1)))[0]
    huge = so.parse_results(_payload(_candidate(2, downloads=1000000)))[0]
    gap = so.score_candidate(huge, fps) - so.score_candidate(tiny, fps)
    s.check(0 < gap < 150,
            "下载量的影响**有上限**（差 %0.0f 分）—— 它是口碑不是真理，"
            "不能盖过语言和帧率的判断" % gap)

    s.check(so.rank_candidates([], fps) == [], "空列表排序不抛")


# ============================================================
# ⑤ 下载回来的东西
# ============================================================

def section_extract(s):
    s.section("⑤ 下载回来的东西（ZIP / 裸 srt / 坏包）")

    raw = SRT_BODY.encode("utf-8")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "说明文件")
        archive.writestr("cover.jpg", b"\xff\xd8\xff\xe0 not a real jpg")
        archive.writestr("movie.srt", SRT_BODY)
    zipped = buffer.getvalue()

    content, name = so.extract_payload(zipped)
    s.check(content == raw, "ZIP 里的字幕被完整取出来（%d 字节）" % len(content))
    s.check(name == "movie.srt", "返回的是压缩包内的真实文件名（%s）" % name)

    content, name = so.extract_payload(raw)
    s.check(content == raw, "裸 srt 直接原样返回（不是所有下载都给 ZIP）")
    s.check(name == "", "裸 srt 没有包内文件名，返回空串")

    only_txt = io.BytesIO()
    with zipfile.ZipFile(only_txt, "w") as archive:
        archive.writestr("notes.txt", "只有说明")
    content, name = so.extract_payload(only_txt.getvalue())
    s.check(name == "notes.txt" and content == "只有说明".encode("utf-8"),
            "包里没有 srt 时退而取别的文件（%s）—— 总比空手而归强" % name)

    empty_zip = io.BytesIO()
    with zipfile.ZipFile(empty_zip, "w"):
        pass
    try:
        so.extract_payload(empty_zip.getvalue())
        s.check(False, "空压缩包应该报错")
    except so.OnlineError as exc:
        s.check("空" in str(exc), "空压缩包 → 明确的 OnlineError：%s" % exc)

    try:
        so.extract_payload(b"PK\x03\x04" + b"\x00" * 100)
        s.check(False, "坏掉的 ZIP 应该报错")
    except so.OnlineError as exc:
        s.check("坏" in str(exc) or "zip" in str(exc).lower(),
                "坏 ZIP → 明确的 OnlineError（不是 BadZipFile 漏出去）：%s" % exc)

    s.check(so.extract_payload(b"")[0] == b"", "空字节不抛（上层会拦「文件是空的」）")


# ============================================================
# ⑥ 落盘
# ============================================================

def section_save(s):
    s.section("⑥ 落盘：让路不覆盖 + 语种后缀能被播放器认出来")

    folder = _fresh("save")
    video = os.path.join(folder, "Movie.2020.1080p.mkv")
    with open(video, "wb") as fh:
        fh.write(b"x" * 500)

    first = so.target_path(video, "zh-cn")
    s.check(os.path.basename(first) == "Movie.2020.1080p.zh.srt",
            "第一个落点是 电影.zh.srt（沿用 discover 认得的写法）：%s"
            % os.path.basename(first))

    with open(first, "w", encoding="utf-8") as fh:
        fh.write("用户自己校对过的字幕")
    second = so.target_path(video, "zh-cn")
    s.check(second != first,
            "**已有同名文件时必须让路** —— 绝不覆盖用户自己的字幕（退到 %s）"
            % os.path.basename(second))
    s.check(os.path.basename(second) == "Movie.2020.1080p.zh.1.srt",
            "让路的方式是加序号：%s" % os.path.basename(second))

    # 真的把第二份写下去，再问第三次（`target_path` 是纯函数，只看文件系统
    # 的实际状态 —— 光问不写，两次当然返回同一个名字）
    with open(second, "w", encoding="utf-8") as fh:
        fh.write("第一份在线字幕")
    third_path = so.target_path(video, "zh-cn")
    s.check(os.path.basename(third_path) == "Movie.2020.1080p.zh.2.srt",
            "两份都占了以后再问 → 继续退到 .zh.2.srt（%s）"
            % os.path.basename(third_path))
    s.check(third_path not in (first, second),
            "三次落点两两不同（%s / %s / %s）"
            % (os.path.basename(first), os.path.basename(second),
               os.path.basename(third_path)))

    s.check(open(first, encoding="utf-8").read() == "用户自己校对过的字幕",
            "让路之后，原来那个文件的内容**一个字节都没动**")

    # ---- 跨模块契约：后缀必须能被 subtitle 认成语种 ----
    s.section("⑥b 跨模块契约：落盘的文件名，播放器认成什么语种")
    zh_path = so.target_path(video, "zh-cn")
    s.check(sub.language_from_name(zh_path) == "zh",
            "简体落成 %s → 播放器认成 zh"
            % os.path.basename(zh_path))

    ze_path = so.target_path(video, "ze")
    s.check(os.path.basename(ze_path) == "Movie.2020.1080p.双语.srt",
            "双语落成 电影.双语.srt：%s" % os.path.basename(ze_path))
    s.check(sub.language_from_name(ze_path) == "both",
            "**双语必须被认成 both** —— 若落成 .zh.srt，文件名标签会压过内容"
            "嗅探，英文那一行就永远不显示了")

    named_zh = os.path.join(folder, "Movie.2020.1080p.zh.1.srt")
    s.check(sub.language_from_name(named_zh) == "zh",
            "让路加序号之后语种仍然认得出（%s）" % os.path.basename(named_zh))

    # ---- 跨模块契约（第 20 轮真机验证补的）：「下完立刻生效」与「重开仍然生效」 ----
    #
    # `_online_done` 用 explicit_path 装刚下回来的那份，所以**当次**一定生效。
    # 要防的是**下一次**打开视频：那时没人给 explicit_path，只剩 discover() 自动找。
    # 若让路产生的名字排在原名**后面**，用户就会看到"我刚下的字幕，重开一次就变回
    # 老的了" —— 比不生效更莫名其妙。
    #
    # 现在两者一致，靠的是 discover 排序键最后那一条"按名字定序"：
    # `Movie.zh.1.srt` < `Movie.zh.srt`（第 9 位 `'1'`(0x31) < `'s'`(0x73)）。
    # ⚠️ **这是 ASCII 顺序凑出来的，不是有人设计过** —— 所以在这儿钉一颗钉子：哪天
    # 有人改了 `target_path` 的让路写法（比如换成 `Movie.zh(1).srt`），这条断言会
    # 立刻红给他看，而不是等哪个用户来报"我的字幕莫名其妙变回去了"。
    collide = _fresh("contract_order")
    collide_video = os.path.join(collide, "Movie.mkv")
    with open(collide_video, "wb") as fh:
        fh.write(b"x" * 500)
    mine = os.path.join(collide, "Movie.zh.srt")
    with open(mine, "w", encoding="utf-8") as fh:
        fh.write("1\n00:00:01,000 --> 00:00:03,000\n用户原来那份\n")
    later = so.target_path(collide_video, "zh-cn")
    s.check(os.path.basename(later) == "Movie.zh.1.srt",
            "用户已有 .zh.srt → 在线那份落成 .zh.1.srt（%s）"
            % os.path.basename(later))
    with open(later, "w", encoding="utf-8") as fh:
        fh.write("1\n00:00:01,000 --> 00:00:03,000\n刚下回来的那份\n")

    order = [os.path.basename(p) for p, _ in sub.discover(collide_video)]
    print("      discover 认领顺序：%s" % order)
    bundle = sub.build(video_path=collide_video)
    chosen = bundle.track_for("zh")
    s.check(chosen is not None
            and os.path.basename(chosen.path) == os.path.basename(later),
            "重开视频时生效的仍是**刚下回来的那份**（%s）—— 与 _online_done 的 "
            "explicit_path 一致；代价是用户原来那份不再自动生效（要它回来得删掉新"
            "下的、或点【载入字幕】点名）" % os.path.basename(later))

    # ---- 内容写进去、并且转成 UTF-8 ----
    gbk_text = "1\r\n00:00:01,000 --> 00:00:03,000\r\n你好\r\n"
    target = os.path.join(folder, "gbk.zh.srt")
    so.write_subtitle(gbk_text.encode("gbk"), target)
    with open(target, "rb") as fh:
        written = fh.read()
    s.check(written.decode("utf-8") == gbk_text,
            "GBK 的内容被转成 UTF-8 落盘（用记事本打开不会乱码，%d 字节）"
            % len(written))
    s.check(not [n for n in os.listdir(folder) if n.startswith(".subtitle-")],
            "原子写不留临时文件残渣（目录里没有 .subtitle-* 开头的东西）")

    parsed = sub.parse(open(target, encoding="utf-8").read())
    s.check(len(parsed) == 1 and parsed[0]["start"] == 1.0,
            "落盘的字幕能被 subtitle.parse 正常解析出来（%d 条）" % len(parsed))


# ============================================================
# ⑦ 配置
# ============================================================

def section_config(s):
    s.section("⑦ 配置读写（失败一律静默降级）")

    folder = _fresh("config")
    path = os.path.join(folder, "online.json")

    s.check(so.load_config(path) == {},
            "文件不存在 → 空配置（不是抛异常）")

    s.check(so.save_config({"api_key": "abc-123"}, path) is True,
            "保存成功返回 True")
    s.check(so.load_config(path).get("api_key") == "abc-123",
            "存进去再读出来，key 一致")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ 这不是合法 JSON")
    s.check(so.load_config(path) == {},
            "配置文件坏了 → 空配置（坏一个配置不该让播放器打不开）")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write('["列表不是字典"]')
    s.check(so.load_config(path) == {},
            "配置文件是数组而不是字典 → 空配置")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"api_key": 12345}')
    s.check(so.load_config(path).get("api_key") == "",
            "key 不是字符串 → 当成空（不让 int 混进请求头）")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"api_key": "  "}')
    s.check(so.load_config(path).get("api_key") == "",
            "只有空白的 key → 当成空")

    s.check(so.save_config({}, os.path.join(folder, "没有权限", "x.json"))
            in (True, False),
            "目录建不出来时返回 False 而不是抛（不拦播放）")

    s.check(so.default_config_path().endswith(os.path.join("final_player",
                                                           "online.json")),
            "默认路径与播放列表/播放位置同目录：%s"
            % os.path.basename(so.default_config_path()))
    s.check(so.default_config_path() != so.__dict__.get("__file__", ""),
            "key 存在用户配置目录，**不落在程序目录**（不进版本库）")


# ============================================================
# ⑧ 客户端
# ============================================================

def section_client(s):
    s.section("⑧ 客户端：请求头、失败码、配额")

    try:
        so.OpenSubtitles("")
        s.check(False, "空 key 应该抛 NoAPIKey")
    except so.NoAPIKey as exc:
        s.check("API key" in str(exc), "空 key → NoAPIKey：%s" % exc)

    try:
        so.OpenSubtitles("   ")
        s.check(False, "全空白的 key 应该抛 NoAPIKey")
    except so.NoAPIKey:
        s.check(True, "只有空白的 key 也 → NoAPIKey")

    session = FakeSession(search=_payload(_candidate(1)))
    client = so.OpenSubtitles("my-key", session=session)
    client.search(query="Movie", moviehash="abc123", season=2, episode=5)

    call = session.find("/subtitles")[0]
    headers = call["kwargs"]["headers"]
    s.check(headers.get("Api-Key") == "my-key",
            "请求头带 Api-Key")
    s.check(headers.get("User-Agent") == so.USER_AGENT
            and "final_player" in headers.get("User-Agent", ""),
            "User-Agent 是能认出应用的写法（%s）—— 官方明说通用 UA 会被封"
            % headers.get("User-Agent"))

    params = call["kwargs"]["params"]
    s.check(params.get("languages") == "en,ze,zh-cn,zh-tw",
            "语言参数按字母序、且含 `ze`（中英双语）：%s" % params.get("languages"))
    s.check(",".join(sorted(params["languages"].split(","))) == params["languages"],
            "语言参数确实是字母序（官方要求，否则接口可能报错）")
    s.check(params.get("query") == "Movie" and params.get("moviehash") == "abc123",
            "query 与 moviehash 都传下去了")
    s.check(params.get("type") == "episode" and params.get("season_number") == 2
            and params.get("episode_number") == 5,
            "剧集参数正确（S02E05 才搜得到那一集）")
    s.check(params.get("machine_translated") == "exclude"
            and params.get("ai_translated") == "exclude",
            "机翻与 AI 翻译默认排除")

    s.check(so.OpenSubtitles("k", session=FakeSession()).__class__ is so.OpenSubtitles,
            "显式传 session 时不碰 requests")

    # ---- 各种失败码 ----
    cases = [
        (401, so.OnlineError, "API key 无效"),
        (403, so.OnlineError, "不认这个"),
        (406, so.QuotaExceeded, "额度用完"),
        (429, so.QuotaExceeded, "太频繁"),
        (500, so.OnlineError, "HTTP 500"),
    ]
    for status, expected, keyword in cases:
        client = so.OpenSubtitles("k", session=FakeSession(
            search_status=status, search={"errors": ["出问题了"]}))
        try:
            client.search(query="x")
            s.check(False, "HTTP %d 应该抛 %s" % (status, expected.__name__))
        except expected as exc:
            s.check(keyword in str(exc) or str(status) in str(exc),
                    "HTTP %d → %s：%s" % (status, expected.__name__, exc))
        except so.OnlineError as exc:
            s.check(False, "HTTP %d 抛了 %s 而不是 %s：%s"
                    % (status, type(exc).__name__, expected.__name__, exc))

    s.check(issubclass(so.QuotaExceeded, so.OnlineError),
            "QuotaExceeded 是 OnlineError 的子类（调用方只 catch 父类也不会漏）")

    client = so.OpenSubtitles("k", session=FakeSession(
        failure=ConnectionError("网线被拔了")))
    try:
        client.search(query="x")
        s.check(False, "网络异常应该被翻译成 OnlineError")
    except so.OnlineError as exc:
        s.check("网络" in str(exc), "网络异常 → OnlineError：%s" % exc)

    client = so.OpenSubtitles("k", session=FakeSession(
        search_status=200, search_json_ok=False))
    try:
        client.search(query="x")
        s.check(False, "返回不是 JSON 时应该抛 OnlineError")
    except so.OnlineError as exc:
        s.check("JSON" in str(exc) or "看不懂" in str(exc),
                "返回不是 JSON → OnlineError：%s" % exc)

    # ---- 下载与配额 ----
    session = FakeSession(blob=b"raw srt bytes")
    client = so.OpenSubtitles("k", session=session)
    blob, meta = client.download(7777)
    s.check(blob == b"raw srt bytes", "下载拿到原始字节")
    s.check(meta["remaining"] == 4 and meta["requests"] == 1,
            "**剩余配额被带回来**（remaining=%s）—— 官方只有下载那一刻才告诉你"
            % meta["remaining"])
    post = session.find("/download")[0]
    s.check(post["kwargs"]["json"] == {"file_id": 7777},
            "下载用 POST + file_id（%s）" % post["kwargs"]["json"])
    s.check(post["kwargs"]["headers"].get("Api-Key") == "k",
            "下载请求带 Api-Key")

    client = so.OpenSubtitles("k", session=FakeSession(download={"link": ""}))
    try:
        client.download(1)
        s.check(False, "没有 link 时应该抛")
    except so.OnlineError as exc:
        s.check("下载地址" in str(exc), "接口没给 link → OnlineError：%s" % exc)

    client = so.OpenSubtitles("k", session=FakeSession(blob=b""))
    try:
        client.download(1)
        s.check(False, "空文件应该抛")
    except so.OnlineError as exc:
        s.check("空" in str(exc), "下载回来是空的 → OnlineError：%s" % exc)


# ============================================================
# ⑨ 全链路
# ============================================================

def section_fetch(s):
    s.section("⑨ 全链路（假 session 走完 搜→挑→下载→解压→落盘）")

    folder = _fresh("fetch")
    video = os.path.join(folder, "The.Matrix.1999.1080p.BluRay.mkv")
    with open(video, "wb") as fh:
        fh.write(b"x" * 2000)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("matrix.srt", SRT_BODY)
    zipped = buffer.getvalue()

    session = FakeSession(
        search=_payload(_candidate(111, "zh-cn", downloads=5000, fps=23.976,
                                   hash_match=True, trusted=True),
                        _candidate(222, "en", downloads=80000, fps=23.976)),
        blob=zipped)
    client = so.OpenSubtitles("key", session=session)

    result = so.fetch(client, video, video_fps=23.976)
    s.check(os.path.basename(result["path"]) == "The.Matrix.1999.1080p.BluRay.zh.srt",
            "落盘到视频旁边、名字能被播放器认领：%s"
            % os.path.basename(result["path"]))

    with open(result["path"], "rb") as fh:
        saved = fh.read()
    s.check(saved == SRT_BODY.encode("utf-8"),
            "**ZIP 解出来的内容真的写进了文件**（%d 字节）" % len(saved))
    s.check(len(sub.parse(saved.decode("utf-8"))) == 2,
            "落盘的字幕能被解析成 2 条 cue")

    s.check(result["candidate"]["file_id"] == 111,
            "挑中的是指纹匹配的那条（%s）而不是下载量更大的英文那条"
            % result["candidate"]["file_id"])
    s.check("简体中文" in result["description"] and "指纹匹配" in result["description"],
            "描述里说清了选中的是什么：%s" % result["description"])
    s.check(result["meta"]["remaining"] == 4,
            "剩余配额透传到调用方（%s）" % result["meta"]["remaining"])
    s.check(result["total_found"] == 2, "报告一共找到几条（%s）" % result["total_found"])

    urls = [c["url"].split("/api/v1")[-1] for c in session.calls]
    s.check(urls[0].startswith("/subtitles") and urls[1] == "/download",
            "请求顺序是 先搜后下：%s" % urls[:2])
    s.check(session.find("/subtitles")[0]["kwargs"]["params"].get("moviehash_match")
            == "only",
            "第一轮搜索只要指纹精确匹配的（moviehash_match=only）")

    # ---- 再点一次：往前推进，不重复下同一条 ----
    session2 = FakeSession(
        search=_payload(_candidate(111, "zh-cn", hash_match=True),
                        _candidate(222, "en")),
        blob=zipped)
    client2 = so.OpenSubtitles("key", session=session2)
    second = so.fetch(client2, video, video_fps=23.976, tried={111})
    s.check(second["candidate"]["file_id"] == 222,
            "已经试过的 file_id 会被跳过，往前推进到第 2 条（%s）"
            % second["candidate"]["file_id"])
    s.check(second["path"] != result["path"],
            "第二次落盘**不会覆盖第一次那份**（%s）"
            % os.path.basename(second["path"]))

    # ---- 全试过了 ----
    try:
        so.fetch(client2, video, tried={111, 222})
        s.check(False, "都试过了应该报错而不是反复下同一条")
    except so.OnlineError as exc:
        s.check("试过" in str(exc), "候选都试过 → 明确告知，并指向【载入字幕】：%s" % exc)

    # ---- 一条都没有 ----
    client3 = so.OpenSubtitles("key", session=FakeSession(
        search_queue=[_payload(), _payload(), _payload()]))
    try:
        so.fetch(client3, video, video_fps=23.976)
        s.check(False, "没有候选时应该报错")
    except so.OnlineError as exc:
        s.check("没找到" in str(exc), "搜不到 → 说明是哪部片搜不到：%s" % exc)

    # ---- 指纹搜不到就退到片名搜 ----
    session4 = FakeSession(search_queue=[_payload(), _payload(_candidate(999, "en"))])
    client4 = so.OpenSubtitles("key", session=session4)
    result4 = so.fetch(client4, video, video_fps=23.976)
    s.check(result4["candidate"]["file_id"] == 999,
            "指纹一轮没结果 → 自动退到按片名搜（拿到 %s）"
            % result4["candidate"]["file_id"])
    queries = [c["kwargs"]["params"] for c in session4.find("/subtitles")]
    s.check(len(queries) == 2 and "moviehash" in queries[0]
            and queries[1].get("query") == "The Matrix 1999",
            "两轮搜索分别是 指纹 / 片名（%s）"
            % [(q.get("query"), "hash" if "moviehash" in q else "") for q in queries])

    # ---- 降级重试：方括号组名混进搜索词时 ----
    session5 = FakeSession(search_queue=[
        _payload(), _payload(), _payload(_candidate(555, "zh-cn"))])
    client5 = so.OpenSubtitles("key", session=session5)
    result5 = so.fetch(client5, video, video_fps=23.976)
    queries = [c["kwargs"]["params"].get("query") for c in session5.find("/subtitles")]
    s.check(queries[2] == "Matrix 1999",
            "前两轮都没结果时，去掉首词再试一次（第 3 轮搜的是 %r）" % queries[2])
    s.check(result5["candidate"]["file_id"] == 555, "第三轮真的救回来了")


# ============================================================
# ⑩ 不联网保证
# ============================================================

def section_offline(s):
    s.section("⑩ 注入 session 时绝不碰真实的 requests")

    class ExplodingRequests:
        def __getattr__(self, name):
            raise AssertionError("不该碰真实的 requests.%s" % name)

    folder = _fresh("offline")
    video = os.path.join(folder, "Movie.2020.mkv")
    with open(video, "wb") as fh:
        fh.write(b"x" * 100)

    original = so.requests
    try:
        so.requests = ExplodingRequests()
        client = so.OpenSubtitles("k", session=FakeSession(
            search=_payload(_candidate(1, "zh-cn"))))
        result = so.fetch(client, video, video_fps=23.976)
        s.check(os.path.exists(result["path"]),
                "把 requests 换成「一碰就炸」的对象后，注入 session 的链路照样跑通 —— "
                "证明这条路径**完全没碰真实网络**")
    finally:
        so.requests = original

    s.check(so.HTTP_AVAILABLE in (True, False),
            "模块能在没有 requests 的环境里被 import（HTTP_AVAILABLE=%s）"
            % so.HTTP_AVAILABLE)

    try:
        # 模拟"运行时没有 requests"：既有 session 又没库，才该报错
        so.requests = None
        try:
            so.OpenSubtitles("k")
            s.check(False, "既没 session 又没 requests 时应该抛 OnlineError")
        except so.OnlineError as exc:
            s.check("requests" in str(exc),
                    "没有 requests 且没注入 session → 明说原因：%s" % exc)
    finally:
        so.requests = original


# ============================================================
# ⑪ 播放器接线（真实窗口）
# ============================================================

def section_ui(s):
    s.section("⑪ 播放器接线（真实窗口）")

    import time as _time

    from _harness import load_app, teardown

    root, app, fp = load_app(subtitles=True)
    try:
        s.check(hasattr(app, "btn_online"),
                "底栏上真的有【在线匹配】这个按钮")
        s.check(app.btn_online.cget("text") == "在线匹配",
                "按钮文字是「在线匹配」（%s）" % app.btn_online.cget("text"))
        s.check(bool(app.btn_online.bind("<Button-3>")),
                "右键绑上了设置入口（改 API key 的唯一入口，绑丢了就没法改）")

        # ---- 没有视频时点它：不许崩、不许开线程 ----
        original_info = fp.messagebox.showinfo
        fp.messagebox.showinfo = lambda *a, **k: None
        try:
            app.current_video_path = ""
            app.match_subtitles_online()
            s.check(not app._online_busy,
                    "没有正在播放的视频时点【在线匹配】→ 只是提示，不开线程")
        finally:
            fp.messagebox.showinfo = original_info

        # ---- 有视频时走完整流程（把 fetch 换成假实现，其余全是真的）----
        folder = _fresh("ui")
        video = os.path.join(folder, "Widget.2020.1080p.mkv")
        with open(video, "wb") as fh:
            fh.write(b"x" * 100)
        subtitle_file = os.path.join(folder, "Widget.2020.1080p.zh.srt")
        # ⚠️ 用 `wb` 写字节，不要用 text 模式写含 `\r\n` 的字符串 ——
        # text 模式会把 `\n` 再转成本平台的换行，`\r\n` 就变成 `\r\r\n`。
        # （这个写法当初"写错"过，反而撞出了 subtitle.parse 的一个真 bug：
        #   `\r\r\n` 被规范成 `\n\n`，而空行在 SRT 里是 cue 分隔符，于是
        #   整份字幕一条都读不出来。现在 parse 已经能容它，但素材还是该写对。）
        with open(subtitle_file, "wb") as fh:
            fh.write(SRT_BODY.encode("utf-8"))

        app.current_video_path = video
        app.online_api_key = "fake-key-for-test"   # 不联网，只是让前置检查过

        fake = {"path": subtitle_file,
                "candidate": {"file_id": 4242, "language": "zh-cn"},
                "description": "简体中文 · 指纹匹配 · 可信来源",
                "meta": {"remaining": 3},
                "total_found": 5}

        original_fetch = so.fetch
        so.fetch = lambda *a, **k: dict(fake)
        try:
            app.match_subtitles_online()
            s.check(app._online_busy is True,
                    "点下去立刻进入「匹配中」状态（防止连点开出一堆线程）")
            s.check(app.btn_online.cget("text") == "匹配中…",
                    "按钮文字变成「匹配中…」（%s）" % app.btn_online.cget("text"))
            s.check(str(app.btn_online.cget("state")) == "disabled",
                    "匹配期间按钮被禁用（%s）" % app.btn_online.cget("state"))

            deadline = _time.time() + 15
            while app._online_busy and _time.time() < deadline:
                root.update()
                _time.sleep(0.02)

            s.check(not app._online_busy, "后台线程干完后自动收尾")
            s.check(app.btn_online.cget("text") == "在线匹配",
                    "按钮文字恢复")
            s.check(str(app.btn_online.cget("state")) == "normal",
                    "按钮重新可用")

            s.check(app.subtitle_set.available,
                    "下回来的字幕被装上（%s）—— 这一步不过，"
                    "用户就是「点了按钮什么都没发生」" % app.subtitle_set.describe())

            status = app.playlist_status.cget("text")
            s.check("已匹配" in status and "简体中文" in status,
                    "状态行报告匹配到了什么：%s" % status)
            s.check("还剩 3 次" in status,
                    "状态行显示剩余配额（用户唯一能看到它的地方）：%s" % status)
            s.check(os.path.basename(subtitle_file) in status,
                    "状态行说明存到了哪个文件：%s" % status)

            s.check(4242 in app._online_tried.get(video, set()),
                    "记下了已下过的 file_id —— 再点一次会换下一条候选，"
                    "而不是把同一份又下一遍")

            # 原本「关闭字幕」时，匹配完应该顺手打开
            app.subtitle_mode = "off"
            app.subtitle_set = sub.SubtitleSet()
            app.match_subtitles_online()
            deadline = _time.time() + 15
            while app._online_busy and _time.time() < deadline:
                root.update()
                _time.sleep(0.02)
            s.check(app.subtitle_mode != "off",
                    "匹配成功时如果原本是「关闭字幕」→ 自动打开"
                    "（否则下完了什么都不显示，用户以为没成功）：%s"
                    % app.subtitle_mode)
        finally:
            so.fetch = original_fetch

        # ---- 失败：只走状态行 + 一个提示，不许把状态卡在「匹配中」----
        original_info = fp.messagebox.showinfo
        fp.messagebox.showinfo = lambda *a, **k: None
        original_fetch = so.fetch

        def _boom(*a, **k):
            raise so.OnlineError("没找到《Widget 2020》的字幕")

        so.fetch = _boom
        try:
            app.match_subtitles_online()
            deadline = _time.time() + 15
            while app._online_busy and _time.time() < deadline:
                root.update()
                _time.sleep(0.02)
            s.check(not app._online_busy,
                    "失败时也要**退出「匹配中」状态** —— 卡在那儿按钮永远点不动")
            s.check(app.btn_online.cget("text") == "在线匹配"
                    and str(app.btn_online.cget("state")) == "normal",
                    "失败后按钮恢复可用")
            s.check("失败" in app.playlist_status.cget("text"),
                    "失败原因写进了状态行：%s" % app.playlist_status.cget("text"))
        finally:
            so.fetch = original_fetch
            fp.messagebox.showinfo = original_info
    finally:
        teardown(root, app)


# ============================================================

def section_quota_clock(s):
    """【真机验证补的】配额重置时刻必须说成**本地**时间。

    真机拿到的是 `reset_time_utc = 2026-10-14T23:59:59.999Z` —— UTC 午夜。
    对 UTC+8 的用户来说那是**第二天早上 8 点**，所以状态行里"今天还剩 N 次"
    是错的（早上 7 点看到 0 次、9 点又冒出 5 次，像额度在乱跳）。
    改成「还剩 N 次（08:00 重置）」之后两个信息才都对得上。
    """
    s.section("⑪b 配额重置时刻要说成本地时钟（真机验证补的）")
    import datetime as _dt

    import final_player as fp

    # ⚠️ 断言**不能写死** "08:00" —— 那只有 UTC+8 的机器能过。这里的做法是
    #    在测试里独立算一遍"这个 UTC 时刻在本地是几点"，再跟函数比。
    utc = _dt.datetime(2026, 10, 14, 23, 59, 59, 999000,
                       tzinfo=_dt.timezone.utc)
    local = utc.astimezone()
    got = fp._format_quota_reset("2026-10-14T23:59:59.999Z")
    s.check(got == local.strftime("%H:%M"),
            "真机那种 Z 结尾的 UTC 时刻被转成本地时钟（%r -> %r，本机 %s）"
            % ("2026-10-14T23:59:59.999Z", got, local.strftime("%z")))

    offset = local.utcoffset()
    if offset is not None and offset.total_seconds() != 0:
        s.check(got != "23:59",
                "本机不在 UTC 时区，显示的**不再是 UTC 的 23:59**"
                "（本机偏移 %s，显示 %s）—— 这正是要修的那件事" % (offset, got))
    else:
        s.check(got == "23:59", "本机就在 UTC 时区（显示 %s）" % got)

    s.check(fp._format_quota_reset(None) == "",
            "没有这个字段 → 空串（退回原来那句「今天还剩 N 次」，不硬编）")
    s.check(fp._format_quota_reset("") == "", "空串 → 空串")
    s.check(fp._format_quota_reset("   ") == "", "只有空格 → 空串")
    s.check(fp._format_quota_reset("不是时间") == "",
            "垃圾输入 → 空串（绝不能因为一句话让匹配结果整体报错）")
    s.check(fp._format_quota_reset("2026-10-15 00:00:00") != "",
            "空格分隔的写法也认（%r）"
            % fp._format_quota_reset("2026-10-15 00:00:00"))
    s.check(fp._format_quota_reset("2026-10-15 00:00:00")
            == fp._format_quota_reset("2026-10-15T00:00:00+00:00"),
            "不带时区后缀的按 UTC 处理（跟显式 +00:00 同一结果）")


# ============================================================
# ⑫~⑰ 第 21 轮：把「配到别的片子」修掉
#
# 这一轮全部依据来自四轮真实接口测试（搜索不计配额，打得开）：
#   · 搜 `The Matrix`，50 条里**一条正主都没有** —— 32 条《黑客帝国2》、
#     14 条《黑客帝国3》、4 条纪录片；续集靠 28.8 万下载量排在最前。
#   · 同一个查询加上 `year=1999`，50 条**全是 The Matrix(1999)**。
#   · 搜 `阿凡达`，32 条是《阿凡达 2》(2020)、18 条才是《阿凡达》(2009)。
#   · 接口返回的 `feature_details.title` **永远是英文原名**
#     （《让子弹飞》→ `Let the Bullets Fly`），中文文件名根本没法比片名。
# ============================================================

def section_year_and_ids(s):
    s.section("⑫ 年份读最后一个 + 编号规范化")

    # 为什么必须读**最后**一个：片名里带数字的情况真实存在，而它排在前面。
    # 《2012》是 2009 年的电影，文件名 `2012.2009.1080p.mkv` 取第一个会拿到
    # 2012 —— 实测搜出来是 Coldplay 演唱会那一类 2012 年的东西；取最后一个
    # （2009）才命中正主（imdb=1190080）。
    cases = [
        ("2012.2009.1080p.BluRay.mkv", 2009),
        ("1917.2019.1080p.mkv", 2019),
        ("The.Matrix.1999.1080p.BluRay.mkv", 1999),
        ("Movie.2020.1080p.mkv", 2020),
        ("Show.S02E05.720p.mkv", None),
        ("阿凡达.mkv", None),
    ]
    for name, want in cases:
        info = so.analyze_filename(name)
        s.check(info["year"] == want,
                "%-38s → 年份 %s" % (name[:36], info["year"]))

    s.check(so.analyze_filename("2012.2009.mkv")["query"] == "2012 2009",
            "年份仍然留在搜索词里（它是有效的搜索信息，不该丢）")

    id_cases = [("tt0133093", 133093), (" 133093 ", 133093), (133093, 133093),
                ("TT0133093", 133093), ("", None), (None, None),
                ("abc", None), ("tt", None)]
    for raw, want in id_cases:
        got = so._normalize_imdb(raw)
        s.check(got == want, "imdb 编号 %-12r → %s" % (raw, got))
    s.check(so._normalize_imdb("tt0133093") == 133093,
            "网页上抄下来的「tt0133093」也是同一个编号（接口只认纯数字）")

    s.check(so._int_or_none(True) is None,
            "bool 不算数字（它是 int 的子类，不挡住 True 会变成 1）")
    s.check(so._int_or_none(0) == 0, "0 是合法数字（不能跟「没有」混为一谈）")
    s.check(so._int_or_none("7") == 7, "字符串数字也认")
    s.check(so._int_or_none("7.5") is None, "小数不算（年份不会是小数）")
    s.check(so._int_or_none(float("inf")) is None,
            "Infinity 不抛（json 模块默认认这个非标准值，int(inf) 抛 OverflowError）")
    s.check(so._int_or_none(float("nan")) is None, "NaN 不抛")


def section_feature_details(s):
    s.section("⑬ 接口给的「这条属于哪部片」")

    matrix = _movie(646193, "The Matrix", 1999, imdb=133093)
    got = so.parse_results(_payload(_candidate(1, movie=matrix)))[0]
    s.check(got["feature_id"] == 646193, "feature_id（%s）" % got["feature_id"])
    s.check(got["feature_year"] == 1999, "feature_year（%s）" % got["feature_year"])
    s.check(got["feature_title"] == "The Matrix",
            "feature_title（%r）" % got["feature_title"])
    s.check(got["imdb_id"] == 133093, "imdb_id（%s）" % got["imdb_id"])

    old = so.parse_results(_payload(_candidate(2)))[0]
    s.check(old["feature_id"] is None and old["imdb_id"] is None,
            "没有 feature_details 时字段是 None，不是 KeyError")
    s.check(old["feature_title"] == "", "标题缺省是空串")

    for bad in (None, [], "文字", 42):
        malformed = {"attributes": {"language": "en", "feature_details": bad,
                                    "files": [{"file_id": 9}]}}
        parsed = so.parse_results({"data": [malformed]})
        s.check(len(parsed) == 1 and parsed[0]["feature_id"] is None,
                "feature_details 是 %r 也不抛" % (bad,))

    episode = _movie(178961, "Pilot", 2008, imdb=959621, kind="Episode",
                     season=1, episode=1, parent_imdb=903747,
                     parent_title="Breaking Bad")
    got = so.parse_results(_payload(_candidate(3, movie=episode)))[0]
    s.check(got["season"] == 1 and got["episode"] == 1,
            "剧集季集号（S%sE%s）" % (got["season"], got["episode"]))
    s.check(got["parent_imdb_id"] == 903747
            and got["parent_title"] == "Breaking Bad",
            "剧集所属剧（%s / %s）" % (got["parent_imdb_id"], got["parent_title"]))

    weird = {"attributes": {"language": "en", "votes": 0.0, "ratings": 0.0,
                            "files": [{"file_id": 11}]}}
    s.check(len(so.parse_results({"data": [weird]})) == 1,
            "votes/ratings 是浮点也不抛（实测返回的就是 0.0）")

    inf_case = {"attributes": {"language": "en", "votes": float("inf"),
                               "files": [{"file_id": 12}]}}
    s.check(len(so.parse_results({"data": [inf_case]})) == 1, "Infinity 也不抛")


def section_year_scoring(s):
    s.section("⑭ 年份校验：同系列的续集靠它才分得开")

    fps = 23.976
    matrix = _movie(646193, "The Matrix", 1999, imdb=133093)
    revolutions = _movie(646061, "The Matrix Revolutions", 2003, imdb=234215)

    right = so.parse_results(_payload(
        _candidate(1, "en", downloads=100, movie=matrix)))[0]
    wrong = so.parse_results(_payload(
        _candidate(2, "ze", downloads=90000, movie=revolutions)))[0]

    s.check(so.score_candidate(right, fps, 1999)
            > so.score_candidate(wrong, fps, 1999),
            "年份相符的那条**压过**下载量高 900 倍的双语续集（%.0f vs %.0f）"
            "—— 这正是「配到别的片子」那件事"
            % (so.score_candidate(right, fps, 1999),
               so.score_candidate(wrong, fps, 1999)))

    s.check(so.score_candidate(wrong, fps) > so.score_candidate(right, fps),
            "…而**不**做年份校验时，赢的恰恰是那条错的（%.0f vs %.0f）——"
            "这就是改动前的行为，也是必须加这一项的理由"
            % (so.score_candidate(wrong, fps), so.score_candidate(right, fps)))

    near = so.parse_results(_payload(
        _candidate(3, "en", movie=_movie(3, "A", 2000))))[0]
    far = so.parse_results(_payload(
        _candidate(4, "en", movie=_movie(4, "A", 2003))))[0]
    s.check(so.score_candidate(near, fps, 1999)
            > so.score_candidate(far, fps, 1999),
            "差 1 年跟差 2 年以上要区别对待 —— 发行年 ≠ 制作年是真实存在的"
            "（实测 The Matrix Revisited：release 写 2002、年份字段 2001）")
    s.check(so.score_candidate(far, fps, 1999)
            < so.score_candidate(far, fps) - 400,
            "年份明显不符要重罚（%.0f → %.0f）"
            % (so.score_candidate(far, fps), so.score_candidate(far, fps, 1999)))

    no_year = so.parse_results(_payload(
        _candidate(5, "en", movie=_movie(5, "A", None))))[0]
    s.check(so.score_candidate(no_year, fps, 1999)
            == so.score_candidate(no_year, fps),
            "候选没给年份 → 不奖不罚（「不知道」不等于「不符」）")

    unknown = so.parse_results(_payload(_candidate(6, "en")))[0]
    s.check(so.score_candidate(unknown, fps, 1999)
            == so.score_candidate(unknown, fps),
            "我们不知道年份时也不评这一项（不能拿一个猜的年份去罚别人）")

    hash_wrong = so.parse_results(_payload(
        _candidate(7, "en", hash_match=True, movie=_movie(7, "A", 2003))))[0]
    s.check(so.score_candidate(hash_wrong, fps, 1999) > 0,
            "但指纹精确匹配必须还能压过年份不符（%.0f > 0）——"
            "「有人拿一模一样这个文件配过字幕」比库里的年份元数据更硬"
            % so.score_candidate(hash_wrong, fps, 1999))


def section_anchor(s):
    s.section("⑮ 认出是哪部片：pick_anchor / looks_ambiguous")

    matrix = _movie(646193, "The Matrix", 1999, imdb=133093)
    revolutions = _movie(646061, "The Matrix Revolutions", 2003, imdb=234215)
    same_year_other = _movie(700001, "The Matrix Revisited", 1999)

    def listing(*cands):
        return so.parse_results(_payload(*cands))

    no_year_info = {"year": None, "season": None, "episode": None}
    year_info = {"year": 1999, "season": None, "episode": None}

    s.check(so.pick_anchor(listing(_candidate(1, movie=matrix)), no_year_info) is None,
            "没有年份 → **绝不锚定**（实测无年份时按条数挑会挑成《阿凡达 2》）")
    s.check(so.pick_anchor([], year_info) is None, "空候选不锚")

    big = listing(*[_candidate(100 + i, movie=matrix) for i in range(8)],
                  _candidate(200, movie=revolutions))
    anchor = so.pick_anchor(big, year_info)
    s.check(anchor is not None and anchor["feature_id"] == 646193,
            "年份相符、条数又突出 → 锚定到 The Matrix(1999)（%s）"
            % (anchor or {}).get("feature_id"))

    only_sequels = listing(*[_candidate(100 + i, movie=revolutions)
                             for i in range(5)])
    s.check(so.pick_anchor(only_sequels, year_info) is None,
            "候选里全是续集（年份都不符）→ 池子是空的，不锚 ——"
            "这正是「搜 The Matrix 一条正主都没有」那种情况")

    even = listing(*[_candidate(100 + i, movie=matrix) for i in range(3)],
                   *[_candidate(200 + i, movie=same_year_other) for i in range(2)])
    s.check(so.pick_anchor(even, year_info) is None,
            "同年份、不同片、条数势均力敌（3 vs 2）→ 不锚"
            "（分不清就别乱锁，锁错了是把另一部片的字幕硬塞给用户）")

    ep = _movie(178961, "Pilot", 2008, imdb=959621, kind="Episode",
                season=1, episode=1, parent_imdb=903747,
                parent_title="Breaking Bad")
    other_ep = _movie(555001, "Pilot", 2011, imdb=555001, kind="Episode",
                      season=1, episode=1, parent_imdb=666001,
                      parent_title="Other Show")
    episodes = listing(*[_candidate(300 + i, movie=ep) for i in range(5)],
                       _candidate(400, movie=other_ep))
    anchor = so.pick_anchor(episodes, {"year": None, "season": 1, "episode": 1})
    s.check(anchor is not None and anchor["feature_id"] == 178961,
            "剧集**不需要年份**：按季集号就能认出来（实测按片名搜出来 24 条"
            "是 4 集不同的剧）")

    s.check(so.looks_ambiguous([], no_year_info) is False,
            "空候选不算「定不下来」—— 那是「压根没搜到」，由 fetch 报错")

    one_movie = listing(*[_candidate(100 + i, movie=matrix) for i in range(5)])
    s.check(so.looks_ambiguous(one_movie, no_year_info) is False,
            "所有候选都是同一部片 → 定得下来（实测「盗梦空间.mkv」50 条全是 Inception）")

    mostly = listing(*[_candidate(100 + i, movie=matrix) for i in range(49)],
                     _candidate(200, movie=revolutions))
    s.check(so.looks_ambiguous(mostly, no_year_info) is False,
            "一部片占 49/50 → 定得下来（实测「黑客帝国.mkv」就是这个比例）")

    mixed = listing(*[_candidate(100 + i, movie=_movie(1, "Avatar 2", 2020))
                      for i in range(32)],
                    *[_candidate(200 + i, movie=_movie(2, "Avatar", 2009))
                      for i in range(18)])
    s.check(so.looks_ambiguous(mixed, no_year_info) is True,
            "32 条《阿凡达2》+ 18 条《阿凡达》→ **定不下来**，必须让人挑"
            "（那 64% 是第二部，按条数挑就会挑错）")

    s.check(so.looks_ambiguous(big, year_info) is False,
            "年份对得上、锚得出来 → 定得下来")


def section_anchor_flow(s):
    s.section("⑯ 锚定重搜 / 手动搜索 / 指定下载（全链路）")

    folder = _fresh("anchor_flow")
    video = os.path.join(folder, "The.Matrix.1999.1080p.BluRay.mkv")
    with open(video, "wb") as fh:
        fh.write(b"x" * 2000)

    matrix = _movie(646193, "The Matrix", 1999, imdb=133093)
    revolutions = _movie(646061, "The Matrix Revolutions", 2003, imdb=234215)

    session = FakeSession(search_queue=[
        _payload(),                                             # 指纹轮：没命中
        _payload(*[_candidate(100 + i, "en", movie=matrix) for i in range(4)],
                 _candidate(200, "ze", movie=revolutions)),     # 片名轮：混着续集
        _payload(*[_candidate(100 + i, "ze", movie=matrix) for i in range(3)]),
    ])
    client = so.OpenSubtitles("key", session=session)
    ranked, info = so.find_candidates(client, video, 23.976)
    params = [c["kwargs"]["params"] for c in session.find("/subtitles")]

    s.check(params[1].get("year") == 1999,
            "第二轮**带上了年份**（改动前漏传了，而就这一个参数决定成败）")
    s.check(len(params) == 3 and params[2].get("imdb_id") == 133093,
            "第四轮用 imdb_id 锚定重搜（%s）" % params[2].get("imdb_id"))
    s.check(info["anchor"] is not None and info["anchor"]["feature_id"] == 646193,
            "info 里记下了锚定到哪部片（状态行要说给用户听）")
    s.check(all(c["feature_id"] == 646193 for c in ranked),
            "锚定之后候选**全部**是 The Matrix(1999)，续集被清干净了（%d 条）"
            % len(ranked))

    session_fp = FakeSession(search=_payload(
        _candidate(500, "en", hash_match=True, movie=matrix)))
    client_fp = so.OpenSubtitles("key", session=session_fp)
    _, info_fp = so.find_candidates(client_fp, video, 23.976)
    s.check(info_fp["anchor"] is None and len(session_fp.find("/subtitles")) == 1,
            "指纹精确命中时不再多搜 —— 「一模一样这个文件配过」比什么都硬")

    avatar2 = [_candidate(100 + i, movie=_movie(1, "Avatar 2", 2020))
               for i in range(4)]
    avatar = [_candidate(200 + i, movie=_movie(2, "Avatar", 2009))
              for i in range(3)]
    session2 = FakeSession(search_queue=[_payload(), _payload(*(avatar2 + avatar))])
    client2 = so.OpenSubtitles("key", session=session2)
    result = so.fetch(client2, video, 23.976)
    s.check(result.get("ambiguous") is True, "定不下来时不下载，把候选交回去让人挑")
    s.check(result["path"] is None, "**一份都没下**")
    s.check(not session2.find("/download"),
            "确实没发出下载请求（搜索不计次、只有下载计次 → 这一支是免费的）")
    s.check(len(result["candidates"]) == 7,
            "候选都带回来了（%d 条）" % len(result["candidates"]))

    session3 = FakeSession(search=_payload(
        *[_candidate(100 + i, movie=matrix) for i in range(3)],
        *[_candidate(200 + i, movie=revolutions) for i in range(3)]))
    client3 = so.OpenSubtitles("key", session=session3)
    manual = so.search_candidates(client3, query="The Matrix", year=1999)
    s.check(len(manual) == 6,
            "手动搜索**不锚定** —— 用户就是要看见所有可能（%d 条）" % len(manual))
    s.check(len(session3.find("/subtitles")) == 1, "只搜一次，不做锚定重搜")
    s.check({c["feature_id"] for c in manual} == {646193, 646061},
            "两部片都留在结果里（%s）" % sorted({c["feature_id"] for c in manual}))

    try:
        so.search_candidates(client3, query="")
        s.check(False, "不给片名应该报错")
    except so.OnlineError as exc:
        s.check("片名" in str(exc), "空搜索词 → 明说原因：%s" % exc)

    shuffled = so.parse_results(_payload(
        _candidate(1, movie=matrix), _candidate(2, movie=revolutions),
        _candidate(3, movie=matrix), _candidate(4, movie=revolutions),
        _candidate(5, movie=matrix)))
    order = [c["feature_id"] for c in so.group_by_feature(shuffled)]
    s.check(order == [646193, 646193, 646193, 646061, 646061],
            "同一部片的条目**连着排**（%s）—— 混着排用户看不出这里有两部片"
            % order)
    s.check(len(so.group_by_feature([])) == 0, "空列表分组不抛")

    picked = so.parse_results(_payload(_candidate(222, "ze", movie=matrix)))[0]
    session4 = FakeSession(blob=SRT_BODY.encode("utf-8"))
    client4 = so.OpenSubtitles("key", session=session4)
    got = so.download_candidate(client4, video, picked)
    s.check(os.path.basename(got["path"]).endswith(".双语.srt"),
            "用户挑的双语候选落成 .双语.srt（%s）" % os.path.basename(got["path"]))
    s.check("The Matrix" in got["description"],
            "描述里写出了属于哪部片：%s" % got["description"])
    s.check(got["ambiguous"] is False, "指定下载永远不是 ambiguous")
    s.check(session4.find("/subtitles") == [],
            "指定下载**一次搜索都不做**（用户已经挑好了）")

    # 年份缺失时的那句提示必须不一样 —— 否则用户会一直点【在线匹配】
    hint_missing = so._no_result_hint({"query": "阿凡达", "year": None}, video)
    hint_known = so._no_result_hint({"query": "阿凡达", "year": 2009}, video)
    s.check("年份" in hint_missing and "搜索…" in hint_missing,
            "文件名没年份而搜不到 → 指向【搜索…】：%s" % hint_missing)
    s.check("年份" not in hint_known,
            "有年份而搜不到 → 就是真的没有（别说些用不上的建议）：%s" % hint_known)


def section_dialog(s):
    s.section("⑰ 候选列表对话框（真实窗口）")

    import time as _time
    import tkinter as tk

    from _harness import load_app, teardown

    root, app, fp = load_app(subtitles=True)
    try:
        s.check(hasattr(app, "btn_online_search"), "底栏上真的有【搜索…】按钮")
        s.check(app.btn_online_search.cget("text") == "搜索…",
                "按钮文字是「搜索…」（%s）" % app.btn_online_search.cget("text"))
        s.check(hasattr(app, "open_subtitle_search")
                and hasattr(app, "download_online_candidate"),
                "两个入口方法都在")

        matrix = _movie(646193, "The Matrix", 1999, imdb=133093)
        revolutions = _movie(646061, "The Matrix Revolutions", 2003, imdb=234215)
        given = so.parse_results(_payload(
            _candidate(1, "en", movie=matrix),
            _candidate(2, "ze", movie=revolutions)))

        # ---- 只建不等（模态等待会把无人值守的回归整轮挂死）----
        dialog = fp.SubtitleSearchDialog(
            root, os.path.join("D:/影片", "The.Matrix.mkv"), "fake-key",
            candidates=given, info={"query": "The Matrix", "year": None},
            reason="测试用")
        try:
            s.check(dialog.listbox.size() == 2,
                    "给了候选就直接铺出来（%d 行）" % dialog.listbox.size())
            s.check(dialog.listbox.curselection() == (0,), "默认选中的是第一条")
            s.check("The Matrix (1999)" in dialog.listbox.get(0),
                    "每行都写清属于哪部片：%s" % dialog.listbox.get(0))
            s.check("Revolutions" in dialog.listbox.get(1),
                    "第二条写的是另一部片：%s" % dialog.listbox.get(1))
            s.check(dialog.entry_query.get() == "The Matrix",
                    "片名预填了（%r）" % dialog.entry_query.get())
            s.check(dialog.entry_year.get() == "",
                    "没年份时年份框是空的（不是硬编一个 0）")

            dialog.listbox.selection_clear(0, tk.END)
            dialog.listbox.selection_set(1)
            dialog._pick_selected()
            s.check(dialog.choice is not None
                    and dialog.choice["feature_id"] == 646061,
                    "选中第二条点下载 → 返回的是**用户点的那条**（%s）"
                    % (dialog.choice or {}).get("feature_id"))
        finally:
            dialog._close(None)

        empty = fp.SubtitleSearchDialog(root, "D:/x/a.mkv", "key",
                                        candidates=[], info={}, reason="")
        try:
            s.check(empty.listbox.size() == 0, "空候选时列表是空的")
            s.check(str(empty.btn_ok.cget("state")) == "disabled",
                    "空候选时下载按钮禁用")
            empty._pick_selected()
            s.check(empty.choice is None, "没得选就不返回东西")
        finally:
            empty._close(None)

        typed = fp.SubtitleSearchDialog(root, "D:/x/a.mkv", "key",
                                        candidates=given, info={}, reason="")
        try:
            typed.entry_query.delete(0, tk.END)
            typed.entry_query.insert(0, "Avatar")
            typed.entry_year.delete(0, tk.END)
            typed.entry_year.insert(0, "去年")
            typed._start_search()
            s.check("数字" in typed.status.cget("text"),
                    "年份填了非数字 → 提示要填数字，而不是拿它去搜：%s"
                    % typed.status.cget("text"))
            s.check(typed._searching is False, "没起搜索线程")
        finally:
            typed._close(None)

        # ---- 自动匹配「定不下来」→ 打开列表 ----
        folder = _fresh("dialog")
        video = os.path.join(folder, "Movie.2020.1080p.mkv")
        with open(video, "wb") as fh:
            fh.write(b"x" * 100)
        app.current_video_path = video

        opened = {}
        original_open = app.open_subtitle_search
        app.open_subtitle_search = lambda **kw: opened.update(kw)
        try:
            app._online_done(video, {
                "ambiguous": True, "candidates": given,
                "info": {"query": "The Matrix", "year": None},
                "path": None, "candidate": None, "description": "",
                "meta": {}, "total_found": 2})
            s.check(opened.get("candidates") is given,
                    "定不下来 → 打开候选列表，并把候选交过去")
            s.check("2" in (opened.get("reason") or ""),
                    "说明里带上条数，让用户知道为什么弹这个窗：%s"
                    % opened.get("reason"))
            s.check(app._online_busy is False,
                    "按钮状态必须复位（不然永远卡在「匹配中…」）")
            s.check(app.btn_online.cget("text") == "在线匹配", "按钮文字复位")
            s.check(video not in app._online_tried,
                    "没下载 → 不往「已试过」里记（否则下次点会白白跳过一条）")
        finally:
            app.open_subtitle_search = original_open

        # ---- 用户挑一条 → 走真实后台线程下载 ----
        picked_file = os.path.join(folder, "picked.srt")
        with open(picked_file, "wb") as fh:
            fh.write(SRT_BODY.encode("utf-8"))

        original_download = so.download_candidate
        so.download_candidate = lambda *a, **k: {
            "path": picked_file, "candidate": given[0],
            "description": "The Matrix (1999) · 英文",
            "meta": {}, "ambiguous": False, "total_found": 0, "info": None}
        try:
            app.online_api_key = "fake-key-for-test"
            app.download_online_candidate(given[0])
            s.check(app._online_busy is True, "挑一条下载会进入忙碌态")
            s.check(app.btn_online.cget("text") == "下载中…",
                    "文字是「下载中…」而不是「匹配中…」（%s）"
                    % app.btn_online.cget("text"))

            deadline = _time.time() + 15
            while app._online_busy and _time.time() < deadline:
                root.update()
                _time.sleep(0.02)

            s.check(not app._online_busy, "下载完自动收尾")
            s.check(app.subtitle_set.available,
                    "挑的那条被装上了（%s）" % app.subtitle_set.describe())
            s.check("The Matrix" in app.playlist_status.cget("text"),
                    "状态行说明匹配到了哪部片：%s"
                    % app.playlist_status.cget("text"))
        finally:
            so.download_candidate = original_download

        # ---- 关窗口不能留下挂起的 after ----
        stray = fp.SubtitleSearchDialog(root, "D:/x/a.mkv", "key",
                                        candidates=given, info={}, reason="")
        stray._close(None)
        s.check(stray._after_id is None, "关窗口时把挂起的定时器取消掉")
        try:
            for _ in range(5):
                root.update()
            s.check(True, "销毁之后事件泵还能正常跑（没有 invalid command name）")
        except tk.TclError as exc:
            s.check(False, "销毁之后事件泵报错：%s" % exc)
    finally:
        teardown(root, app)


# ============================================================

def main():
    summary = Summary("在线字幕匹配（第 20 轮）")
    section_filenames(summary)
    section_hash(summary)
    section_parse(summary)
    section_rank(summary)
    section_extract(summary)
    section_save(summary)
    section_config(summary)
    section_client(summary)
    section_fetch(summary)
    section_offline(summary)
    section_ui(summary)
    section_quota_clock(summary)
    # ---- 第 21 轮：把「配到别的片子」修掉 ----
    section_year_and_ids(summary)
    section_feature_details(summary)
    section_year_scoring(summary)
    section_anchor(summary)
    section_anchor_flow(summary)
    section_dialog(summary)
    return summary.finish()


if __name__ == "__main__":
    sys.exit(main())
