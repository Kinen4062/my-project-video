import os
import sys       # 【P2】退出前冲刷输出缓冲
import shutil  # 用于复制文件
import tempfile  # 【P1】转换产物落到系统临时目录，不再污染工作目录
import uuid      # 【P1】每次转换生成唯一临时文件名，避免互相覆盖
import subprocess  # 【第 11 轮】调用 ffmpeg 把原音轨补回转换产物

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import cv2
import torch
import numpy as np
from PIL import Image, ImageTk
import threading
import time
import math
from transformers import AutoImageProcessor, AutoModelForDepthEstimation
from ffpyplayer.player import MediaPlayer
# ============ 深色主题配色 ============
DARK_BG      = "#1e1e1e"   # 主背景
DARK_BG2     = "#2b2b2b"   # 次级背景（面板、进度条槽）
DARK_BG3     = "#3c3c3c"   # 控件背景（按钮、输入框）
DARK_FG      = "#e0e0e0"   # 主文字
DARK_FG2     = "#a0a0a0"   # 次要文字
DARK_HL      = "#555555"   # 高亮 / hover
DARK_ACCENT  = "#4a9eff"   # 强调色（进度、选中）
# 普通按钮（深灰底 + 扁平）
BTN_NORMAL = dict(
    bg=DARK_BG3, fg=DARK_FG,
    activebackground=DARK_HL, activeforeground=DARK_FG,
    relief=tk.FLAT, bd=0, padx=8, pady=4,
)

# 危险按钮（红底，用于取消转换）
BTN_DANGER = dict(
    bg="#c0392b", fg="white",
    activebackground="#e74c3c", activeforeground="white",
    relief=tk.FLAT, bd=0, padx=10, pady=4,
)

# 主要行动按钮（橙底，用于转换）
BTN_PRIMARY = dict(
    bg="#e67e22", fg="white",
    activebackground="#f39c12", activeforeground="white",
    relief=tk.FLAT, bd=0, padx=8, pady=4,
)

# 成功按钮（绿底，用于导出）
BTN_SUCCESS = dict(
    bg="#27ae60", fg="white",
    activebackground="#2ecc71", activeforeground="white",
    relief=tk.FLAT, bd=0, padx=8, pady=4,
)

# ============================================================
# 全局快捷键的"让路"规则
# ============================================================
# 原则只有一条：**只有当这个键在该控件上真的有原生含义时才让路**。
# 让路过多 = 快捷键变成哑巴，这比"偶尔双重动作"更严重 —— 2026-10-13 栽过一次：
# 原来 ARROW_CONFLICT_WIDGETS = SPACE_CONFLICT_WIDGETS + (Scale, Scale)，
# 把 Button / Combobox 一并算进了"方向键冲突"，于是**只要点过任意一个按钮或下拉框，
# ←/→/J/L/M/F 就全部失灵**。用户报的正是"空格/K/←/→/J/L/M/F 全都没反应"。
#
# 所以规则写成"控件类型 → 该控件真正会吃的键"的有序表，**逐键判断**。
# 一条铁律：不能用"这个控件是冲突控件"一概而论 —— 一个控件常常只吃一部分键
# （下拉框吃方向键却完全不吃字母键）。
#
# 下面每条都实测定过，不是照抄 Tk 文档推断的。

_ARROW_KEYS = ("left", "right", "up", "down")
_ALL_SHORTCUT_KEYS = _ARROW_KEYS + ("space", "m", "f", "j", "k", "l")

# 真正"要打字"的控件：空格打空格、方向键移光标、字母变输入内容 → 全让。
TEXT_INPUT_WIDGETS = (tk.Entry, ttk.Entry, tk.Text)

# ⚠️ 顺序有意义，**第一个类型匹配上的规则就是定论**：
#    该控件吃哪些键写在它的集合里，不在这集合里的键一律不让路。
#    不能写成"没匹配上就继续往下找" —— 那样 ttk.Combobox（它是 ttk.Entry 的
#    子类）会掉进下面那条更宽的"输入框全让路"里，空格和字母键就又被吞掉了。
KEY_YIELD_RULES = (
    # 只读下拉框：实测**列表关着**的时候只有 ↓ 有真实动作（弹出候选列表），
    # ←/→/↑ 一概改不动它的值、空格也不弹不改。规则表里先按"让方向键"记，
    # 再由 `_shortcut_should_yield()` 里的例外按"列表开着/关着"细分。
    # 这条必须排在最前面：**ttk.Combobox 是 ttk.Entry 的子类**
    # （isinstance 对 Entry 也成立）。排后面就会被"输入框全让路"吞掉 ——
    # 焦点在一次下拉框选择之后落到下拉框上，空格和字母键就全哑了，
    # 正是用户报的那个现象。这个坑是让路矩阵测试当场抓出来的。
    (ttk.Combobox, _ARROW_KEYS),
    # 真正"要打字"的控件：空格打空格、方向键移光标、字母变输入内容 → 全让。
    (TEXT_INPUT_WIDGETS, _ALL_SHORTCUT_KEYS),
    # 滑块：实测 ←/→/↑/↓ 调值。空格和字母一概不吃。
    ((tk.Scale, ttk.Scale), _ARROW_KEYS),
    # 列表：方向键移动选中、空格选中当前项。
    ((tk.Listbox,), _ARROW_KEYS + ("space",)),
    # 按钮/复选/单选：Tk 的类绑定会把空格绑到"激活自己"上。
    #
    # 按钮这一档实际已经被 __init__ 里的 bind_class 替换掉了（空格在按钮上
    # 直接就是播放/暂停），所以这里更多是**兜底**：万一将来有哪个按钮类没被
    # bind_class 覆盖到，至少还知道"空格要留给它"，不会出现双重动作。
    # ⚠️ 刻意只有 "space"：把按钮算进方向键让路，就是那个 bug 的根源。
    ((tk.Button, ttk.Button, tk.Checkbutton, ttk.Checkbutton,
      tk.Radiobutton, ttk.Radiobutton), ("space",)),
)


def should_yield_shortcut(widget, key):
    """这个按键要不要让给这个控件自己处理？

    `key` 用 Tk 的 keysym 小写形式（"left"/"space"/"m"…）。
    **第一个类型匹配上的规则就是定论** —— 顺序有意义，见 KEY_YIELD_RULES 的注释。
    """
    if widget is None:
        return False
    for types_, keys in KEY_YIELD_RULES:
        if isinstance(widget, types_):
            return key in keys
    return False

# 【第 11 轮·新】键盘快捷键的步长
VOLUME_KEY_STEP = 0.05        # ↑/↓ 每按一次改变的音量（0~1）
SHORTCUT_SEEK_STEP_SEC = 10.0  # J/L 每按一次快退/快进的秒数

# 【P1 修复】音频"跳转"为什么只能用重建播放器，而不能用 seek()：
# 实测本构建 ffpyplayer（ffpyplayer 自带 ffmpeg）得到的三条结论：
#   1) MediaPlayer.seek() 只移动它自己的视频流 —— seek(7.5) 后 ffpyplayer 的
#      帧 pts 跳到 7.52，但**音频时钟仍从 0.35 继续走**，音频根本没被定位。
#      而本程序的画面由 OpenCV 输出，ffpyplayer 的视频流完全用不到，
#      所以 seek() 在这里是纯无用功。
#   2) 暂停状态下调用 seek() 会直接段错误（SIGSEGV，进程退出码 139）；
#      刚恢复播放就调 seek() 同样会崩。
#   3) 创建播放器时传 ff_opts['ss'] 是**唯一**能真正定位音频的手段，
#      但它是"非精确 seek"，会回退到目标之前的关键帧
#      （实测 city.mp4 关键帧只有 0s/12s：ss=5 落到 0s、ss=17 落到 12s）。
# 结论：音频跳转一律走 _restart_audio_at()，代码里不再出现任何 seek() 调用。
# 代价是跳转精度受视频关键帧间隔限制（常见素材 1~2 秒）。

# 【P1 修复 2】音频重建是"贵"操作，实测一次要 ~0.5 秒（见下），
# 而拖动进度条是高频操作，因此这里做三层收敛：
#   1) 拖动进度条的过程中不重建音频，只在"停手"后重建一次；
#   2) 短时间内多次跳转请求会被合并（防抖），只执行最后一次；
#   3) 暂停状态下完全不重建（音频本来就不出声），
#      只记住目标位置，等恢复播放时再重建一次。
# 实测依据（本机 .venv 的 ffpyplayer，media 为 20 秒的 city.mp4）：
#   _create_audio_player()  ≈ 1.6 ms   （构造是异步的，很快）
#   _close_audio_player()   ≈ 400~580 ms（等读线程退出，是固定开销，
#                                        与已播放时长无关，清空队列、
#                                        先 set_pause/set_volume 都无效）
# 所以卡顿的唯一有效对策就是"少重建" + "别在主线程重建"。
#
# 【本次修复】"那 0.5 秒别占主线程"已经落地：见 _begin_audio_handoff()。
#   做法是"后台音频交接"，三步走：
#     ① 按住旧播放器（set_pause，静音）；② 主线程先建新播放器（≈1.6ms）；
#     ③ 等新播放器的音频确认在跑，再让工作线程去关旧的（那 0.5 秒在后台）。
#   为什么要"先建新、后关旧"（别改回去）：本构建的 SDL 混音器在最后一个
#   音频通道被关掉时会被 Mix_CloseAudio() 销毁，而销毁后调 set_volume 会
#   踩 NULL 段错误（实测 4/4 崩）。先建新的话旧播放器一直占着通道、混音器
#   全程有效 —— 音量因此能在新播放器**出声之前**就位（见 _audio_volume_resume），
#   顺带声音回来得也快得多（不用等混音器销毁再重开）。
#   关键纪律：第 ③ 步把旧实例交给工作线程之后，主线程**绝不触碰**它
#   （_cancel_audio_handoff 只 join、不调用它的方法）。
#   顺带说明（本次修复的另一个坑）：本构建的 `ff_opts['volume']` **对实际音量
#   无效**，只有 `set_volume()` 才进混音。两件事都写在 _apply_volume_now()
#   的注释里，改音量相关代码前先看它。
AUDIO_RESTART_DEBOUNCE_MS = 250   # 音频重建合并窗口
SEEK_DEBOUNCE_MS = 500            # 进度条"停手"多久才真正跳转
AUDIO_HANDOFF_POLL_MS = 30        # 后台音频交接的取回轮询间隔
AUDIO_HANDOFF_KEEPALIVE_MAX = 3.0  # 最多让旧播放器多活这么久（等新播放器音频就绪）
AUDIO_VOLUME_RESUME_POLL_MS = 10   # 抢发窗口里的音量重试间隔（比帧间隔细，
                                   # 免得"音频已经出声、音量还没落到位"）
AUDIO_CLOCK_REWIND_EPS = 0.01     # 音频时钟读数回跳超过这么多就判定为抖动，不采信
AUDIO_VOLUME_SYNC_DELAY = 0.4     # 还没确认"播放器真有音轨"时，隔多久重试一次音量

# 【第 11 条修复】音画同步改用"音频主时钟"
# ------------------------------------------------------------
# 原实现：视频靠 after(1000/fps) 自力更生数帧，音频由 ffpyplayer 独立线程按
# 真实时钟播放。一旦单帧处理耗时超过帧间隔（3D 模式下很常见），视频就自动
# 掉速，而音频照走 —— 误差只增不减，长时间播放必然漂移。
#
# 现在改成：每帧读一次 ffpyplayer 的主时钟（默认即音频时钟），用它反推
# "此刻画面应该在第几帧"，落后就丢帧追赶、超前就多等。处理慢时表现为
# 丢帧（位置仍然正确），而不是掉速累积误差。
#
# 实测本构建 get_pts() 的语义（eos 判别实验，media 为 20 秒的 city.mp4）：
#   1) 返回的是"音频时钟"：**自带 ss 偏移**（从第 12 秒起播时 pts 就从 12 起算），
#      而**增量是墙钟速度**（atempo 加快的是媒体消耗速度，不是时钟本身）：
#      1.0x / 1.5x / 0.5x / 2.0x 四种速率下斜率都是 ~1.0；
#      2.0x 时 eos 落在 pts≈10，1.0x 时落在 pts≈20。
#      => 只取"推进量"：clock = 校准时的视频位置 + (pts − 校准读数) × playback_speed。
#         （不能直接当媒体绝对位置用：ss 是非精确 seek 会回退到关键帧。）
#   2) 新播放器创建后**立刻**调用 get_pts() 会段错误（SIGSEGV）：
#      实测 delay=0 两次全崩、delay≥0.02s 全正常。
#      这里取 0.25 秒留足余量，未满则本次不参与同步（回退盲调度）。
#   3) 暂停时 pts 冻结（读它是安全的，但暂停态本来就不推进视频）。
#   4) 音频初始化失败时主时钟不可用，同样回退盲调度。
#   5) 【踩过的坑】"没有音轨"的文件**不会**让 MediaPlayer 构造报错 —— ffmpeg
#      只是打不开文件/配不起 filtergraph（stderr 打印 "Failed to open file ...
#      or configure filtergraph"），而 get_pts() 从此**永远返回 0.0**。
#      本程序自己用 cv2.VideoWriter 导出的 3D 视频（左右拼接）正是这种情况：
#      实测 city.mp4 的 pts 每 0.1 秒走 0.1 秒，而 output_3d_video.mp4 /
#      temp_3d_output.mp4 / 我的3D视频1.mp4 全部恒为 0.0。
#      一旦采信这个冻住的读数，等待量就会随播放进度**线性增长**
#      （第 1 秒起 ≈0.8s/帧、第 2 秒 ≈1.8s/帧），表现为画面越播越慢、
#      直到近乎卡死。实测对照：同一个 12 秒素材，无音轨版 6 秒只推进
#      0.88 秒（0.147x），有音轨版 0.958x。
#      对策：把"pts 返回了合法数字"降级为必要条件，另外必须**亲眼见过它推进**
#      —— 先要求推进量 ≥ AUDIO_CLOCK_ADVANCE_MIN，再要求它在播放期间持续
#      变化；若连续 AUDIO_CLOCK_STALL_SEC 读数一动不动，就判定为死时钟，
#      整个播放器实例永久弃用它，回退到按帧率的盲调度。
AUDIO_CLOCK_WARMUP_SEC = 0.25     # 播放器创建后多久才允许读 pts
AUDIO_CLOCK_ADVANCE_MIN = 0.05    # 至少见过时钟推进这么多秒，才承认它可用
AUDIO_CLOCK_STALL_SEC = 1.5       # 播放中时钟连续这么久读数完全不变 → 判死
FRAME_SYNC_TOLERANCE_FRAMES = 2   # 落后不超过这么多帧时只微调等待，不丢帧
FRAME_SYNC_MAX_WAIT_FRAMES = 4    # 主时钟算出的等待上限（兜底，见 update_frame）
# 【第 11 条修复·补漏】没有音轨的文件（本程序导出的 3D 视频就是）没有音频
# 时钟可对 —— 此时改用**墙钟**当主时钟，语义与音频时钟完全一致
# （"此刻画面应该播到第几秒"），于是丢帧追赶、倍速、拖动全都复用同一条代码路径。
# 以"重设基准那一刻的（墙钟时刻, 媒体位置）"为零点，之后 target = 零点媒体
# 位置 + 墙钟经过的时间 × 倍速。基准在起播/跳转/变速/暂停恢复/片尾回绕时重设。
# 迟到超过 SILENT_CLOCK_MAX_LAG_SEC 视为"中途被卡住"（系统调度、窗口最小化等），
# 直接重新对表、不去追这份旧账，免得一次性跳掉一大段画面。
SILENT_CLOCK_MAX_LAG_SEC = 1.0
# 丢帧的实现方式（实测 960x540 / 25fps 素材）：
#   grab() 顺序跳过      ≈ 0.3 ms/帧（线性，可预期）
#   set(POS_FRAMES, n)   ≈ 20~90 ms 一次（要回到前一关键帧再解码到目标帧，
#                          关键帧越稀疏越慢；city.mp4 只有 0s/12s 两个关键帧）
# 所以小跨度丢帧一律用 grab；只有跨度大到 grab 反而不划算时才退回 set。
FRAME_SYNC_MAX_GRAB = 60

# 【P2 修复】render_current_frame 里最贵的一步是"四周那圈模糊背景"：
# 要把整幅放大到 cover 尺寸、降采样、模糊、再放大回窗口大小。
# 它只是变暗后的陪衬（alpha=0.45），画面变化极慢，没必要每帧重算。
# 这里每 N 帧重算一次、其余帧复用缓存。
BG_REFRESH_INTERVAL = 3

# 【P2 修复】全屏时"鼠标移动 → 显示底栏"的事件节流（毫秒）。
# 不节流的话，鼠标每移动一个像素就会 after_cancel + after 一次。
MOTION_THROTTLE_MS = 100

# 【P2 修复】关闭窗口时，最多等转换线程多久（秒）去响应取消、
# 删掉写到一半的临时文件，然后才强制退出进程。
CONVERSION_JOIN_TIMEOUT_SEC = 1.0

# ============================================================
# 【第 11 轮·新】转换产物补回音轨
# ------------------------------------------------------------
# 背景：`process_video_to_3d` 用 `cv2.VideoWriter` 写 3D 产物，而它**只写视频流**
# —— 转换出来的文件**没有音轨**。用户体感是"视频一转成 3D 就没声音了"。
# （也正因如此，本程序为"无音轨文件"准备了一整套兜底：死时钟检测、墙钟主时钟、
#   永不调 set_volume —— 那些逻辑仍然要保留，因为用户完全可能打开别的无声文件。）
#
# 对策：转换收尾时调一次 ffmpeg，把**原视频的音轨**并进产物。视频流走
# `-c:v copy`（不重编码），所以只多花一两秒。
#
# 为什么必须多路径查找、还要能优雅降级：
#   本机唯一一份 ffmpeg 在 ffpyplayer 的 wheel 里
#   （`.venv/share/ffpyplayer/ffmpeg/bin/ffmpeg.exe`），**不在 PATH 上**；
#   而且只有 FULL-BUILD 轮子才带它，别的机器装了 ffpyplayer 未必有。
#   ⇒ 找不到就**静默降级**为无声产物（转换本身绝不因此失败），只在控制台说明一句。
# ============================================================
FFMPEG_MUX_TIMEOUT_SEC = 300      # 单次 mux 的超时；超时即放弃音轨（大文件也够）
FFMPEG_AUDIO_BITRATE = "160k"     # 音轨重新编码成 aac 的码率（原轨可能是任意格式）

_FFMPEG_PATH = None      # 缓存：""=找过但没有；其它=可执行文件路径
_FFMPEG_SEARCHED = False


def find_ffmpeg():
    """找一个可用的 ffmpeg 可执行文件；找不到返回 None。

    调用方（`_mux_audio_into_video` / 测试套件）**必须**处理 None 的情况。
    搜索结果会缓存，所以可以放心地在热路径外反复调用。
    """
    global _FFMPEG_PATH, _FFMPEG_SEARCHED
    if _FFMPEG_SEARCHED:
        return _FFMPEG_PATH or None
    _FFMPEG_SEARCHED = True

    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    cands = []
    env = os.environ.get("FFMPEG_BINARY")           # ① 显式指定的优先
    if env:
        cands.append(env)
    which = shutil.which("ffmpeg")                  # ② PATH
    if which:
        cands.append(which)
    # ③ ffpyplayer 的 FULL-BUILD 轮子里自带一份（本机就是靠这条找到的）
    proj_root = os.path.dirname(os.path.abspath(__file__))
    for base in (sys.prefix, proj_root):
        for sub in (("share", "ffpyplayer", "ffmpeg", "bin"),
                    (".venv", "share", "ffpyplayer", "ffmpeg", "bin")):
            cands.append(os.path.join(base, *sub, exe))
    try:                                            # ④ imageio-ffmpeg 自带的
        import imageio_ffmpeg
        cands.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass

    for c in cands:
        try:
            if c and os.path.isfile(c):
                _FFMPEG_PATH = c
                return c
        except Exception:
            continue
    _FFMPEG_PATH = ""
    return None


def has_audio_stream(ffmpeg, path):
    """判断媒体文件里有没有音轨。

    用 `ffmpeg -i` 的 stderr 里有没有 "Audio:" 字样来判断，**不依赖 ffprobe**
    （ffpyplayer 那份 wheel 里只有 ffmpeg.exe，没有 ffprobe.exe）。
    注意：`ffmpeg -i xxx` 不带输出参数时退出码就是 1，属正常，不能拿退出码判断。
    """
    try:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-i", path],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              timeout=60)
    except Exception:
        return False
    return b"Audio:" in (proc.stderr or b"")


def _remove_quietly(path):
    """删文件，失败不抛异常（临时文件清理到处都要用）"""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


class PlayState:
    """【P2 修复】播放状态。

    原来用 is_playing / is_paused 两个布尔量表达，语义重叠：
      - 没有任何入口把 is_playing 置回 False 表示"已停止"，
        它实际恒等于"没暂停"；
      - 两个独立布尔量有 4 种组合，其中 (False, False) 与 (False, True)
        都表示"没在放"，读代码时分不清哪种才算停。
    收敛成单一枚举后非法组合直接不存在。读侧仍保留同名的
    is_playing / is_paused 只读属性，写侧一律改 self.play_state。
    """

    IDLE = "idle"        # 没有打开中的视频（初始 / 关闭 / 正在换文件）
    PLAYING = "playing"
    PAUSED = "paused"


class VideoPlayerApp:
    def __init__(self, root):
        self.root = root
        self.setup_dark_theme()
        self.root.title("AI 3D 播放器 - 测试版")
        self.root.geometry("950x700")

        # --- 播放器状态 ---
        self.cap = None
        # 【P2 修复】播放状态收敛为单一枚举（见 PlayState 说明）。
        # is_playing / is_paused 是只读属性，不要给它们赋值。
        self.play_state = PlayState.IDLE
        self.playback_speed = 1.0
        self.is_fullscreen = False
        self.is_converting = False

        # --- 进度条核心变量 ---
        self.total_frames = 0
        self.video_fps = 30.0
        self.is_seeking = False
        self.seek_var = tk.DoubleVar()
        self.seek_timer = None
        # 【P1 修复 2】进度条防抖相关的状态：
        #   _scale_pressed：鼠标是否还按在进度条上（拖动中不做音频重建）
        #   _last_jump_frame：暂停态已经渲染过的帧号（点击时记按下的位置，
        #                     停手防抖跳转后记真实落点），用于让随后的
        #                     finish_seek 跳过重复的 set + 渲染；
        #                     下一次按下进度条时清空
        #   _pending_audio_pos / _audio_restart_timer：挂起/合并音频重建请求
        self._scale_pressed = False
        self._last_jump_frame = None
        self._pending_audio_pos = None
        self._audio_restart_timer = None
        self._last_title_text = None
        self.current_frame_image = None
        self.current_video_path = ""

        # 【修复】用于防止定时器叠加导致视频加速
        self.after_id = None

        # 【新增】窗口缩放防抖用的定时器 ID
        self.resize_after_id = None
        self.is_resizing = False  # 【新增】窗口拖动中标志

        self._hide_controls_id = None  # 全屏下自动隐藏底栏的定时器
        self._controls_visible = True  # 当前底栏是否可见
        # 【P2 修复】全屏鼠标移动的上一次处理时刻（用于节流），
        # 以及转换线程的引用（关窗时给它一点时间去清理临时文件）
        self._last_motion_at = 0.0
        self.conversion_thread = None

        # --- 3D 调节状态 ---
        self.parallax_value = 0.0  # 视差偏移，正值增强 3D
        self.swap_eyes = False  # 是否交换左右眼
        self.current_raw_frame = None  # 保存未处理的 SBS 原始帧
        self._mesh_hw = None  # remap 网格缓存
        self._mesh_x = None
        self._mesh_y = None

        # --- 3D 显示模式变量 ---
        self.display_mode = "2D"
        self.mode_var = tk.StringVar(value="2D")

        # --- AI 模型相关 ---
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = None
        self.image_processor = None

        # ============================================================
        # 【关键修复】顶层布局改用 grid
        # ------------------------------------------------------------
        # 原来的 pack 布局里，video_label 先打包并带 expand=True。
        # 一旦贴上图片（尤其是切换 3D 模式后重绘），它的"请求尺寸"
        # 会随图像变大，把后打包的底栏挤出可视区。
        # 而 change_display_mode 里重新 pack 底栏时，又会把底栏挪到
        # 打包顺序的更后面，于是彻底把按钮顶没了。
        #
        # grid 用 row weight 分配空间，第 0 行吸收所有额外空间，
        # 第 1、2 行（进度条、按钮栏）高度固定，永远不会被挤掉。
        # ============================================================
        self.root.grid_rowconfigure(0, weight=1)   # 视频区吸收所有额外空间
        self.root.grid_columnconfigure(0, weight=1)

        # 【修复】用容器包住 video_label，防止视频图像"请求尺寸"撑大网格
        self.video_container = tk.Frame(self.root, bg="black")
        self.video_container.grid(row=0, column=0, sticky="nsew")

        self.video_label = tk.Label(self.video_container, bg="black")
        self.video_label.pack(fill=tk.BOTH, expand=True)
        # 【关键】监听容器尺寸变化，底栏显隐/窗口缩放都会触发重绘
        self.video_container.bind("<Configure>", self.on_window_resize)

        # 【第 11 轮·修正 2】点一下画面 = 把键盘焦点收回主窗口。
        #
        # 为什么需要这个：焦点一旦被按钮/滑块/下拉框拿走，那些"在该控件上确实有
        # 原生含义"的键就会让路（见 KEY_YIELD_RULES 的说明）。这是刻意保留
        # 的取舍（否则滑块会和步进抢方向键），但必须给用户一个**一眼能想到的恢复
        # 动作**，不能让人以为"快捷键坏了"。
        # 点画面是最自然的选择：用户本来就在看画面。
        # （tk.Label / tk.Frame 的 takefocus 默认是 0，所以点它不会反过来抢走焦点。）
        for _w in (self.video_container, self.video_label):
            _w.bind("<Button-1>", self._restore_shortcut_focus)

        self.progress_frame = tk.Frame(self.root, bg=DARK_BG)
        self.progress_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=5)

        self.scale = ttk.Scale(self.progress_frame, from_=0, to=100, orient=tk.HORIZONTAL,
                               variable=self.seek_var, command=self.on_scale_move)
        self.scale.pack(fill=tk.X)
        self.scale.bind("<ButtonRelease-1>", self.on_scale_release)
        self.scale.bind("<Button-1>", self.on_scale_click)
        # 【P1 修复 2】兜底：用户可能在进度条上按下、把鼠标拖出控件后才松开，
        # 那样 scale 自己收不到 ButtonRelease，_scale_pressed 会永远停在 True，
        # 导致音频一直挂起等不到重建。这里在窗口级也接一次松开事件
        # （on_scale_release 会先检查 _scale_pressed，别处点一下不会误触发）。
        self.root.bind("<ButtonRelease-1>", self.on_scale_release, add="+")

        # ============================================================
        # 【P2 修复】底栏原来是一整行塞 12 个控件（音量+静音、打开、转换、
        # 播放、3D模式、截图、导出、倍速…），950px 宽必然拥挤甚至被裁。
        # 现在按用途拆成两行：
        #   row 2  btn_frame —— 主操作（打开 / 转换 / 播放 / 截图 / 导出）
        #   row 3  aux_frame —— 设置项（3D模式 / 倍速 / 音量）
        # 转换进度面板与视差栏顺延到 row 4 / row 5。
        # ============================================================
        self.btn_frame = tk.Frame(self.root, bg=DARK_BG)
        self.btn_frame.grid(row=2, column=0, sticky="ew")

        self.aux_frame = tk.Frame(self.root, bg=DARK_BG)
        self.aux_frame.grid(row=3, column=0, sticky="ew", padx=10)

        # --- 转换进度控制 ---
        self.conversion_cancel_event = threading.Event()

        # 转换进度面板（默认隐藏，转换时出现）
        self.conversion_frame = tk.Frame(self.root, bg=DARK_BG2)
        self.conversion_frame.grid(row=4, column=0, sticky="ew", padx=10, pady=5)
        self.conversion_frame.grid_remove()  # 初始隐藏

        self.conv_label = tk.Label(
            self.conversion_frame, text="转换中: 0/0 (0%)",
            bg=DARK_BG2, fg=DARK_FG, font=("Microsoft YaHei", 9)
        )
        self.conv_label.pack(side=tk.LEFT, padx=5)

        self.conv_progress_var = tk.DoubleVar(value=0)
        # 【P2 修复】去掉 length=300：下面 pack 用了 fill=X + expand=True，
        # length 会被完全覆盖，留着只会让人误以为进度条是固定宽度。
        conv_progressbar = ttk.Progressbar(
            self.conversion_frame,
            variable=self.conv_progress_var,
            maximum=100,
        )
        conv_progressbar.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        self.btn_cancel_convert = tk.Button(
            self.conversion_frame,
            text="取消转换",
            command=self.cancel_conversion,
            **BTN_DANGER,
        )
        self.btn_cancel_convert.pack(side=tk.LEFT, padx=5)

        # ---------- row 2：主操作 ----------
        # 【P2 修复】btn_open / btn_screenshot / btn_export 从来只被赋值、
        # 没有任何地方引用它们，改为局部变量（Tk 控件由父容器的 children
        # 持有，不会被回收）。真正需要保留引用的只有后面要改文案/状态的几个。
        tk.Button(self.btn_frame, text="打开已有3D视频",
                  command=self.open_video, **BTN_NORMAL).pack(side=tk.LEFT, padx=5, pady=10)

        self.btn_convert = tk.Button(self.btn_frame, text="导入2D视频一键转3D",
                                     command=self.start_conversion, **BTN_PRIMARY)
        self.btn_convert.pack(side=tk.LEFT, padx=5, pady=10)

        self.btn_play = tk.Button(self.btn_frame, text="播放", command=self.toggle_play, **BTN_NORMAL)
        self.btn_play.pack(side=tk.LEFT, padx=5, pady=10)

        tk.Button(self.btn_frame, text="截图",
                  command=self.save_screenshot, **BTN_NORMAL).pack(side=tk.LEFT, padx=5, pady=10)

        tk.Button(self.btn_frame, text="导出3D视频",
                  command=self.export_3d_video, **BTN_SUCCESS).pack(side=tk.LEFT, padx=5, pady=10)

        # 【第 11 轮·新】快捷键提示入口。放最右边，不挤占主操作区
        # （这一行本来就偏满，见 P2 记录里关于底栏拥挤的说明）。
        tk.Button(self.btn_frame, text="快捷键 (F1)",
                  command=self.show_shortcuts,
                  **BTN_NORMAL).pack(side=tk.RIGHT, padx=5, pady=10)

        # ---------- row 3：设置项 ----------
        tk.Label(self.aux_frame, text="3D模式:", bg=DARK_BG, fg=DARK_FG).pack(side=tk.LEFT, padx=(5, 5))
        mode_combo = ttk.Combobox(
            self.aux_frame,
            textvariable=self.mode_var,
            values=["2D", "红蓝3D", "偏振-隔行", "偏振-隔列", "偏振-棋盘"],
            width=10,
            state="readonly"
        )
        mode_combo.pack(side=tk.LEFT)
        mode_combo.bind("<<ComboboxSelected>>", self.change_display_mode)

        tk.Label(self.aux_frame, text="倍速:", bg=DARK_BG, fg=DARK_FG).pack(side=tk.LEFT, padx=(15, 5))
        self.speed_var = tk.StringVar(value="1.0x")
        speed_combo = ttk.Combobox(self.aux_frame, textvariable=self.speed_var,
                                   values=["0.5x", "1.0x", "1.5x", "2.0x"], width=5, state="readonly")
        speed_combo.pack(side=tk.LEFT)
        speed_combo.bind("<<ComboboxSelected>>", self.change_speed)

        # 音量控制
        tk.Label(self.aux_frame, text="音量:", bg=DARK_BG, fg=DARK_FG).pack(side=tk.LEFT, padx=(15, 5))
        self.volume_var = tk.DoubleVar(value=1.0)
        volume_scale = ttk.Scale(
            self.aux_frame, from_=0.0, to=1.0,
            variable=self.volume_var, orient=tk.HORIZONTAL,
            length=80, command=self.change_volume
        )
        volume_scale.pack(side=tk.LEFT)

        self.btn_mute = tk.Button(self.aux_frame, text="🔊", command=self.toggle_mute, **BTN_NORMAL)
        self.btn_mute.pack(side=tk.LEFT, padx=5)

        # --- 视差与左右眼控制栏（仅 3D 模式有意义，见 _sync_parallax_visibility）---
        self.parallax_frame = tk.Frame(self.root, bg=DARK_BG)
        self.parallax_frame.grid(row=5, column=0, sticky="ew", padx=10, pady=2)

        self.parallax_label = tk.Label(self.parallax_frame, text="视差: 0", bg=DARK_BG, fg=DARK_FG)
        self.parallax_label.pack(side=tk.LEFT)

        self.parallax_var = tk.DoubleVar(value=0)
        ttk.Scale(
            self.parallax_frame, from_=-15, to=15,
            variable=self.parallax_var, orient=tk.HORIZONTAL,
            length=200, command=self.change_parallax
        ).pack(side=tk.LEFT, padx=5)

        self.btn_swap_eyes = tk.Button(
            self.parallax_frame, text="交换左右眼",
            command=self.toggle_swap_eyes, **BTN_NORMAL,
        )
        self.btn_swap_eyes.pack(side=tk.LEFT, padx=5)

        # 【第 11 轮·修正】这几条全局键一律用 **bind_all**，不用 root.bind。
        #
        # 为什么必须改：Tk 的 widget 绑定只在"事件投递到的那个控件就是 root
        # 自己"时才触发。用户点过按钮/滑块/下拉框之后，焦点就留在那个子控件上，
        # 事件投给子控件 → root 的绑定**根本不执行** → 所有全局快捷键集体失灵
        # （空格早在第 1 轮就有这个毛病，只是一直没人点到按钮后再按空格）。
        # bind_all 注册在 bindtags 最后一级 "all" 上，任何控件的事件都会轮到它。
        #
        # 代价与对策：处理器在子控件之后执行，所以"抢键"造成的双重动作无法靠
        # 返回 "break" 撤销，只能在处理器**内部**判断该不该让路 —— 判据集中在
        # 下面的 KEY_YIELD_RULES（逐键），改造前那套"清单"就是在这里失效的。
        self.root.bind_all("<space>", self._on_space_key)
        self.root.bind_all("<F11>", self.toggle_fullscreen)

        # 【第 11 轮·修正 2】把"空格=播放/暂停"从按钮手里彻底拿回来。
        #
        # 背景：Tk 的 Button 类默认把 `<space>` 绑到"激活这个按钮"上；而按钮被
        # 鼠标点过之后焦点就留在它身上（Tk 的按钮类绑定会 `focus` 自己）。
        # 于是"点一下打开视频 → 按空格想播放"变成了**重新弹出文件选择框** ——
        # 用户看到的就是"空格没反应"。实测确认过：焦点在按钮上按空格，按钮的
        # command 会真的执行（复现脚本还因此卡在模态文件框上）。
        #
        # 为什么不用"让路"那套：让路只能让我们的处理器**不执行**，撤不掉按钮已经
        # 发生的动作（bind_all 是 bindtags 最后一级）。所以要换掉类绑定 —— 放在
        # class 这一级，**在 "all" 之前**执行，直接做事并返回 "break"，
        # 按钮那条原生绑定根本不会跑，双重动作从源头消失。
        #
        # 代价：键盘用户不能再"Tab 到按钮再按空格激活"。本程序是鼠标驱动的播放器，
        # 而空格=播放/暂停的一致性是用户第一直觉，这个取舍划算。
        # （回车键仍然能激活按钮 —— Tk 给 Button 还绑了 <Return>，没有动它。）
        for _cls in ("Button", "TButton"):
            self.root.bind_class(_cls, "<space>", self._on_button_space)

        # 【第 11 轮·新】键盘快捷键。原来只有空格（播放/暂停）和 F11（全屏），
        # 连最基本的"逐帧看"、"调音量"都得去摸鼠标。这里逐个 keysym 绑到同一个
        # 入口：一是好维护，二是"焦点冲突"只需要在一处判断（见 _on_key_shortcut）。
        for seq in ("<Left>", "<Right>", "<Up>", "<Down>",
                    "<Key-m>", "<Key-f>", "<Key-j>", "<Key-k>", "<Key-l>"):
            self.root.bind_all(seq, self._on_key_shortcut)
        self.root.bind_all("<F1>", self.show_shortcuts)

        # 【第 11 轮·修正 2】窗口重新拿到焦点时兜一道底。
        # Tk 的键盘事件只交给"当前焦点窗口"，**焦点窗口为空时按键全部被丢弃**
        # （连 bind_all 都收不到）。窗口刚创建、或被别的窗口抢走再还回来时，
        # Tk 内部的焦点窗口可能是空的 —— 那就会表现成"所有快捷键都没反应"。
        # 这时把它落到主窗口上即可；已经在某个控件上就不动（别打断输入框）。
        self.root.bind("<FocusIn>", self._ensure_keyboard_focus)

        # 【P2 修复】缩放重绘只由 video_container 的 <Configure> 驱动
        # （见 on_window_resize 的说明），不再重复绑 root。

        # --- 音频播放状态 ---
        self.audio_player = None  # ffpyplayer 的 MediaPlayer 实例
        self.audio_volume = 1.0  # 音量 0.0 ~ 1.0
        self.audio_muted = False  # 是否静音
        self.audio_failed = False  # 【P1】音频初始化失败后不再每帧重建
        # 【第 11 条修复】音频主时钟的基准：
        #   _audio_progress_ref —— 音频时钟的参考读数（None = 尚未校准）
        #   _audio_clock_base   —— 校准时视频所在位置（秒），主时钟以此为起点
        #   _audio_started_at   —— 播放器创建时刻，用于跳过"过早读 pts 会崩"的窗口
        #   _audio_calibrated   —— 是否已"见过时钟真的推进"（见文件顶部第 5 条）；
        #                          为 False 时绝不用它同步，只盲调度
        #   _audio_clock_dead   —— 本播放器实例的时钟已判定不可用，永不再采信
        #   _audio_last_pts / _audio_frozen_since —— 死时钟检测（读数不动的计时）
        self._audio_progress_ref = None
        self._audio_clock_base = 0.0
        self._audio_started_at = 0.0
        self._audio_calibrated = False
        self._audio_clock_dead = False
        self._audio_last_pts = None
        self._audio_frozen_since = None
        # 无音轨文件的墙钟主时钟基准：(墙钟时刻, 当时媒体位置)，None = 需要重新对表
        # （见 _silent_clock_media_seconds）
        self._silent_clock_base = None

        # 【本次修复】后台音频交接的状态（见 _begin_audio_handoff）：
        #   _audio_handoff_thread   —— 正在"关掉旧播放器"的工作线程（None = 没有）
        #   _audio_handoff_old      —— **还没交给工作线程**的那个旧播放器。它多活
        #                              这几百毫秒是有用途的：只要它还占着一条 SDL
        #                              混音通道，混音器就不会被销毁，于是可以对
        #                              新播放器安全地调 set_volume（见 _apply_volume_now）。
        #                              新播放器的音频一确认在跑，就把它交出去关掉
        #                              （_poll_audio_handoff_ready）。
        #   _audio_handoff_started  —— 本次交接的开始时刻（给上面那步兜底超时用）
        #   _audio_handoff_pending  —— 交接进行中：新播放器的音频还没被确认，
        #                              墙钟主时钟要接管画面（见 _silent_clock_media_seconds）
        #   _audio_handoff_poll_id  —— 取回"关旧播放器"结果的定时器
        #   _audio_handoff_seq      —— 代次：换文件/关窗时 +1
        self._audio_handoff_thread = None
        self._audio_handoff_old = None
        self._audio_handoff_started = 0.0
        self._audio_handoff_pending = False
        self._audio_handoff_seq = 0
        self._audio_handoff_poll_id = None
        # 推迟音量同步的重试时刻（见 _apply_volume_now：没确认"音频真的在跑"就不调
        # set_volume，隔 AUDIO_VOLUME_SYNC_DELAY 再试）
        self._audio_volume_sync_at = 0.0
        # 【第 11 轮修复】"音频是为拖动而挂起的，还没被交给新播放器"。
        # 拖动时 _suspend_audio_for_seek 会把音频 set_pause(True)，这段时间它的
        # pts 是冻住的 —— **不能**拿它去判"死时钟"（否则连续快速拖动会被误判成
        # "这个文件没有音轨"，见 _audio_clock_media_seconds 的提前返回）。
        # 音频一被重建/恢复就解除（_mark_audio_live）。
        self._audio_suspended_for_seek = False
        # 【本次修复】"抢发音量"窗口：交接刚开始的那几百毫秒里，旧播放器还占着
        # SDL 混音通道（所以混音器必然有效），可以放心地对新播放器反复调
        # set_volume —— 于是音量能在它出声**之前**就位。没有这段窗口的话，新播放器
        # 会先按默认满音量响 ~0.3 秒（=用户看到的"拖完进度条声音先变大"）。
        # 什么时候开、什么时候关，见 _begin_audio_handoff / _poll_audio_handoff_ready。
        self._audio_volume_resume = False
        self._audio_volume_resume_timer = None
        # 主线程每渲染一帧就发布一次"当前视频位置（秒）"，工作线程建新播放器时
        # 用它做 ss —— 交接要 0.5 秒，期间视频还在往前走，用请求时的旧位置
        # 会让新音频一开始就落后半秒（口型对不上）。
        self._last_media_sec = 0.0

        # 【P1】本次会话中"由本程序转换生成"的 3D 文件路径。
        # 导出按钮只认这个字段，不再靠"工作目录里碰巧有个同名文件"来判断，
        # 避免用【打开已有3D视频】载入别的文件后导出上一次的残留产物。
        self.session_output_path = None
        # 【第 11 轮·新】上一次转换产物有没有成功补上音轨（给完成提示用，见
        # _mux_audio_into_video —— cv2.VideoWriter 不写音轨，转换收尾要补一次）
        self._last_output_has_audio = False

        # 音频对齐相关：见 _restart_audio_at() 的说明。
        # 本构建的 ffpyplayer 只能靠"重建播放器 + ss"定位音频，
        # 且暂停态不能 seek，因此不需要额外状态。

        # 【P2 修复】最初的显示模式是 2D，此时视差栏没有任何作用，
        # 直接隐藏（切到 3D 模式时会自动出现，见 _sync_parallax_visibility）。
        self._sync_parallax_visibility()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # 【P2 修复】只由 video_container 驱动缩放重绘，靠这个记录上一次的
        # 容器尺寸，避免同一尺寸的重复 Configure 触发无谓的重绘。
        self._last_container_size = None

        # 【P2 修复】模糊背景缓存（见 BG_REFRESH_INTERVAL）
        self._bg_cache = None
        self._bg_cache_size = None
        self._bg_age = 0

    # ============================================================
    # 【P2 修复】播放状态的两个"视图"属性
    # ------------------------------------------------------------
    # 状态本体只有 self.play_state 一个（见 PlayState）。
    # 这里保留 is_playing / is_paused 两个只读属性，是为了让读取处
    # 保持自然，同时杜绝"两个布尔量各改各的"造成的非法组合。
    # 给它们赋值会抛 AttributeError —— 这正是想要的：改状态请用 play_state。
    # ============================================================
    @property
    def is_playing(self):
        return self.play_state == PlayState.PLAYING

    @property
    def is_paused(self):
        return self.play_state == PlayState.PAUSED

    # ============================================================
    # 窗口尺寸变化 → 重新渲染当前帧（等比缩放填满 video_label）
    # ============================================================
    def on_window_resize(self, event=None):
        """容器尺寸变化 → 重新渲染当前帧（等比缩放填满）。

        【P2 修复】原来在 root 和 video_container 上各绑了一次 <Configure>：
          - Tk 的 bindtags 会把子控件的 Configure 冒泡到 toplevel，所以绑在
            root 上必须靠 event.widget 过滤才不误触发，逻辑正确但很脆弱；
          - 同一次拖拽会经由两条链各走一遍，反复切换 is_resizing。
        现在只由 video_container 驱动 —— 它是第 0 行（唯一带 weight 的行）里
        sticky=nsew 的容器，窗口缩放、底栏显隐、全屏进出都必然改变它的尺寸，
        事件一个都不会漏，也就不再需要按 event.widget 过滤。
        """
        now = (self.video_container.winfo_width(), self.video_container.winfo_height())
        if now == self._last_container_size:
            return
        self._last_container_size = now

        # 尺寸变化过程中用"黑底快渲染"，避免每一步都做一次昂贵的模糊背景
        self.is_resizing = True
        if self.current_frame_image is not None:
            self.render_current_frame(fast=True)

        # 防抖：底栏刚隐藏/显示时，容器尺寸可能要连续触发几次 Configure
        if self.resize_after_id is not None:
            self.root.after_cancel(self.resize_after_id)
        self.resize_after_id = self.root.after(60, self._finish_resize)

    def _finish_resize(self):
        self.resize_after_id = None
        self.is_resizing = False
        self.render_current_frame()

    def render_current_frame(self, fast=None):
        """
        始终完整显示视频（min 缩放），四周用模糊背景铺满，
        保证 video_container 的任何区域都不会留白。

        【P0 修复】这里原来有一句 `self.resize_after_id = None`：
        它只丢掉定时器 ID、并不取消那个还活着的 after 回调，属于
        渲染函数越权修改防抖状态。后果是 on_window_resize 之后再也
        取消不掉它，防抖回调会重复排队、多渲染若干次。渲染函数
        只负责画，不再碰任何定时器状态。
        """
        if fast is None:
            fast = self.is_resizing

        frame = self.current_frame_image
        if frame is None:
            return

        # 读取 video_container 的实际尺寸（它由 grid 第 0 行决定）
        win_w = self.video_container.winfo_width()
        win_h = self.video_container.winfo_height()
        if win_w <= 1 or win_h <= 1:
            win_w = self.video_label.winfo_width()
            win_h = self.video_label.winfo_height()
        if win_w <= 1 or win_h <= 1:
            win_w = self.root.winfo_width()
            win_h = self.root.winfo_height() - 100
        if win_w <= 1 or win_h <= 1:
            win_w, win_h = 850, 520

        h, w = frame.shape[:2]

        # 前景：min 缩放，完整显示
        fg_scale = min(win_w / w, win_h / h)
        fg_w = max(1, int(w * fg_scale))
        fg_h = max(1, int(h * fg_scale))
        interp = cv2.INTER_AREA if fg_scale < 1.0 else cv2.INTER_LINEAR
        fg = cv2.resize(frame, (fg_w, fg_h), interpolation=interp)

        if fg_w == win_w and fg_h == win_h:
            # 刚好铺满，直接用
            canvas = fg
        else:
            # 需要填充背景
            if fast:
                # 拖动态：黑背景
                canvas = np.zeros((win_h, win_w, 3), dtype=np.uint8)
            else:
                # 稳定态：模糊背景铺满整个窗口
                # 【P2 修复】这层背景走的是缓存（见 _get_letterbox_bg）。
                # 必须 .copy()：下面要把前景"贴"进 canvas，直接写缓存会把
                # 当前帧的前景烙进背景里，下一次复用就露出上一帧的影子。
                canvas = self._get_letterbox_bg(frame, win_w, win_h).copy()

            # 把前景居中贴上去
            x0 = (win_w - fg_w) // 2
            y0 = (win_h - fg_h) // 2
            canvas[y0:y0 + fg_h, x0:x0 + fg_w] = fg

        cv2image = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGBA)
        imgtk = ImageTk.PhotoImage(image=Image.fromarray(cv2image))
        self.video_label.imgtk = imgtk
        self.video_label.config(image=imgtk)

    def _get_letterbox_bg(self, frame, win_w, win_h):
        """生成"铺满整个窗口的模糊暗背景"（带缓存）。

        【P2 修复】原来每渲染一帧就要：把整幅放大到 cover 尺寸（可能比窗口
        还大）→ 降采样 → 高斯模糊 → 再放大回窗口大小 → 整体压暗。视频每帧
        都在变，但这层背景是 0.45 倍压暗 + 强模糊的陪衬，肉眼几乎看不出
        逐帧差异，所以按 BG_REFRESH_INTERVAL 帧重算一次、其余帧复用即可。

        缓存以窗口尺寸为键：窗口一变立刻重算（否则会在旧尺寸的图上取错区域）。
        返回的数组归缓存所有，调用方不要再往里写（需要写请先 copy）。
        """
        size = (win_w, win_h)
        if self._bg_cache_size != size:
            self._bg_cache_size = size
            self._bg_cache = None
            self._bg_age = 0        # 尺寸变了：立刻重算

        if self._bg_cache is None or self._bg_age >= BG_REFRESH_INTERVAL:
            h, w = frame.shape[:2]
            bg_scale = max(win_w / w, win_h / h)
            bg_w = max(1, int(w * bg_scale))
            bg_h = max(1, int(h * bg_scale))
            bg = cv2.resize(frame, (bg_w, bg_h), interpolation=cv2.INTER_LINEAR)

            # 居中裁剪到窗口大小
            bx = max(0, (bg_w - win_w) // 2)
            by = max(0, (bg_h - win_h) // 2)
            bg = bg[by:by + win_h, bx:bx + win_w]

            # 降采样模糊，提高性能
            small_w = max(1, win_w // 8)
            small_h = max(1, win_h // 8)
            bg_small = cv2.resize(bg, (small_w, small_h), interpolation=cv2.INTER_AREA)
            bg_small = cv2.GaussianBlur(bg_small, (0, 0), sigmaX=3, sigmaY=3)
            bg = cv2.resize(bg_small, (win_w, win_h), interpolation=cv2.INTER_LINEAR)
            bg = cv2.convertScaleAbs(bg, alpha=0.45, beta=0)

            # 保证与窗口同尺寸（浮点舍入可能差 1 像素）
            if bg.shape[0] != win_h or bg.shape[1] != win_w:
                bg = cv2.resize(bg, (win_w, win_h), interpolation=cv2.INTER_LINEAR)
            self._bg_cache = bg
            self._bg_age = 0
        else:
            self._bg_age += 1

        return self._bg_cache

    def open_video(self, file_path=None):
        if file_path is None:
            file_path = filedialog.askopenfilename(filetypes=[("MP4 Video", "*.mp4"), ("All Files", "*.*")])
            if not file_path: return

        # 停止旧视频的播放循环，防止定时器叠加导致加速
        # 【P2 修复】原来这里是手写两行（is_playing=False + 取消定时器），
        # 与 on_close 里的收尾逻辑重复，现统一走 _stop_playback_loop()
        self._stop_playback_loop()

        self.current_video_path = file_path
        if self.cap is not None: self.cap.release()

        self.cap = cv2.VideoCapture(file_path)
        if not self.cap.isOpened():
            messagebox.showerror("错误", "无法打开视频文件")
            return

        # 释放旧的音频播放器
        # 【本次修复】换文件前必须先作废正在进行的后台交接：那次交接可能还有
        # 一个旧实例等着被关（_audio_handoff_old）或正在工作线程里关，
        # 不作废的话下面 _close_audio_player() 会把当前播放器也关掉，
        # 而交接随后又会去关它自己那个 —— 两个 SDL 音频设备拆装顺序错乱。
        # _cancel_audio_handoff() 会把在飞的旧实例一并处理掉，之后这里
        # 关掉的才是"当前这一个"。
        self._cancel_audio_handoff()
        self._close_audio_player()

        # 【P1】播放状态先就位：音频播放器必须按"当前速率/暂停/静音"来创建，
        # 否则换速率后重新打开文件又会退回 1.0x。
        # 【P2 修复】状态收敛为单一枚举（见 PlayState）。
        self.play_state = PlayState.PLAYING
        self.is_seeking = False
        # 【P1 修复 2】换文件时清掉与上一个文件相关的跳转/重建状态
        self._cancel_seek_timer()
        self._cancel_audio_restart_timer()
        self._pending_audio_pos = None
        self._last_jump_frame = None
        self._scale_pressed = False
        self._silent_clock_base = None      # 换了文件，墙钟主时钟重新对表
        self._last_media_sec = 0.0          # 音频交接用的"当前视频位置"也归零
        self.audio_failed = False  # 换文件后允许重试一次音频
        # 【第 11 轮修复】换了文件，"为拖动挂起"的豁免不能跨文件残留
        self._mark_audio_live()
        self.btn_play.config(text="暂停")

        # 创建新的音频播放器（与视频同一文件，从 0 秒开始）
        self.audio_player = self._create_audio_player(file_path, 0.0)

        fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.video_fps = fps if fps > 0 else 30.0

        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.scale.config(to=self.total_frames)
        self.seek_var.set(0)

        self.update_frame()

        # 【第 11 轮·修正 2】刚选完文件的用户，下一个动作几乎一定是播放/暂停。
        # 而此刻焦点还留在"打开视频"那个按钮上 —— 空格会被按钮吃掉（重新打开
        # 文件对话框），"按空格没反应"的观感就是这么来的。这里主动把焦点收回来。
        self._restore_shortcut_focus()

    def start_conversion(self):
        if self.is_converting:
            messagebox.showwarning("提示", "正在转换中，请稍候...")
            return

        file_path = filedialog.askopenfilename(
            filetypes=[("MP4 Video", "*.mp4"), ("All Files", "*.*")]
        )
        if not file_path:
            return

        self.is_converting = True
        self.conversion_cancel_event.clear()

        # 显示转换进度面板
        self.conversion_frame.grid()
        self.conv_label.config(text="正在加载模型，请稍候...")
        self.conv_progress_var.set(0)
        self.btn_cancel_convert.config(text="取消转换", state=tk.NORMAL)

        self.btn_convert.config(text="转换中...", state=tk.DISABLED)

        # 【P2 修复】留下线程引用，关窗时给它一点时间去删掉写到一半的临时文件
        self.conversion_thread = threading.Thread(
            target=self.process_video_to_3d, args=(file_path,)
        )
        self.conversion_thread.daemon = True
        self.conversion_thread.start()

    @staticmethod
    def _make_temp_output_path():
        """为本次转换生成一个系统临时目录下的唯一输出路径。

        【P1 修复】原来固定写工作目录下的 "temp_3d_output.mp4"：
          - 从任何目录启动都往那儿写，目录不可写就直接失败；
          - 多次转换互相覆盖；
          - "导出"只能靠同名文件存在与否来判断，无法确定它是不是本次产物。
        """
        name = "ai3d_%s.mp4" % uuid.uuid4().hex[:10]
        return os.path.join(tempfile.gettempdir(), name)

    def process_video_to_3d(self, input_path):
        output_path = self._make_temp_output_path()
        cap = None
        out = None
        cancelled = False
        try:
            if self.model is None:
                print("正在加载深度估计模型...")
                model_id = "depth-anything/Depth-Anything-V2-Small-hf"
                self.image_processor = AutoImageProcessor.from_pretrained(model_id)
                self.model = AutoModelForDepthEstimation.from_pretrained(model_id).to(self.device)
                print("模型加载完成！")

            cap = cv2.VideoCapture(input_path)
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            out = cv2.VideoWriter(output_path, fourcc, fps, (width * 2, height))

            max_disparity = 20
            x_base, y_base = np.meshgrid(np.arange(width), np.arange(height))
            x_base, y_base = x_base.astype(np.float32), y_base.astype(np.float32)

            frame_count = 0
            start_time = time.time()

            while cap.isOpened():
                # 【取消检查】每帧都检查一次取消标志
                if self.conversion_cancel_event.is_set():
                    cancelled = True
                    print("用户取消了转换")
                    break

                ret, frame = cap.read()
                if not ret:
                    break
                frame_count += 1

                # --- 深度估计 ---
                image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image_pil = Image.fromarray(image)
                inputs = self.image_processor(images=image_pil, return_tensors="pt").to(self.device)
                with torch.no_grad():
                    outputs = self.model(**inputs)
                    predicted_depth = outputs.predicted_depth

                prediction = torch.nn.functional.interpolate(
                    predicted_depth.unsqueeze(1), size=image_pil.size[::-1],
                    mode="bicubic", align_corners=False
                )
                depth_output = prediction.squeeze().cpu().numpy()
                # 【P0 修复】原来直接用 np.max(depth_output) 当除数：
                # 纯色/全黑等"平坦帧"的最大深度为 0，除零得到 NaN，
                # astype("uint8") 之后就是一张垃圾图，连镜像都会被污染。
                depth_output = np.nan_to_num(depth_output, nan=0.0,
                                             posinf=0.0, neginf=0.0)
                d_max = float(np.max(depth_output)) if depth_output.size else 0.0
                if d_max > 1e-6:
                    depth_map = (depth_output * 255.0 / d_max).astype("uint8")
                else:
                    depth_map = np.zeros(depth_output.shape, dtype="uint8")

                depth_normalized = depth_map.astype(np.float32) / 255.0
                shift = depth_normalized * max_disparity
                left_eye = cv2.remap(frame, x_base + shift, y_base,
                                     cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
                right_eye = cv2.remap(frame, x_base - shift, y_base,
                                      cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

                sbs_frame = np.hstack((left_eye, right_eye))
                out.write(sbs_frame)

                # 【进度更新】每 5 帧回传一次，避免频繁调用 root.after
                if frame_count % 5 == 0 or frame_count == total_frames:
                    self.root.after(
                        0, self._update_conversion_progress,
                        frame_count, total_frames, start_time
                    )

            cap.release()
            cap = None
            out.release()
            out = None

            if cancelled:
                # 删除写到一半的临时文件
                self._cleanup_partial_output(output_path)
                self.root.after(0, self._on_conversion_cancelled)
            else:
                # 【第 11 轮·新】把原视频的音轨补回产物。
                # cv2.VideoWriter 只写视频流 → 不补的话用户会得到一段无声的 3D 视频。
                # 失败不影响转换结果（拿到的仍是能播的无声文件），所以不看返回值
                # 决定成败，只记下来给完成提示用。
                self._last_output_has_audio = self._mux_audio_into_video(
                    output_path, input_path)
                self.root.after(0, self.on_conversion_done, output_path)

        except Exception as e:
            print(f"转换出错: {e}")
            if cap is not None:
                cap.release()
            if out is not None:
                out.release()
            if cancelled:
                self._cleanup_partial_output(output_path)
                self.root.after(0, self._on_conversion_cancelled)
            else:
                # 【P2 修复】原来这里写的是
                #     self.root.after(0, lambda: messagebox.showerror("转换错误", str(e)))
                # Python 会在 except 块结束时删除变量 e，而这个 lambda 是"稍后"
                # 才由事件循环调用的 —— 那时 e 已经不存在，直接抛 NameError，
                # 于是错误提示框根本弹不出来，用户只看到转换莫名结束
                # （控制台里留一条 "Exception in Tkinter callback"）。
                # 现在先把消息取出来，闭包只捕获这个普通局部变量。
                err_msg = str(e)
                self.root.after(0, lambda: messagebox.showerror("转换错误", err_msg))
                self.root.after(0, self._reset_conversion_ui)

    @staticmethod
    def _cleanup_partial_output(output_path):
        """删掉写到一半的转换产物（用户取消 / 转换报错时）。

        【P2 修复】原来"取消"分支和"报错"分支各抄了一份 try/remove，
        且报错分支的删除是静默的。现在收敛成一个出口，失败也照样打印原因。
        """
        if not output_path:
            return
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except Exception as e:
            print(f"删除临时文件失败: {e}")

    def _mux_audio_into_video(self, video_path, audio_source_path):
        """把 `audio_source_path` 的音轨并入 `video_path`（**原地替换**）。

        【第 11 轮·新】`cv2.VideoWriter` 只写视频流，转换产物天生没有音轨
        （症状："视频转成 3D 之后就没声音了"）。这里在转换收尾补一次音轨。

        返回 True = 音轨已并进去；返回 False = 没补（产物仍是可播放的无声文件）。
        下面三种情况都走 False，且**一律不抛异常** —— 补音轨是锦上添花，
        绝不能让它把整个转换搞失败：
          ① 找不到 ffmpeg（多路径查找见模块级 `find_ffmpeg`）；
          ② 源文件自己就没有音轨（没什么可补的）；
          ③ ffmpeg 执行失败或超时。

        实现要点：
          - 视频流 `-c:v copy` 不重编码，只多花一两秒；
          - 音轨统一重编码成 aac（原轨可能是任意格式/采样率，copy 进 mp4 未必合法）；
          - `-shortest` 让音轨跟着视频长度收尾（音频比视频长时不会拖出静音尾巴）；
          - 先写同目录临时文件再 `os.replace` 原子替换 —— 中途失败也不会毁掉
            已经写好的无声产物。
        """
        ffmpeg = find_ffmpeg()
        if ffmpeg is None:
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
            "-c:a", "aac", "-b:a", FFMPEG_AUDIO_BITRATE,
            "-shortest",
            tmp_path,
        ]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  timeout=FFMPEG_MUX_TIMEOUT_SEC)
        except Exception as e:
            print(f"补音轨失败（ffmpeg 调用异常）: {e}")
            _remove_quietly(tmp_path)
            return False

        if (proc.returncode != 0 or not os.path.exists(tmp_path)
                or os.path.getsize(tmp_path) == 0):
            detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
            print(f"补音轨失败（ffmpeg 退出码 {proc.returncode}）: {detail[-400:]}")
            _remove_quietly(tmp_path)
            return False

        try:
            os.replace(tmp_path, video_path)
        except Exception as e:
            print(f"补音轨失败（替换产物出错）: {e}")
            _remove_quietly(tmp_path)
            return False
        return True

    def _update_conversion_progress(self, current, total, start_time):
        """在主线程更新进度条和文字"""
        elapsed = time.time() - start_time

        if total > 0:
            percent = current / total * 100
            self.conv_progress_var.set(percent)

            if current > 0 and elapsed > 0:
                eta = elapsed / current * (total - current)
                eta_str = f"{int(eta // 60)}分{int(eta % 60)}秒"
            else:
                eta_str = "--"

            self.conv_label.config(
                text=f"转换中: {current}/{total} 帧 ({percent:.1f}%) - 预计剩余 {eta_str}"
            )
        else:
            self.conv_label.config(text=f"转换中: 已处理 {current} 帧 - 预计剩余 --")

    def cancel_conversion(self):
        """用户点击取消按钮"""
        if not self.is_converting:
            return

        if not messagebox.askyesno(
                "确认取消",
                "确定要取消当前转换吗？\n已处理的进度将被丢弃。"
        ):
            return

        self.conversion_cancel_event.set()
        self.btn_cancel_convert.config(text="正在取消...", state=tk.DISABLED)
        self.conv_label.config(text="正在取消，请稍候...")

    def _on_conversion_cancelled(self):
        """转换线程已响应取消，恢复 UI"""
        self.conversion_cancel_event.clear()
        self._reset_conversion_ui()
        messagebox.showinfo("已取消", "转换已取消，临时文件已清理。")

    def _reset_conversion_ui(self):
        """恢复转换按钮和进度面板到初始状态"""
        self.is_converting = False
        self.btn_convert.config(text="导入2D视频一键转3D", state=tk.NORMAL)
        self.conversion_frame.grid_remove()
        self.conv_progress_var.set(0)
        self.conv_label.config(text="转换中: 0/0 (0%)")
        self.btn_cancel_convert.config(text="取消转换", state=tk.NORMAL)

    def on_conversion_done(self, output_path):
        self._reset_conversion_ui()

        # 【P1】登记本次转换产物，导出按钮只认它；顺手清掉上一次的临时文件
        previous = self.session_output_path
        self.session_output_path = output_path
        if previous and previous != output_path and os.path.exists(previous):
            try:
                os.remove(previous)
            except Exception:
                pass

        self.open_video(output_path)

        # 【P2 修复】原来这里无条件把模式切成"红蓝3D"，提示语也直接假设
        # 用户有红蓝眼镜 —— 没有眼镜的人会看到一张奇怪的叠影图，还以为
        # 转换坏了。现在改成先问一句：要就切，不要就保持 2D 左右分屏
        # （转换产物本身就是 SBS 格式，2D 下也能正常看）。
        switch_3d = messagebox.askyesno(
            "转换完成",
            "3D 视频已生成（左右分屏格式）。\n\n"
            "%s\n\n"
            "现在切换到【红蓝3D】模式吗？\n"
            "（需要戴红蓝眼镜才能看出立体感；选'否'则保持 2D 左右分屏显示）\n\n"
            "临时文件位置：\n%s\n\n"
            "点【导出3D视频】可另存为正式文件。"
            % ("已保留原视频的音轨。" if self._last_output_has_audio else
               "产物未包含音轨（没找到 ffmpeg，或源视频本身没有声音）。",
               output_path)
        )
        if switch_3d:
            self.display_mode = "红蓝3D"
            self.mode_var.set("红蓝3D")
            self._sync_parallax_visibility()
            if self.current_raw_frame is not None:
                self.current_frame_image = self._process_3d(self.current_raw_frame)
            self.render_current_frame()

    # ============================================================
    # 【关键修复】切换 3D 模式：不再重新 pack 底栏（那样会破坏打包顺序）
    # grid 布局下底栏位置稳定，只需刷新画面即可
    # ============================================================
    def change_display_mode(self, event=None):
        self.display_mode = self.mode_var.get()
        self.root.update_idletasks()
        if self.current_raw_frame is not None:
            self.current_frame_image = self._process_3d(self.current_raw_frame)
        # 【P2 修复】视差/交换左右眼只在 3D 模式下有意义，跟着模式显隐
        self._sync_parallax_visibility()
        self.render_current_frame()

    def _event_target_widget(self, event):
        """判断"这次按键实际是冲着哪个控件去的"，用于决定全局快捷键要不要让路。

        【第 11 轮·修正】以前用 `self.root.focus_get()` 判断。改用 bind_all 之后
        必须换掉，理由有两个：
          1. `focus_get()` 在窗口没有系统焦点时**返回 None**（无人值守跑测试、
             或被别的窗口挡住时就是这样），此时 isinstance 判断恒为 False
             → 该让路的没让路 → 一次按键两个动作。而 `event.widget` 由 Tk 直接
             写明，跟系统焦点无关。
          2. `focus_get()` 给的是"焦点在谁身上"，`event.widget` 给的是"事件发给
             了谁" —— 后者才是我们真正关心的（bind_all 收到的正是同一个事件）。
        取不到 `event.widget` 时（例如直接调用处理器）才退回 focus_get()。
        """
        widget = getattr(event, "widget", None)
        if widget is None:
            widget = self.root.focus_get()
        return widget

    def _ensure_keyboard_focus(self, event=None):
        """窗口重新获得焦点后，若 Tk 的焦点窗口是空的就把它落到主窗口上。

        这不是可有可无的保险：Tk 的键盘事件只交给"当前焦点窗口"，**焦点窗口为空
        时事件被直接丢弃**，`bind_all` 也收不到 —— 表现就是"所有快捷键都没反应"。
        实测过：`tk.call("focus")` 为空串时，`event_generate` 出来的按键纹丝不动。

        只在"空"的时候补，已经落在某个控件上就完全不动 —— 否则会把用户正在
        输入框里打的字打断。
        """
        try:
            if not self.root.tk.call("focus"):
                self.root.focus_set()
        except Exception:
            pass

    def _restore_shortcut_focus(self, event=None):
        """点一下视频画面：把键盘焦点收回主窗口，让全局快捷键重新生效。

        焦点被按钮/滑块/下拉框拿走之后，那些"在该控件上确实有原生含义"的键会
        让路（方向键让给滑块、空格让给按钮）。这是刻意保留的取舍，但用户需要一个
        一眼能想到的恢复动作 —— 点画面最自然，因为他本来就在看画面。
        """
        try:
            self.root.focus_set()
        except Exception:
            pass

    def _combobox_popdown_open(self, widget):
        """这个下拉框的候选列表当前是弹出来的吗？

        用 Tk 自己的 `ttk::combobox::PopdownWindow` 问，不猜。
        （列表从没弹过时这个命令会顺手把 popdown 窗口建出来，`ismapped` 仍是 False。）
        """
        try:
            popdown = widget.tk.call("ttk::combobox::PopdownWindow", widget)
            return bool(widget.tk.call("winfo", "ismapped", popdown))
        except Exception:
            return False

    def _shortcut_should_yield(self, target, key):
        """最终裁决：这个键要不要让给 `target` 自己处理？

        底座是 `should_yield_shortcut()`（那张有序规则表），再加**两个刻意的例外**。
        例外之所以单独放在这里，是因为它们依赖"运行时状态/实例身份"，写不进
        纯类型的规则表。
        """
        if target is None:
            return False

        if target is self.scale and key in _ARROW_KEYS:
            # 例外一：进度条。
            # 它虽然是 ttk.Scale，但语义上是"定位控件"而不是"参数控件"：F1 里写的
            # 就是"←/→ 上一帧、下一帧"。让路给 ttk.Scale 的后果是"滑块只挪一小格，
            # 而且键盘操作不触发 ButtonRelease —— 定位要等 500ms 的防抖定时器才
            # 落地"（实测），既慢又不是逐帧，用户的观感就是"←/→ 没反应"。
            # 音量/视差滑块照旧让路：它们的方向键动作立即且可见。
            return False

        if isinstance(target, ttk.Combobox) and key in _ARROW_KEYS:
            # 例外二：下拉框。分两种状态，因为实测行为不一样：
            #   · 候选列表**开着**：↑/↓/←/→ 都在浏览选项 → 全让。
            #   · 候选列表**关着**：实测只有 ↓ 有真实动作（弹出列表），
            #     ←/→/↑ 改不动它的值（无事发生）。
            #     让路给一个"无事发生"的键 = 按键变哑巴，所以只让 ↓。
            #     这一条直接决定用户会不会觉得"←/→ 没反应"：改完倍速之后焦点
            #     就留在下拉框上，此时 ←/→ 必须归播放器（逐帧步进），
            #     否则要么哑掉、要么**又改一次倍速**（每次改倍速要付 0.5 秒重建）。
            if self._combobox_popdown_open(target):
                return True
            return key == "down"

        return should_yield_shortcut(target, key)

    def _on_button_space(self, event=None):
        """按在按钮上的空格：仍然是播放/暂停，不是"激活这个按钮"。

        这是**替换**掉 Tk 的 Button 类默认绑定（见 __init__ 里的 bind_class），
        运行时机比 bind_all 早一级，所以返回 "break" 能真正阻止按钮被激活。
        """
        self.toggle_play()
        return "break"

    def _on_space_key(self, event=None):
        """全局空格键：播放 / 暂停。

        【P0 修复】先看事件落在哪个控件上：按钮/复选/单选/输入框/列表这些
        "空格有原生含义"的控件要让路，交给它们自己处理，避免"按钮被激活 +
        播放状态被切换"的双重动作。
        让路与否**逐键**判断，依据是 `_shortcut_should_yield()`（规则表 + 两个例外）。
        """
        if self._shortcut_should_yield(self._event_target_widget(event), "space"):
            return None
        self.toggle_play()

    def toggle_play(self):
        if self.cap is None:
            return
        # 【P2 修复】状态只有 play_state 一个本体（见 PlayState），
        # 播放/暂停在这里显式互翻，不再修改两个布尔量。
        self.play_state = PlayState.PAUSED if self.is_playing else PlayState.PLAYING
        self.btn_play.config(text="继续" if self.is_paused else "暂停")

        # 同步音频
        if self.is_paused:
            if self.audio_player is not None:
                try:
                    self.audio_player.set_pause(True)
                except Exception:
                    pass
        else:
            # 【P1 修复 2】恢复播放：把暂停期间挂起的音频对齐兑现掉。
            # 暂停时拖动/切倍速只登记了目标位置、没有重建（重建要 0.5 秒，
            # 而且暂停态听不到声音，纯属白卡），这里补做一次。
            # 重建出来的新播放器本身就是播放态，无需再 set_pause(False)。
            if not self._flush_pending_audio_restart():
                if self.audio_player is not None:
                    try:
                        self.audio_player.set_pause(False)
                    except Exception:
                        pass
            # 【第 11 轮修复】恢复播放 = 音频重新活过来，解除"为拖动挂起"的
            # 死时钟豁免（暂停态拖动会把音频压住并留下这个标记）。
            self._mark_audio_live()
            # 暂停期间的时间不能算进墙钟主时钟的账（否则一恢复就会被判成
            # "落后一大截"而狂丢帧），所以重新对表。
            self._silent_clock_base = None
            self.update_frame()

    def change_speed(self, event):
        """切换倍速。

        【P1 修复】视频的倍速是靠"缩短帧间隔"实现的，音频走的是另一条
        ffpyplayer 线程、原本恒为 1.0x，于是变速即音画错位。
        这里同步把音频也换到同一速率（ffmpeg atempo 滤镜，只能在创建时
        指定，所以是重建播放器），并从当前视频位置接续。
        """
        new_speed = float(self.speed_var.get().replace("x", ""))
        if new_speed == self.playback_speed:
            return
        self.playback_speed = new_speed
        self._silent_clock_base = None      # 帧间隔变了，墙钟主时钟重新对表

        if not self.current_video_path:
            return
        # 【P1 修复 2】暂停态切倍速不必立刻付 0.5 秒的重建代价 ——
        # 登记位置，恢复播放时按新速率重建即可。
        if self.is_paused:
            self._pending_audio_pos = self._current_video_seconds()
            return
        self._restart_audio_at(self._current_video_seconds())

    # ============================================================
    # 【第 11 轮·新】键盘快捷键
    # ------------------------------------------------------------
    # 原来只有空格（播放/暂停）与 F11（全屏）。补上播放器最基本的那几个：
    #   ← / →      单帧后退 / 前进
    #   ↑ / ↓      音量 ±5%
    #   J / L      快退 / 快进 10 秒
    #   K          播放/暂停（和 J/L 配套，Vim 习惯）
    #   M          静音开关
    #   F          全屏开关（F11 同义）
    #   F1         在界面上列出这些快捷键
    # ============================================================
    def _on_key_shortcut(self, event=None):
        """新增快捷键的统一入口。

        纪律：**焦点落在"这个键在该控件上真的有原生含义"时才放行**。
        判断是**逐键**的 —— 不能说"这个控件是冲突控件"，因为一个控件可能只吃
        方向键而不吃字母键（只读下拉框就是：↓ 弹列表，但空格和 M/J/K/L/F 它一概不碰）。

        判据集中在 `should_yield_shortcut()` / `KEY_YIELD_RULES`（实测得出）。
        逐键判断这一点是被用户报的 bug 教出来的：原来靠一张清单走天下，
        `ARROW_CONFLICT_WIDGETS` 把 Button / Combobox 都算进去了，于是**点过任意
        按钮或下拉框之后，←/→/J/L/M/F 全部失灵**（用户原话："快捷键无反应"）。

        返回 "break"（实测确认过）：bind_all 是 bindtags 的最后一级，返回
        "break" 不会影响任何控件自身的处理（那一步早就做完了），但它会中止
        "all" 这个标签上排在我们后面的其它绑定。真正防住双重动作的是上面那次
        让路判断 —— 别让"步进一帧"和"滑块自己动一下"同时发生。
        """
        if self.cap is None:
            return None

        key = (event.keysym or "").lower()
        if key not in _ALL_SHORTCUT_KEYS or key == "space":
            return None

        target = self._event_target_widget(event)
        if self._shortcut_should_yield(target, key):
            return None

        if key in ("left", "right"):
            self.step_frame(-1 if key == "left" else 1)
        elif key in ("up", "down"):
            self.step_volume(VOLUME_KEY_STEP if key == "up" else -VOLUME_KEY_STEP)
        elif key == "m":
            self.toggle_mute()
        elif key == "f":
            self.toggle_fullscreen()
        elif key == "j":
            self.nudge_position(-SHORTCUT_SEEK_STEP_SEC)
        elif key == "l":
            self.nudge_position(SHORTCUT_SEEK_STEP_SEC)
        elif key == "k":
            self.toggle_play()
        else:
            return None
        return "break"

    def _rebase_master_clock(self):
        """把主时钟的零点挪到"当前的视频位置"。

        手动改变播放位置（逐帧步进）之后必须调用，否则同步逻辑会在下一帧把
        画面拽回原处 —— 看起来就是"按键没反应"：主时钟的零点还锚在跳转前的
        位置，它算出的目标帧仍是旧的，而同步只会向前追（grab/丢帧），不会回头。

        实现上只是把参考读数清空：下一次 `_audio_clock_media_seconds` 会用
        "此刻的视频位置"重新建零点（这条路径本来就在，回绕/校准都走它）。
        墙钟（无音轨文件）同理 —— 清掉基准即重新对表。
        """
        self._audio_progress_ref = None
        self._silent_clock_base = None

    def step_frame(self, delta):
        """单帧步进（←/→）。播放态与暂停态都能用。

        暂停态是主要用法（逐帧看 3D 效果的差别）：`cap.set` 之后必须带
        `force=True` 重绘一帧 —— 复用第 8 轮那个修复，暂停态不会因为
        is_playing 为 False 而提前 return。

        几处刻意的取舍：
          - **不做音频重建**。差一帧只有 ~40ms，远小于能察觉的音画错位；
            而本构建重建一次要付 ~0.5 秒（后台交接），连按几下就一直在重建。
            暂停态只把位置**登记**下来（_pending_audio_pos），恢复播放时统一对齐。
          - 播放态下这一帧可能被主时钟同步"吸收"掉（同步只向前追），所以
            想精确看某一帧就暂停后再按 —— 界面上给的提示也是这么写的。
          - 播放态下按 → 的效果等于"立刻把下一帧显示出来"（那一帧本来 40ms 后
            就要出现），按 ← 才是明显的后退一帧；想精确逐帧看就**先暂停**再按，
            界面上给的提示也是这么写的。
          - 位置用 `cap.get(POS_FRAMES)` 读，它给的是**下一帧的序号**（比正在
            显示的那帧大 1 —— 这个坑在跳转那一段的注释里也写过）。
            所以"从当前显示的这一帧走 delta 帧"要写成 `POS_FRAMES - 1 + delta`。
        """
        if self.cap is None or self.total_frames <= 1:
            return
        fps = self.video_fps if self.video_fps > 0 else 25.0
        displayed = int(self.cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1
        target = self._clamp_seek_frame(displayed + delta)
        if target == displayed:
            return

        self._cancel_seek_timer()
        self.is_seeking = False
        self._last_jump_frame = None        # 手动跳过了，作废"已渲染落点"的记录
        self.seek_var.set(target)
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, target)

        if self.is_paused:
            self.update_frame(force=True)
            # 暂停态听不到声音，只要把目标位置登记下来，恢复播放时再重建对齐
            self._request_audio_restart(target / fps)
        else:
            self._rebase_master_clock()
            # 这条路径可能是"焦点在进度条上按 ←/→"来的：ttk.Scale 的类绑定会先
            # 跑 on_scale_move → _suspend_audio_for_seek()（把声音 set_pause 压住），
            # 而我们上面刚把它的防抖定时器取消了 —— finish_seek 不会再来，
            # 挂起就没人负责解除。所以这里自己把音频放回来（很便宜，不重建）。
            self._resume_audio_after_seek_suspend()

    def nudge_position(self, delta_sec):
        """快退/快进若干秒（J/L），走既有的跳转路径。

        直接用 `finish_seek`：夹取范围、暂停态重绘、音频对齐、落点记录
        全都有现成实现，不另起一套（另写一份迟早会和那边不一致）。
        """
        if self.cap is None or self.total_frames <= 1:
            return
        fps = self.video_fps if self.video_fps > 0 else 25.0
        target = self._clamp_seek_frame(self.seek_var.get() + delta_sec * fps)
        self._last_jump_frame = None        # 允许 finish_seek 真的跳这一下
        self.seek_var.set(target)
        self.finish_seek()

    def step_volume(self, delta):
        """↑/↓ 调音量。走 change_volume，保证滑块、静音标记、音量下发三件事一致。"""
        new_value = max(0.0, min(1.0, self.audio_volume + delta))
        if abs(new_value - self.audio_volume) < 1e-9:
            return
        self.volume_var.set(new_value)      # 让滑块跟着动
        self.change_volume(new_value)

    def show_shortcuts(self, event=None):
        """列出所有快捷键（底栏的"快捷键"按钮 / F1）"""
        messagebox.showinfo(
            "键盘快捷键",
            "空格 / K      播放、暂停\n"
            "←  /  →       上一帧、下一帧（暂停后按可以精确逐帧看）\n"
            "J  /  L       快退、快进 %.0f 秒\n"
            "↑  /  ↓       音量 +5%%、-5%%\n"
            "M             静音开关\n"
            "F  /  F11     全屏开关\n"
            "F1            显示这份说明\n\n"
            "万一按键没反应：点一下画面即可。\n"
            "（焦点停在按钮/滑块/下拉框上时，那几个键在该控件上有自己\n"
            "　的用途，会先让给它用 —— 点画面就把焦点收回来。）"
            % SHORTCUT_SEEK_STEP_SEC,
        )

    # ============================================================
    # 【关键修复】全屏切换改用 grid_remove / grid，避免混用 pack
    # ============================================================
    def toggle_fullscreen(self, event=None):
        self.is_fullscreen = not self.is_fullscreen
        self.root.attributes('-fullscreen', self.is_fullscreen)

        if self.is_fullscreen:
            # 进入全屏：绑定鼠标移动（只绑主窗口，见 _on_mouse_move），启动自动隐藏
            self.root.bind("<Motion>", self._on_mouse_move)
            self._last_motion_at = time.monotonic()
            self._show_controls()
            self._schedule_hide_controls()
        else:
            # 退出全屏：解绑，恢复常显
            self.root.unbind("<Motion>")
            if self._hide_controls_id is not None:
                self.root.after_cancel(self._hide_controls_id)
                self._hide_controls_id = None
            self._show_controls()
            self.root.config(cursor="")

        self.root.update_idletasks()
        self.render_current_frame()

    def _schedule_hide_controls(self):
        """重置隐藏计时器：鼠标停 2 秒后隐藏"""
        if self._hide_controls_id is not None:
            self.root.after_cancel(self._hide_controls_id)
        self._hide_controls_id = self.root.after(2000, self._hide_controls)

    def _hide_controls(self):
        """隐藏底栏。video_container 会自动扩展，Configure 事件触发重绘。"""
        self._hide_controls_id = None
        if not self.is_fullscreen:
            return
        self.progress_frame.grid_remove()
        self.btn_frame.grid_remove()
        self.aux_frame.grid_remove()
        self.parallax_frame.grid_remove()
        self.conversion_frame.grid_remove()
        self.root.config(cursor="none")
        self._controls_visible = False
        # 不需要手动 render，video_container 尺寸变化会通过 Configure 事件自动触发

    def _show_controls(self):
        """显示底栏。video_container 会自动缩回，Configure 事件触发重绘。"""
        if self.is_converting:
            self.conversion_frame.grid()
        self.progress_frame.grid()
        self.btn_frame.grid()
        self.aux_frame.grid()
        self.root.config(cursor="")
        self._controls_visible = True
        # 【P2 修复】原来这里无条件恢复视差栏：2D 模式下它根本没有作用，却
        # 照样被显示出来。改由 _sync_parallax_visibility 按当前模式决定。
        self._sync_parallax_visibility()
        # 同样不需要手动 render

    def _sync_parallax_visibility(self):
        """按当前显示模式显隐"视差 / 交换左右眼"整行。

        【P2 修复】_process_3d 在 2D 模式下是直接 return 的，视差滑块和
        "交换左右眼"完全不起作用，但控件仍然可交互（拖了没反应，很像坏了）。
        现在 2D 模式把整行收起来，切到任意 3D 模式再出现；
        全屏隐藏底栏期间也不允许它自己冒出来。
        """
        if self.display_mode == "2D" or not self._controls_visible:
            self.parallax_frame.grid_remove()
        else:
            self.parallax_frame.grid()

    def _on_mouse_move(self, event=None):
        """全屏下鼠标移动 → 显示底栏 + 重置隐藏计时。

        【P2 修复】原来用 bind_all("<Motion>") 绑在整个应用上，且每一个
        像素的移动都要做一次 after_cancel + after（1080p 窗口里鼠标一动
        就是几十次/秒）。现在：
          - 只绑主窗口：子控件的 Motion 会沿 bindtags 冒泡到 toplevel
            （widget → class → toplevel → all），行为不变，但不会再收到
            其它窗口（对话框等）的事件；
          - 按 MOTION_THROTTLE_MS 节流，鼠标狂甩也不会把事件循环塞满。
        """
        if not self.is_fullscreen:
            return
        now = time.monotonic()
        if now - self._last_motion_at < MOTION_THROTTLE_MS / 1000.0:
            return
        self._last_motion_at = now
        if not self._controls_visible:
            self._show_controls()
        self._schedule_hide_controls()

    def save_screenshot(self):
        """把当前画面另存为图片。

        【P2 修复】原来固定写工作目录下的 screenshot.jpg：多次截图互相覆盖，
        而且提示里只说文件名，用户不知道到底存去了哪里。
        """
        if self.current_frame_image is None:
            messagebox.showwarning("提示", "当前没有画面。")
            return

        save_path = filedialog.asksaveasfilename(
            title="保存截图",
            defaultextension=".jpg",
            filetypes=[("JPEG 图片", "*.jpg"), ("PNG 图片", "*.png"), ("所有文件", "*.*")],
            initialfile="截图_%s.jpg" % time.strftime("%Y%m%d_%H%M%S"),
        )
        if not save_path:
            return

        ext = os.path.splitext(save_path)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png"):
            ext = ".jpg"
        try:
            # 用 imencode + 二进制写文件，而不是 cv2.imwrite：
            # OpenCV 的 imwrite 走窄字节路径，在 Windows 上遇到中文/非 ASCII
            # 路径会直接失败（这正是"截图_20261013.jpg"这类默认名的常见坑）。
            ok, buf = cv2.imencode(ext, self.current_frame_image)
            if not ok:
                raise RuntimeError("图像编码失败")
            with open(save_path, "wb") as f:
                f.write(buf.tobytes())
        except Exception as e:
            messagebox.showerror("截图失败", "保存失败：\n%s" % e)
            return

        messagebox.showinfo("截图成功", "截图已保存到：\n%s" % os.path.abspath(save_path))

    def export_3d_video(self):
        """将本次转换生成的3D视频导出为一个独立文件。

        【P1 修复】只导出 session_output_path 记录的那个文件。
        原来判断的是工作目录下有没有 "temp_3d_output.mp4"，
        于是"打开已有3D视频"载入别的文件后点导出，导出的会是上一次
        转换遗留的残留产物（甚至可能是中途报错留下的残缺文件）。
        """
        temp_file = self.session_output_path
        if not temp_file or not os.path.exists(temp_file):
            messagebox.showwarning(
                "提示",
                "还没有可导出的3D视频。\n"
                "请先点击【导入2D视频一键转3D】完成一次转换，再点击导出。"
            )
            return

        save_path = filedialog.asksaveasfilename(
            defaultextension=".mp4",
            filetypes=[("MP4 Video", "*.mp4")],
            title="选择保存位置",
            initialfile="我的3D视频.mp4"
        )

        if not save_path:
            return

        try:
            shutil.copy(temp_file, save_path)
            messagebox.showinfo("导出成功", f"3D视频已成功导出到：\n{save_path}\n\n可以直接发送给朋友，或使用支持3D的播放器观看。")
        except Exception as e:
            messagebox.showerror("导出错误", f"导出失败：\n{str(e)}")

    def _clamp_seek_frame(self, frame):
        """把跳转目标夹进有效帧范围。

        【P2 回归修复·顺带】进度条的刻度是 0..total_frames，拖到最右端会得到
        帧号 == total_frames，而最后一帧的序号是 total_frames - 1。越界之后
        cap.read() 必然失败，update_frame 里"读不到就当放完了"的分支会把画面
        绕回第 0 帧 —— 表现出来就是"拖到最右边，画面反而跳回开头"。
        用户请求的是一个位置，越界就取最后一个合法位置。
        """
        if self.total_frames <= 0:
            return 0
        return max(0, min(int(frame), self.total_frames - 1))

    def _cancel_seek_timer(self):
        """取消待执行的跳转（进度条防抖定时器）"""
        if self.seek_timer is not None:
            try:
                self.root.after_cancel(self.seek_timer)
            except Exception:
                pass
            self.seek_timer = None

    def on_scale_move(self, value):
        """用户拖动进度条（每移动一次触发一次）。

        实测确认：程序自己写 seek_var（update_frame 每帧同步位置）**不会**
        触发这个回调 —— ttk.Scale 的 command 只在用户操作时触发，
        所以这里不需要区分"是谁在改"，直接把每次回调都当成用户操作。
        """
        self._begin_seek_interaction()
        if self.cap is None:
            return

        # 【P0 修复】改用 open_video 里已经做过兜底的 self.video_fps。
        # 原来直接读 cap.get(CAP_PROP_FPS) 当除数，某些容器/流会返回 0，
        # 一拖进度条就 ZeroDivisionError。
        fps = self.video_fps if self.video_fps > 0 else 30.0
        current_sec = int(float(value) / fps)
        total_sec = int(self.total_frames / fps)
        # 拖动时这个回调每秒触发几十次，只在秒数真的变了才去改窗口标题
        title = f"AI 3D 播放器 - {current_sec}秒 / {total_sec}秒"
        if self.root.title() != title:
            self.root.title(title)

    def _begin_seek_interaction(self):
        """"用户正在操作进度条"的公共入口：进入 seeking 态 + 重置跳转防抖。

        【P2 修复】on_scale_move 与 on_scale_click 原来各抄了一遍
        "压住音频 → 置 is_seeking → 重置防抖定时器"，现在收敛到这里。
        """
        if not self.is_seeking:
            # 用户刚刚开始碰进度条：先把音频压住，避免一边拖动一边
            # 听到严重错位的旧音频（这一步很便宜，只是 set_pause，不重建）
            self._suspend_audio_for_seek()

        self.is_seeking = True

        # 【P1 修复 2】防抖从 200ms 放宽到 SEEK_DEBOUNCE_MS：
        # 拖动时每次移动都会重置它，所以"还在拖"的时候不会跳转；
        # 只有停手够久、或者松手（on_scale_release）时才真正跳一次。
        # 原来 200ms 太短，缓慢拖动时会在拖动过程中反复触发 finish_seek，
        # 每次都带走一次 ~0.5 秒的音频重建 —— 这就是"拖一下卡一下"的来源。
        self._cancel_seek_timer()
        self.seek_timer = self.root.after(SEEK_DEBOUNCE_MS, self.finish_seek)

    def on_scale_click(self, event):
        # 先记下"鼠标按在进度条上"：后面的提前返回不能漏掉这个状态，
        # 否则松手时 on_scale_release 会因为 _scale_pressed 为 False 而忽略
        self._scale_pressed = True
        # 新一次交互开始：忘掉上一次留下的落点记录
        # （_last_jump_frame 在暂停态松手后会被保留，见 finish_seek）
        self._last_jump_frame = None
        if self.cap is None:
            return
        width = self.scale.winfo_width()
        if width <= 1:
            return
        # 【P2 修复】这里仍然自己算一次 ratio，而不是等着 ttk.Scale 去跳：
        # ttk 的 Press 是"类绑定（class bindtag）"，本函数是"控件绑定"，
        # Tk 严格按 bindtags 顺序执行 —— 控件绑定先跑，此刻 seek_var 里还是
        # 旧值，不自己算就没法在暂停态立刻渲染目标帧。
        # 两处算出的位置一致，不会造成"跳两次"（程序写 seek_var 不触发 command）。
        ratio = max(0.0, min(1.0, event.x / width))
        target_frame = self._clamp_seek_frame(ratio * self.total_frames)
        self.seek_var.set(target_frame)

        self._begin_seek_interaction()

        if self.is_paused:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
            self.update_frame(force=True)
            # 记下"已经渲染过这一帧"，松手后的 finish_seek 不必再做一遍
            self._last_jump_frame = target_frame

    def on_scale_release(self, event):
        # 这个回调同时挂在 scale 和 root 上（后者是兜底，见绑定处），
        # 只有"确实从进度条上按下去过"才处理，别处松手直接忽略。
        if not self._scale_pressed:
            return
        self._scale_pressed = False
        self._cancel_seek_timer()
        if self.cap is None:
            return
        self.finish_seek()

    def finish_seek(self):
        if self.cap is None:
            return
        self._cancel_seek_timer()
        target_frame = self._clamp_seek_frame(self.seek_var.get())
        fps = self.video_fps if self.video_fps > 0 else 30.0

        self.is_seeking = False
        # 【P1 修复 2】暂停态点击进度条时 on_scale_click 已经 set + 渲染过
        # 同一帧，这里再去 set 一次、再渲染一次纯属重复（约 100ms）。
        # 【P2 回归修复】渲染过之后不能把 _last_jump_frame 一律清成 None：
        # update_frame 会把读到的位置回写进 seek_var，而 POS_FRAMES 给的是
        # "下一帧的序号"（比请求值大 1）。清成 None 的话，用户"按住停手够久
        # ——防抖已经跳转过了——再松手"时，会拿这个 +1 的读数再跳一次，
        # 表现为每这么操作一次画面就悄悄多走一帧。所以记下真实落点，
        # 让紧随其后的那次松手成为空操作（下一次按下时会重新置 None）。
        landed = None
        if target_frame != self._last_jump_frame:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
            if self.is_paused:
                self.update_frame(force=True)
                landed = int(self.seek_var.get())
        self._last_jump_frame = landed
        self._silent_clock_base = None  # 跳转后重新对表，别把跳转前的时间算进来

        # 同步音频位置
        # 【P1 修复】原来的写法是
        #     self.audio_player.seek(int(self.seek_var.get()), relative=False, whence='frame')
        # 两个问题：MediaPlayer.seek 根本没有 whence 参数（每次都抛 TypeError 被
        # except 吞掉），而且实测 seek() 也定位不了音频。所以改成重建播放器，
        # 用 ff_opts['ss'] 做定位（详见 _restart_audio_at 的说明）。
        # 【P1 修复 2】不再直接重建，统一走"合并 / 挂起"入口。
        # 并且：如果鼠标还按在进度条上（说明这次是"停手够久"的兜底跳转），
        # 也只挂起音频重建 —— 拖动过程中绝不能付 0.5 秒的重建代价，
        # 等松手时（on_scale_release → 再走一次 finish_seek）一次性对齐。
        if self._scale_pressed:
            self._cancel_audio_restart_timer()
            self._pending_audio_pos = target_frame / fps
        else:
            self._request_audio_restart(target_frame / fps)

    def _stop_playback_loop(self):
        """停止播放循环：把状态置回"没在放"并取消挂起的"下一帧"定时器。

        【P2 修复】"停播"这件事原来在 open_video 和 on_close 里各写了一遍
        （is_playing=False + 取消定时器），语义完全一样，容易改漏一处。
        现在收敛成这一个出口。
        """
        self.play_state = PlayState.IDLE
        self._cancel_pending_frame()

    def _cancel_pending_frame(self):
        """取消"下一帧"定时器，并把计时器引用清干净。

        【P0 修复】播放循环有两个入口：定时器自身回调，以及
        toggle_play / on_scale_click 里的直接调用。必须保证任何时刻
        只有一个 after 挂起，否则会出现两条并行链、越播越快。
        """
        if self.after_id is not None:
            try:
                self.root.after_cancel(self.after_id)
            except Exception:
                pass
            self.after_id = None

    def _schedule_next_frame(self, delay_ms):
        """统一下一帧的调度出口：先取消旧定时器，再挂新的。

        调用它时既可能是"定时器正在回调"（取消自己是空操作），
        也可能是"被直接调用"（此时取消掉了上一次遗留的挂起定时器），
        两种情况都收敛成单链。
        """
        self._cancel_pending_frame()
        self.after_id = self.root.after(max(1, int(delay_ms)), self.update_frame)

    def update_frame(self, force=False):
        if self.cap is None: return
        # 【本次修复】交接收尾，每帧一次：
        #   等新播放器的音频确认在跑，再把旧播放器交给工作线程关掉
        #   （同时关闭抢发窗口 —— 之后混音器可能出现空档，不能再调 set_volume）。
        # 音量"抢发"（_audio_volume_resume）**不在这里做**：帧间隔 40ms（25fps）
        # 比"音频通道就绪"的时间尺度粗，会留下"已经出声、音量还是满音量"的一段
        # （实测 ~45ms，正好一帧）。改由 10ms 的独立定时器负责
        # （_start_volume_resume_poll），窗口一关就停。
        self._poll_audio_handoff_ready()

        # 【第 11 轮修复】挂起的音频重建请求必须有"一定会被兑现"的保证。
        # 原来只有"交接交给工作线程 → 取回时补一次"这么一条路径，而"新播放器还
        # 在等校准"那段（_audio_handoff_old 非空、thread 为空）不在这条路径上，
        # 请求会被无声丢掉 —— 而音频此刻已被 set_pause 压住，于是**彻底没声音**，
        # 1.5 秒后还会被误判成无音轨文件（声音与音量滑块一并永久失效）。
        # 触发条件很普通：短时间连续拖几下进度条。这里每帧兜一次，保证不会漏。
        if (self._pending_audio_pos is not None
                and not self.is_paused and not self.is_seeking
                and not self._scale_pressed
                and self._audio_handoff_thread is None
                and self._audio_handoff_old is None):
            self._do_audio_restart()

        # 【本次修复】到点应用"推迟过的音量同步"（见 _apply_volume_now：
        # 还没确认音频真的在跑时不能调 set_volume，会段错误；
        # 确认之后（时钟校准过 / 抢发窗口开着）才下发）
        if self._audio_volume_sync_at and time.time() >= self._audio_volume_sync_at:
            self._audio_volume_sync_at = 0.0
            self._apply_volume_now()
        if self.is_seeking and not force:
            # 【P0 修复】这里原来直接 self.root.after(...) 而不登记 ID，
            # 导致这条 is_seeking 轮询链在 on_close 里取消不掉，
            # destroy 之后回调触发会抛 invalid command name。
            self._schedule_next_frame(30)
            return
        # 【P2 回归修复】这里原来是 not self.is_playing —— 两个布尔量时代的写法：
        # 当时"暂停"只是把 is_paused 置真，is_playing 仍然为真，所以暂停态带
        # force 还能往下走、读到帧并渲染。
        # 合并成 PlayState 之后 is_playing 变成「play_state == PLAYING」，
        # 暂停时它就是 False，于是该条件恒真 —— update_frame(force=True)
        # 直接返回、什么都不画。症状就是：
        #   暂停态点进度条 / 拖进度条松手后，位置跳过去了但画面纹丝不动
        #   （视差滑块、交换左右眼是直接调 render_current_frame 的，不受影响）。
        # 正确语义：只有 IDLE（压根没在放）才不画；暂停态带 force 要照画。
        if self.play_state == PlayState.IDLE or (self.is_paused and not force):
            # 播放循环就此停止，清掉可能残留的定时器引用
            self._cancel_pending_frame()
            return

        fps = self.video_fps if self.video_fps > 0 else 30.0

        # 【第 11 条修复】读一次音频主时钟，判断"这一帧应该显示在哪里"。
        # force（暂停态重绘一帧）不参与同步：用户就是要看当前位置。
        # 主时钟不可用时 clock_sec 为 None，本帧完全走原来的行为。
        # 【第 11 条修复·补漏】主时钟内部先试音频时钟；文件根本没有音轨
        # （本程序自己导出的 3D 视频就是）时退回墙钟 —— 语义完全一致，
        # 所以下面的丢帧追赶、倍速、拖动全都不用改。否则没有墙钟兜底时
        # 只能"按帧率盲走"，单帧处理一慢整段视频就慢下来（实测
        # 2560x720@60fps 的转换产物只有 0.92x，模拟慢处理会掉到 0.54x）。
        clock_sec = None if force else self._master_clock_media_seconds()
        if clock_sec is not None and self.total_frames > 1:
            target_frame = int(clock_sec * fps)
            target_frame = max(0, min(target_frame, self.total_frames - 1))
            cur_frame = int(self.cap.get(cv2.CAP_PROP_POS_FRAMES))
            behind = target_frame - cur_frame
            if behind > FRAME_SYNC_TOLERANCE_FRAMES:
                # 视频落后于主时钟：丢掉中间帧，追上目标帧。
                # 用 grab() 顺序跳过（≈0.3ms/帧），不要用 set() 跳转
                # ——后者要回到关键帧再解码，一次要 20~90ms，反而追不动。
                if behind <= FRAME_SYNC_MAX_GRAB:
                    for _ in range(behind):
                        if not self.cap.grab():
                            break
                else:
                    self.cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)

        start_time = time.time()
        ret, frame = self.cap.read()
        if not ret:
            # 【P1 修复】循环回绕：视频跳回第 0 帧，音频也必须一起回到 0 秒。
            # 原来只重置了视频，音频继续往后走，第二遍起音画永久错位。
            # eos 之后必须整体重建播放器才能让音频从 0 秒重新开始
            # （本程序不用 seek，原因见文件顶部关于 seek/ss 的实测说明）。
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret, frame = self.cap.read()
            if not ret: return
            # 回绕后主时钟要重新对表：音频是重建播放器（下面 _begin_audio_handoff），
            # 墙钟则以"现在 + 第 0 秒"为新零点。
            self._silent_clock_base = None
            # 回绕必须立即对齐（不能走防抖），同时撤销任何挂起的跳转请求，
            # 否则稍后还会再重建一次。
            # 【本次修复】这里原来是同步重建 —— 每循环一圈就卡 0.5 秒。
            # 现在走后台交接：画面立刻从第 0 帧继续播，声音晚 ~0.5 秒接上
            # （工作线程建新播放器时会取"那时的视频位置"，所以接得上）。
            self._cancel_audio_restart_timer()
            self._pending_audio_pos = None
            self._restart_audio_at(0.0)

        if not self.is_seeking:
            # 【P2 修复】原来这里先存 self.current_frame 再写 seek_var，
            # 而 current_frame 全文件就只在这一处被写、下一行被读，
            # 纯属跳转用的中转变量，直接内联掉、不再占一个实例状态。
            self.seek_var.set(int(self.cap.get(cv2.CAP_PROP_POS_FRAMES)))
        # 【本次修复】发布"当前视频位置"，供后台音频交接建新播放器时取用
        # （交接要 0.5 秒，期间画面继续走，用请求时的旧位置会让音频落后半秒）
        try:
            self._last_media_sec = int(self.cap.get(cv2.CAP_PROP_POS_FRAMES)) / fps
        except Exception:
            pass

        # ================= 3D 核心处理区 =================
        # 保存原始 SBS 帧（供暂停时调视差用）
        self.current_raw_frame = frame.copy()

        # 统一交给 _process_3d 处理，并渲染
        self.current_frame_image = self._process_3d(frame)
        self.render_current_frame()
        # ==================================================

        # 【第 11 条修复】算下一帧前要等多少毫秒。
        #   有主时钟（音频时钟，或没有音轨时的墙钟）：直接看"下一帧该出现的
        #     媒体时刻"与"主时钟当前时刻"差多少，再折算成墙钟。这个式子自带
        #     倍速（媒体前进速度 = speed），也自带处理耗时补偿 —— clock 是
        #     渲染之后才读的，本帧多花的时间已经算进去了。
        #   没主时钟：退回原来的"帧间隔 − 处理耗时"。
        now_sec = None if force else self._master_clock_media_seconds()
        if now_sec is not None:
            next_media_sec = int(self.cap.get(cv2.CAP_PROP_POS_FRAMES)) / fps
            wait_sec = (next_media_sec - now_sec) / self.playback_speed
            # 兜底：主时钟再怎么算，单帧等待也不该超过 FRAME_SYNC_MAX_WAIT_FRAMES
            # 个帧间隔。正常同步时等的是"下一帧该出现的媒体时刻"，量级就是
            # 1 个帧间隔，够不着这个上限；万一将来又冒出"读数不靠谱"的情况
            # （设备中途卡住、读数跳回等），有它兜着最多是短暂不同步，
            # 而不会把画面拖成慢动作 —— 死时钟检测漏网时的最后一道保险。
            max_wait_ms = int((1000.0 / fps / max(self.playback_speed, 0.01))
                              * FRAME_SYNC_MAX_WAIT_FRAMES)
            delay = max(1, min(int(wait_sec * 1000), max_wait_ms))
        else:
            process_time = (time.time() - start_time) * 1000
            target_delay = 1000.0 / fps
            delay = max(1, int(target_delay / self.playback_speed - process_time))

        self._schedule_next_frame(delay)

    def on_close(self):
        """关闭窗口时彻底释放所有资源，确保进程真正退出"""
        # 1. 通知转换线程退出（不等待，它是 daemon 线程）
        if self.is_converting:
            self.conversion_cancel_event.set()

        # 2. 停止视频播放循环（【P2 修复】与 open_video 共用同一个出口）
        self._stop_playback_loop()

        # 3. 取消所有挂起的 after 定时器
        for attr in ("after_id", "resize_after_id", "seek_timer",
                     "_hide_controls_id", "_audio_restart_timer",
                     "_audio_handoff_poll_id", "_audio_volume_resume_timer"):
            tid = getattr(self, attr, None)
            if tid is not None:
                try:
                    self.root.after_cancel(tid)
                except Exception:
                    pass
                setattr(self, attr, None)

        # 3.5 【本次修复】作废并等待后台音频交接，避免"关窗时还有线程在动音频"。
        #     只等 0.3 秒：交接线程是 daemon，真没结束就随进程一起结束；
        #     此时主线程手上没有任何播放器，不存在两个 SDL 音频设备并存的问题。
        self._cancel_audio_handoff(timeout=0.3)

        # 4. 释放音频播放器（关键：不 close 会导致 SDL 线程不退出，进程卡住）
        self._close_audio_player()

        # 5. 释放视频捕获
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

        # 6. 如果正在转换，给它一点时间去响应取消、删掉写到一半的临时文件。
        #    【P2 修复】原来直接 os._exit，转换线程会被"就地斩断"，
        #    %TEMP% 下会留一个残缺 mp4。这里最多等 CONVERSION_JOIN_TIMEOUT_SEC，
        #    超时就算了（它是 daemon 线程，不会拖住进程）。
        thread = self.conversion_thread
        if thread is not None and thread.is_alive():
            thread.join(CONVERSION_JOIN_TIMEOUT_SEC)
            self.conversion_thread = None

        # 7. 销毁 Tk 窗口
        try:
            self.root.quit()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

        # 8. 兜底：强制退出进程。
        #    【P2 修复】这一步的定位是"最后手段"，不是常规退出路径 ——
        #    上面已经按顺序停播、close 了音频播放器（SDL 线程）、release 了
        #    cap、join 了转换线程、destroy 了窗口；只有 SDL / OpenCV 仍残留
        #    非 daemon 线程时才会走到这里。os._exit 不执行 atexit、也不
        #    flush 缓冲区，所以：① 先冲刷 stdout/stderr；② 不要在这之后
        #    再添加任何需要收尾的逻辑。
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(0)

    # ============================================================
    # 【P1】音频播放器的创建 / 重建
    # ------------------------------------------------------------
    # 本构建的 ffpyplayer 没有 set_playback_rate（已核对导出符号），
    # 倍速只能在创建时通过 ffmpeg 的 atempo 滤镜指定 —— 所以"改速率"
    # 必须重建播放器；而"跳转到某个位置"同样只能靠重建（原因见文件顶部
    # 关于 seek/ss 的实测结论）。于是三处需求（倍速、进度条、循环回绕）
    # 统一收敛到 _restart_audio_at()。
    # ============================================================
    def _close_audio_player(self):
        """安全关闭并清空音频播放器

        注意：**不要在后台音频交接进行中调用** —— 交接期间旧实例可能还留在
        _audio_handoff_old / 工作线程手里，先关当前这个会把混音器拆掉，
        让那次交接里的抢发音量撞上已销毁的混音器。换文件 / 关窗前必须先
        _cancel_audio_handoff()。
        """
        if self.audio_player is not None:
            try:
                self.audio_player.close_player()
            except Exception:
                pass
            self.audio_player = None
        # 【第 11 轮修复】没有播放器了，"为拖动挂起"的豁免随之失效
        # （没有播放器时 _audio_clock_media_seconds 本来就会提前返回，
        #   这里清掉是为了不让标记跨文件残留）。
        self._mark_audio_live()

    def _audio_ff_opts(self, start_sec=0.0):
        """构造 ffpyplayer 的 ff_opts（纯读取当前状态，可在线程里调用）。

        只读 self 上的简单属性（倍速/音量/静音/暂停），不做任何写入、不碰
        播放器对象，所以交给工作线程执行是安全的。
        """
        ff_opts = {
            'paused': bool(self.is_paused),  # 与视频状态保持一致
            # 【注意】这个 volume 只写进 ffpyplayer 的 settings.audio_volume，
            # 在本构建（编入 SDL2_mixer）里**不参与混音**，对实际声音大小无效，
            # 只有 get_volume() 会读它。真正的音量必须靠创建后补一次 set_volume()
            # —— 见 _apply_volume_now() 与 _poll_audio_handoff()。这里留着是为了
            # 万一换到非 SDL2_mixer 的构建时行为一致。
            'volume': 0.0 if self.audio_muted else self.audio_volume,
            # 【P1 修复 2】画面由 OpenCV 输出，ffpyplayer 自己的视频流没人消费。
            # 不禁用的话它会一头解码到视频队列填满，纯属白烧一半 CPU，
            # 还会让读线程阻塞在队列上。实测 vn=1 不影响音频时钟推进、
            # 不影响 ss 定位、不影响暂停/恢复。
            'vn': 1,
        }
        # 倍速：atempo 支持 0.5 ~ 2.0，正好覆盖下拉框的四个档位
        if self.playback_speed and self.playback_speed != 1.0:
            ff_opts['af'] = "atempo=%g" % self.playback_speed
        # 从指定位置开始（重建时用来接续当前播放进度）
        if start_sec and start_sec > 0.01:
            ff_opts['ss'] = float(start_sec)
        return ff_opts

    def _reset_audio_clock_state(self):
        """换了一个音频播放器实例后，重置主时钟的校准状态（只在主线程调用）。

        【第 11 条修复】换了播放器，主时钟基准需要重新校准
        （会在下一次能安全读 pts 时建立，见 _audio_clock_media_seconds）。
        死时钟是按"播放器实例"判定的：新实例重新给一次机会，
        免得一次误判让此后所有重建都直接放弃音频。
        """
        self._audio_progress_ref = None
        self._audio_calibrated = False
        self._audio_clock_dead = False
        self._audio_last_pts = None
        self._audio_frozen_since = None
        self._audio_started_at = time.time()

    def _create_audio_player(self, file_path, start_sec=0.0):
        """**同步**创建音频播放器（构造本身只要 ~1.6ms）。

        两条路径调用：① 打开文件；② _begin_audio_handoff 启动后台交接时
        （先建新的、后关旧的，见那一段说明）。**不要**在拖动进度条这类高频
        路径上直接调用它 —— 那类路径要先走 _begin_audio_handoff()，
        否则又回到"关旧播放器卡主线程"的老问题。
        返回创建的 MediaPlayer；失败返回 None 并置 audio_failed。
        """
        if not file_path or self.audio_failed:
            return None

        player = self._new_audio_player(file_path, start_sec)
        if player is None:
            self.audio_failed = True
            return None
        self._reset_audio_clock_state()

        # 双保险：ff_opts 已设置过，这里再显式对齐一次实际状态
        try:
            player.set_pause(bool(self.is_paused))
        except Exception:
            pass
        return player

    def _new_audio_player(self, file_path, start_sec=0.0):
        """真正构造 MediaPlayer 的**纯函数**（不写任何 self 状态）。

        之所以把"构造"单独拆成一个不碰 self 的纯函数，是为了让不同路径
        都能安全地复用它 —— 尤其 _poll_audio_handoff 里主线程要用它建新
        播放器（时钟校准状态由调用方 _create_audio_player 统一重置，
        避免两个线程同时改这些字段）。
        """
        if not file_path:
            return None
        try:
            return MediaPlayer(file_path, ff_opts=self._audio_ff_opts(start_sec))
        except Exception as e:
            print(f"音频初始化失败（将静音播放）: {e}")
            return None

    def _restart_audio_at(self, start_sec=0.0):
        """重建音频播放器，并把音频定位到 start_sec 附近 —— 本程序唯一的音频跳转手段。

        三处调用：切换倍速（换 atempo 滤镜）、循环回绕（eos 之后旧播放器已到结尾）、
        拖动进度条（原实现调 seek，实测无效，见文件顶部的说明）。

        精度说明：定位靠创建时传 ff_opts['ss']，它是"非精确 seek"，
        会回退到目标之前的关键帧（实测 city.mp4 只有 0s/12s 两个关键帧，
        ss=5 落到 0s、ss=17 落到 12s）。所以跳转位置会有最多一个关键帧间隔的
        偏差 —— 这是底层库的限制，不做 seek 是为了避开暂停态段错误。

        注意：**调用后立刻返回，不会阻塞主线程**（新播放器在主线程构造，
        只要 ~1.6ms；旧播放器的关闭在后台线程里做）。
        """
        self._begin_audio_handoff(start_sec)

    # ============================================================
    # 【本次修复】后台音频交接
    # ------------------------------------------------------------
    # 背景：本构建的 close_player() 固定要 ~0.5 秒（等读线程退出，实测无法加速），
    # 在主线程里做就是肉眼可见的卡顿 —— 拖完进度条松手后，画面先正常播 0.25 秒
    # （防抖窗口），然后冻住 0.55 秒，用户看到的就是"卡一下才继续播"。
    #
    # 做法：交接分成三步，把"关旧"那 ~0.5 秒交给工作线程，主线程一直不阻塞：
    #   ① 先按住旧的（set_pause，静音）——此刻它还在主线程手里，动它是安全的；
    #   ② **先建新的**（1.6ms）——此时旧播放器仍占着混音通道，SDL 混音器必然有效，
    #      于是可以对新的安全调 set_volume：音量能在它出声之前就位（见
    #      _audio_volume_resume 的说明）；
    #   ③ 等新播放器的音频确认在跑，再把它交出去关（工作线程）。
    # 换来的好处：声音回来得快（不用等混音器被销毁再重开，约省 0.5 秒），
    # 音量也不会先按满音量响一下。
    # 纪律：第 ③ 步之后主线程**绝不触碰**那个被关的实例（见文件顶部注释）。
    # ============================================================
    def _begin_audio_handoff(self, start_sec=0.0):
        """启动一次后台音频交接（非阻塞）。返回 True 表示已启动。

        只允许同时存在一个交接：期间新的请求会留在 _pending_audio_pos 里，
        等本次取回时再补一次（见 _poll_audio_handoff）。
        """
        if self._audio_handoff_thread is not None or self._audio_handoff_old is not None:
            return False                     # 上一次的旧播放器还没交出去
        if not self.current_video_path or self.audio_failed:
            return False

        old = self.audio_player
        # 【关键】"旧播放器此刻的音频通道是否活着"要**先**记下来：
        # 下面 _create_audio_player() 会把 _audio_calibrated 归零（那是按实例的
        # 状态）。这个布尔量决定"抢发窗口"能不能开 —— 旧播放器被确认过有音轨，
        # 它才一定占着混音通道、混音器才一定有效（见 _apply_volume_now）。
        old_had_audio = bool(old is not None and self._audio_calibrated)

        # ① 让旧的闭嘴（它还在主线程手里，这一步安全且很便宜）。
        #    不这么做的话，在它被真正关掉之前会和新播放器的声音叠在一起。
        if old is not None:
            try:
                old.set_pause(True)
            except Exception:
                pass

        # ② 先建新的：混音器此刻必然有效（旧播放器占着通道）。
        #    用**现在**的视频位置做 ss —— 音频随后 ~几十毫秒就开始出声，
        #    用请求时的旧位置会略有滞后。
        self.audio_player = self._create_audio_player(
            self.current_video_path, self._last_media_sec or start_sec)
        # 【第 11 轮修复】音频已经交给一个活着的播放器了：解除"为拖动挂起"的
        # 死时钟豁免（否则真·无音轨文件会漏判，见 _mark_audio_live）。
        self._mark_audio_live()
        # 抢发窗口：开着就允许立刻/反复调 set_volume（此时混音器一定有效）。
        # 先立刻下发一次 —— 大多数情况下新播放器的音频通道这一刻就已经挂上了，
        # 这一发就落到位（音量在它出声之前生效）；再挂 10ms 重试定时器兜底，
        # 因为通道也可能晚一点点才就绪，那时每一帧才试一次就太粗了。
        self._audio_volume_resume = old_had_audio
        if old_had_audio:
            self._apply_volume_now()
            self._start_volume_resume_poll()

        # ③ 旧的先留着，等新播放器的音频确认在跑（_poll_audio_handoff_ready），
        #    这样整个交接期间混音器都不会被销毁。
        self._audio_handoff_old = old
        self._audio_handoff_started = time.time()
        self._audio_handoff_pending = True
        # 交接期间由墙钟主时钟驱动画面，重新对表
        self._silent_clock_base = None
        return True

    def _poll_audio_handoff_ready(self):
        """主线程每帧问一次：新播放器的音频确认在跑了吗？确认了就把旧的交出去关。

        为什么非要等：交接期间旧播放器一直被"按住"（paused）留着没关，它占着
        一条 SDL 混音通道，混音器因此不会被销毁 —— 这就是"抢发窗口"能吃下发
        set_volume 的前提（混音器一死，Mix_Volume 就会踩 NULL 段错误，
        见 _apply_volume_now）。一旦这里把它交出去关掉，混音器就可能出现空档，
        所以这一步之后要立刻关掉抢发窗口。

        "确认在跑"的判据与音量下发一致：音频时钟被观测到真的在走
        （_audio_calibrated）；另外三种情况也收尾 —— 确认这个文件没有音轨
        （audio_failed / 时钟判死）、**有待兑现的重建请求**（见下），
        或兜底超时 AUDIO_HANDOFF_KEEPALIVE_MAX
        （比如交接期间用户把视频暂停了，时钟不会走，不能让旧的无限留着）。
        """
        old = self._audio_handoff_old
        if old is None:
            return
        ready = (self._audio_calibrated or self.audio_failed
                 or self._audio_clock_dead
                 # 【第 11 轮修复】有待兑现的重建请求 ⇒ 眼下这个"新播放器"马上
                 # 就会被替换掉，没必要再等它校准（等下去就是白等满 3 秒才出声）。
                 # "交接还在等校准、用户又拖了一下"正是这种情况。
                 or self._pending_audio_pos is not None
                 or time.time() - self._audio_handoff_started
                 > AUDIO_HANDOFF_KEEPALIVE_MAX)
        if not ready:
            return

        self._audio_handoff_old = None
        # 抢发窗口就此关闭：下面工作线程会去关旧播放器（那 0.5 秒里混音器可能
        # 被销毁），从这一刻起不再对播放器下发音量 —— 后续由校准点那条路负责。
        self._audio_volume_resume = False
        self._stop_volume_resume_poll()
        thread = threading.Thread(
            target=self._audio_handoff_worker, args=(old,),
            name="audio-handoff", daemon=True,
        )
        self._audio_handoff_thread = thread
        thread.start()
        self._schedule_audio_handoff_poll()

    def _audio_handoff_worker(self, old):
        """工作线程：**只负责关掉旧播放器**（这一步要 ~0.5 秒，不能占主线程）。

        它由主线程在 _poll_audio_handoff_ready 里、确认新播放器的音频已经在跑
        之后才启动。启动之后主线程绝不再碰这个实例（_cancel_audio_handoff
        只 join、不调用它的方法）。

        【为什么"建新的"不在这里做（别改回去）】建新必须留在主线程：① 构造只要
        1.6ms，不占时间；② 更关键的是，建新必须发生在**旧播放器还活着**的时候，
        这样 SDL 混音器全程有效，set_volume 才安全（见 _apply_volume_now 的实测）。
        """
        if old is not None:
            try:
                old.close_player()
            except Exception:
                pass

    def _schedule_audio_handoff_poll(self):
        if self._audio_handoff_poll_id is not None:
            return
        try:
            self._audio_handoff_poll_id = self.root.after(
                AUDIO_HANDOFF_POLL_MS, self._poll_audio_handoff)
        except Exception:
            self._audio_handoff_poll_id = None

    def _cancel_audio_handoff_poll(self):
        if self._audio_handoff_poll_id is not None:
            try:
                self.root.after_cancel(self._audio_handoff_poll_id)
            except Exception:
                pass
            self._audio_handoff_poll_id = None

    # ---- 抢发窗口的音量重试定时器（见 _audio_volume_resume） ----
    # 为什么单独用 10ms 定时器而不是搭 update_frame 的车：抢发救的是"音量要赶在
    # 新播放器**出声之前**落位"，而出声发生在交接开始后 ~40ms 左右；帧间隔 40ms
    # （25fps）意味着第一帧重试就可能整帧错过，实测会留下 ~45ms 的"满音量响一下"。
    # 10ms 比一帧细 4 倍，足以把这个窗口压到基本不可闻。代价是窗口期内（最多
    # AUDIO_HANDOFF_KEEPALIVE_MAX）每 10ms 调一次 set_volume —— 幂等、极便宜。
    def _start_volume_resume_poll(self):
        if self._audio_volume_resume_timer is not None:
            return
        try:
            self._audio_volume_resume_timer = self.root.after(
                AUDIO_VOLUME_RESUME_POLL_MS, self._on_volume_resume_tick)
        except Exception:
            self._audio_volume_resume_timer = None

    def _on_volume_resume_tick(self):
        self._audio_volume_resume_timer = None
        if not self._audio_volume_resume:
            return                              # 窗口已被关掉（交接就绪/换文件/关窗）
        self._apply_volume_now()
        self._start_volume_resume_poll()        # 继续轮询；窗口一关自然停

    def _stop_volume_resume_poll(self):
        if self._audio_volume_resume_timer is not None:
            try:
                self.root.after_cancel(self._audio_volume_resume_timer)
            except Exception:
                pass
            self._audio_volume_resume_timer = None

    def _poll_audio_handoff(self):
        """主线程取回交接：旧播放器已经关好了，**在这里建新的**（1.6ms，不可见）。"""
        self._audio_handoff_poll_id = None
        thread = self._audio_handoff_thread
        if thread is None:
            return
        if thread.is_alive():
            self._schedule_audio_handoff_poll()      # 还没好，过 30ms 再看
            return

        self._audio_handoff_thread = None
        self._audio_handoff_pending = False

        # 交接期间又来了新的跳转请求 → 再补一次（只补一次，防抖已经在请求侧做过）
        if self._pending_audio_pos is not None and self.audio_player is not None:
            pos = self._pending_audio_pos
            self._pending_audio_pos = None
            self._begin_audio_handoff(pos)

    def _cancel_audio_handoff(self, timeout=2.0):
        """作废并等待正在进行的音频交接（换文件 / 关窗时必须先调用）。

        两种"在飞"的状态都要收：① 旧播放器已经交给工作线程在关 —— join 它；
        ② 旧播放器还留在 _audio_handoff_old 里等着"新播放器音频就绪"——
        这里不等了，直接把它交给工作线程关掉。等待都是有界的：超时就放弃
        （daemon 线程，进程退出即止），因为此后主线程已经不再持有那个实例。
        """
        self._cancel_audio_handoff_poll()
        self._audio_volume_resume = False       # 抢发窗口即刻关闭
        self._stop_volume_resume_poll()
        self._audio_handoff_seq += 1

        old = self._audio_handoff_old
        self._audio_handoff_old = None
        if old is not None:
            thread = threading.Thread(
                target=self._audio_handoff_worker, args=(old,),
                name="audio-handoff", daemon=True,
            )
            self._audio_handoff_thread = thread
            thread.start()

        thread = self._audio_handoff_thread
        if thread is not None:
            try:
                thread.join(timeout)
            except Exception:
                pass
        self._audio_handoff_thread = None
        self._audio_handoff_pending = False

    # ============================================================
    # 【P1 修复 2】音频重建的"请求 / 合并 / 挂起"
    # ------------------------------------------------------------
    # 一次重建要 ~0.5 秒（close 时等读线程退出），而拖动进度条是高频动作。
    # 直接重建的后果就是"拖一下卡一下、连续拖就连续卡"。
    # 这里把重建请求集中到一个入口：
    #   - 播放态：登记目标位置 + 防抖（AUDIO_RESTART_DEBOUNCE_MS），
    #             短时间内的多次请求只执行最后一次；
    #   - 暂停态：音频本来不出声，重建毫无意义，只记住位置，
    #             等用户恢复播放时再重建（拖动体验最差的就是这一档）。
    # ============================================================
    def _cancel_audio_restart_timer(self):
        if self._audio_restart_timer is not None:
            try:
                self.root.after_cancel(self._audio_restart_timer)
            except Exception:
                pass
            self._audio_restart_timer = None

    def _suspend_audio_for_seek(self):
        """开始拖动进度条：压住音频，并撤销尚未执行的重建请求。

        只是 set_pause（很便宜），不重建。目的是别让用户一边拖一边听到
        严重错位的旧音频；真正的对齐等停手后由 _request_audio_restart 负责。

        【第 11 轮修复】同时标记 `_audio_suspended_for_seek`：被压住期间 pts 是
        冻的，死时钟检测必须豁免它（详见 _audio_clock_media_seconds）——
        否则"连续快速拖动"会被误判成无音轨文件，声音永久消失。
        """
        self._audio_suspended_for_seek = True
        self._cancel_audio_restart_timer()
        self._pending_audio_pos = None
        if self.audio_player is not None:
            try:
                self.audio_player.set_pause(True)
            except Exception:
                pass

    def _mark_audio_live(self):
        """解除 `_audio_suspended_for_seek`：音频已经交给一个活着的播放器了。

        与 `_suspend_audio_for_seek` 配对。必须覆盖**所有**"音频重新活过来"的
        出口，否则这个豁免会永久残留、把真正的无音轨文件漏判（那正是第 7 轮
        "播放变慢"回归的来源）：
          - `_begin_audio_handoff` 成功建出新播放器；
          - `_do_audio_restart` 判定"这份音频永远不会再恢复"（无音轨/已判死）；
          - `_close_audio_player` / `open_video`（换文件、关窗）；
          - `toggle_play` 恢复播放。
        """
        self._audio_suspended_for_seek = False

    def _resume_audio_after_seek_suspend(self):
        """音频正被"为拖动而挂起"时就地放回来（**不重建**播放器）。

        与 `_suspend_audio_for_seek` 配对。用在"本来像是要拖动、结果走成了逐帧
        步进"这条路径上（焦点在进度条上按 ←/→，见 `_on_key_shortcut`）：
        `step_frame` 会取消 seek 的防抖定时器，于是 `finish_seek` 不会来，
        而解除挂起本来就挂在它身上 —— 不补这一手，声音会一直哑着，
        且 `_audio_suspended_for_seek` 永久残留（那会让真·无音轨文件漏判，
        正是第 7 轮"播放变慢"回归的来源）。

        代价只有一次 `set_pause(False)`：差一帧 40ms，远小于能察觉的音画错位，
        没必要为此付一次 0.5 秒的重建（这就是"逐帧步进不做音频重建"那条纪律）。
        """
        if not self._audio_suspended_for_seek:
            return                          # 绝大多数情况：压根没挂起，零开销
        self._mark_audio_live()
        if self.audio_player is not None:
            try:
                self.audio_player.set_pause(False)
            except Exception:
                pass

    def _apply_volume_now(self):
        """把当前音量/静音状态下发到播放器；**没确认"这个播放器有音轨"就不调**。

        【为什么必须显式下发 —— 本次修复的根因】
        本构建的 ffpyplayer 编入了 SDL2_mixer（`USE_SDL2_MIXER`），音频实际音量
        是"每个混音通道"的音量，只有 `Mix_Volume(通道, 音量)` 能改它 ——
        而 ffpyplayer 只在 `set_volume()` 里调用 `Mix_Volume`。
        构造参数 `ff_opts['volume']` **只写进一个不参与混音的字段**
        （`settings.audio_volume`，只有 `get_volume()` 会读它），
        对实际声音大小**没有任何作用**。实测（probe_mvol.py，直接读混音通道音量）：
        `MediaPlayer(city.mp4, ff_opts={'volume':0.3})` 建好后
        `get_volume()` 报 0.296875，但混音通道全是默认满音量 128；
        调过 `set_volume(0.3)` 之后通道才变成 38。
        => 任何**新建**的播放器都必须补一次 `set_volume`，否则它一定按满音量播。
        （症状就是"拖完进度条声音变大、可滑块还停在原位"。）

        【为什么不能无条件调 —— 段错误】
        `set_volume()` 在"没有真实音轨"的播放器上会段错误（access violation，
        Python 的 except 拦不住）。实测（probe_vol2.py，4 个变体）：
          - 第一个播放器就是无音轨文件、等 0.5s 调 set_volume        → OK
          - 先建过一个有音轨的播放器（city.mp4）再建无音轨的         → **崩**
          - 无音轨播放器建好后反复 get_pts、再调 set_volume          → **崩**
          - 有音轨（city→city / 单个 city）怎么调                     → 从不崩
        即：无音轨时播放线程会退出，之后往它投递音量命令就踩到已释放的内存；
        而"播放器太新"不是原因（0.4 秒的延时闸门拦不住，0.45 秒后照样崩）。

        【判据】两者之一成立就允许下发：
          ① `_audio_calibrated` —— 这个**实例**的音频时钟被观测到真的在走，
             说明混音器已重新工作、音频回调在跑（`Mix_Volume` 需要的条件）；
          ② `_audio_volume_resume` —— "抢发窗口"（见 _begin_audio_handoff）：
             旧播放器还没被关，仍占着一条混音通道 ⇒ 混音器必然有效。
        都不成立就只登记，由 update_frame 每 AUDIO_VOLUME_SYNC_DELAY 重试；
        确认无音轨（audio_failed / 时钟已判死）则彻底放弃 —— 那里没声音可调。

        【为什么不能用"播放器有多新"或"这个文件有没有音轨"来判断】（都试过，会崩）
        裸播放器实测（probe_mvol2.py，各 4 轮）：
          - 两个播放器并存（全程没关过谁）+ 建好立刻 set_volume  → 4/4 安全
          - 关掉旧播放器 → 建新 → **立刻** set_volume            → 4/4 段错误
          - 关掉旧播放器 → 建新 → 等 0.2s / 0.6s 再 set_volume   → 4/4 安全
          - 关旧的动作在主线程还是工作线程做，结果一样
        即崩溃条件是"旧播放器被关掉 ⇒ `Mix_CloseAudio()` 销毁了混音器、
        `mix_channel` 变 NULL，而新播放器还没来得及重新打开它" ——
        ctypes 直接复现过：崩溃现场是"读 NULL + 0x18"（正是 struct 里 volume
        字段的偏移）。所以"等 0.2 秒"这种**时间猜测**不能用：机器一忙、文件一大，
        读线程 0.2 秒内开不出混音器，照样崩。
        本程序的对策是**结构上别让混音器被销毁**：交接时先建新播放器、后关旧的，
        且等新播放器音频确认在跑才去关（`_audio_volume_resume` 就是这段窗口）。
        （早期版本按"文件有音轨"放行也是错的：文件有音轨不代表这个新实例的
          混音器已经就绪 —— 实测立刻调同样 100% 崩。）
        """
        p = self.audio_player
        if p is None or self.audio_failed or self._audio_clock_dead:
            # 没有可用播放器，或已确认这个文件没有音轨：不再重试
            # （交接期间新播放器建好前 audio_player 是 None；建好后由"抢发窗口"
            #   和校准点两处负责补，不依赖这个定时重试）
            self._audio_volume_sync_at = 0.0
            return
        if not (self._audio_calibrated or self._audio_volume_resume):
            self._audio_volume_sync_at = time.time() + AUDIO_VOLUME_SYNC_DELAY
            return
        self._audio_volume_sync_at = 0.0
        try:
            p.set_volume(0.0 if self.audio_muted else self.audio_volume)
        except Exception:
            pass

    def _request_audio_restart(self, seconds):
        """请求把音频对齐到 seconds 秒（会合并、可能挂起，见本节说明）"""
        if not self.current_video_path or self.audio_failed:
            return
        seconds = max(0.0, float(seconds))
        if self.is_paused:
            # 暂停态：不重建，只登记，等恢复播放时再处理
            self._cancel_audio_restart_timer()
            self._pending_audio_pos = seconds
            return
        self._pending_audio_pos = seconds
        if not self._scale_pressed:
            # 【本次修复】鼠标已经松开：没必要再等防抖窗口 —— 交接本身不再
            # 阻塞主线程，立刻开工能让声音早 0.25 秒回来。
            self._cancel_audio_restart_timer()
            self._do_audio_restart()
            return
        self._cancel_audio_restart_timer()
        self._audio_restart_timer = self.root.after(
            AUDIO_RESTART_DEBOUNCE_MS, self._do_audio_restart
        )

    def _do_audio_restart(self):
        """执行（或合并）一次音频重建请求 —— 现在是**非阻塞**的后台交接。

        【第 11 轮修复】原实现在"已有交接在跑"时会把 `_pending_audio_pos` 丢掉，
        只留一句"等它取回时再补一次"—— 但取回路径只在**交接已交给工作线程**
        （`_audio_handoff_thread` 非空）时才会跑，而"新播放器还在等校准"这段
        （`_audio_handoff_old` 非空、thread 为空）根本不在那条路径上。
        后果很严重：这份重建请求既没被执行、也没被记住，而音频此时已经被
        `_suspend_audio_for_seek` 压住了 —— 新播放器永远停在暂停态，
        用户听到的是**彻底没声音**，1.5 秒后还会被误判成无音轨文件。
        （触发条件很普通：短时间连续拖几下进度条。）

        现在改为：把请求**留着**，交给 update_frame 每帧的兜底检查兑现。
        """
        self._audio_restart_timer = None
        pos = self._pending_audio_pos
        if pos is None:
            return
        # 没有文件 / 音频已判死：这份请求永远不会被兑现，就地作废，
        # 免得每帧都重试一遍（也顺手解除拖动挂起标记，让死时钟检测恢复工作）。
        if not self.current_video_path or self.audio_failed or self._audio_clock_dead:
            self._pending_audio_pos = None
            self._mark_audio_live()
            return
        if self._audio_handoff_thread is not None or self._audio_handoff_old is not None:
            # 已有交接在跑：**保留** _pending_audio_pos，别丢。
            # 交接收尾（_poll_audio_handoff）会用它再补一次；
            # update_frame 里还有一道每帧兜底，保证它一定会被兑现。
            return
        self._pending_audio_pos = None
        if self._begin_audio_handoff(pos) is False:
            # 走到这里说明上面三个前提刚刚变了（理论上不会），留着重试。
            self._pending_audio_pos = pos

    def _flush_pending_audio_restart(self):
        """把挂起的重建兑现（恢复播放时调用）

        返回 True 表示已经发起过重建，调用方不需要再动 set_pause。
        【本次修复】这里原来是**同步**重建：用户一按"继续"，主线程就卡 0.5 秒。
        现在改成后台交接 —— 画面立刻开始播，声音晚 ~0.5 秒接上。
        """
        self._cancel_audio_restart_timer()
        pos = self._pending_audio_pos
        if pos is None:
            return False
        if self._audio_handoff_thread is not None:
            return True                      # 交接在跑，它取回时会用最新位置补一次
        self._pending_audio_pos = None
        return bool(self._begin_audio_handoff(pos))

    def _audio_clock_media_seconds(self):
        """用音频主时钟推算"此刻应该播到第几秒"，不可用时返回 None。

        返回值是**媒体时间轴**上的秒数（已按倍速换算），可直接换算视频帧号。

        以下任一情况返回 None，调用方回退到"按帧率盲调度"：
          - 没有音频播放器 / 音频初始化失败；
          - 本实例的时钟已被判定为"死时钟"（无音轨等，见顶部第 5 条）；
          - 时钟还没被证实"真的在走"（推进量不足 / 读数一直不动）；
          - 暂停中（pts 会冻结，此时它不代表播放进度）；
          - 正在拖动、或有挂起/待执行的音频重建：此时音频被 set_pause 压住，
            位置和视频对不上，拿它同步反而会把画面往回拽；
          - 播放器创建不足 AUDIO_CLOCK_WARMUP_SEC（过早读 pts 会段错误，见顶部说明）。
        """
        p = self.audio_player
        if p is None or self.audio_failed or self._audio_clock_dead:
            return None
        if (self.is_paused or self.is_seeking or self._scale_pressed
                or self._audio_suspended_for_seek):
            # 【第 11 轮修复】最后一项是新增的：音频"为了拖动被 set_pause 压住、
            # 还没被交给新播放器"时，pts 同样是冻的。
            # 原来只判断 is_seeking / _scale_pressed，而那两个标志在**松手后立刻**
            # 就恢复 False —— 于是"连续快速拖动"里两下之间的空隙，会被当成
            # "音频在播放却一动不动"，累计 1.5 秒就判死时钟 + audio_failed，
            # 声音和音量滑块从此永久失效（实测：拖 6 次就必现）。
            return None
        if self._pending_audio_pos is not None or self._audio_restart_timer is not None:
            return None
        if (time.time() - self._audio_started_at) < AUDIO_CLOCK_WARMUP_SEC:
            return None
        try:
            pts = float(p.get_pts())
        except Exception:
            return None
        if not math.isfinite(pts) or pts < 0.0:
            return None

        # --- 死时钟检测 -------------------------------------------------
        # 无音轨文件（cv2.VideoWriter 的产物）get_pts() 恒为 0.0，且**不抛异常**。
        # 这里要求读数必须"动起来"：一动不动满 AUDIO_CLOCK_STALL_SEC 就判死。
        # 计时从"第一次发现读数与上次相同"起算，所以暂停/拖动这些不读 pts 的
        # 时段不会累加进来，不会误杀正常音频。
        now = time.time()
        if pts != self._audio_last_pts:
            self._audio_last_pts = pts
            self._audio_frozen_since = None
        else:
            if self._audio_frozen_since is None:
                self._audio_frozen_since = now
            elif now - self._audio_frozen_since >= AUDIO_CLOCK_STALL_SEC:
                self._audio_clock_dead = True
                # 从头到尾一次都没推进过 => 这个文件根本没有音轨。
                # 顺手把音频重建也停掉：反正没有声音可放，重建只会白白
                # 花掉每次 ~0.5 秒（拖动进度条最明显）。若之前推进过，
                # 说明只是设备卡了一下，保留播放器、之后还能恢复。
                if not self._audio_calibrated:
                    self.audio_failed = True
                return None

        # --- 校准：必须"见过它推进"才承认它是主时钟 ----------------------
        # 【关键】只取音频"推进了多少"，不取它的"绝对媒体位置"。
        # 因为重建音频用的 ss 是"非精确 seek"，会回退到前一个关键帧
        # （实测 city.mp4 关键帧只有 0s/12s：请求 5 秒落到 0 秒、17 秒落到 12 秒），
        # 此时音频的绝对起点与视频位置能差一整个关键帧间隔。若拿绝对位置当
        # 主时钟，视频会被硬拽到音频那边（表现为画面卡住、甚至往回跳）。
        # 所以以"校准时的视频位置"为零点，之后 clock = 零点 + 音频推进量
        # —— 既不累积漂移，也不会拽走画面。
        # （实测 pts 的增量就是墙钟速度：atempo 加快的是媒体消耗，不是时钟本身）
        if self._audio_progress_ref is None:
            self._audio_progress_ref = pts
            self._audio_clock_base = self._current_video_seconds()
            return None                      # 校准当帧不参与同步
        if not self._audio_calibrated:
            if (pts - self._audio_progress_ref) < AUDIO_CLOCK_ADVANCE_MIN:
                return None                  # 还没证明它会走，继续盲调度
            # 观察到真实推进了：重新锚定零点。上面等待的这段时间视频是靠
            # 盲调度走的（速度是对的），所以此刻的视频位置才是可靠零点。
            self._audio_progress_ref = pts
            self._audio_clock_base = self._current_video_seconds()
            self._audio_calibrated = True
            # 【本次修复】确认音频真的在跑之后，补一次音量下发 —— 新建播放器只有
            # set_volume 才真正起作用，构造时的 ff_opts['volume'] 是不进混音的
            # （详见 _apply_volume_now）。到点由 update_frame 执行（那里本来就有
            # "推迟的音量同步"通道）。拖动重建出来的播放器通常更早就在"抢发窗口"
            # 里补过了，这一次是幂等的兜底。
            self._audio_volume_sync_at = time.time()
            return None                      # 校准当帧不参与同步

        # 【本次修复·补】读数回跳一律不采信。
        # 播放中媒体时间不可能倒退，但实测这个音频时钟偶尔会跳回去
        # （日志里出现过 12.412 → 12.285）。按它算出的时钟会落后画面一截，
        # update_frame 就会让画面"等音频"，表现为一次 ~0.16 秒的停顿 ——
        # 拖动那 0.5 秒的大卡顿修掉之后，这种小抽动反而成了最显眼的那个。
        # 处理：以新读数为基准重新对表（视频位置为零点，所以画面不会被拽走），
        # 本帧不参与同步，下一帧起照常。
        if pts < self._audio_progress_ref - AUDIO_CLOCK_REWIND_EPS:
            self._audio_progress_ref = pts
            self._audio_clock_base = self._current_video_seconds()
            return None

        return max(0.0, self._audio_clock_base
                   + (pts - self._audio_progress_ref) * self.playback_speed)

    def _silent_clock_media_seconds(self):
        """无音轨文件的墙钟主时钟：返回"此刻画面应该播到第几秒"。

        语义与 _audio_clock_media_seconds 完全一致，所以丢帧追赶 / 倍速 /
        拖动全都复用 update_frame 里同一条路径，只是时钟来源换成墙钟。

        只有确认文件没有音轨（audio_failed / _audio_clock_dead）时才启用：
        有音轨的文件必须听音频时钟的，否则会跟音频打起来。

        【本次修复】后台音频交接期间（_audio_handoff_pending）也走墙钟：
        那 0.5 秒里没有音频播放器、也就没有音频时钟，但画面必须照常按实时
        速度走，不能退化成"按帧率盲走"（那样单帧一慢整段就慢下来）。
        """
        if not (self.audio_failed or self._audio_clock_dead
                or self._audio_handoff_pending):
            return None
        if self.cap is None:
            return None
        now = time.monotonic()
        if self._silent_clock_base is None:
            # 重新对表（起播 / 跳转 / 变速 / 恢复播放 / 回绕之后）
            self._silent_clock_base = (now, self._current_video_seconds())
            return None                       # 对表当帧不参与同步
        base_wall, base_media = self._silent_clock_base
        target = base_media + (now - base_wall) * self.playback_speed
        if target - self._current_video_seconds() > SILENT_CLOCK_MAX_LAG_SEC:
            # 中途被卡了太久（系统调度、窗口最小化、磁盘卡顿……）：
            # 不追这份旧账，直接以现在为新零点，免得一次跳掉一大段画面。
            self._silent_clock_base = (now, self._current_video_seconds())
            return None
        return max(0.0, target)

    def _master_clock_media_seconds(self):
        """本帧的主时钟读数：优先音频时钟，没有音轨时退回墙钟。"""
        clock = self._audio_clock_media_seconds()
        if clock is not None:
            return clock
        return self._silent_clock_media_seconds()

    def _current_video_seconds(self):
        """当前视频播放位置（秒），用于音频重建时接续"""
        if self.cap is None:
            return 0.0
        try:
            frame = int(self.cap.get(cv2.CAP_PROP_POS_FRAMES))
        except Exception:
            frame = 0
        fps = self.video_fps if self.video_fps > 0 else 30.0
        return max(0.0, frame / fps)

    def change_volume(self, value):
        """音量滑块回调。

        记下目标值后交给 _apply_volume_now() 下发（它只对"已确认有音轨"的
        播放器调 set_volume；无音轨文件跳过 —— 那里本来也没声音可调）。
        【注意】新建的播放器**不会**自动继承音量：ff_opts['volume'] 在本构建里
        不进混音，所以拖动/变速重建之后都要补一次 set_volume（见 _apply_volume_now）。
        """
        self.audio_volume = float(value)
        self.audio_muted = False
        self.btn_mute.config(text="🔊")
        self._apply_volume_now()

    def toggle_mute(self):
        """静音开关（下发路径同 change_volume）"""
        self.audio_muted = not self.audio_muted
        self.btn_mute.config(text="🔇" if self.audio_muted else "🔊")
        self._apply_volume_now()

    def _get_mesh(self, h, w):
        """缓存 remap 用的坐标网格，避免每帧重建"""
        if self._mesh_hw != (h, w):
            x = np.arange(w, dtype=np.float32)
            y = np.arange(h, dtype=np.float32)
            self._mesh_x, self._mesh_y = np.meshgrid(x, y)
            self._mesh_hw = (h, w)
        return self._mesh_x, self._mesh_y

    def _process_3d(self, frame):
        """把 SBS 原始帧处理成当前显示模式的帧"""
        if self.display_mode == "2D":
            return frame

        h, w = frame.shape[:2]
        w_half = w // 2
        if w_half <= 0:
            return frame

        # 【P2 修复】原来这里无条件做两次 cv2.resize(left/right_eye, (w_half, h))：
        #   - w 为偶数时（绝大多数情况）切片宽度正好就是 w_half，
        #     目标尺寸与源尺寸完全相同 → 纯空操作，每帧白做两次全画幅拷贝；
        #   - w 为奇数时右眼比左眼宽 1 像素，这时才真的需要裁到同一宽度。
        # 所以改成"只在宽度不一致时才 resize"。另外切片本身是**非连续视图**
        # （行跨距还是整幅宽度），统一用 ascontiguousarray 交一份连续内存给
        # 后面的 cv2.split / cv2.remap，避免它们内部再偷偷转换。
        left_eye = frame[:, 0:w_half]
        right_eye = frame[:, w_half:w]
        left_eye = (cv2.resize(left_eye, (w_half, h)) if left_eye.shape[1] != w_half
                    else np.ascontiguousarray(left_eye))
        right_eye = (cv2.resize(right_eye, (w_half, h)) if right_eye.shape[1] != w_half
                     else np.ascontiguousarray(right_eye))

        # 【新增】交换左右眼
        if self.swap_eyes:
            left_eye, right_eye = right_eye, left_eye

        # 【新增】实时视差调节
        extra = self.parallax_value
        if extra != 0:
            x_base, y_base = self._get_mesh(h, w_half)
            left_eye = cv2.remap(left_eye, x_base + extra, y_base,
                                 cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            right_eye = cv2.remap(right_eye, x_base - extra, y_base,
                                  cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

        # 3D 合成（逻辑与原代码一致）
        if self.display_mode == "红蓝3D":
            b_l, g_l, r_l = cv2.split(left_eye)
            b_r, g_r, r_r = cv2.split(right_eye)
            return cv2.merge((b_l, g_l, r_r))

        elif self.display_mode == "偏振-隔行":
            combined = np.zeros_like(left_eye)
            combined[0::2, :] = left_eye[0::2, :]
            combined[1::2, :] = right_eye[1::2, :]
            return combined

        elif self.display_mode == "偏振-隔列":
            combined = np.zeros_like(left_eye)
            combined[:, 0::2] = left_eye[:, 0::2]
            combined[:, 1::2] = right_eye[:, 1::2]
            return combined

        elif self.display_mode == "偏振-棋盘":
            combined = np.zeros_like(left_eye)
            combined[0::2, 0::2] = left_eye[0::2, 0::2]
            combined[0::2, 1::2] = right_eye[0::2, 1::2]
            combined[1::2, 0::2] = right_eye[1::2, 0::2]
            combined[1::2, 1::2] = left_eye[1::2, 1::2]
            return combined

        return frame

    def _refresh_paused_frame(self):
        """暂停状态下按当前设置重算并重绘一帧。

        【P2 修复】change_parallax 与 toggle_swap_eyes 结尾的三行完全一样，
        提取到这里。播放中不需要调用 —— 下一帧自然会带上新设置。
        """
        if self.is_paused and self.current_raw_frame is not None:
            self.current_frame_image = self._process_3d(self.current_raw_frame)
            self.render_current_frame()

    def change_parallax(self, value):
        """视差滑块回调"""
        self.parallax_value = float(value)
        self.parallax_label.config(text=f"视差: {int(self.parallax_value)}")
        self._refresh_paused_frame()

    def toggle_swap_eyes(self):
        """交换左右眼"""
        self.swap_eyes = not self.swap_eyes
        self.btn_swap_eyes.config(
            text="已交换左右眼" if self.swap_eyes else "交换左右眼"
        )
        self._refresh_paused_frame()

    def setup_dark_theme(self):
        """配置全局深色主题：clam 风格 + 深灰配色"""
        self.root.configure(bg=DARK_BG)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            style.theme_use("alt")

        # 全局 ttk 默认样式
        style.configure(".",
                        background=DARK_BG,
                        foreground=DARK_FG,
                        fieldbackground=DARK_BG3,
                        bordercolor=DARK_BG3,
                        lightcolor=DARK_BG3,
                        darkcolor=DARK_BG3,
                        troughcolor=DARK_BG2,
                        focuscolor=DARK_BG3,
                        )

        # Scale（播放进度、音量、视差）
        style.configure("TScale",
                        background=DARK_BG,
                        troughcolor=DARK_BG2,
                        bordercolor=DARK_BG2,
                        lightcolor=DARK_HL,
                        darkcolor=DARK_HL,
                        )
        style.map("TScale",
                  background=[("active", DARK_HL)],
                  )

        # Progressbar（转换进度）
        style.configure("TProgressbar",
                        background=DARK_ACCENT,
                        troughcolor=DARK_BG2,
                        bordercolor=DARK_BG2,
                        lightcolor=DARK_ACCENT,
                        darkcolor=DARK_ACCENT,
                        )

        # Combobox（3D模式、倍速）
        style.configure("TCombobox",
                        fieldbackground=DARK_BG3,
                        background=DARK_BG3,
                        foreground=DARK_FG,
                        arrowcolor=DARK_FG,
                        bordercolor=DARK_BG3,
                        )
        style.map("TCombobox",
                  fieldbackground=[("readonly", DARK_BG3)],
                  foreground=[("readonly", DARK_FG)],
                  background=[("readonly", DARK_BG3)],
                  )

        # Combobox 下拉列表（这是 tk.Listbox，需要用 option_add 单独配）
        self.root.option_add("*TCombobox*Listbox.background", DARK_BG3)
        self.root.option_add("*TCombobox*Listbox.foreground", DARK_FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", DARK_ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

if __name__ == "__main__":
    root = tk.Tk()
    app = VideoPlayerApp(root)
    root.mainloop()