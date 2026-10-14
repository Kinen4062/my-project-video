# -*- coding: utf-8 -*-
"""转换产物补音轨套件（第 11 轮新增功能）。

`cv2.VideoWriter` 只写视频流 —— 所以"视频转成 3D 之后没声音了"。
修法是在转换收尾用 ffmpeg 把原视频的音轨并进产物（`-c:v copy` 不重编码）。

这里不跑真正的转换（那要加载 Depth-Anything 模型，又慢又要联网），而是**直接
拿一对素材测 mux 本身**：
  - 视频：`cv2.VideoWriter` 造的无声左右分屏（和转换产物同源）；
  - 音源：city.mp4。

覆盖三件事：① 有音源时能补上；② 源文件本来就没音轨时安静地不改动产物；
③ 补出来的文件仍然能正常打开播放（音轨并进去之后容器没有写坏）。
"""

import os
import sys

import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import (city_fixture, make_noaudio_fixture,  # noqa: E402
                      run_suite, work_dir)


def probe(ffmpeg, path):
    """用 ffmpeg 看一个文件的基本信息（有没有音轨 / 时长）"""
    import subprocess
    proc = subprocess.run([ffmpeg, "-hide_banner", "-i", path],
                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          timeout=60)
    text = (proc.stderr or b"").decode("utf-8", "replace")
    return {"has_audio": "Audio:" in text,
            "has_video": "Video:" in text,
            "text": text}


def body(root, app, fp, s):
    ffmpeg = fp.find_ffmpeg()
    s.check(ffmpeg is not None,
            "找到 ffmpeg 可执行文件（找不到就只能降级为无声产物）")
    if ffmpeg is None:
        s.note("找不到 ffmpeg，跳过后续用例 —— 生产代码此时会静默降级，"
               "转换本身不受影响")
        return

    city = city_fixture()
    d = work_dir("mux")

    s.section("① 前置事实：转换产物的形态就是『无音轨』")
    source_video = os.path.join(d, "fake_3d_silent.mp4")
    make_noaudio_fixture(source_video, city, seconds=3.0)
    info = probe(ffmpeg, source_video)
    s.check(info["has_video"] and not info["has_audio"],
            "cv2.VideoWriter 造出的左右分屏产物确实没有音轨（问题本体的来源）")
    s.check(not fp.has_audio_stream(ffmpeg, source_video),
            "has_audio_stream() 也能正确判定它没有音轨")
    s.check(fp.has_audio_stream(ffmpeg, city), "has_audio_stream() 能认出 city.mp4 有音轨")

    s.section("② 把 city.mp4 的音轨补进无声产物")
    ok = app._mux_audio_into_video(source_video, city)
    s.check(ok, "_mux_audio_into_video 返回 True")
    info = probe(ffmpeg, source_video)
    s.check(info["has_audio"], "补完之后产物里真的有音轨了")
    s.check(info["has_video"], "视频流还在")
    # 视频流是 -c:v copy 过来的，分辨率不该变
    src = cv2.VideoCapture(city)
    src_w = int(src.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(src.get(cv2.CAP_PROP_FRAME_HEIGHT))
    src.release()
    cap = cv2.VideoCapture(source_video)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    ok_read, _ = cap.read()
    cap.release()
    s.check(ok_read and w == src_w * 2 and h == src_h and frames >= 60,
            "补完的文件仍能正常解码（%dx%d，%d 帧；期望 %dx%d）"
            % (w, h, frames, src_w * 2, src_h))
    s.check(not os.path.exists(source_video + ".muxing.mp4"),
            "临时文件已清掉（不是留在旁边）")

    s.section("③ 源文件本来就没有音轨时，安静地不改动产物")
    silent = os.path.join(d, "silent_again.mp4")
    make_noaudio_fixture(silent, city, seconds=2.0)
    before = os.path.getsize(silent)
    ok = app._mux_audio_into_video(silent, silent)
    s.check(ok is False, "返回 False（没有音轨可补），而不是抛异常")
    s.check(os.path.getsize(silent) == before, "产物没有被改动（大小不变）")
    s.check(probe(ffmpeg, silent)["has_video"], "产物仍然可读")

    s.section("④ 找不到 ffmpeg 时必须优雅降级")
    saved_path, saved_searched = fp._FFMPEG_PATH, fp._FFMPEG_SEARCHED
    saved_env = os.environ.get("FFMPEG_BINARY")
    try:
        fp._FFMPEG_PATH = ""          # 伪装成"找不到"
        fp._FFMPEG_SEARCHED = True
        s.check(fp.find_ffmpeg() is None, "伪装生效：find_ffmpeg() 返回 None")
        s.check(app._mux_audio_into_video(silent, city) is False,
                "没有 ffmpeg 时返回 False，不抛异常（转换不会被它搞失败）")
    finally:
        fp._FFMPEG_PATH, fp._FFMPEG_SEARCHED = saved_path, saved_searched
        if saved_env is None:
            os.environ.pop("FFMPEG_BINARY", None)
        else:
            os.environ["FFMPEG_BINARY"] = saved_env
    s.check(fp.find_ffmpeg() is not None, "缓存已恢复（不影响后续用例）")


if __name__ == "__main__":
    sys.exit(run_suite("转换补音轨", body))
