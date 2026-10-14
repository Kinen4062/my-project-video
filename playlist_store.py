# -*- coding: utf-8 -*-
"""播放列表的落盘 —— 一个很小的 JSON 文件。

## 放在哪儿
用户配置目录（Windows 用 `%APPDATA%\\final_player\\playlist.json`，其它平台用
`~/.config/final_player/playlist.json`）。不放程序目录是因为程序目录可能是只读的
安装位置；也不放视频旁边，因为列表里的视频本来就在不同盘、不同目录里，没有
"共同的所在"。播放列表属于"这台机器上这个用户的偏好"，放用户配置目录才对。

## 原则：读写失败一律静默降级
列表存不下、文件坏了、目录建不出来 —— 全都不能影响播放器本身（返回 False /
空列表就完事）。播放列表是锦上添花的功能，没有它播放器照样得能跑。

## 为什么要"临时文件 + os.replace"
直接 `open(path, "w")` 写一半时断电/崩溃，会留下一个**语法不完整的 JSON**，
下次启动读不回来，用户的列表就整个没了。先写同目录的临时文件再原子替换，
要么是旧内容、要么是新内容，不存在"半个文件"。
"""

import json
import os
import tempfile

FORMAT_VERSION = 1


def default_path():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "final_player", "playlist.json")


def load(path=None):
    """读回播放列表；任何异常都返回 `[]`（文件不存在是正常情况）。"""
    path = path or default_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []

    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []

    # 整理成程序内部的一致形态：只留非空字符串、按 normcase 去重、保持顺序
    out, seen = [], set()
    for p in items:
        if not isinstance(p, str) or not p.strip():
            continue
        key = os.path.normcase(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def save(items, path=None):
    """原子写入；成功返回 True，失败返回 False（**不抛**）。"""
    path = path or default_path()
    payload = {"version": FORMAT_VERSION, "items": [p for p in items if p]}
    directory = os.path.dirname(path) or "."
    tmp = None
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".playlist-", suffix=".tmp")
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
