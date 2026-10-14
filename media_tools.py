# -*- coding: utf-8 -*-
"""跟外部媒体工具（ffmpeg）打交道的纯函数。

【第 12 轮·重构】从 `final_player.py` 里搬出来的第一块。这三个函数要么只吃参数、
要么只碰传进来的路径，**完全不读写 VideoPlayerApp 的状态** —— 所以搬出来不会
带来任何隐藏耦合。

⚠️ 刻意**没有**把 `find_ffmpeg()` 搬过来。它带着一对全局缓存
（`_FFMPEG_PATH` / `_FFMPEG_SEARCHED`），而测试正是靠改写那对变量来伪造
"这台机器上没有 ffmpeg"的场景（`tests/test_audio_mux.py` 第 ④ 节）。
缓存留在 `final_player` 里，那个测试接缝就原样保留；搬走的话测试要么失效，
要么得跟着改 —— 前者会漏掉回归，后者会削弱防线。

因此本模块的 mux **由调用方传入 ffmpeg 路径**，自己不做查找。这也顺带
去掉了一个隐性依赖（原来 `_mux_audio_into_video` 内部偷偷调 `find_ffmpeg()`，
"补音轨能不能跑"取决于一个它签名上看不见的全局状态）。
"""

import os
import subprocess


FFMPEG_MUX_TIMEOUT_SEC = 300      # 单次 mux 的超时；超时即放弃音轨（大文件也够）
FFMPEG_AUDIO_BITRATE = "160k"     # 音轨重新编码成 aac 的码率（原轨可能是任意格式）


def has_audio_stream(ffmpeg, path):
    """判断媒体文件里有没有音轨。

    用 `ffmpeg -i` 的 stderr 里有没有 "Audio:" 字样来判断，**不依赖 ffprobe**
    （ffpyplayer 那份 wheel 里只有 ffmpeg.exe，没有 ffprobe.exe）。
    注意：`ffmpeg -i xxx` 不带输出参数时退出码就是 1，属正常，不能拿退出码判断。
    """
    if not ffmpeg:
        return False
    try:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-i", path],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              timeout=60)
    except Exception:
        return False
    return b"Audio:" in (proc.stderr or b"")


def remove_quietly(path):
    """删文件，失败不抛异常（临时文件清理到处都要用）"""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def mux_audio_into_video(video_path, audio_source_path, ffmpeg,
                         timeout=FFMPEG_MUX_TIMEOUT_SEC,
                         bitrate=FFMPEG_AUDIO_BITRATE):
    """把 `audio_source_path` 的音轨并入 `video_path`（**原地替换**）。

    【第 11 轮·新】`cv2.VideoWriter` 只写视频流，转换产物天生没有音轨
    （症状："视频转成 3D 之后就没声音了"）。这里在转换收尾补一次音轨。

    返回 True = 音轨已并进去；返回 False = 没补（产物仍是可播放的无声文件）。
    下面三种情况都走 False，且**一律不抛异常** —— 补音轨是锦上添花，
    绝不能让它把整个转换搞失败：
      ① 没拿到 ffmpeg 路径（调用方 `find_ffmpeg()` 返回 None）；
      ② 源文件自己就没有音轨（没什么可补的）；
      ③ ffmpeg 执行失败或超时。

    实现要点：
      - 视频流 `-c:v copy` 不重编码，只多花一两秒；
      - 音轨统一重编码成 aac（原轨可能是任意格式/采样率，copy 进 mp4 未必合法）；
      - `-shortest` 让音轨跟着视频长度收尾（音频比视频长时不会拖出静音尾巴）；
      - 先写同目录临时文件再 `os.replace` 原子替换 —— 中途失败也不会毁掉
        已经写好的无声产物。
    """
    if not ffmpeg:
        print("未找到 ffmpeg，转换产物将不带音轨（视频本身不受影响）")
        return False
    if not has_audio_stream(ffmpeg, audio_source_path):
        print("源视频没有音轨，无需补音轨")
        return False

    tmp_path = video_path + ".muxing.mp4"
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", video_path, "-i", audio_source_path,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", bitrate,
        "-shortest",
        tmp_path,
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              timeout=timeout)
    except Exception as e:
        print(f"补音轨失败（ffmpeg 调用异常）: {e}")
        remove_quietly(tmp_path)
        return False

    if (proc.returncode != 0 or not os.path.exists(tmp_path)
            or os.path.getsize(tmp_path) == 0):
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        print(f"补音轨失败（ffmpeg 退出码 {proc.returncode}）: {detail[-400:]}")
        remove_quietly(tmp_path)
        return False

    try:
        os.replace(tmp_path, video_path)
    except Exception as e:
        print(f"补音轨失败（替换产物出错）: {e}")
        remove_quietly(tmp_path)
        return False
    return True
