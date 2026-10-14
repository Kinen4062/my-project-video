# -*- coding: utf-8 -*-
"""回归测试的公共设施（第 11 轮固化）。

## 为什么要有这个目录

第 1~10 轮的验证脚本都是"现写、现跑、现删"，全在 `%TEMP%` 里。结果是：每改一次
播放逻辑，就得把「37 项功能用例 + 随机交错压力测试 + 定时器泄漏检查 + 三素材测速
+ 混音通道探针」**从头重写一遍**。这个目录把它们收拢成能重复运行的套件。

## 用法

    python run_tests.py              # 默认：3 个压力种子 × 60 次操作
    python run_tests.py --full       # 5 个种子 × 80 次操作（发布前跑）
    python tests/test_playback.py    # 也可以单独跑某一个套件

## 约定（都是踩过的坑，别改）

- **每个套件跑在独立进程里**（`run_tests.py` 用 subprocess 拉起）：Tk / SDL 有全局
  状态，同进程内互相污染；更要紧的是真段错误时能**定位到具体套件**，而不是整轮
  回归一起陪葬 —— 第 9、10 轮那两个段错误就是靠这个才抓到的。
- **Tk 必须用 `root.update()` 泵事件循环**，不能 `sleep` 等 —— `after` 回调不会
  自己跑。
- **收尾绝不调用 `app.on_close()`**：它会 `os._exit(0)`，直接把测试进程掐掉。
  统一走 `teardown()`。
- **断言只记不抛**（`Summary.check`），一个用例失败不影响后面的用例，跑完再汇总
  退出码。
- **量"音量"只认混音通道值**，不认 `get_volume()` —— 后者读的是不参与混音的
  `settings.audio_volume`，会撒谎（见 MEMORY 第 9 条）。
- 运行期产物一律落在 `%TEMP%/fp_tests/`，`run_tests.py` 收尾时清掉。
"""

import ctypes
import os
import statistics
import sys
import tempfile
import time

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
CITY = os.path.join(PROJ, "test_videos", "city.mp4")
TMP_ROOT = os.path.join(tempfile.gettempdir(), "fp_tests")

# 混音通道里"满音量"的值（SDL_mixer 用 0..128 表示 0..1）
MIXER_MAX = 128


def to_mixer_level(volume_0_1):
    """把程序的 0.0~1.0 音量换算成混音通道值（便于断言时给容差）"""
    return int(round(volume_0_1 * MIXER_MAX))


def work_dir(name):
    """给某个套件一个固定的临时工作目录（存在就复用，运行期产物都放这里）"""
    d = os.path.join(TMP_ROOT, name)
    os.makedirs(d, exist_ok=True)
    return d


# ============================================================
# 测试素材
# ============================================================
def city_fixture():
    """有音轨的测试素材。缺失就用 ffmpeg 现场合成一份（保证套件自给自足）。

    为什么非要"有音轨"的素材：整套音频逻辑（音频主时钟、音量下发、抢发窗口、
    后台交接）都只在有音轨时才被触发。用无声素材测等于什么都没测。
    """
    if os.path.exists(CITY):
        return CITY
    import final_player as fp
    ffmpeg = fp.find_ffmpeg()
    if ffmpeg is None:
        raise RuntimeError(
            "缺少测试素材：%s 不存在，且找不到 ffmpeg 来合成替代素材。\n"
            "请恢复 test_videos/city.mp4，或安装 ffmpeg。" % CITY)
    dst = os.path.join(work_dir("_fixtures"), "city_synth.mp4")
    if not os.path.exists(dst) or os.path.getsize(dst) == 0:
        import subprocess
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=8",
               "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=8",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
               dst]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=120)
    return dst


def make_noaudio_fixture(dst, src=None, seconds=3.0):
    """造一个"**无音轨** + 左右分屏"的假 3D 产物，返回路径。

    这正是本程序 `cv2.VideoWriter` 转换产物的形态（只写视频流 ⇒ 没有音轨）。
    必须专门测这一档，因为"无音轨"是下面这一整套兜底逻辑的唯一触发条件：
    死时钟检测、墙钟主时钟、永不调 `set_volume`（调了就踩 NULL 段错误）。
    第 9、10 轮那两个段错误全都是无声素材才暴露出来的。
    """
    src = src or city_fixture()
    cap = cv2.VideoCapture(src)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    want = int(fps * seconds)
    writer = cv2.VideoWriter(dst, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w * 2, h))
    n = 0
    try:
        while n < want:
            ok, frame = cap.read()
            if not ok:
                break
            writer.write(cv2.hconcat([frame, frame]))
            n += 1
    finally:
        writer.release()
        cap.release()
    if n == 0:
        raise RuntimeError("造无音轨素材失败：源视频一帧都没读到")
    return dst


# ============================================================
# 断言收集
# ============================================================
class Summary:
    """把断言"只记不抛"地收集起来，跑完统一汇总退出码。"""

    def __init__(self, title):
        self.title = title
        self.passed = 0
        self.fails = []

    def section(self, name):
        print("\n=== %s ===" % name)

    def check(self, cond, msg):
        if cond:
            self.passed += 1
            print("  PASS  " + msg)
        else:
            self.fails.append(msg)
            print("  FAIL  " + msg)
        return bool(cond)

    def note(self, msg):
        print("        " + msg)

    def finish(self):
        print("\n---- %s: %d PASS / %d FAIL ----"
              % (self.title, self.passed, len(self.fails)))
        if self.fails:
            print("失败项：")
            for f in self.fails:
                print("  - " + f)
        return 0 if not self.fails else 1


# ============================================================
# 混音通道探针（量"真实音量"的唯一可靠手段）
# ============================================================
class Mixer:
    """直接读 SDL2_mixer 的每通道状态。

    这是"音量到底有没有生效"唯一可靠的判据：
      - `app.audio_player.get_volume()` 读的是 `settings.audio_volume`，
        一个**不参与混音**的字段（`ff_opts['volume']` 写的也是它），会撒谎；
      - 真实响度只看通道值，而通道值只有 `Mix_Volume` 能反映。

    另外：混音器被销毁后 `Mix_Volume` 会踩 NULL（读 NULL+0x18 = volume 字段偏移），
    ctypes 会抛 `OSError: access violation`。这里捕获它并返回 `None` ——
    **那个 None 本身就是"混音器没了"的证据**，第 10 轮正是靠它定位到崩溃机制的。
    """

    def __init__(self):
        self.dll = None
        self.available = False
        self.reason = ""
        self._load()

    def _load(self):
        candidates = []
        for root in (sys.prefix, os.path.join(PROJ, ".venv")):
            candidates.append(os.path.join(root, "share", "ffpyplayer", "sdl", "bin",
                                           "SDL2_mixer.dll"))
        path = next((p for p in candidates if os.path.isfile(p)), None)
        if path is None:
            self.reason = "找不到 SDL2_mixer.dll（本构建的 ffpyplayer 才是带混音器的版本）"
            return
        try:
            os.add_dll_directory(os.path.dirname(path))
        except Exception:
            pass
        try:
            self.dll = ctypes.CDLL(path)
        except Exception as e:                       # pragma: no cover
            self.reason = "加载 SDL2_mixer 失败: %r" % (e,)
            return
        self.dll.Mix_Volume.restype = ctypes.c_int
        self.dll.Mix_Volume.argtypes = [ctypes.c_int, ctypes.c_int]
        self.dll.Mix_Playing.restype = ctypes.c_int
        self.dll.Mix_Playing.argtypes = [ctypes.c_int]
        self.available = True
        self.path = path

    def _chan_count(self):
        try:
            return max(8, int(self.dll.Mix_AllocateChannels(-1)))
        except Exception:
            return 8

    def snapshot(self):
        """[(通道, 音量, 是否正在出声), ...]；混音器已销毁则返回 None。"""
        if not self.available:
            return None
        out = []
        try:
            for ch in range(self._chan_count()):
                vol = self.dll.Mix_Volume(ch, -1)
                playing = self.dll.Mix_Playing(ch)
                out.append((ch, vol, playing))
        except OSError:
            return None
        return out

    def channels_at(self, level, playing=True, tol=3):
        """音量约等于 level（且在出声）的通道号列表；混音器没了返回 None。"""
        snap = self.snapshot()
        if snap is None:
            return None
        return [ch for ch, vol, ply in snap
                if abs(vol - level) <= tol and (playing is None or bool(ply) == playing)]

    def audio_channels(self):
        """当前正在出声的通道 [(通道, 音量), ...]；混音器没了返回 None。"""
        snap = self.snapshot()
        if snap is None:
            return None
        return [(ch, vol) for ch, vol, ply in snap if ply]


# ============================================================
# Tk 事件泵 / 光标拖动 / 帧间隔探针
# ============================================================
def pump(root, seconds):
    """泵 Tk 事件循环 —— `after` 回调只有在 update() 里才会跑。"""
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.003)


def drag(app, root, ratio_a, ratio_b, hold_ms=50):
    """模拟"按住进度条从 ratio_a 拖到 ratio_b 再松手"（0.0~1.0 的比例）。"""
    w = app.scale.winfo_width()
    if w <= 1:
        w = 600
    app.scale.event_generate("<Button-1>", x=int(w * ratio_a), y=8)
    pump(root, hold_ms / 1000.0)
    app.scale.event_generate("<B1-Motion>", x=int(w * ratio_b), y=8)
    pump(root, hold_ms / 1000.0)
    app.scale.event_generate("<ButtonRelease-1>", x=int(w * ratio_b), y=8)


class FrameProbe:
    """统计"画面真的渲染出一帧"的时间间隔。

    探针挂在 `render_current_frame` 上而不是 `update_frame`：后者在拖动/跳转
    期间会被高频调用（30ms 轮询），会把"没出帧的空档"算成帧间隔。
    `render_current_frame` 才是"新画面真的上屏"的信号。
    """

    def __init__(self, app):
        self.gaps = []
        self._last = None
        self._orig = app.render_current_frame

        def hook(*args, **kwargs):
            now = time.time()
            if self._last is not None:
                self.gaps.append((now - self._last) * 1000.0)
            self._last = now
            return self._orig(*args, **kwargs)

        app.render_current_frame = hook

    def reset(self):
        """清零。**测暂停/恢复相关时序时必须在"按继续"的前一刻调用** ——
        暂停期间本来就不出帧，那段空档会被误算成一次大卡顿。"""
        self.gaps = []
        self._last = None

    def stats(self):
        if not self.gaps:
            return {"n": 0, "median": 0.0, "max": 0.0, "over200": 0, "over400": 0}
        return {
            "n": len(self.gaps),
            "median": statistics.median(self.gaps),
            "max": max(self.gaps),
            "over200": sum(1 for g in self.gaps if g > 200),
            "over400": sum(1 for g in self.gaps if g > 400),
        }


def measure_speed(app, root, window_sec):
    """稳态播放速度 = 媒体位置推进量 / 墙钟（不碰界面）。

    必须用**媒体位置推进量**算，不能用渲染帧数（丢帧时会低估）。
    窗口由调用方给，且**必须明显短于片长** —— 否则会穿过片尾回绕，
    算出来是倒放/接近 0（`output_3d_video.mp4` 只有 4 秒，踩过这个坑）。
    """
    fps = app.video_fps if app.video_fps > 0 else 25.0
    p0 = app.cap.get(cv2.CAP_PROP_POS_FRAMES)
    t0 = time.time()
    pump(root, window_sec)
    p1 = app.cap.get(cv2.CAP_PROP_POS_FRAMES)
    t1 = time.time()
    if p1 < p0:                       # 兜底：万一还是绕了圈
        p1 += app.total_frames
    return (p1 - p0) / fps / max(1e-6, (t1 - t0))


def measure_speed_window(app, margin=0.4, cap_sec=3.0):
    """按片长挑一个安全的测速窗口"""
    fps = app.video_fps if app.video_fps > 0 else 25.0
    duration = (app.total_frames / fps) if app.total_frames else 20.0
    return min(cap_sec, max(0.8, duration * margin))


# ============================================================
# 应用装载 / 收尾
# ============================================================
def take_keyboard_focus(root, widget=None):
    """把 Tk 的**键盘焦点窗口**摆到 `widget`（默认主窗口）上并返回是否成功。

    这不是可选的礼节，是硬条件。Tk 对键盘事件做"焦点重定向"：事件要交给当前
    焦点窗口，**当前没有焦点窗口时事件直接作废** —— `-window` / `when` 都救不了。
    而刚 `Tk()` 出来的窗口 `tk.call("focus")` 是空串（只是"可视图元"），
    所以必须显式抢一次。

    【第 11 轮踩过】前台手跑测试时窗口常常顺手拿到焦点，看起来一切正常；
    改成后台任务跑（`run_tests.py --full` 重定向到日志）后窗口抢不到焦点，
    快捷键那一组 14 项全灭，症状是"按键之后位置纹丝不动"。

    为什么是 `focus_force` 而不是 `focus_set`：后者走的是"请求系统把焦点给
    我这个窗口"，没有用户交互时系统会拒绝，`tk.call("focus")` 仍为空（实测）。
    `focus_force` 直接设置 Tk 内部的焦点窗口，不依赖系统配合。

    **注意：焦点不是设一次就一劳永逸。** 窗口在无人值守环境下不是前台窗口，
    每处理一轮 `update()` 都可能收到失活消息而把 Tk 焦点清回空串。所以凡是
    要发按键的地方都得**在发之前重新确认一次**，不能缓存在变量里。
    """
    target = widget if widget is not None else root
    try:
        target.focus_force()
        root.update()
        return bool(root.tk.call("focus"))
    except Exception:
        return False


def load_app(geometry="1000x700"):
    if PROJ not in sys.path:
        sys.path.insert(0, PROJ)
    import tkinter as tk
    import final_player as fp
    root = tk.Tk()
    root.geometry(geometry)
    root.update()
    app = fp.VideoPlayerApp(root)
    root.update()
    take_keyboard_focus(root)          # 见函数说明：不抢焦点则按键全被丢弃
    return root, app, fp


def teardown(root, app):
    """收尾。**绝不调用 `app.on_close()`**（它会 os._exit(0) 掐掉测试进程）。"""
    for fn in (getattr(app, "_stop_playback_loop", None),
               lambda: app._cancel_audio_handoff(timeout=1.0),
               getattr(app, "_close_audio_player", None)):
        try:
            if fn:
                fn()
        except Exception:
            pass
    try:
        if app.cap is not None:
            app.cap.release()
    except Exception:
        pass
    try:
        root.destroy()
    except Exception:
        pass


def run_suite(title, body):
    """套件统一入口：装载 → 跑 body(root, app, fp, summary) → 收尾 → 返回退出码"""
    summary = Summary(title)
    root, app, fp = load_app()
    try:
        body(root, app, fp, summary)
    finally:
        teardown(root, app)
    return summary.finish()
