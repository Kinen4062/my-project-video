# -*- coding: utf-8 -*-
"""对媒体文件做"只看文件、不动状态"的纯函数。

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

import cv2
import numpy as np


FFMPEG_MUX_TIMEOUT_SEC = 300      # 单次 mux 的超时；超时即放弃音轨（大文件也够）
FFMPEG_AUDIO_BITRATE = "160k"     # 音轨重新编码成 aac 的码率（原轨可能是任意格式）


def probe_audio_stream(ffmpeg, path):
    """探测媒体文件里有没有音轨。**三态**返回：

        True  —— 确认有音轨
        False —— 确认没有音轨
        None  —— 探测不了（没给 ffmpeg 路径 / 子进程起不来 / 超时），下不了结论

    用 `ffmpeg -i` 的 stderr 里有没有 "Audio:" 字样来判断，**不依赖 ffprobe**
    （ffpyplayer 那份 wheel 里只有 ffmpeg.exe，没有 ffprobe.exe）。
    注意：`ffmpeg -i xxx` 不带输出参数时退出码就是 1，属正常，不能拿退出码判断。

    【第 16 轮】为什么要分三态：两个调用方对"不确定"的**默认值正好相反**。
      · 补音轨（mux）问的是"要不要补" —— 不确定时按**不需要**处理（少做一件事
        不会有副作用），所以 `has_audio_stream()` 保持原来的二态语义；
      · 播放器问的是"要不要建音频播放器" —— 不确定时必须按**需要**处理
        （多建一个没用的播放器只是浪费，而漏建会让本来有声音的文件永久静音）。
    一个函数同时满足两种相反的默认值，只能靠调用方各自解释 None。
    """
    if not ffmpeg or not path:
        return None
    try:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-i", path],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              timeout=60)
    except Exception:
        return None
    return b"Audio:" in (proc.stderr or b"")


def has_audio_stream(ffmpeg, path):
    """判断媒体文件里有没有音轨（补音轨用）。

    二态：只有**确认有**才返回 True。探测不了也算 False —— 这是 mux 场景要的
    默认值（判断不了就别去动人家的音轨）。三态的原始接口见 probe_audio_stream。
    """
    return probe_audio_stream(ffmpeg, path) is True


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


# ============================================================
# 【第 23 轮·新】3D 布局探测：这个视频**本身**是不是已经排成了 3D？
# ============================================================
# 起因：用户对一个已经是 3D（左右分屏）的视频点了【转换为3d】，程序照着
# 「2D → 3D」又转了一遍 —— 得到的是四格画面（每一半又被拆成左右两份），
# 还白烧几分钟算力。
#
# 判据：左右（或上下）两半是不是**同一幅画面的两个视点**（本程序的产物正是
# `left = remap(frame, x+shift)` / `right = remap(frame, x-shift)` 拼起来的）。
# 下面三组数全部来自实测打表，见 docs/final_player_第23轮_*.md：
#
#   · 真 3D（本程序产物 / 合成 SBS / 合成 TAB）：err_best 0.77~9.39，梯度中位 19.4~41.3
#   · 真 2D（city.mp4 / 录屏 / bug1.mp4）      ：err_best 29.8~65.7，梯度中位 13.0~23.6
#   · 纯色 / 翻黑场 / 加黑边                  ：err_best 0.06~12.0，**梯度中位 0.0~5.7**
#
# ⚠️ 最后一行才是真正的坑：**纯色帧的两半"完全一样"，长得和真 3D 一模一样**
# （残差 0.06，比真 3D 还小）。所以只看残差必然把黑场、纯色片头误判成 3D。
# 把它俩分开的是"这一帧有没有细节可比" —— 梯度中位数。低于 DETAIL_MIN 的帧
# 直接跳过、不投票；整段视频一帧都不够判时返回 None（= 不拦）。
DETECT_HALF_SIZE = 200        # 每个半幅缩到这个宽度/高度再比（统一位移尺度、也够快）
DETECT_MAX_SHIFT = 8          # 在半幅 200px 的尺度上搜 ±8px 位移（≈ ±4%）
DETECT_SAMPLE_FRAMES = 6      # 均匀取样帧数
DETECT_DETAIL_MIN = 8.0       # 该帧的梯度中位数 ≥ 它才算"够判"
DETECT_ERR_MAX = 15.0         # 最佳位移下的平均残差 < 它 → 两半像同一幅画
DETECT_MED_MAX = 8.0          # 残差中位数 < 它 → 一半以上像素都对得上
DETECT_VOTE_SHARE = 0.6       # 够判的帧里要有这个比例投同一票才下结论
DETECT_MIN_USABLE = 2         # 至少这么多帧够判，否则不下结论


def _shift_abs_diff(a, b, axis, d):
    """把 `b` 沿 `axis` 平移 `d` 像素后与 `a` 逐像素比，返回差值图。

    越界部分用 edge 复制填充 —— 用 0 填充会在边缘造出一条假的"高差异带"，
    把本来匹配得上的两半硬拉低。
    """
    pad = DETECT_MAX_SHIFT
    if axis == 1:
        bp = np.pad(b, ((0, 0), (pad, pad)), mode="edge")
        return np.abs(a - bp[:, pad + d: pad + d + a.shape[1]])
    bp = np.pad(b, ((pad, pad), (0, 0)), mode="edge")
    return np.abs(a - bp[pad + d: pad + d + a.shape[0], :])


def _half_match_stats(frame, axis):
    """把 frame 沿 axis 劈成两半，量"它们像不像同一幅画"。劈不开 → None。

    `axis=1` 左右劈（SBS），`axis=0` 上下劈（TAB）。
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if axis == 1:
        if gray.shape[1] % 2:
            return None
        a, b = gray[:, :gray.shape[1] // 2], gray[:, gray.shape[1] // 2:]
    else:
        if gray.shape[0] % 2:
            return None
        a, b = gray[:gray.shape[0] // 2, :], gray[gray.shape[0] // 2:, :]

    # 缩到统一步长：位移是"像素"单位，不统一尺度就没法共用一套阈值
    span = a.shape[1] if axis == 1 else a.shape[0]
    scale = DETECT_HALF_SIZE / float(span)
    if scale < 1.0:
        size = (int(round(a.shape[1] * scale)), int(round(a.shape[0] * scale)))
        a = cv2.resize(a, size, interpolation=cv2.INTER_AREA)
        b = cv2.resize(b, size, interpolation=cv2.INTER_AREA)
    a = a.astype(np.float32)
    b = b.astype(np.float32)

    best_err, best_map = None, None
    for d in range(-DETECT_MAX_SHIFT, DETECT_MAX_SHIFT + 1):
        diff = _shift_abs_diff(a, b, axis, d)
        err = float(diff.mean())
        if best_err is None or err < best_err:
            best_err, best_map = err, diff

    gx = cv2.Sobel(a, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(a, cv2.CV_32F, 0, 1, ksize=3)
    return {
        "err_best": best_err,
        "med": float(np.median(best_map)),
        # "够不够判"的凭据：这一帧自己有多少细节。用**中位数**而不是均值 ——
        # 均值会被一条窄噪声带（合成纯色样本顶上那 6 行）拉高，而中位数
        # 如实地说"这块画面整体是平的"。
        "grad_med": float(np.median(np.sqrt(gx * gx + gy * gy))),
    }


def _sample_frames(cap, total, count):
    """在整段里均匀取 `count` 帧。

    跳过头尾各 8%：片头常是黑场/厂标、片尾常是字幕卡，都不代表正片。
    """
    frames = []
    for i in range(count):
        if total > 1:
            idx = int(total * (0.08 + 0.84 * i / max(count - 1, 1)))
            cap.set(cv2.CAP_PROP_POS_FRAMES, min(idx, total - 1))
        ret, frame = cap.read()
        if not ret:
            continue
        frames.append(frame)
    return frames


def detect_3d_layout(path, sample_frames=DETECT_SAMPLE_FRAMES):
    """这个视频**本身**是不是已经排成了 3D 布局？

    返回 `"sbs"`（左右分屏）/ `"tab"`（上下分屏）/ `None`（不是，或者看不出来）。

    ⚠️ **看不出来一律返回 None（= 放行）**，这是刻意的取舍：把一段真 2D 拦住
    （用户想转却转不了）比放过一段已经是 3D 的（白转几分钟）更难受。
    所以只在"证据够硬"时才下结论 —— 够判的帧要够多，且其中多数投同一票。
    """
    if not path or not os.path.exists(path):
        return None
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return None
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        frames = _sample_frames(cap, total, sample_frames)
    finally:
        cap.release()

    # 两个方向分别投票：一段视频不可能既是左右分屏又是上下分屏，
    # 所以把两边的"够判帧数"分开数（合成一个会把分母算大、谁都过不了线）。
    for axis, name in ((1, "sbs"), (0, "tab")):
        usable = hits = 0
        for frame in frames:
            stats = _half_match_stats(frame, axis)
            if stats is None or stats["grad_med"] < DETECT_DETAIL_MIN:
                continue                      # 纯色/黑场/几乎没细节 → 这一帧说了不算
            usable += 1
            if (stats["err_best"] < DETECT_ERR_MAX
                    and stats["med"] < DETECT_MED_MAX):
                hits += 1
        if usable >= DETECT_MIN_USABLE and hits >= usable * DETECT_VOTE_SHARE:
            return name
    return None
