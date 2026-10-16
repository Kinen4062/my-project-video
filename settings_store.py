# -*- coding: utf-8 -*-
"""用户偏好的落盘 —— 一个很小的 JSON 文件（第 25 轮·半宽 SBS）。

## 和 playlist_store / resume_store 有什么不同

那两个各自管一份**列表型**数据（播放列表、播放位置）：键是路径、值是记录，
schema 很窄。而这里要存的是**标量偏好**（"半宽 SBS 开不开"），跟它们不是一个
形状 —— 硬塞进去要么污染它们的结构，要么把 JSON 改成两用（读的时候还得猜
是哪一种），两边都不划算。

## 只认白名单键

`load()` 只回 `DEFAULTS` 里声明过的键，而且每个键都有默认值。于是：
文件不存在 / 是坏的 JSON / 是别的格式 / 手改出多余键 —— 全都退化成"用默认值"，
既不会让播放器起不来，也不会让脏数据渗进程序状态。**加新设置时先往 DEFAULTS
里登记**，否则它永远不会被读出来。

## 写失败一律静默降级

跟那两个模块同一套做法（临时文件 + `os.replace` 原子替换，理由见
`playlist_store` 的模块注释）：存不下、目录建不出来，都只是"这次没记住"，
绝不影响播放器本身。
"""

import json
import os
import tempfile

FORMAT_VERSION = 1

# 所有已知设置与它们的默认值。**加新设置先在这里登记** —— load() 只认这里的键。
DEFAULTS = {
    # 半宽 SBS（half-SBS）：转换时把每一半横向压一半再并排（产物整幅宽度 = 源宽度，
    # 于是天然是 16:9）；播放时按"每半要横向拉回 2 倍"来合成 3D。
    # 默认开 —— 产物全屏时正好左右各占一半、铺满屏幕。
    "half_sbs": True,
}


def default_path():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = (os.environ.get("XDG_CONFIG_HOME")
                or os.path.join(os.path.expanduser("~"), ".config"))
    return os.path.join(base, "final_player", "settings.json")


def load(path=None):
    """读回设置。返回的字典**一定包含 `DEFAULTS` 里的每一个键**。"""
    out = dict(DEFAULTS)
    path = path or default_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return out          # 没有 / 坏了，都是正常情况：全用默认值
    if not isinstance(data, dict):
        return out

    for key, default in DEFAULTS.items():
        value = data.get(key)
        # 类型不对就丢掉（手改坏的 JSON 不该渗进来）。
        # ⚠️ 用 type() 不用 isinstance()：bool 是 int 的子类，isinstance 会把
        #    1/0 当成合法的布尔值收下，于是 "half_sbs": 1 这种手改值能进来。
        if type(value) is type(default):
            out[key] = value
    return out


def save(values, path=None):
    """原子写入；成功返回 True，失败返回 False（**不抛**）。"""
    path = path or default_path()
    payload = {"version": FORMAT_VERSION}
    for key in DEFAULTS:            # 只写已知键，多余的一律不落盘
        if key in values:
            payload[key] = values[key]

    directory = os.path.dirname(path) or "."
    tmp = None
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".settings-", suffix=".tmp")
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
