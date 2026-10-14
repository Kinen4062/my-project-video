# -*- coding: utf-8 -*-
""""上次看到哪儿了"的落盘 —— 一个很小的 JSON 文件（第 17 轮·记忆播放位置）。

## 它记什么

用户没看完就离开（关掉播放器、或者切去别的视频）的那些片子，各自停在第几
秒。下次再打开同一个文件时，播放器据此问一句"接着看，还是从头"，或者在
片尾自动连播那条路径上直接静默接着播。

## 放在哪儿 / 写不进去怎么办

跟播放列表同一套做法（见 `playlist_store`）：放用户配置目录，读写失败一律
静默降级。记录"看到哪儿了"是锦上添花，绝不能因为它让播放器本身出问题 ——
配置目录只读、磁盘满、文件被别的进程锁住，全部按"没有这条记录"处理。

## 为什么除了秒数还要存 (文件大小, 修改时间)

记录是**按路径**查的，而路径会指向不同的东西：用户拿别的片子覆盖了同名
文件、重新转换把一个文件重新生成了一遍，都会让上次记下的秒数变得毫无意义
（极端情况很荒谬：拿 2 小时长片的位置去播一个 30 秒的短片）。所以顺手存下
`(大小, 修改时间)` 当指纹，查询时对不上就当作**没有这条记录**。

## 为什么要"临时文件 + os.replace"

与 `playlist_store` 逐字同因：直接覆盖写时断电/崩溃会留下半个 JSON，
下次读不回来，**所有**记录一起丢。先写同目录临时文件再原子替换，
要么是旧内容、要么是新内容。
"""

import json
import os
import tempfile

FORMAT_VERSION = 1
# 最多保留多少条记录。按"记录时刻"淘汰最旧的 —— 这个文件是给"接着上次看"
# 用的，十年前那部片子记不记得住意义不大，但它也不该无人看管地一直长大。
MAX_ENTRIES = 200


def default_path():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "final_player", "positions.json")


def make_key(path):
    """把一个视频路径规范成查表用的键。

    `normcase` 让 Windows 上大小写不同的同一个路径落到同一条记录，
    `abspath` 让相对路径与绝对路径也归一到一起。播放列表那边用的是同一个
    手法（`playlist_store` 内部也是 normcase 去重）。
    """
    return os.path.normcase(os.path.abspath(path))


def _is_number(value):
    """`bool` 是 `int` 的子类，得单独挡掉 —— `True` 当秒数用毫无意义。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _clean_entry(raw):
    """把一条记录整理成内部形态；不合法返回 None。

    "不合法就丢"而不是"猜一个默认值"：坏数据（手改坏、别版本写的）唯一安全
    的处理方式是当作没有，绝不能凭空造一个位置出来把用户送到片子中间。
    """
    if not isinstance(raw, dict):
        return None
    path = raw.get("path")
    sec, dur = raw.get("sec"), raw.get("dur")
    if not isinstance(path, str) or not path.strip():
        return None
    if not _is_number(sec) or sec < 0:
        return None
    if not _is_number(dur) or dur <= 0:
        return None
    entry = {"path": path, "sec": float(sec), "dur": float(dur)}
    for name in ("size", "mtime", "at"):
        if _is_number(raw.get(name)):
            entry[name] = float(raw[name])
    return entry


def load(path=None):
    """读回全部记录 `{键: 记录}`；文件不存在或任何异常都返回 `{}`。"""
    path = path or default_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}

    items = data.get("positions") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return {}

    out = {}
    for raw in items:
        entry = _clean_entry(raw)
        if entry is not None:
            out[make_key(entry["path"])] = entry
    return out


def save(positions, path=None):
    """原子写入；成功返回 True，失败返回 False（**不抛**）。"""
    path = path or default_path()
    items = [entry for entry in (positions or {}).values()
             if isinstance(entry, dict) and entry.get("path")]
    if len(items) > MAX_ENTRIES:
        items.sort(key=lambda e: e.get("at") or 0.0)
        items = items[-MAX_ENTRIES:]
    payload = {"version": FORMAT_VERSION, "positions": items}
    directory = os.path.dirname(path) or "."
    tmp = None
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".positions-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass
        return False
