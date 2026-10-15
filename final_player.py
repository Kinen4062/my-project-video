import os
import sys       # 【P2】退出前冲刷输出缓冲
import shutil  # 用于复制文件
import tempfile  # 【P1】转换产物落到系统临时目录，不再污染工作目录
import uuid      # 【P1】每次转换生成唯一临时文件名，避免互相覆盖

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import cv2
import torch
import numpy as np
from PIL import Image, ImageTk
import threading
import time
import math
import datetime    # 【第 20 轮】把配额的 UTC 重置时刻换成本地时钟（见 _format_quota_reset）
import queue      # 【第 20 轮】后台线程 → 主线程的唯一安全通道（见 _poll_online）
from transformers import AutoImageProcessor, AutoModelForDepthEstimation
from ffpyplayer.player import MediaPlayer

# ============================================================
# 【第 12 轮·重构】本地模块
# ------------------------------------------------------------
# 从本文件搬走的都是"不读写 VideoPlayerApp 状态"的纯函数，各自成文件：
#   media_tools —— ffmpeg 相关（判音轨 / 补音轨 / 删临时文件）
#   depth3d     —— SBS 原始帧 → 当前显示模式的帧（含 remap 网格缓存）
#   viewfit     —— 取景计算（min 缩放几何 + 四周模糊背景的缓存）
# 本文件仍然保留同名薄封装方法（`_mux_audio_into_video` / `_process_3d` /
# `_get_letterbox_bg`），所以**调用点一行都不用改**。
#
# 【第 14 轮·播放列表与拖放】又多两个，同样是"不碰 app 状态"的独立件：
#   playlist_store —— 播放列表的落盘（一个小 JSON，读写失败静默降级）
#   filedrop       —— 从资源管理器拖文件进来（纯 ctypes，无需第三方库）
# 它们都跟着仓库一起走，不装任何新依赖。
#
# 【第 17 轮·记忆播放位置】再来一个，与 playlist_store 是一对：
#   resume_store —— "上次看到哪儿"的落盘（同样是配置目录下的小 JSON）
# 注意它的**姊妹关系**：两者都只管"数据怎么存"，"什么时候存"由本文件决定
# （见 _settle_current_position）。这里再没有别的账要算。
#
# 【第 18 轮·字幕】还是同一个路子：
#   subtitle —— SRT/VTT 的解析、找同名字幕、认语种、把文字渲染成图层
# 它同样**不知道播放器存在**（不读 app 状态、不碰控件）。本文件只负责
# "什么时候加载 / 显示哪一种 / 画在画面哪一处"，以及多出来的那两个控件的状态。
# 分工与 media_tools / depth3d / viewfit 完全一致。
# ============================================================
import media_tools
import depth3d
import viewfit
import playlist_store
import resume_store
import subtitle
# 【第 20 轮·在线字幕匹配】去 OpenSubtitles 要一份现成的字幕。
# 与其它本地模块同一路子：纯逻辑、不碰 Tk、网络层可注入（所以它能离线测）。
# **不引入任何新依赖** —— 它只用已经装好的 requests。
import subtitle_online
import filedrop
# 下面这个名字只是**再导出**：实现已经不在本文件里了，
# 但测试套件引用 `final_player.has_audio_stream`，所以要留着。
from media_tools import has_audio_stream   # noqa: F401  (re-export)

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

# 【第 22 轮】原来这里还有一个 BTN_SUCCESS（绿底，给【导出3D视频】用）。
# 那个按钮并进【转换为3d】之后它就没有任何引用了 —— 按本仓库对"只声明、
# 不使用"的一贯态度（P2 那轮清过一批）删掉，要用再照 BTN_PRIMARY 加回来。

# 【第 14 轮·播放列表】右侧列表栏的宽度（像素）。
# 用"固定宽度 + grid_propagate(False)"而不是让 Listbox 按字符数自己撑：
# 列表项是文件路径，长度千差万别，若按内容撑宽，窗口会被一条长路径顶变形。
PLIST_MIN_WIDTH = 236

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
# 【第 18 轮】多了 "v"（切换字幕模式）。
# 【第 19 轮】多了 "b"（开关独立字幕栏）。
# 加字母键只影响"让路矩阵"里那张输入框的清单（输入框本来就该吃掉所有字母键），
# 所以规则表一行都不用改。
#
# ⚠️ 这张表**必须**与 `_bind_global_shortcuts` 里真正绑定的序列一致 ——
# 第 19 轮把它改成了"照着这张表生成绑定"，就是为了堵住第 18 轮那个坑：
# 当时表里有 "v"、`_on_key_shortcut` 里也实现了 "v"，但绑定清单是手写的、
# 漏掉了 `<Key-v>`，于是**测试全绿、真机上 V 键完全没反应**。
_ALL_SHORTCUT_KEYS = _ARROW_KEYS + ("space", "m", "f", "j", "k", "l", "v", "b")

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
# 【第 15 轮·修复】"换文件"这条路径当初漏了：open_video 里是**同步**调
#   close_player()，于是每点一次播放列表就卡一次（实测 440~810ms；连续切换
#   8 次合计 1565ms，其中 83% 花在这一句上）—— 症状和当初"拖一下卡一下"
#   一模一样，只是触发方式从"拖进度条"变成了"点列表"。
#   现在换文件也走"先建后关"：_detach_audio_player()（摘下 + 止声，不阻塞）
#   → 建新播放器 → _retire_audio_player()（旧的留到新音频确认在跑再后台关）。
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
# 【第 16 轮·新】"这个文件有没有音轨"的探测缓存条数上限。
# 每次探测要起一个 ffmpeg 子进程（实测 ~120ms），所以结论按
# `(绝对路径, 文件大小, 修改时间)` 缓存 —— 同一个文件第二次打开是 0ms，
# 而文件被重新转换/替换（大小或时间变了）时缓存会自动失效。
AUDIO_PROBE_CACHE_MAX = 32
# 启动后预探测播放列表里前多少个文件（见 _warm_audio_probe_cache）。
# 用户点列表往往是连着点的，趁空闲先算好，点哪个都是 0ms。
AUDIO_PROBE_WARM_MAX = 20
AUDIO_PROBE_WARM_DELAY_MS = 1000  # 启动后隔多久开始预热（先让界面和首次播放就位）

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

# 【P2 修复】"四周那圈模糊背景每 N 帧重算一次"这条策略连同它的常量一起搬到了
# `viewfit.py`（见 `viewfit.BG_REFRESH_INTERVAL` / `viewfit.LetterboxBgCache`）——
# 它是纯取景计算，跟窗口控件无关。

# 【P2 修复】全屏时"鼠标移动 → 显示底栏"的事件节流（毫秒）。
# 不节流的话，鼠标每移动一个像素就会 after_cancel + after 一次。
MOTION_THROTTLE_MS = 100

# 【P2 修复】关闭窗口时，最多等转换线程多久（秒）去响应取消、
# 删掉写到一半的临时文件，然后才强制退出进程。
CONVERSION_JOIN_TIMEOUT_SEC = 1.0

# ============================================================
# 【第 17 轮·新】记忆播放位置
# ------------------------------------------------------------
# 需求：播放器关掉、或者切去别的视频之后再点回这个没看完的片子时，
# 让用户选"从头播"还是"接着看"。
#
# 整个功能只需回答一个问题：**这一次离开，算不算"没看完"？**
# 下面两条阈值就是全部判据，一句话能讲清：
#
#   停在 RESUME_MIN_POSITION_SEC 秒以内 → 当作"还没开始看"，不值得记也不值得问
#   距末尾 RESUME_TAIL_SEC 秒以内      → 当作"看完了"，顺手把记录清掉
#   中间那一大段                        → 记下来，下次问一句
#
# 两个值取得一样（5 秒）纯属省得记两套数：偏头的 5 秒和偏尾的 5 秒，在
# 观感上都是"基本没看"和"基本看完了"。改大了不会出错，只会让询问变多
# （用户被问烦了自然会点"从头"），所以宁可保守。
#
# ⚠️ 判"看完"只用"距末尾多少秒"，**不用比例**。曾经想过"过了 95% 也算完"，
#    但 2 小时的片子剩 6 分钟（95%）明明还想接着看，按比例判就会把记录清掉。
#    按绝对秒数判，长片短片一视同仁。
RESUME_MIN_POSITION_SEC = 5.0
RESUME_TAIL_SEC = 5.0

# ============================================================
# 【第 18 轮·新】双语字幕
# ------------------------------------------------------------
# 需求：只支持中文字幕与英文字幕的双语字幕，其它语言留扩展位但不做。
#
# "双语"的落点：**字幕文字是画在"合成好的一整屏"上的**（见 render_current_frame），
# 而不是做成一个 Tk 控件贴在画面下方。这么选有三个理由：
#   ① 缩放/全屏/3D 模式/暂停重绘全都自动带上，不需要各自处理一遍；
#   ② 字幕位置能跟着"视频画面那块矩形"走（有黑边时贴着视频底、而不是窗口底）；
#   ③ 截图就是所见即所得。
# 代价是每帧多一次 `paste` —— 所以文字本身按内容缓存成图层（见 subtitle.py）。
#
# 显示模式：关 / 中文 / 英文 / 中英双语。四种模式的名字在这里定义，
# **顺序即 V 键的循环顺序**。
SUBTITLE_MODES = ("off", "zh", "en", "both")
SUBTITLE_MODE_LABELS = {"off": "关闭", "zh": "中文", "en": "英文", "both": "中英双语"}
# 默认"中英双语"：有中英两条就上下两行一起显示，只有一种语言时自然只显示那一种
# （见 SubtitleSet.lines_for）。这是"双语字幕"这个需求直接对应的默认值。
SUBTITLE_DEFAULT_MODE = "both"

# 字号 = 视频显示高度 × 这个比例（所以全屏时字幕会跟着变大）。
SUBTITLE_FONT_SIZE_RATIO = 0.042
SUBTITLE_FONT_SIZE_MIN = 14       # 窗口再小也要看得清（视频区只有 200px 高时按比例只有 8px）
SUBTITLE_FONT_SIZE_MAX = 64       # 4K 全屏时别涨到糊脸
SUBTITLE_WIDTH_RATIO = 0.92       # 一行字幕最多占视频宽度的比例，超了就折行
SUBTITLE_BOTTOM_MARGIN_RATIO = 0.05   # 距视频画面底部留多少（按视频显示高度算）
SUBTITLE_BOTTOM_MARGIN_MIN = 4

# 【第 19 轮·新】独立字幕栏 —— 把字幕挪到画面之外的一条横条里。
#
# 起因：字幕画在画面内底部会遮住画面下缘一条。2D 看片时这是所有播放器的标准
# 形态、用户也熟，但**3D 模式的价值就是沉浸感**，遮挡在那里最扎眼。
#
# 为什么不做"放进黑边里"：黑边只在"窗口宽高比 ≠ 视频宽高比"时才存在 ——
# 全屏看 16:9 片子装在 16:9 显示器上时画面刚好铺满，黑边是 0 像素，字幕无处可放，
# 只能再退回画面内。那种方案是"有时候行"，还要写一套 fallback。
# 从窗口底部**切**一条出来则永远有效：代价写死在明面上（画面小一点），
# 换来"字幕与画面彻底互不干扰"。
#
# 默认关闭（`subtitle_bar = False`）：不改变任何人已经习惯的形态。
SUBTITLE_BAR_RATIO = 0.14          # 字幕栏高度 = 窗口高度 × 这个比例
SUBTITLE_BAR_MIN = 48              # 再矮就放不下"中文一行 + 英文一行"
SUBTITLE_BAR_MAX = 150             # 别把画面吃掉一大块
SUBTITLE_BAR_MIN_WINDOW_H = 260    # 窗口比这还矮就干脆不启用（画面会被压得没法看）
# ⚠️ 这是 **BGR**：它被写进还没 `cvtColor` 的 canvas（见 render_current_frame）。
# 换颜色时别忘了通道顺序反着看，否则想要深蓝会得到深红。
SUBTITLE_BAR_COLOR_BGR = (12, 12, 12)
SUBTITLE_BAR_FONT_RATIO = 0.30     # 栏里的字号按**栏高**算（栏是固定高度的一条）
SUBTITLE_BAR_FONT_MIN = 13
SUBTITLE_BAR_FONT_MAX = 40

# 【第 20 轮】在线匹配：主线程多久看一次后台线程的结果（毫秒）。
# 80ms 是"手感"和"开销"的折中 —— 匹配本身要好几秒，轮询再密也只是白烧 CPU，
# 再疏就会让"匹配完成"明显滞后于实际完成。
ONLINE_POLL_INTERVAL_MS = 80


def _format_quota_reset(value):
    """把接口给的 UTC 重置时刻转成**本地时钟**，返回 `"HH:MM"`；读不出来返回 `""`。

    为什么非转不可：官方按 **UTC 午夜** 重置下载配额 —— 真机验证拿到的原话是
    `reset_time_utc = 2026-10-14T23:59:59.999Z`。对 UTC+8 的用户来说那是
    **第二天早上 8 点**，所以直接说「今天还剩 N 次」等于告诉用户一个错误的
    重置时刻：早上 7 点看到「0 次」，9 点又冒出「5 次」，像是额度乱跳。
    改成「还剩 N 次（08:00 重置）」之后，"还剩几次"和"什么时候恢复"就都对得上了。

    ⚠️ 只用标准库。**别引第三方库来解析时间**，也别假设输入一定是 ISO ——
    真机上见过尾部是 `...Z`，测试里为了让格式容错用的是空格分隔。所以这里
    两种都试，全失败就返回空串（宁可少说一句，也不能因为一句话让匹配结果报错）。
    """
    if not value:
        return ""
    text = str(value).strip()
    moment = None
    try:
        moment = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                        "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                moment = datetime.datetime.strptime(text, pattern)
                break
            except Exception:
                continue
    if moment is None:
        return ""
    if moment.tzinfo is None:
        # 没有时区信息的当成 UTC —— 接口给的就是 UTC，它只是不总是带后缀。
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    try:
        return moment.astimezone().strftime("%H:%M")
    except Exception:
        return ""

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
#
# 【第 12 轮·重构】mux 的实现与它的两个常量（超时 / 码率）搬去了
# `media_tools.py`；**这里只留查找与缓存**。之所以不一起搬：下面这对全局变量
# 是测试伪造"本机没有 ffmpeg"的唯一接缝（见 tests/test_audio_mux.py 第 ④ 节），
# 留在原处那个接缝才继续有效 —— 搬走就得改测试，等于削弱防线。
# ============================================================
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


def format_hms(seconds):
    """秒 → `"12:34"` / `"1:02:34"`（负数、非数字一律按 0 处理）。

    【第 17 轮·新】"记忆播放位置"要给用户看时间（"上次看到 12:34 / 45:00"），
    而进度条那边只在**窗口标题**里显示"秒"，没有格式化函数可用。写成模块级
    纯函数是为了能在测试里单独量它（边界：0 / 59 / 60 / 3599 / 3600 / 负数）。
    """
    try:
        total = int(max(0.0, float(seconds)))
    except (TypeError, ValueError):
        total = 0
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%d:%02d" % (minutes, secs)


# `has_audio_stream()` 与 `_remove_quietly()` 的实现已搬到 media_tools.py。
# 前者仍在本模块出现 —— 那是**再导出**（见文件顶部的 import），测试套件要用。


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


class ResumeDialog:
    """【第 17 轮·新】"这个片子要接着看吗"的模态对话框。

    ## 为什么不用 messagebox

    三选一（继续 / 从头 / 取消）在 `messagebox.askyesnocancel` 里只能映射成
    "是 / 否 / 取消"，按钮文字跟含义对不上 —— 用户得先读懂问题、再去猜哪个
    按钮是"从头"。自建一个 Toplevel，三个按钮直接把动作写在脸上。

    ## 为什么"建"与"等"要拆开

    `__init__` 只把窗口搭出来，`show()` 才 `grab_set` + `wait_window`。
    这样测试可以**只建不等**：直接 `_pick("resume")` 就能验证三个返回值，
    不必去点真实窗口 —— 模态等待会把无人值守的回归整轮挂死。

    ## 三条退出路径都算数

    回车 = 继续（默认动作）、Esc = 取消、点标题栏的 × = 取消。
    一个都不能漏：漏了 × 的那次，`wait_window` 返回后 `choice` 是 None，
    照旧按"取消"处理 —— 所以这里即使不绑 `WM_DELETE_WINDOW`，行为也正确。
    绑它是为了让三条路径都**显式**在代码里看得见。
    """

    def __init__(self, master, file_path, sec, dur):
        self.choice = None
        self.top = tk.Toplevel(master)
        self.top.title("继续播放？")
        self.top.configure(bg=DARK_BG)
        self.top.resizable(False, False)
        try:
            self.top.transient(master)
        except tk.TclError:
            pass

        wrap = tk.Frame(self.top, bg=DARK_BG, padx=18, pady=14)
        wrap.pack(fill=tk.BOTH, expand=True)

        tk.Label(wrap, text="这个视频上次没有看完", bg=DARK_BG, fg=DARK_FG,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        tk.Label(wrap, text=os.path.basename(file_path) or file_path,
                 bg=DARK_BG, fg=DARK_ACCENT, font=("Microsoft YaHei UI", 10),
                 wraplength=400, justify="left").pack(anchor="w", pady=(4, 8))
        tk.Label(wrap,
                 text="上次看到 %s / %s，还剩 %s\n要接着看，还是从头开始？"
                      % (format_hms(sec), format_hms(dur),
                         format_hms(max(0.0, dur - sec))),
                 bg=DARK_BG, fg=DARK_FG2, justify="left",
                 font=("Microsoft YaHei UI", 10)).pack(anchor="w")

        row = tk.Frame(wrap, bg=DARK_BG)
        row.pack(fill=tk.X, pady=(14, 0))
        # 默认（回车）是"继续播放"：这是用户点回来时更可能想要的那个。
        self.btn_resume = tk.Button(row, text="继续播放",
                                    command=lambda: self._pick("resume"),
                                    **BTN_PRIMARY)
        self.btn_restart = tk.Button(row, text="从头播放",
                                     command=lambda: self._pick("restart"),
                                     **BTN_NORMAL)
        self.btn_cancel = tk.Button(row, text="取消",
                                    command=lambda: self._pick(None),
                                    **BTN_NORMAL)
        self.btn_resume.pack(side=tk.LEFT)
        self.btn_restart.pack(side=tk.LEFT, padx=8)
        self.btn_cancel.pack(side=tk.RIGHT)

        self.top.bind("<Return>", lambda e: self._pick("resume"))
        self.top.bind("<Escape>", lambda e: self._pick(None))
        self.top.protocol("WM_DELETE_WINDOW", lambda: self._pick(None))
        self.btn_resume.focus_set()
        self.top.update_idletasks()
        self._center_on(master)

    def _center_on(self, master):
        """摆在主窗口偏上一点的位置 —— 正中间会压住画面正中的主体。"""
        try:
            w, h = self.top.winfo_reqwidth(), self.top.winfo_reqheight()
            x = master.winfo_rootx() + max(0, (master.winfo_width() - w) // 2)
            y = master.winfo_rooty() + max(0, (master.winfo_height() - h) // 3)
            self.top.geometry("+%d+%d" % (x, y))
        except Exception:
            pass

    def _pick(self, choice):
        """记下选择并关掉窗口（`show()` 的 `wait_window` 随之返回）。"""
        self.choice = choice
        try:
            self.top.destroy()
        except Exception:
            pass

    def show(self):
        """等用户点完；返回 `"resume"` / `"restart"` / `None`（取消）。"""
        try:
            self.top.grab_set()
            self.top.wait_window()
        except Exception:
            pass
        return self.choice


class SubtitleSearchDialog:
    """【第 21 轮·新】"自己挑一份字幕"的模态对话框。

    ## 为什么非要有它

    实测出来的硬伤：**文件名里没有年份时，自动匹配没法确定是哪一部片。**
    搜 `The.Matrix.mkv` 返回的 50 条里**一条正主都没有**（32 条续集 + 14 条
    续集 + 4 条纪录片）；搜 `阿凡达.mkv` 返回 32 条《阿凡达 2》(2020) 和
    18 条《阿凡达》(2009)。这不是排序问题 —— 接口给回来的候选里就混着
    好几年的片子，怎么排都定不下来。

    这种情况与其硬下一份错的（白花一次当天配额，而且字幕画到屏幕上才
    发现对不上），不如把候选摆出来让人一眼挑走。改片名/年份能重搜，
    所以连"接口对中文剧名支持不好"这类问题（实测搜「权力的游戏」返回
    0 条）也有了出路 —— 换个英文名再搜。

    ## 跟 ResumeDialog 同一套范式

    `__init__` 只建、`show()` 才 `grab_set` + `wait_window`：测试可以只建
    不等（模态等待会把无人值守的回归整轮挂死）。

    ## 搜索为什么在后台线程

    搜索是网络请求，卡住就是整个窗口没响应。但 `after` **绝不能从后台
    线程调用**（实测 `RuntimeError: main thread is not in main loop`，
    而且后台线程自己抓不住这个异常）—— 所以跟主窗口那边一样，走
    "队列 + 主线程自我续期的 after 轮询"。关窗口时**必须 `after_cancel`**：
    窗口都销毁了还在排 after，只会换来一串 `invalid command name ...`。
    """

    def __init__(self, master, video_path, api_key, candidates=None,
                 info=None, reason="", video_fps=0.0, session=None):
        self.choice = None
        self.candidates = []
        self.session = session
        self.api_key = api_key
        self.video_path = video_path
        self.video_fps = video_fps or 0.0
        self.info = dict(info or {})
        self._searching = False
        self._after_id = None
        self._queue = None

        self.top = tk.Toplevel(master)
        self.top.title("挑一份字幕")
        self.top.configure(bg=DARK_BG)
        self.top.resizable(False, False)
        try:
            self.top.transient(master)
        except tk.TclError:
            pass

        wrap = tk.Frame(self.top, bg=DARK_BG, padx=18, pady=14)
        wrap.pack(fill=tk.BOTH, expand=True)

        if reason:
            tk.Label(wrap, text=reason, bg=DARK_BG, fg=DARK_ACCENT,
                     font=("Microsoft YaHei UI", 10, "bold"),
                     wraplength=520, justify="left").pack(anchor="w", pady=(0, 8))

        # ---- 搜什么 ----
        row = tk.Frame(wrap, bg=DARK_BG)
        row.pack(fill=tk.X)
        tk.Label(row, text="片名:", bg=DARK_BG, fg=DARK_FG).pack(side=tk.LEFT)
        self.entry_query = tk.Entry(row, bg=DARK_BG3, fg=DARK_FG,
                                    insertbackground=DARK_FG, relief=tk.FLAT,
                                    width=34)
        self.entry_query.pack(side=tk.LEFT, padx=(5, 12), ipady=3)
        tk.Label(row, text="年份:", bg=DARK_BG, fg=DARK_FG).pack(side=tk.LEFT)
        self.entry_year = tk.Entry(row, bg=DARK_BG3, fg=DARK_FG,
                                   insertbackground=DARK_FG, relief=tk.FLAT,
                                   width=6)
        self.entry_year.pack(side=tk.LEFT, padx=(5, 12), ipady=3)
        self.btn_search = tk.Button(row, text="搜索", command=self._start_search,
                                    **BTN_PRIMARY)
        self.btn_search.pack(side=tk.LEFT)

        # 年份是这一类问题的总开关（见类注释），所以要在界面上说一句 ——
        # 不说的话没人会想到去填它。
        tk.Label(wrap, text="填上年份能精确得多（同名/同系列片子靠它分开）",
                 bg=DARK_BG, fg=DARK_FG2,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(4, 8))

        # ---- 候选列表 ----
        box = tk.Frame(wrap, bg=DARK_BG)
        box.pack(fill=tk.BOTH, expand=True)
        self.listbox = tk.Listbox(
            box, height=12, width=70, bg=DARK_BG3, fg=DARK_FG,
            selectbackground=DARK_ACCENT, selectforeground="white",
            highlightthickness=0, bd=0, activestyle="none",
            font=("Microsoft YaHei UI", 9))
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(box, orient=tk.VERTICAL,
                               command=self.listbox.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.config(yscrollcommand=scroll.set)

        self.status = tk.Label(wrap, text="", bg=DARK_BG, fg=DARK_FG2,
                               font=("Microsoft YaHei UI", 9),
                               wraplength=520, justify="left")
        self.status.pack(anchor="w", pady=(8, 0))

        # ---- 按钮 ----
        buttons = tk.Frame(wrap, bg=DARK_BG)
        buttons.pack(fill=tk.X, pady=(12, 0))
        self.btn_ok = tk.Button(buttons, text="下载这份", command=self._pick_selected,
                                state=tk.DISABLED, **BTN_PRIMARY)
        self.btn_ok.pack(side=tk.LEFT)
        self.btn_cancel = tk.Button(buttons, text="取消",
                                    command=lambda: self._close(None),
                                    **BTN_NORMAL)
        self.btn_cancel.pack(side=tk.RIGHT)

        self.entry_query.bind("<Return>", lambda _e: self._start_search())
        self.entry_year.bind("<Return>", lambda _e: self._start_search())
        self.listbox.bind("<Return>", lambda _e: self._pick_selected())
        self.listbox.bind("<Double-Button-1>", lambda _e: self._pick_selected())
        self.top.bind("<Escape>", lambda _e: self._close(None))
        self.top.protocol("WM_DELETE_WINDOW", lambda: self._close(None))

        if info:
            self.entry_query.insert(0, str(info.get("query") or ""))
            if info.get("year"):
                self.entry_year.insert(0, str(info["year"]))

        self.top.update_idletasks()
        self._center_on(master)

        # ⚠️ 判据必须是 `is not None` 而不是真值判断：`candidates=[]` 的意思是
        # **"已经搜过了，一条都没有"**（该铺个空列表并说句"换个词再试"），
        # 跟 `None`（"还没搜"）是两回事。用真值判断会把前者错当成后者、
        # 白白再搜一次，而且搜完之前按钮状态是错的。
        if candidates is not None:
            # 已经有候选了（自动匹配"定不下来"时带过来的）—— 直接摆出来，
            # 不再搜一次。搜索不花配额，但白等一个来回没有意义。
            self._fill(candidates)
        else:
            self._start_search()

    # ---------- 界面 ----------

    def _center_on(self, master):
        try:
            w, h = self.top.winfo_reqwidth(), self.top.winfo_reqheight()
            x = master.winfo_rootx() + max(0, (master.winfo_width() - w) // 2)
            y = master.winfo_rooty() + max(0, (master.winfo_height() - h) // 3)
            self.top.geometry("+%d+%d" % (x, y))
        except Exception:
            pass

    def _set_status(self, text):
        try:
            self.status.config(text=text)
        except Exception:
            pass

    def _fill(self, candidates):
        """把候选铺进列表。**空列表也要处理** —— 那是"换个词再搜"的提示。"""
        self._searching = False
        try:
            self.btn_search.config(text="搜索", state=tk.NORMAL)
            self.listbox.delete(0, tk.END)
        except Exception:
            return

        self.candidates = list(candidates or [])
        for candidate in self.candidates:
            self.listbox.insert(tk.END,
                                subtitle_online.describe_candidate(candidate))

        if not self.candidates:
            self.btn_ok.config(state=tk.DISABLED)
            self._set_status("一条都没搜到 —— 换个片名、或者把年份去掉再试")
            return

        self.listbox.selection_set(0)
        self.listbox.see(0)
        self.btn_ok.config(state=tk.NORMAL)
        movies = len({c.get("feature_id") or c.get("imdb_id")
                      for c in self.candidates})
        self._set_status("找到 %d 条，分属 %d 部片子 —— 选一条后点【下载这份】"
                         % (len(self.candidates), movies))

    # ---------- 搜索（后台线程 + 主线程轮询）----------

    def _start_search(self):
        if self._searching:
            return
        query = self.entry_query.get().strip()
        year_text = self.entry_year.get().strip()
        year = subtitle_online._int_or_none(year_text) if year_text else None
        if year_text and year is None:
            self._set_status("年份要填数字，比如 1999")
            return
        if not query:
            self._set_status("先填片名")
            return

        self._searching = True
        self.btn_search.config(text="搜索中…", state=tk.DISABLED)
        self.btn_ok.config(state=tk.DISABLED)
        self.listbox.delete(0, tk.END)
        self.candidates = []
        self._set_status("正在搜…")

        # 每次搜索都用一条全新的队列，不然上一次的结果会被这一次捡走
        self._queue = queue.Queue()
        thread = threading.Thread(
            target=self._worker,
            args=(query, year, self.info.get("season"),
                  self.info.get("episode")),
            name="subtitle-search", daemon=True)
        thread.start()
        self._poll()

    def _worker(self, query, year, season, episode):
        """后台线程：**只往队列里放一个元组，绝不碰任何 Tk 对象。**"""
        try:
            client = subtitle_online.OpenSubtitles(self.api_key,
                                                   session=self.session)
            found = subtitle_online.search_candidates(
                client, query=query, year=year, season=season,
                episode=episode, video_fps=self.video_fps)
            payload = ("ok", found)
        except Exception as exc:
            payload = ("error", str(exc))
        try:
            self._queue.put(payload)
        except Exception:
            pass

    def _poll(self):
        """主线程：看后台线程有没有结果。**只允许主线程调用。**"""
        if not self._searching:
            return
        try:
            payload = self._queue.get_nowait()
        except queue.Empty:
            try:
                self._after_id = self.top.after(ONLINE_POLL_INTERVAL_MS,
                                                self._poll)
            except Exception:
                self._searching = False
            return
        except Exception:
            self._searching = False
            try:
                self.btn_search.config(text="搜索", state=tk.NORMAL)
            except Exception:
                pass
            self._set_status("搜索结果读不出来，请再试一次")
            return

        if payload[0] == "ok":
            self._fill(payload[1])
        else:
            self._searching = False
            try:
                self.btn_search.config(text="搜索", state=tk.NORMAL)
            except Exception:
                pass
            self._set_status("搜索失败：%s" % payload[1])

    # ---------- 收尾 ----------

    def _pick_selected(self):
        selection = self.listbox.curselection()
        if not selection or not self.candidates:
            self._set_status("先在上面选一条")
            return
        index = selection[0]
        if 0 <= index < len(self.candidates):
            self._close(self.candidates[index])

    def _close(self, choice):
        """记下选择、**取消挂起的 after**、关窗口。"""
        self.choice = choice
        self._searching = False
        if self._after_id is not None:
            try:
                self.top.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        try:
            self.top.destroy()
        except Exception:
            pass

    def show(self):
        """等用户挑完；返回选中的候选（字典），取消则 `None`。"""
        try:
            self.top.grab_set()
            self.top.wait_window()
        except Exception:
            pass
        return self.choice


class VideoPlayerApp:
    def __init__(self, root, playlist_path=None, resume_path=None):
        """搭建整个应用。

        【第 12 轮·重构】这个方法原来是 391 行、一口气干七件事，没人能一口气
        看懂。现在按"职责 + 依赖顺序"拆成若干段，每段一个方法。

        ⚠️ **调用顺序就是原来的创建顺序**，逐行搬移、逻辑一行没改 ——
        下面这些顺序依赖是硬约束，重排会坏：
          · `_init_playback_state` 必须在建控件之前跑完（控件要拿 seek_var /
            mode_var 当 variable）；
          · `_build_control_bars` 必须在 `_finish_init` 之前（后者要同步
            parallax_frame 的显隐）；
          · 全局键的注册要等控件都建好（bind_class 会作用于之后创建的按钮）——
            所以【第 14 轮】新增的 `_build_playlist_panel` 也必须排在
            `_bind_global_shortcuts` 之前，否则列表栏里的按钮吃不到"空格=播放"。

        `playlist_path` 是播放列表 JSON 的位置；默认走 `playlist_store` 的用户
        配置目录。测试会传一个临时文件进来 —— 否则跑一次回归就会把用户真实的
        播放列表覆盖掉，而"列表里有东西"本身又会干扰用例（见 tests/_harness.py）。

        【第 17 轮】`resume_path` 是"记忆播放位置"那份 JSON 的位置，同样由测试
        传临时文件进来（理由逐字同上：不能把用户真实的观看进度覆盖掉）。
        """
        self.root = root
        self.setup_dark_theme()
        self.root.title("AI 3D 播放器 - 测试版")
        # 【第 14 轮】默认加宽到 1180：右侧多了一栏 236px 的播放列表，
        # 原来的 950 会让视频区被压得比底栏还窄。
        self.root.geometry("1180x720")

        self._init_playback_state()
        self._init_playlist_state(playlist_path)
        self._init_resume_state(resume_path)
        self._init_subtitle_state()
        self._build_video_area()
        self._build_progress_bar()
        self._build_control_bars()
        self._build_playlist_panel()
        self._bind_global_shortcuts()
        self._init_audio_state()
        self._init_session_state()
        self._finish_init()

    def _init_playback_state(self):
        """播放状态、进度条变量、各种定时器句柄、3D 调节量与显示模式、模型句柄。

        必须在建控件之前跑完：下面几个控件要拿 `seek_var` / `mode_var`
        当 `variable` 传进去。
        """
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
        # remap 坐标网格的缓存，内部自己持有 x/y 与尺寸 —— 见 depth3d.MeshCache
        self._mesh = depth3d.MeshCache()

        # --- 3D 显示模式变量 ---
        self.display_mode = "2D"
        self.mode_var = tk.StringVar(value="2D")

        # --- AI 模型相关 ---
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = None
        self.image_processor = None

    def _init_playlist_state(self, playlist_path=None):
        """【第 14 轮·新】播放列表的**纯状态**（不建控件）。

        跟 `_init_playback_state` 一样，必须在建控件之前跑完 —— `_build_playlist_panel`
        要读 `self.playlist` 才能把内容画出来。

        `self.playlist` 就是唯一的事实来源：面板上的 Listbox 只是它的视图，
        增删一律"先改列表 → 存盘 → 再刷新视图"，避免两边各改各的。
        """
        self.playlist = playlist_store.load(playlist_path)
        self._playlist_path = playlist_path or playlist_store.default_path()
        # 播不动的项（文件不存在 / 打不开）。**只影响"自动续播"的挑选**：
        # 编进这个集合是为了让片尾续播能跳过它继续往下找，不至于卡在同一项上
        # 反复重试（那就成了死循环）。用户手点这一项时照旧播放，等于手动重试。
        self._playlist_failed = set()
        # 【第 14 轮】片尾自动续播开关。默认开 —— 列表里有两个以上文件时，
        # 播完一项自动播下一项（列表尾回到第 1 项），这是播放列表该有的样子。
        # 关掉它就退回"单文件行为"：播完无缝回绕当前这一个。
        # 老回归套件会把这项关掉，因为它们量的是**单文件**的片尾行为
        # （片尾回绕卡不卡、测速窗口会不会穿过 EOF），不该被列表内容牵着走。
        self.autoplay_next = True
        self._dnd_enabled = False
        self._playlist_visible = True

    def _init_resume_state(self, resume_path=None):
        """【第 17 轮·新】"记忆播放位置"的**纯状态**（不建控件、不碰播放）。

        与 `_init_playlist_state` 是一对：同样是"用户配置目录下的小 JSON"，
        同样必须在建控件之前跑完吗 —— 其实这里**没这个要求**（没有任何控件
        读它），但紧挨着放，两口子的事一眼能对上。

        四个字段，各自存在的理由：
          · `_resume_positions` —— 内存里的记录表 `{键: 记录}`，唯一事实来源。
            查询/写入都走它，写盘只是把它序列化出去（见 _save_resume）。
          · `resume_enabled`    —— **总开关**。关掉后既不记也不读，等于退回
            第 16 轮的行为。测试基架默认关掉它（见 tests/_harness.load_app）：
            老套件会反复打开同一个文件，记录一开，第二次打开就会"静默续播"
            到上次停的位置 —— 那些用例量的东西就悄悄变了样子。
            更要紧的是：不隔离的话，跑一次回归就往用户**真实的**观看进度里
            写东西（配置文件路径同样在 _harness 里被换掉，双保险）。
          · `_resume_ignored`   —— 不该进这套体系的文件。目前只有一种：
            `open_video(record=False)` 打开的转换临时产物（躺在 %TEMP% 里，
            下次转换就没），记下来只会留下点不回去的死条目。
          · `_resume_path`      —— 这份 JSON 在哪儿（测试会换成临时文件）。
        """
        self._resume_path = resume_path or resume_store.default_path()
        self._resume_positions = resume_store.load(self._resume_path)
        self.resume_enabled = True
        self._resume_ignored = set()

    def _init_subtitle_state(self):
        """【第 18 轮·新】字幕的**纯状态**（不建控件、不碰播放）。

        必须在 `_build_control_bars` 之前跑完 —— 底栏那个"字幕模式"下拉框要拿
        `self.subtitle_var` 当 variable（与 seek_var / mode_var 同一个道理）。

        字段各自的理由：
          · `subtitle_enabled` —— **总开关**。关掉后既不加载也不绘制，等于退回
            第 17 轮的行为。测试基架默认关掉（见 tests/_harness.load_app）：
            字幕要在每帧上多贴一层，会跟几条"帧间隔 / 切换耗时"的断言抢预算，
            而一个不确定的耗时断言等于没有断言。
          · `subtitle_auto`    —— 打开视频时要不要自动找同名字幕。
            单独留一个开关是给"载入字幕（手动指定）"那条路用的：用户手选的文件
            一律加 `subtitle_auto` 的判断，不因为开关关着就被忽略。
          · `subtitle_mode`    —— 用户的意愿（关 / 中 / 英 / 中英）。**换文件不重置**：
            选了"只看中文"就该一直是只看中文。
          · `subtitle_set`     —— 当前视频可用的字幕（`SubtitleSet`，最多一中一英）。
          · `subtitle_font_path` —— 找到的中文字体；None = 画不了字幕（见
            `subtitle.find_font` 的说明，找不到就静默不画，绝不因此崩）。
          · `_last_display_image` —— 上一次真正上屏的那一屏（PIL 图，含字幕与黑边），
            截图用它做"所见即所得"。
          · `subtitle_bar` —— 【第 19 轮】独立字幕栏开关。开着时从窗口底部切出一条
            纯色横条专门放字幕，字幕因此**永不遮画面**（3D 看片时尤其需要）；
            代价是画面为了腾地方缩小一点。**默认关**：画在画面内底部是所有播放器
            的标准形态，不该被悄悄改掉。换文件不重置（和 `subtitle_mode` 一样，
            它是"我怎么看片"的偏好，不是"这个视频"的属性）。
        """
        self.subtitle_enabled = True
        self.subtitle_auto = True
        self.subtitle_mode = SUBTITLE_DEFAULT_MODE
        self.subtitle_bar = False
        self.subtitle_set = subtitle.SubtitleSet()
        self.subtitle_font_path = subtitle.find_font()
        self._subtitle_renderer = subtitle.SubtitleRenderer(self.subtitle_font_path)
        self._last_display_image = None
        self.subtitle_var = tk.StringVar(
            value=SUBTITLE_MODE_LABELS[SUBTITLE_DEFAULT_MODE])
        self.subtitle_bar_var = tk.BooleanVar(value=False)

        # 【第 20 轮·新】在线匹配的几个状态位。
        #   · `online_api_key` —— 从用户配置目录读进来的 key（**首次用到才读**，
        #     见 `_online_api_key`：这个方法按约定只做纯状态初始化、不碰文件）。
        #     **绝不写死在代码里、绝不进版本库**：官方要求一个应用一个 key，
        #     而这个程序只在这台机器上给你自己用，走的也是你自己的每日配额。
        #     读失败就是空串（见 subtitle_online.load_config，它不抛）。
        #   · `_online_busy`    —— 同一时刻只允许一个匹配在跑（网络慢，
        #     连点几下会开好几条线程、白烧配额）。
        #   · `_online_tried`   —— 视频路径 → 已经下过的 file_id 集合。
        #     用户对匹配结果不满意时再点一次，会**往前推进到下一条候选**，
        #     而不是把同一条又下一遍（那样既费配额又永远拿到同一份）。
        #     按路径分开记：换一部片子就该从头挑。
        self.online_api_key = None
        self._online_busy = False
        self._online_tried = {}
        # 【第 20 轮】后台线程的结果通过这条队列回到主线程。
        # ⚠️ 别改成"后台线程直接调 root.after" —— 那会抛
        # `RuntimeError: main thread is not in main loop`（只有 mainloop
        # 正在转的时候才侥幸不炸），而且**后台线程抓不到这个异常**，
        # 结果是 `_online_busy` 永远停在 True、按钮永远点不动。实测踩过。
        self._online_queue = None
        self._online_thread = None

    def _build_video_area(self):
        """顶层 grid 布局 + 视频显示区（video_container / video_label）。

        为什么必须用 grid 而不是 pack：见下面那段长注释 —— pack 布局下
        video_label 的"请求尺寸"会随图像变大，把后打包的底栏挤出可视区。
        """
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

    def _build_progress_bar(self):
        """进度条（row 1）与它的三个交互事件"""
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

    def _build_control_bars(self):
        """底栏两行（主操作 / 设置项）、转换进度面板、视差与左右眼控制栏。

        【P2】底栏原来是一整行塞 12 个控件（音量+静音、打开、转换、播放、
        3D模式、截图、导出、倍速…），950px 宽必然拥挤甚至被裁，所以按用途
        拆成两行；转换进度面板与视差栏顺延到 row 4 / row 5。
        """
        # ============================================================
        # 【P2 修复】底栏原来是一整行塞 12 个控件（音量+静音、打开、转换、
        # 播放、3D模式、截图、导出、倍速…），950px 宽必然拥挤甚至被裁。
        # 现在按用途拆成两行：
        #   row 2  btn_frame —— 主操作（打开视频 / 转换为3d / 播放 / 截图）
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
        # 【第 22 轮·改】这一行原来有三个按钮：
        #     打开已有3D视频 / 导入2D视频一键转3D / 导出3D视频
        # 合成两个：
        #     【打开视频】  选文件就播 —— 原「打开已有3D视频」。去掉名字里的
        #                   "3D"是因为它本来就跟源文件无关：本程序对任何视频
        #                   都是**实时** 2D→3D 渲染，"打开"只是打开。
        #     【转换为3d】  源 = 当前视频（没有就选一个）→ 转 → **转完问存哪儿**。
        #                   原来「导入2D视频一键转3D」和「导出3D视频」是分开的
        #                   两步（转完只留一句"点导出可另存"），现在合成一条：
        #                   转换的自然收尾就是"要不要存下来"，导出不再是常驻按钮。
        # 【P2 修复】btn_screenshot 从来只被赋值、没有任何地方引用它，改为局部
        # 变量（Tk 控件由父容器的 children 持有，不会被回收）。真正需要保留
        # 引用的只有后面要改文案/状态的几个。
        tk.Button(self.btn_frame, text="打开视频",
                  command=self.open_video, **BTN_NORMAL).pack(side=tk.LEFT, padx=5, pady=10)

        self.btn_convert = tk.Button(self.btn_frame, text="转换为3d",
                                     command=self.start_conversion, **BTN_PRIMARY)
        self.btn_convert.pack(side=tk.LEFT, padx=5, pady=10)

        self.btn_play = tk.Button(self.btn_frame, text="播放", command=self.toggle_play, **BTN_NORMAL)
        self.btn_play.pack(side=tk.LEFT, padx=5, pady=10)

        tk.Button(self.btn_frame, text="截图",
                  command=self.save_screenshot, **BTN_NORMAL).pack(side=tk.LEFT, padx=5, pady=10)

        # 【第 11 轮·新】快捷键提示入口。放最右边，不挤占主操作区
        # （这一行本来就偏满，见 P2 记录里关于底栏拥挤的说明）。
        tk.Button(self.btn_frame, text="快捷键 (F1)",
                  command=self.show_shortcuts,
                  **BTN_NORMAL).pack(side=tk.RIGHT, padx=5, pady=10)

        # 【第 14 轮·新】播放列表显隐。放在"快捷键"按钮左边的位置：
        # 两个都是"不常用到的开关"，挤在最右边不占主操作区。
        self.btn_playlist = tk.Button(self.btn_frame, text="隐藏列表",
                                      command=self.toggle_playlist, **BTN_NORMAL)
        self.btn_playlist.pack(side=tk.RIGHT, padx=5, pady=10)

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

        # 【第 18 轮·新】字幕：模式下拉 + 手动载入。
        # 放在这一行（设置项）而不是主操作行 —— 它跟"3D模式/倍速/音量"是一类东西，
        # 都是"改看法"的设置，不是"要执行"的动作；主操作行也早就挤满了。
        tk.Label(self.aux_frame, text="字幕:", bg=DARK_BG,
                 fg=DARK_FG).pack(side=tk.LEFT, padx=(15, 5))
        self.subtitle_combo = ttk.Combobox(
            self.aux_frame,
            textvariable=self.subtitle_var,
            values=[SUBTITLE_MODE_LABELS[mode] for mode in SUBTITLE_MODES],
            width=8,
            state="readonly",
        )
        self.subtitle_combo.pack(side=tk.LEFT)
        self.subtitle_combo.bind("<<ComboboxSelected>>", self.on_subtitle_mode_change)

        # 手动载入：名字跟视频对不上号的字幕文件（下载下来常叫 1.srt / chs.srt）
        # 靠它救。自动找同名字幕是主路径，这个是兜底。
        tk.Button(self.aux_frame, text="载入字幕",
                  command=self.load_subtitle_file, **BTN_NORMAL).pack(side=tk.LEFT, padx=5)

        # 【第 19 轮·新】字幕栏开关（也可按 B 键）。勾上之后从窗口底部切出一条
        # 纯色横条专放字幕，字幕就**不再遮画面**了 —— 看 3D 时尤其有用。
        # 放在【载入字幕】右边：它跟"字幕模式"是一组（都只管字幕怎么显示）。
        self.subtitle_bar_check = tk.Checkbutton(
            self.aux_frame, text="字幕栏", variable=self.subtitle_bar_var,
            command=self.on_subtitle_bar_toggle,
            bg=DARK_BG, fg=DARK_FG, selectcolor=DARK_BG3,
            activebackground=DARK_BG, activeforeground=DARK_FG,
            highlightthickness=0, bd=0,
        )
        self.subtitle_bar_check.pack(side=tk.LEFT, padx=(10, 0))

        # 【第 20 轮·新】在线匹配（左键匹配 / 右键改 API key）。
        # 放在这一组的末尾：它跟"字幕模式 / 载入字幕 / 字幕栏"是同一件事的
        # 不同来路 —— 前面几个都是"字幕已经在手上了怎么显示"，这个是
        # "手上还没有，去弄一份来"。
        # ⚠️ 右键改 key 是刻意做成隐晦入口的：配一次就不用再动，
        # 摆在明面上会占掉底栏本来就紧张的位置（见 F1 里的说明）。
        self.btn_online = tk.Button(
            self.aux_frame, text="在线匹配",
            command=self.match_subtitles_online, **BTN_NORMAL)
        self.btn_online.pack(side=tk.LEFT, padx=(10, 0))
        self.btn_online.bind("<Button-3>", lambda _event: self.open_online_settings())

        # 【第 21 轮·新】自己搜、自己挑。存在的理由见 SubtitleSearchDialog
        # 的类注释：文件名里没有年份时自动匹配**定不下来是哪部片**
        # （实测搜 "The Matrix" 一条正主都没有），这种情况得让人看一眼。
        self.btn_online_search = tk.Button(
            self.aux_frame, text="搜索…",
            command=self.open_subtitle_search, **BTN_NORMAL)
        self.btn_online_search.pack(side=tk.LEFT, padx=(4, 0))

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

    # ============================================================
    # 【第 14 轮·新】播放列表
    # ------------------------------------------------------------
    # 数据只有一份：`self.playlist`（一个路径列表）。Listbox 只是它的**视图**，
    # 所有增删一律走"先改列表 → 存盘 → 再刷新视图"，两边不会各改各的。
    #
    # 和"打开文件"的关系：任何**用户主动播放**的视频都会自动记进列表
    # （见 open_video 的 record 参数）—— 这就是需求里的"将要播放的视频记录
    # 在播放器右侧列表中"。例外只有一种：转换产生的临时产物不算
    # （它在 %TEMP% 里，下一次转换就会被删掉，记进列表只会留下死条目）。
    # ============================================================
    def _build_playlist_panel(self):
        """右侧播放列表栏（第 1 列，跨越全部 6 行）。

        布局：顶层 grid 本来是"1 列 6 行"，这里开出第 1 列专供列表栏。
        让列表栏 rowspan=6 占满右边缘、而底栏（进度条 / 按钮 / 进度面板）
        仍然只占第 0 列 —— 于是底栏会在列表栏左边收住，不会钻到列表下面。

        列宽用 minsize + `pack_propagate(False)` 固定：列表项是文件路径，长短
        差得远，若让 Listbox 按内容撑宽，窗口会被一条长路径顶变形。

        ⚠️ 这里是 **`pack_propagate`** 不是 `grid_propagate`：面板的**子控件是
        pack 进去的**，而 `grid_propagate` 只管"用 grid 管理的子控件"，关掉它
        面板还是会被里面的 Listbox 撑成 20 个字符宽（实测 236 → 169px）。

        ⚠️ 必须在 `_bind_global_shortcuts` 之前调用 —— 那一句 `bind_class`
        只作用于"当时已经存在"的按钮（见 `__init__` 的顺序说明）。
        """
        self.root.grid_columnconfigure(1, weight=0, minsize=PLIST_MIN_WIDTH)

        self.playlist_frame = tk.Frame(self.root, bg=DARK_BG2, width=PLIST_MIN_WIDTH)
        self.playlist_frame.grid(row=0, column=1, rowspan=6, sticky="ns")
        self.playlist_frame.pack_propagate(False)

        header = tk.Frame(self.playlist_frame, bg=DARK_BG2)
        header.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(6, 2))
        self.playlist_title = tk.Label(
            header, text="播放列表", bg=DARK_BG2, fg=DARK_FG,
            font=("Microsoft YaHei", 10, "bold"),
        )
        self.playlist_title.pack(side=tk.LEFT)

        # 状态/提示行（放最底，先 pack 的 BOTTOM 在最下面）
        self.playlist_status = tk.Label(
            self.playlist_frame, text="可把视频文件直接拖进来",
            bg=DARK_BG2, fg=DARK_FG2, font=("Microsoft YaHei", 8),
            anchor="w", justify=tk.LEFT, wraplength=PLIST_MIN_WIDTH - 16,
        )
        self.playlist_status.pack(side=tk.BOTTOM, fill=tk.X, padx=6, pady=(0, 6))

        # 操作按钮（两行）
        btns = tk.Frame(self.playlist_frame, bg=DARK_BG2)
        btns.pack(side=tk.BOTTOM, fill=tk.X, padx=6, pady=(4, 2))
        row1 = tk.Frame(btns, bg=DARK_BG2)
        row1.pack(side=tk.TOP, fill=tk.X)
        row2 = tk.Frame(btns, bg=DARK_BG2)
        row2.pack(side=tk.TOP, fill=tk.X, pady=(4, 0))
        for text, cmd, parent in (
                ("添加", self.playlist_add_dialog, row1),
                ("移除", self.playlist_remove_selected, row1),
                ("清空", self.playlist_clear, row1),
                ("上一个", self.playlist_prev, row2),
                ("下一个", self.playlist_next, row2)):
            tk.Button(parent, text=text, command=cmd,
                      **BTN_NORMAL).pack(side=tk.LEFT, padx=2)

        # 列表本体 + 滚动条
        body = tk.Frame(self.playlist_frame, bg=DARK_BG2)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=4)
        self.plist_scroll = tk.Scrollbar(body, orient=tk.VERTICAL,
                                         command=lambda *a: self.playlist_box.yview(*a))
        self.plist_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.playlist_box = tk.Listbox(
            body, bg=DARK_BG3, fg=DARK_FG,
            selectbackground=DARK_ACCENT, selectforeground="#ffffff",
            highlightthickness=0, bd=0, activestyle="none",
            selectmode=tk.EXTENDED,
            # exportselection=False 是关键：否则列表框的选中会和"系统选中区"
            # 绑定，别处一选东西这边的高亮就没了，看着像"选中丢了"。
            exportselection=False,
        )
        self.playlist_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.playlist_box.config(yscrollcommand=self.plist_scroll.set)
        self.playlist_box.bind("<Double-Button-1>", self.playlist_play_selected)
        self.playlist_box.bind("<Return>", self.playlist_play_selected)
        self.playlist_box.bind("<<ListboxSelect>>", self._on_playlist_select)

    def _video_filetypes(self):
        """打开/添加对话框的过滤器 —— 与拖放共用 `filedrop.DEFAULT_VIDEO_EXTS`，
        免得"能拖进来却选不了"这种两处不一致。"""
        patterns = " ".join("*" + e for e in filedrop.DEFAULT_VIDEO_EXTS)
        return [("视频文件", patterns), ("MP4 Video", "*.mp4"), ("All Files", "*.*")]

    def _set_playlist_status(self, msg):
        try:
            self.playlist_status.config(text=msg)
        except Exception:
            pass

    def _save_playlist(self):
        return playlist_store.save(self.playlist, self._playlist_path)

    def _playlist_index_of(self, path):
        """路径在列表里的下标；不在列表里（或没在播）返回 None。

        刻意**不缓存**这个下标：列表会被增删、顺序会变，缓存出来的下标迟早
        和列表对不上。每次从 `current_video_path` 现算，永远自洽。
        """
        if not path:
            return None
        key = os.path.normcase(path)
        for i, p in enumerate(self.playlist):
            if os.path.normcase(p) == key:
                return i
        return None

    def _refresh_playlist_ui(self, select_path=None):
        """把 `self.playlist` 画到 Listbox 上（单向：列表 → 视图）。"""
        self.playlist_box.delete(0, tk.END)
        for p in self.playlist:
            # 显示文件名而不是整条路径：路径动辄上百字符，列表宽度放不下，
            # 完整路径在选中时显示到下面的状态行里。
            self.playlist_box.insert(tk.END, os.path.basename(p) or p)
        self.playlist_title.config(text="播放列表 (%d)" % len(self.playlist))

        idx = self._playlist_index_of(select_path or self.current_video_path)
        if idx is not None:
            self.playlist_box.selection_clear(0, tk.END)
            self.playlist_box.selection_set(idx)
            self.playlist_box.see(idx)

    def _on_playlist_select(self, event=None):
        """选中项变化 → 状态行显示它的完整路径（列表里只显示文件名）。"""
        sel = self.playlist_box.curselection()
        if not sel:
            return
        idx = sel[-1]
        if 0 <= idx < len(self.playlist):
            self._set_playlist_status(self.playlist[idx])

    def _playlist_add(self, paths):
        """加入列表（去重、保持顺序），返回**真正新增**的条数。"""
        added = 0
        existing = {os.path.normcase(p) for p in self.playlist}
        for p in paths:
            if not p:
                continue
            key = os.path.normcase(p)
            if key in existing:
                continue
            existing.add(key)
            self.playlist.append(p)
            self._playlist_failed.discard(p)
            added += 1
        if added:
            self._save_playlist()
            self._refresh_playlist_ui()
        return added

    def playlist_add_dialog(self):
        """"添加"按钮：多选视频加进列表；当前没有在播的话，顺手播第一个。"""
        paths = filedialog.askopenfilenames(
            title="添加到播放列表", filetypes=self._video_filetypes())
        paths = list(paths) if paths else []
        if not paths:
            return
        added = self._playlist_add(paths)
        self._set_playlist_status(
            "已加入 %d 个视频" % added if added else "这些视频已经在列表里了")
        if self.play_state == PlayState.IDLE:
            self._playlist_play(paths[0])

    def playlist_remove_selected(self):
        """从列表里移除选中项。**不停止播放** —— 移除的是列表条目，不是文件。"""
        sel = list(self.playlist_box.curselection())
        if not sel:
            self._set_playlist_status("先在上面的列表里点一下要移除的条目")
            return
        removed = 0
        for idx in sorted(sel, reverse=True):       # 从后往前删，下标才不会错位
            if 0 <= idx < len(self.playlist):
                self._playlist_failed.discard(self.playlist.pop(idx))
                removed += 1
        if removed:
            self._save_playlist()
            self._refresh_playlist_ui()
            self._set_playlist_status("已从列表移除 %d 项（文件没有被删）" % removed)

    def playlist_clear(self, confirm=True):
        """清空列表（只清条目，不删文件、不停止当前播放）。

        `confirm=False` 供测试用 —— 免得 `askyesno` 把无人值守的回归卡在模态框上。
        """
        if not self.playlist:
            return
        if confirm and not messagebox.askyesno(
                "清空播放列表",
                "把列表里的 %d 个视频全部移除？\n"
                "（只清列表，不会删除文件，也不会停止当前正在播的视频）"
                % len(self.playlist)):
            return
        self.playlist = []
        self._playlist_failed.clear()
        self._save_playlist()
        self._refresh_playlist_ui()
        self._set_playlist_status("列表已清空")

    def playlist_play_selected(self, event=None):
        """双击条目 / 在列表里按回车 → 播放它。"""
        sel = self.playlist_box.curselection()
        idx = sel[0] if sel else self._playlist_index_of(self.current_video_path)
        if idx is None or not (0 <= idx < len(self.playlist)):
            return
        self._playlist_play(self.playlist[idx])

    def playlist_next(self):
        self._playlist_step(+1)

    def playlist_prev(self):
        self._playlist_step(-1)

    def _playlist_step(self, delta):
        """上一个 / 下一个（按列表顺序，两端循环）。"""
        n = len(self.playlist)
        if n == 0:
            self._set_playlist_status("列表还是空的：先点【添加】或把视频拖进来")
            return
        cur = self._playlist_index_of(self.current_video_path)
        idx = (cur + delta) % n if cur is not None else (0 if delta > 0 else n - 1)
        self._playlist_play(self.playlist[idx])

    def _playlist_play(self, path):
        """播放列表里的一项（用户显式点的）。"""
        # 用户手点 = 手动重试：把它从"播不动"名单里摘出来
        self._playlist_failed.discard(path)
        if not os.path.isfile(path):
            self._set_playlist_status("文件不在了：%s" % os.path.basename(path))
            return
        self.open_video(path)

    def _advance_playlist(self):
        """片尾自动续播：返回下一个要播的路径；没有可续播的项则返回 None。

        规则刻意保守 —— **只有列表里还有别的项时**才返回路径（列表尾回到第 1 项）。
        于是"只放一个文件"的老用法完全不受影响：那时列表里只有当前这一项，
        返回 None，走 update_frame 原来的"原地无缝回绕"（不用重新打开文件，
        也就没有那一下卡顿）。找不到的条目会被记进 `_playlist_failed` 跳过，
        保证这个循环一定会往前推进，不会卡在同一项上反复重试。
        """
        n = len(self.playlist)
        if not self.autoplay_next or n < 2:
            return None
        cur = self._playlist_index_of(self.current_video_path)
        if cur is None:
            return None
        for step in range(1, n):
            cand = self.playlist[(cur + step) % n]
            if cand in self._playlist_failed:
                continue
            if not os.path.isfile(cand):
                self._playlist_failed.add(cand)
                continue
            return cand
        return None

    def toggle_playlist(self, event=None):
        """显示 / 隐藏右侧列表栏 —— 这是**用户的意愿开关**。

        【第 15 轮·改】只翻转 `_playlist_visible`，实际显隐交给
        `_sync_playlist_visibility()`。因为"要不要显示"现在由两个条件共同决定
        （用户意愿 + 全屏自动隐藏），这里只负责前者，别的地方（全屏隐藏/唤出）
        负责后者。把 grid 操作留在两处各写一遍，迟早会漏掉 minsize 那一步。
        """
        self._playlist_visible = not self._playlist_visible
        self.btn_playlist.config(
            text="隐藏列表" if self._playlist_visible else "显示列表")
        self._sync_playlist_visibility()
        # 尺寸变了会触发 video_container 的 <Configure>，画面自动重绘，这里不用管

    def _on_files_dropped(self, paths):
        """资源管理器把文件拖进窗口了（`filedrop` 已保证：主线程 + 在 Tk 队列里）。

        行为：把拖进来的视频全部加进列表，并立刻播第一个。
        """
        videos = filedrop.expand_dropped_paths(paths)
        if not videos:
            self._set_playlist_status("拖进来的东西里没有能播的视频文件")
            return
        added = self._playlist_add(videos)
        if len(videos) == 1:
            msg = "已拖入：%s" % (os.path.basename(videos[0]) or videos[0])
        else:
            msg = "已拖入 %d 个视频（新增 %d 个）" % (len(videos), added)
        self._set_playlist_status(msg)
        self._playlist_play(videos[0])

    def _bind_global_shortcuts(self):
        """注册全部全局快捷键（bind_all / bind_class / FocusIn 兜底）。

        三条纪律都写在下面的长注释里，改动前务必读：
          ① 必须用 `bind_all` —— `root.bind` 在焦点落到任何子控件之后**根本不执行**；
          ② 按钮上的空格要用 `bind_class` 替换掉 Tk 的类绑定 —— 让路撤不回
             一个已经发生的动作；
          ③ 焦点窗口为空时按键会被 Tk 全部丢弃，用 `<FocusIn>` 兜底补上。
        """
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
        #
        # 【第 19 轮·修复】这张清单原来是一个**手写的元组**，第 18 轮加了 V 键却
        # 忘了往这里补 `<Key-v>` —— 后果是"V 切换字幕"在真机上完全没反应，而测试
        # 却一直是绿的（它直接调 `_on_key_shortcut`，绕过了 Tk 的真实绑定）。
        # 现在改成**照着 `_ALL_SHORTCUT_KEYS` 生成**：在表里 == 真的绑上了，
        # 想让某个键失联就必须显式删掉它，不可能再悄悄漏一个。
        # （测试 tests/test_subtitles.py 第 ⑩ 节会挨个检查 binding 是否存在。）
        _KEY_SEQUENCES = {"left": "<Left>", "right": "<Right>",
                          "up": "<Up>", "down": "<Down>"}
        for _key in _ALL_SHORTCUT_KEYS:
            if _key == "space":
                continue      # 空格单独绑（要跟按钮的类绑定配合），见上面
            self.root.bind_all(_KEY_SEQUENCES.get(_key, "<Key-%s>" % _key),
                               self._on_key_shortcut)
        self.root.bind_all("<F1>", self.show_shortcuts)

        # 【第 11 轮·修正 2】窗口重新拿到焦点时兜一道底。
        # Tk 的键盘事件只交给"当前焦点窗口"，**焦点窗口为空时按键全部被丢弃**
        # （连 bind_all 都收不到）。窗口刚创建、或被别的窗口抢走再还回来时，
        # Tk 内部的焦点窗口可能是空的 —— 那就会表现成"所有快捷键都没反应"。
        # 这时把它落到主窗口上即可；已经在某个控件上就不动（别打断输入框）。
        self.root.bind("<FocusIn>", self._ensure_keyboard_focus)

        # 【P2 修复】缩放重绘只由 video_container 的 <Configure> 驱动
        # （见 on_window_resize 的说明），不再重复绑 root。

    def _init_audio_state(self):
        """音频播放器句柄、主时钟基准、后台交接状态、"抢发音量"窗口。

        这一段每个字段都配了半屏注释，因为它们的语义全是踩出来的
        （尤其 `_audio_handoff_old`、`_audio_suspended_for_seek`、
        `_audio_volume_resume` 三条）—— 改音频代码前请连同那些注释一起读，
        别只看字段名。
        """
        # --- 音频播放状态 ---
        self.audio_player = None  # ffpyplayer 的 MediaPlayer 实例
        self.audio_volume = 1.0  # 音量 0.0 ~ 1.0
        self.audio_muted = False  # 是否静音
        self.audio_failed = False  # 【P1】音频初始化失败后不再每帧重建
        # 【第 16 轮·新】当前文件到底有没有音轨。三态（见 _probe_audio_track）：
        #   None  = 还没探测 / 探测不了（缺 ffmpeg）→ 按"有音轨"处理，照常建播放器
        #   True  = 有音轨 → 正常
        #   False = 确认没有 → **根本不建音频播放器**（见 open_video 里那一段）
        self.current_has_audio = None
        # 探测结论的缓存，键是 (绝对路径, 文件大小, 修改时间)。
        # 这个探测要起 ffmpeg 子进程（~120ms），而用户点列表时是连着点的。
        self._audio_probe_cache = {}
        # 启动后是否预热列表文件的音轨结论（见 _warm_audio_probe_cache）。
        # 测试基架（tests/_harness.py）会把它关掉：预热线程会起 ffmpeg 子进程，
        # 跟"连续切换耗时 <150ms"这类断言抢磁盘和 CPU，让结果变得不确定。
        self._audio_probe_warm_enabled = True
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
        # 【第 15 轮·修复】所有"正在后台关旧播放器"的线程。
        # 换文件不再同步 close_player()（实测要等读线程退出 440~810ms，是
        # "连点几下列表就一顿一顿"的唯一来源），改为交给后台线程关。
        # 关窗时有界地 join 它们（见 _join_audio_retire），
        # 免得进程退出时还有线程在拆 SDL 音频设备。
        self._audio_retire_threads = []

    def _init_session_state(self):
        """本次会话的产物状态（"另存"永远只认 session_output_path）"""
        # 【P1】本次会话中"由本程序转换生成"的 3D 文件路径。
        # 另存只认这个字段，不再靠"工作目录里碰巧有个同名文件"来判断，
        # 避免用【打开视频】载入别的文件后另存到上一次的残留产物。
        self.session_output_path = None
        # 【第 11 轮·新】上一次转换产物有没有成功补上音轨（给完成提示用，见
        # _mux_audio_into_video —— cv2.VideoWriter 不写音轨，转换收尾要补一次）
        self._last_output_has_audio = False

        # 音频对齐相关：见 _restart_audio_at() 的说明。
        # 本构建的 ffpyplayer 只能靠"重建播放器 + ss"定位音频，
        # 且暂停态不能 seek，因此不需要额外状态。

    def _finish_init(self):
        """收尾：同步视差栏显隐、接上关窗回调、建背景缓存。

        必须放在最后 —— `_sync_parallax_visibility` 依赖 parallax_frame 已建好。
        """
        # 【P2 修复】最初的显示模式是 2D，此时视差栏没有任何作用，
        # 直接隐藏（切到 3D 模式时会自动出现，见 _sync_parallax_visibility）。
        self._sync_parallax_visibility()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # 【P2 修复】只由 video_container 驱动缩放重绘，靠这个记录上一次的
        # 容器尺寸，避免同一尺寸的重复 Configure 触发无谓的重绘。
        self._last_container_size = None

        # 【P2 修复】四周模糊背景的缓存（实现在 viewfit.LetterboxBgCache，
        # "每 N 帧重算一次"的常量是 viewfit.BG_REFRESH_INTERVAL）
        self._bg = viewfit.LetterboxBgCache()

        # 【第 14 轮·新】播放列表：把上次退出时的列表画出来，再接上"拖文件进来"。
        # 放在最后是因为拖放要用已经建好的主窗口句柄（winfo_id）。
        self._refresh_playlist_ui()
        self._dnd_enabled = filedrop.enable_file_drop(self.root, self._on_files_dropped)
        if not self._dnd_enabled:
            # 拖放是可选增强（非 Windows 没有）：说清楚，别让人以为程序坏了
            self._set_playlist_status("本平台不支持拖放；请用【添加】按钮")

        # 【第 16 轮·新】趁空闲把播放列表里各文件的"有没有音轨"先探好 ——
        # 探测一次要 ~120ms，用户点列表是连着点的，先算好就每次都是 0ms
        # （见 _warm_audio_probe_cache）。用 after 延后启动，让界面和首次播放
        # 先就位；不必登记这个 after id —— destroy 之后 Tk 不会再触发它。
        # 开关在方法里判（测试基架会关掉它）。
        self.root.after(AUDIO_PROBE_WARM_DELAY_MS, self._warm_audio_probe_cache)

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
        win_w, win_h = self._video_area_size()

        # 【第 19 轮·新】独立字幕栏：从窗口**底部**切一条出去，画面只在上面那块
        # 里取景。`bar_h == 0`（默认）时下面每一句都和以前一模一样。
        # 注意栏高只取决于"有没有字幕集 / 开关 / 窗口高度"，**不取决于这一秒有没有
        # 字幕在显示** —— 否则播放中会出现"栏一会有一会没有"，画面跟着一抽一抽，
        # 那比遮挡难看得多。
        bar_h = self._subtitle_bar_height(win_h)
        avail_h = win_h - bar_h

        h, w = frame.shape[:2]

        # 前景：min 缩放，完整显示（几何计算见 viewfit.fit_min）
        fg_w, fg_h, interp = viewfit.fit_min(w, h, win_w, avail_h)
        fg = cv2.resize(frame, (fg_w, fg_h), interpolation=interp)

        if fg_w == win_w and fg_h == win_h:
            # 刚好铺满，直接用（有字幕栏时 fg_h == avail_h < win_h，走不到这里）
            canvas = fg
            # 【第 18 轮】字幕要贴着"视频画面那块矩形"底部，所以这里得记住它。
            video_box = (0, 0, fg_w, fg_h)
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

            # 把前景居中贴上去（居中是在**可用区**里居中，不是整个窗口）
            x0 = (win_w - fg_w) // 2
            y0 = (avail_h - fg_h) // 2
            canvas[y0:y0 + fg_h, x0:x0 + fg_w] = fg
            video_box = (x0, y0, fg_w, fg_h)

        if bar_h:
            # 字幕栏是一条**纯色**横条（不是模糊背景）—— 必须让人一眼看出它
            # 在画面之外，否则用户会以为字幕还是画在画面里。
            canvas[win_h - bar_h:win_h, :, :] = SUBTITLE_BAR_COLOR_BGR
            subtitle_box = (0, win_h - bar_h, win_w, bar_h)
            subtitle_placement = "bar"
        else:
            subtitle_box = video_box
            subtitle_placement = "overlay"

        cv2image = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGBA)
        image = Image.fromarray(cv2image)
        # 【第 18 轮·新】字幕画在这里 —— "整屏已经合成完、还没上屏"的那一刻。
        #
        # 为什么是这里（改前先想清楚）：所有让画面变化的路径（播放的每一帧、
        # 缩放、全屏进出、切 3D 模式、暂停态重绘、拖进度条）最后都汇集到这一个
        # 方法，字幕挂在它上面就自然全覆盖了，一处都不用另写。
        # 而且它拿到的是 `video_box`（视频真正占的那块矩形），字幕因此贴着视频
        # 底部而不是窗口底部 —— 有黑边时才不会飘到黑边里去。
        # 没有字幕时这个方法第一句就返回，开销为零（老套件的耗时不变量靠它）。
        #
        # 【第 19 轮】多了一个落点：`subtitle_box` 在"字幕栏"开着时是窗口底部
        # 那条横条（画面之外），否则就是 `video_box` 本身（画面之内）。
        self._compose_subtitles(image, subtitle_box, placement=subtitle_placement)
        # 截图要"所见即所得"，所以把真正上屏的这一屏留一份
        self._last_display_image = image

        imgtk = ImageTk.PhotoImage(image=image)
        self.video_label.imgtk = imgtk
        self.video_label.config(image=imgtk)

    def _video_area_size(self):
        """视频显示区的实际像素尺寸 `(w, h)`。

        拿不到就逐级退回（容器 → 标签 → 主窗口 → 兜底值）—— 这套顺序原来写在
        `render_current_frame` 里，第 19 轮抽出来是因为"字幕栏要不要启用"也得问
        同一个尺寸。两处各写一份迟早会不一致，而那种不一致的表现是**状态行说
        "字幕栏"、画面上却没有栏**。
        """
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
        return win_w, win_h

    def _subtitle_bar_height(self, win_h):
        """字幕栏该占多少像素；**0 = 不启用**（退回"画在画面内"）。

        五个条件一个不满足就不切栏，每条都有明确理由：
          · 开关没开          → 默认行为不变；
          · 总开关关 / 模式=关闭 → 不画字幕要栏干什么；
          · 没有字幕集        → 切出来是条空横条，白让画面变小；
          · 找不到中文字体    → 反正一个字都画不出来（见 subtitle.find_font）；
          · 窗口太矮          → 切掉 14% 之后画面已经没法看了，宁可不切。

        ⚠️ 这里**故意不看"当前这一秒有没有 cue"**：字幕之间有空档（一句说完、
        下一句还没到），如果按 cue 判断，栏会在播放中一闪一闪、画面跟着一抽一抽。
        """
        if not self.subtitle_bar:
            return 0
        if not self.subtitle_enabled or self.subtitle_mode == "off":
            return 0
        if not self.subtitle_set.available:
            return 0
        if not self.subtitle_font_path:
            return 0
        if win_h < SUBTITLE_BAR_MIN_WINDOW_H:
            return 0
        return max(SUBTITLE_BAR_MIN,
                   min(SUBTITLE_BAR_MAX, int(win_h * SUBTITLE_BAR_RATIO)))

    def _get_letterbox_bg(self, frame, win_w, win_h):
        """生成"铺满整个窗口的模糊暗背景"（带缓存）。

        【第 12 轮·重构】计算与缓存都搬到了 `viewfit.LetterboxBgCache`
        （纯 numpy/cv2，不碰控件）。这里保留同名方法只为让调用点不变，
        缓存实例是 `self._bg`。

        返回的数组归缓存所有，调用方不要再往里写（需要写请先 copy）——
        `render_current_frame` 里就是 `.copy()` 出去的。
        """
        return self._bg.get(frame, win_w, win_h)

    # ============================================================
    # 【第 18 轮·新】双语字幕
    # ------------------------------------------------------------
    # 分工：解析 / 找同名文件 / 认语种 / 排版折行 全在 `subtitle.py`，那边不知道
    # 播放器存在。这里只做四件事 —— **什么时候加载、显示哪一种、画在哪儿、
    # 状态行说什么**，跟前几轮的模块分工完全一致。
    #
    # 为什么把文字画进画面、而不是做一个 Tk 控件贴在视频下方：见 SUBTITLE_* 那组
    # 常量上面的说明（缩放/全屏/3D/暂停重绘全自动带上 + 位置能跟着视频矩形走 +
    # 截图所见即所得）。
    # ============================================================
    def _subtitle_filetypes(self):
        patterns = " ".join("*" + ext for ext in subtitle.SUBTITLE_EXTS)
        return [("字幕文件", patterns), ("SubRip 字幕", "*.srt"),
                ("WebVTT 字幕", "*.vtt"), ("所有文件", "*.*")]

    def load_subtitle_file(self):
        """手动指定字幕文件（底栏【载入字幕】）。

        自动找同名字幕是主路径；这个是兜底 —— 网上下的字幕常常叫
        `1.srt` / `chs.srt` / `[某字幕组]电影.srt`，跟视频名字毫无关系。
        """
        if not self.subtitle_enabled:
            self._set_playlist_status("字幕功能已关闭")
            return
        path = filedialog.askopenfilename(title="选择字幕文件",
                                          filetypes=self._subtitle_filetypes())
        if not path:
            return
        if not self.current_video_path:
            self._set_playlist_status("先打开一个视频，再载入字幕")
            return
        self._load_subtitles(self.current_video_path, explicit_path=path)
        if not self.subtitle_set.available:
            messagebox.showwarning(
                "字幕",
                "这个文件里没读出字幕：\n%s\n\n"
                "（支持 .srt / .vtt。空文件、只有时间轴没有文字、"
                "以及根本不是字幕的文本文件都会是这个结果。）" % path)
            return
        # 用户主动载入 = 明确想看字幕。原本是"关闭"的话顺手打开，
        # 否则载完什么都不显示，用户会以为没生效。
        if self.subtitle_mode == "off":
            self.subtitle_mode = SUBTITLE_DEFAULT_MODE
        self._sync_subtitle_ui()
        self.render_current_frame()
        self._set_playlist_status(self._subtitle_status_text())

    # ============================================================
    # 【第 20 轮】在线字幕匹配（OpenSubtitles）
    # ------------------------------------------------------------
    # 为什么整条流程必须丢进后台线程：一轮匹配最坏是「3 次搜索 + 1 次下载」，
    # 每次都可能一直卡到超时。跑在主线程上就是**界面直接冻住** —— Tk 的事件
    # 循环被阻塞，连窗口重绘都不会发生，看起来像程序死了。
    #
    # 结果怎么回到主线程：**队列 + 主线程轮询**，而不是「后台线程里直接调
    # root.after」。后者看着更省事，实测会抛
    # `RuntimeError: main thread is not in main loop` —— 它只在 mainloop
    # 正好转着的时候才侥幸不炸；更糟的是这个异常**后台线程自己抓不住**，
    # 于是 `_online_busy` 永远停在 True、按钮从此点不动（只能重启程序）。
    #
    # 现在分工很清楚：后台线程只干两件事 —— 干活、把结果塞进
    # `queue.Queue`（它本身线程安全）；`after` 全部由主线程发起
    # （`match_subtitles_online` 起头 → `_poll_online` 自我续期，
    # 而 `_poll_online` 本来就是被主线程的 after 调起来的）。
    #
    # ⚠️ 后台线程里**绝不能碰任何 Tk 对象**，连 `winfo_exists()` 都会炸 ——
    # Tk 不是线程安全的（第 16 轮在字幕预热那里踩过）。
    # ============================================================

    def _online_api_key(self):
        """当前可用的 API key。**首次用到才读配置**。

        初始化时不读（那时按约定只做纯状态初始化、不碰文件系统）——
        代价就是第一次点【在线匹配】会多一次很小的读盘。
        """
        if self.online_api_key is None:
            self.online_api_key = subtitle_online.load_config().get("api_key", "")
        return self.online_api_key

    def open_online_settings(self):
        """填 / 改 OpenSubtitles 的 API key（底栏【在线匹配】上点右键）。

        返回 True 表示手上有一个非空的 key 可用。
        """
        current = self._online_api_key()
        if current:
            shown = ("%s……%s" % (current[:6], current[-4:])
                     if len(current) > 12 else "（已设置）")
        else:
            shown = "（还没设置）"

        messagebox.showinfo(
            "在线字幕匹配 · 需要一个 API key",
            "从 OpenSubtitles 自动匹配字幕，需要一个免费的 API key。\n\n"
            "申请步骤（大约 1~2 分钟）：\n"
            "  1. 打开 opensubtitles.com，注册并验证邮箱\n"
            "  2. 右上角头像 → API consumers → NEW CONSUMER\n"
            "  3. Name 随便填，Allow anonymous 选 true，保存\n"
            "  4. 复制生成的 API Key，粘到下一步的输入框\n\n"
            "小提示：把这个 consumer 设成 Under Development，\n"
            "未登录也能有 100 次/天的下载额度（不设的话只有 5 次）。\n\n"
            "以后想改：右键点底栏的【在线匹配】。\n\n"
            "当前 key：%s" % shown)

        new_key = simpledialog.askstring(
            "OpenSubtitles API key",
            "把 API Key 粘到这里（留空 = 清除）：",
            initialvalue=current or "")
        if new_key is None:
            return False                 # 用户按了取消
        new_key = new_key.strip()

        if not subtitle_online.save_config({"api_key": new_key}):
            # 存不下也**不拦着用户用** —— 本次仍按输入的 key 走，只是下次要重填
            messagebox.showwarning(
                "没能保存配置文件",
                "写不进这个位置：\n%s\n\n"
                "本次仍可用刚输入的 key，但下次启动需要重新填写。"
                % subtitle_online.default_config_path())

        self.online_api_key = new_key
        self._set_playlist_status(
            "已保存字幕源的 API key" if new_key else "已清除字幕源的 API key")
        return bool(new_key)

    def match_subtitles_online(self):
        """底栏【在线匹配】：取一份字幕放到视频旁边，顺手装上。

        每次点都**往前推进一条候选**（见 `_online_tried`）—— 对结果不满意
        就直接再点一次，不用去找什么"换一个"的菜单。
        """
        if self._online_busy:
            self._set_playlist_status("上一个匹配还在跑，稍等一下")
            return

        target = self.current_video_path
        if not target or not os.path.exists(target):
            messagebox.showinfo("先打开一个视频",
                                "还没有正在播放的视频，没法匹配字幕。")
            return

        if not self._online_api_key() and not self.open_online_settings():
            return
        api_key = self._online_api_key()
        if not api_key:
            return

        self._online_busy = True
        self.btn_online.config(text="匹配中…", state=tk.DISABLED)
        self._set_playlist_status("正在匹配字幕…")

        # ⚠️ fps 必须在这里（主线程）读出来当**参数**传进去，不能让后台线程
        # 自己去读 app 状态。另外：只有真的开了视频才把 fps 传下去 ——
        # `video_fps` 的默认值是 30.0，拿它去匹配会让 23.976 的片子被判成
        # "帧率不符"（传 0 = 不知道，就不会误罚）。
        video_fps = self.video_fps if self.cap is not None else 0.0

        # 每次匹配都用一条**全新**的队列：不然上一次残留的结果会被这一次
        # 捡走，出现"点了没反应却突然跳到上一个结果"。
        self._online_queue = queue.Queue()
        self._online_thread = threading.Thread(
            target=self._online_worker, args=(target, api_key, video_fps),
            name="subtitle-online", daemon=True)
        self._online_thread.start()
        # 轮询由**主线程**发起（本方法就在主线程里跑），之后每次续期也由
        # `_poll_online` 自己发起 —— 整条链上 `after` 都不会跨线程调用。
        self.root.after(ONLINE_POLL_INTERVAL_MS, self._poll_online)

    def _online_worker(self, video_path, api_key, video_fps):
        """后台线程：搜 → 挑 → 下载 → 解压 → 落盘。

        **只往队列里放一个元组，不碰任何 Tk 对象。** 成功和失败共用一个回程
        （元组第一项区分），主线程那边就只有一个入口要看。
        """
        try:
            client = subtitle_online.OpenSubtitles(api_key)
            result = subtitle_online.fetch(
                client, video_path, video_fps,
                tried=self._online_tried.get(video_path, set()))
            payload = ("ok", video_path, result)
        except Exception as exc:
            payload = ("error", str(exc),
                       isinstance(exc, subtitle_online.NoAPIKey),
                       isinstance(exc, subtitle_online.QuotaExceeded))
        try:
            self._online_queue.put(payload)
        except Exception:
            # 队列已经被换掉了（用户关掉又重开了一次匹配）—— 这条结果作废
            pass

    def _poll_online(self):
        """主线程：看后台线程有没有结果。**只允许主线程调用。**

        用轮询而不是让后台线程回调，代价是最多 80ms 的延迟，换来的是
        「`after` 永远在主线程里被调用」这条硬保证 —— 那正是上面注释里
        那个 RuntimeError 的来源。
        """
        if not self._online_busy:
            return              # 窗口关了 / 已被别处收尾 → 停止续期，别再排自己

        try:
            payload = self._online_queue.get_nowait()
        except queue.Empty:
            # 还没好。先确认后台线程还活着，再排下一轮。
            thread = self._online_thread
            if thread is not None and not thread.is_alive():
                # 线程没了却没留下结果 —— 只可能是它在放结果之前就崩了。
                # **这里必须收尾**，否则按钮会永远停在「匹配中…」。
                self._online_failed("匹配线程意外结束，请再试一次")
                return
            self.root.after(ONLINE_POLL_INTERVAL_MS, self._poll_online)
            return
        except Exception:
            self._online_failed("匹配结果读不出来，请再试一次")
            return

        if payload[0] == "ok":
            self._online_done(payload[1], payload[2])
        else:
            self._online_failed(payload[1], payload[2], payload[3])

    def _online_done(self, video_path, result):
        """主线程：把拿到的字幕装上、恢复按钮、报告结果。"""
        self._online_busy = False
        self.btn_online.config(text="在线匹配", state=tk.NORMAL)

        if result.get("ambiguous"):
            # **定不下来是哪部片 → 一份都不下**，把候选摆出来让人自己挑。
            # 为什么不下：下一份错的花的是当天 5 次配额里的一次（未登录只有
            # 5 次），而且用户当场看不出来 —— 字幕画到屏幕上才发现对不上。
            # 搜索不计配额，所以这一支是**免费**的，只是多问一句。
            found = result.get("candidates") or []
            movies = len({c.get("feature_id") or c.get("imdb_id")
                          for c in found})
            self._set_playlist_status("匹配到多部片子，需要你挑一个")
            self.open_subtitle_search(
                candidates=found, info=result.get("info"),
                reason="文件名里没有年份，搜到的 %d 条分属 %d 部片子 —— 挑一个吧"
                       % (len(found), movies))
            return

        # 记下来，下次点同一部片子时跳过它（"再点一次 = 换一条"）
        self._online_tried.setdefault(video_path, set()).add(
            result["candidate"]["file_id"])

        # 用 explicit_path 装上：这条路不受"自动找同名字幕"开关影响。
        # 直接给路径还有个好处 —— 若视频旁边已经有用户自己的字幕，新下的这份
        # 会以 `电影.zh.1.srt` 落盘（绝不覆盖），此时按路径显式加载，
        # 用户**立刻**就能看到刚下回来的这份。
        self._load_subtitles(video_path, explicit_path=result["path"])

        # 用户点的就是"我要看字幕"。原本是「关闭」的话顺手打开，
        # 否则下完了什么都不显示，会让人以为没成功（跟【载入字幕】一个道理）。
        if self.subtitle_mode == "off":
            self.subtitle_mode = SUBTITLE_DEFAULT_MODE
            self._sync_subtitle_ui()
        self.render_current_frame()

        notes = ["已匹配 %s" % result["description"]]
        remaining = result["meta"].get("remaining")
        if remaining is not None:
            # 官方只在下载那一刻告诉你还剩几次，所以这里一定要露给用户看。
            # 顺带把**重置时刻**说清楚：配额按 UTC 午夜重置，对 UTC+8 的用户
            # 那是早上 8 点 —— 只说「今天还剩 N 次」会让人以为过了 0 点就恢复
            # （见 _format_quota_reset）。
            clock = _format_quota_reset(result["meta"].get("reset_time_utc"))
            if clock:
                notes.append("还剩 %s 次（%s 重置）" % (remaining, clock))
            else:
                notes.append("今天还剩 %s 次" % remaining)
        notes.append("存到 %s" % os.path.basename(result["path"]))
        self._set_playlist_status(" · ".join(notes))

    def _online_failed(self, message, needs_key=False, quota=False):
        """主线程：把失败说清楚（说清"为什么"和"接下来能做什么"）。"""
        self._online_busy = False
        self.btn_online.config(text="在线匹配", state=tk.NORMAL)
        self._set_playlist_status("字幕匹配失败：%s" % message)

        if needs_key:
            if messagebox.askyesno("需要 API key",
                                   "%s\n\n现在去配置吗？" % message):
                self.open_online_settings()
            return

        if quota:
            messagebox.showwarning(
                "下载额度用完了",
                "%s\n\n"
                "免费额度按天算。想多要一些的话，去 opensubtitles.com 把你自己\n"
                "那个 consumer 设成 Under Development（100 次/天）；\n"
                "或者登录账号（20 次/天）。\n\n"
                "实在不行也可以点【载入字幕】手动指定一个文件。" % message)
            return

        messagebox.showinfo("没能匹配到字幕",
                            "%s\n\n可以：\n"
                            "· 点【搜索…】自己搜、自己挑（最管用）\n"
                            "· 再点一次【在线匹配】（会换下一条候选）\n"
                            "· 点【载入字幕】手动指定一个文件" % message)

    def open_subtitle_search(self, candidates=None, info=None, reason=""):
        """底栏【搜索…】：打开"自己挑一份字幕"的窗口。

        两条来路，用同一扇窗：

        · 用户主动点【搜索…】→ 不带参数，窗口打开后用文件名解析出的片名
          和年份先搜一次；
        · 自动匹配**定不下来**时（见 `subtitle_online.looks_ambiguous`）
          → 带着已经搜好的候选和一句说明进来，不再白搜一遍。

        挑中就下载。**用户的选择优先于我们的打分** —— 打分再精细也只是猜，
        用户看一眼片名和年份就知道对不对。
        """
        target = self.current_video_path
        if not target or not os.path.exists(target):
            messagebox.showinfo("先打开一个视频", "还没有正在播放的视频，没法搜字幕。")
            return
        if self._online_busy:
            self._set_playlist_status("上一个匹配还在跑，稍等一下")
            return

        if not self._online_api_key() and not self.open_online_settings():
            return
        api_key = self._online_api_key()
        if not api_key:
            return

        # 没有 info 时（用户主动点的）自己解析一遍文件名，把输入框预填上
        if info is None:
            info = subtitle_online.analyze_filename(target)

        video_fps = self.video_fps if self.cap is not None else 0.0
        dialog = SubtitleSearchDialog(
            self.root, target, api_key, candidates=candidates, info=info,
            reason=reason, video_fps=video_fps)
        chosen = dialog.show()
        if chosen:
            self.download_online_candidate(chosen)

    def download_online_candidate(self, candidate):
        """下载用户在候选列表里挑中的那一条。

        跟 `match_subtitles_online` 只有一处不同：不再搜索、也不再打分，
        直接下他点的那条。其余（按钮忙碌态、后台线程、回程处理）完全一样，
        所以回程照旧走 `_poll_online` → `_online_done`。
        """
        if self._online_busy:
            self._set_playlist_status("上一个匹配还在跑，稍等一下")
            return
        target = self.current_video_path
        if not target or not os.path.exists(target):
            messagebox.showinfo("先打开一个视频", "还没有正在播放的视频。")
            return
        api_key = self._online_api_key()
        if not api_key:
            return

        self._online_busy = True
        # 文字用「下载中…」而不是「匹配中…」：这条路是用户点名要的那一条，
        # 不会再换候选，说"匹配"会让人以为还在挑。
        self.btn_online.config(text="下载中…", state=tk.DISABLED)
        self._set_playlist_status(
            "正在下载 %s…" % subtitle_online.describe_candidate(candidate))

        self._online_queue = queue.Queue()
        self._online_thread = threading.Thread(
            target=self._download_candidate_worker,
            args=(target, api_key, candidate),
            name="subtitle-download", daemon=True)
        self._online_thread.start()
        self.root.after(ONLINE_POLL_INTERVAL_MS, self._poll_online)

    def _download_candidate_worker(self, video_path, api_key, candidate):
        """后台线程：下载**指定的**那条候选。

        回程格式跟 `_online_worker` 一模一样（元组第一项区分成败），
        所以主线程那边只有一个入口要看。
        """
        try:
            client = subtitle_online.OpenSubtitles(api_key)
            result = subtitle_online.download_candidate(
                client, video_path, candidate)
            payload = ("ok", video_path, result)
        except Exception as exc:
            payload = ("error", str(exc),
                       isinstance(exc, subtitle_online.NoAPIKey),
                       isinstance(exc, subtitle_online.QuotaExceeded))
        try:
            self._online_queue.put(payload)
        except Exception:
            pass

    def _load_subtitles(self, video_path, explicit_path=None):
        """给这个视频装配字幕（自动找同名 / 用指定文件）。

        两个开关各管一段，别合并：
          · `subtitle_enabled` —— 总开关，关掉就什么都不加载也不画（测试基架用）；
          · `subtitle_auto`    —— 只管"打开视频时自动找同名字幕"这一条路。
            手选的字幕文件（`explicit_path`）不受它影响：用户既然点名了，
            就该认。
        """
        if (not self.subtitle_enabled
                or (explicit_path is None and not self.subtitle_auto)):
            self.subtitle_set = subtitle.SubtitleSet()
        else:
            self.subtitle_set = subtitle.build(video_path,
                                               explicit_path=explicit_path)
        self._sync_subtitle_ui()
        return self.subtitle_set

    def _sync_subtitle_ui(self):
        """把当前模式刷到下拉框上。

        下拉框**始终可用**，不因为"没找到字幕"就灰掉：它旁边就是【载入字幕】，
        用户从这儿能看出现在是哪种模式，再点一下就载入一份。至于"为什么没显示
        字幕"，交给状态行那句话解释（见 _subtitle_status_text）。
        """
        try:
            self.subtitle_var.set(
                SUBTITLE_MODE_LABELS.get(self.subtitle_mode, "关闭"))
        except Exception:
            pass
        # 勾选框也跟着状态走：测试可以直接改 `subtitle_bar` 而不去点控件，
        # 两边若不一致，界面上会显示"没勾"但字幕其实在栏里。
        try:
            self.subtitle_bar_var.set(bool(self.subtitle_bar))
        except Exception:
            pass

    def _subtitle_status_text(self):
        """状态行要说的那一句话。"""
        if not self.subtitle_set.available:
            return ("没有找到字幕文件 —— 把 .srt/.vtt 改成与视频同名放在旁边，"
                    "或点【载入字幕】指定一个")
        if not self.subtitle_font_path:
            # 找不到字体时不画字幕（见 subtitle.find_font）。但**不能一声不吭**：
            # 用户会觉得"字幕没生效"，而真相是这台机器缺字体。说清楚。
            return "找到字幕了，但本机没有可用的中文字体，画不出来"
        text = "%s · %s" % (SUBTITLE_MODE_LABELS[self.subtitle_mode],
                            self.subtitle_set.describe())
        if self.subtitle_bar and self.subtitle_mode != "off":
            # 勾了字幕栏就说清楚它到底生效没有 —— 窗口太矮时不切栏是刻意的，
            # 但用户只看到"勾了却没栏"，会以为开关坏了。
            if self._subtitle_bar_height(self._video_area_size()[1]):
                text += " · 字幕栏"
            else:
                text += " · 字幕栏未启用（窗口太矮，先画在画面内）"
        return text

    def on_subtitle_mode_change(self, event=None):
        """下拉框选了新模式。"""
        label = self.subtitle_var.get()
        for mode, text in SUBTITLE_MODE_LABELS.items():
            if text == label:
                self.subtitle_mode = mode
                break
        self.render_current_frame()
        self._set_playlist_status(self._subtitle_status_text())
        # 焦点会留在下拉框身上（Tk 的 Combobox 选完就把焦点收在自己那儿），
        # 之后空格/方向键都会被它吃掉 —— 跟"选完文件、点完按钮"是同一个毛病，
        # 同样主动把焦点交回画面（见 _restore_shortcut_focus）。
        self._restore_shortcut_focus()

    def cycle_subtitle_mode(self):
        """V 键：按 `SUBTITLE_MODES` 的顺序循环那四种模式。"""
        modes = list(SUBTITLE_MODES)
        index = modes.index(self.subtitle_mode) if self.subtitle_mode in modes else 0
        self.subtitle_mode = modes[(index + 1) % len(modes)]
        self._sync_subtitle_ui()
        self.render_current_frame()
        self._set_playlist_status(self._subtitle_status_text())

    def on_subtitle_bar_toggle(self):
        """【第 19 轮·新】勾选框回调：独立字幕栏开关。

        只改"画在哪"，不改字号也不改内容 —— 所以开关前后画面上的差别只有两处：
        字幕的位置，和画面为了腾地方缩小了一点（每帧都要重排几何，不只是一次）。

        **必须主动重绘**：字幕栏不占 Tk 布局（它是画进画面里的），所以不会有
        `<Configure>` 事件来替我们触发重画 —— 少了这一句就是"勾了没反应"。
        """
        self.subtitle_bar = bool(self.subtitle_bar_var.get())
        self.render_current_frame()
        self._set_playlist_status(self._subtitle_status_text())
        # 勾选框被点过之后焦点留在它身上，空格就会去拨这个勾选框而不是播放/暂停
        # —— 跟"选完下拉框"同款毛病，同样主动把焦点交回画面。
        self._restore_shortcut_focus()

    def toggle_subtitle_bar(self):
        """B 键：开关字幕栏（与勾选框完全同一个动作）。

        状态的实际归属仍是 `subtitle_bar`，这里只是**改一下控件变量**再走同一条
        回调 —— 不另写一份逻辑，就不会出现"按键与勾选框行为不一致"。
        """
        self.subtitle_bar_var.set(not self.subtitle_bar)
        self.on_subtitle_bar_toggle()

    # ---------- 画 ----------
    def _subtitle_seconds(self):
        """当前**正在显示的那一帧**的媒体时刻（秒）。"""
        if self.cap is None:
            return 0.0
        try:
            position = int(self.cap.get(cv2.CAP_PROP_POS_FRAMES))
        except Exception:
            return 0.0
        fps = self.video_fps if self.video_fps > 0 else 30.0
        # ⚠️ POS_FRAMES 读的是"下一帧的序号"，正在显示的是它减一。差的这一帧
        # 就是 40ms，字幕会提前一点点冒出来 —— 单看根本看不出来，但既然顺手
        # 能写对，就别给自己留一个"一帧的错位"。
        return max(0.0, (position - 1) / fps)

    def _subtitle_rows(self):
        """这一帧要画的字幕行 `[(语种, 文字), ...]`；没有就返回 `[]`。"""
        if not self.subtitle_enabled or self.subtitle_mode == "off":
            return []
        if self.cap is None or not self.subtitle_set.available:
            return []
        return self.subtitle_set.lines_for(self._subtitle_seconds(),
                                           self.subtitle_mode)

    def _compose_subtitles(self, image, box, placement="overlay"):
        """把字幕贴到 `image`（已合成好的整屏，PIL RGBA）上。

        `box` 是字幕的容器矩形 `(x, y, w, h)`，两种落点各有出处：
          · `placement="overlay"` —— box 就是**视频画面那块矩形**：字幕贴着它的
            底部居中，所以有黑边时不会飘进黑边里，全屏时又正好在最下面；
          · `placement="bar"` —— box 是窗口底部那条**独立字幕栏**：字幕在栏里
            垂直居中，一个字都不会压到画面上（见 `_subtitle_bar_height`）。

        `placement="bar"` 时字号按**栏高**算（栏是固定高度的一条），`overlay`
        时按**画面高度**算（所以全屏时字幕会跟着变大）；两者都夹在 min/max
        之间 —— 窗口很小时按比例算出来只有几像素，看不清等于没有；4K 全屏时
        不夹住又会糊脸。
        """
        rows = self._subtitle_rows()
        if not rows:
            return
        box_x, box_y, box_w, box_h = box
        if box_w < 40 or box_h < 24:
            return                      # 太小到没法放字幕，宁可不画

        if placement == "bar":
            size = max(SUBTITLE_BAR_FONT_MIN,
                       min(SUBTITLE_BAR_FONT_MAX,
                           int(box_h * SUBTITLE_BAR_FONT_RATIO)))
        else:
            size = max(SUBTITLE_FONT_SIZE_MIN,
                       min(SUBTITLE_FONT_SIZE_MAX,
                           int(box_h * SUBTITLE_FONT_SIZE_RATIO)))

        wrap_w = int(box_w * SUBTITLE_WIDTH_RATIO)
        layer = self._subtitle_renderer.layer(rows, size, wrap_w)
        if layer is None:
            return
        layer_w, layer_h = layer.size

        if placement == "bar" and layer_h > box_h - 4:
            # 极端情况才会走到：窗口很矮 + 中英两行各自还折了行（最多 4 行）。
            # 缩小一档重画，宁愿字小也不许漫到画面上去。
            # 刻意不做更强的裁剪 —— 裁剪会把字切掉半截，比"压到画面底边一点点"
            # 更难看。
            size = max(SUBTITLE_BAR_FONT_MIN,
                       int(size * max(0.5, (box_h - 6) / float(layer_h))))
            layer = self._subtitle_renderer.layer(rows, size, wrap_w)
            if layer is None:
                return
            layer_w, layer_h = layer.size

        paste_x = box_x + (box_w - layer_w) // 2
        if placement == "bar":
            paste_y = box_y + max(0, (box_h - layer_h) // 2)
        else:
            paste_y = (box_y + box_h - layer_h
                       - max(SUBTITLE_BOTTOM_MARGIN_MIN,
                             int(box_h * SUBTITLE_BOTTOM_MARGIN_RATIO)))
        image.paste(layer, (paste_x, paste_y), layer)

    # ============================================================
    # 【第 17 轮·新】记忆播放位置
    # ------------------------------------------------------------
    # 需求：播放器关掉、或者切去别的视频之后，再点回这个没看完的片子时，
    # 让用户选"从头播"还是"接着看"。
    #
    # 三件事，各管一段：
    #   ① 离开一个视频时记下它停在哪儿 —— `_settle_current_position()`
    #      （两个调用点：open_video 开头换文件时、on_close 关窗时；另有
    #        update_frame 撞到片尾那一次，那是"看完了"，记录要清掉）
    #   ② 再次打开时查记录、问一句 —— `_lookup_resume()` + open_video 开头那段
    #   ③ 记录落盘，关掉播放器也还在 —— `resume_store`（独立模块）
    #
    # 两个决定值得先写下来：
    #
    # · **路径是键，但记录里要带文件指纹。** 同一个路径指向的可能是不同的东西
    #   （重新转换一次就把它覆盖了），指纹对不上就当没有这条记录
    #   （见 _lookup_resume 里那两类"失效"）。
    #
    # · **"记"只在离开时发生，播放过程中一次都不写盘。** 播放时每秒去更新一次
    #   磁盘文件既没必要、又会在长片里反复写；而"停在哪儿"这件事只在离开那一
    #   刻才成为事实上的最终答案。于是写盘的频率是"用户切一次视频"，可以忽略。
    # ============================================================
    def _settle_current_position(self, finished=False):
        """离开当前视频时结算它的位置：记下来，或者把记录清掉。

        `finished=True` 表示"这一遍播到头了"（`update_frame` 里 `cap.read()`
        已经读不出帧）—— 那时位置不可信（cap 停在末尾，也可能停在某个解码
        出错的地方），所以不去量它，**直接当看完**、把记录清掉。

        它**不碰任何播放状态**：只读 `current_video_path` 和 cap 的位置，然后写
        自己的记录表。正因如此，放在 open_video 最开头（用户接下来还可能点
        "取消"）也是安全的 —— 就算取消了，顺手记一下位置也毫无副作用。
        """
        if not self.resume_enabled:
            return
        path = self.current_video_path
        if not path or not os.path.isfile(path):
            return
        if resume_store.make_key(path) in self._resume_ignored:
            return
        if finished:
            self._forget_resume(path)
            return

        dur = self._position_duration_sec()
        if dur <= 0:
            return                          # 拿不到时长就无从判断"看完没有"
        sec = self._current_video_seconds()
        if sec >= dur - RESUME_TAIL_SEC:
            self._forget_resume(path)       # 已经贴着片尾，按看完处理
            return
        if sec < RESUME_MIN_POSITION_SEC:
            return                          # 太靠前，等于从头，不值得打扰用户
        self._remember_resume(path, sec, dur)

    def _position_duration_sec(self):
        """当前视频的总时长（秒）；拿不到返回 0。"""
        if self.total_frames <= 0:
            return 0.0
        fps = self.video_fps if self.video_fps > 0 else 30.0
        return self.total_frames / fps

    def _remember_resume(self, path, sec, dur):
        """写一条位置记录：先改内存，再立刻落盘。

        立刻落盘而不是"攒到关窗再写"：关播放器的方式不止一种（正常关、任务
        管理器结束、掉电），"切走这个视频"是最近一次能保证写得下去的机会。
        代价也很小 —— 几 KB 的 JSON，频率是"用户切一次视频"。
        """
        entry = {"path": path, "sec": float(sec), "dur": float(dur),
                 "at": time.time()}
        try:
            st = os.stat(path)
            entry["size"] = float(st.st_size)
            entry["mtime"] = float(st.st_mtime)
        except OSError:
            pass            # 拿不到指纹就不带，查询时那段校验自然跳过
        self._resume_positions[resume_store.make_key(path)] = entry
        self._save_resume()

    def _forget_resume(self, path):
        """删掉这个文件的记录（本来就没有的话不写盘）。"""
        if self._resume_positions.pop(resume_store.make_key(path), None) is not None:
            self._save_resume()

    def _save_resume(self):
        return resume_store.save(self._resume_positions, self._resume_path)

    def _lookup_resume(self, path):
        """这个文件上次停在哪儿？返回记录，没有（或已失效）返回 None。

        两类"失效"会被**就地清掉**，免得下次还拿它来问：

        · 文件指纹对不上（大小 / 修改时间变了）—— 同一路径已经是另一个文件了，
          上次的位置毫无意义。这一条挡住的是很实在的坑：重新转换覆盖了同名
          文件之后，拿旧位置去播新文件，一开局就冲到中段。
        · 位置本身越界 —— 记录是别处写来的（换了机器、手改过 JSON），
          或者片尾那一段边界情况。
        """
        if not self.resume_enabled:
            return None
        key = resume_store.make_key(path)
        if key in self._resume_ignored:
            return None
        entry = self._resume_positions.get(key)
        if entry is None:
            return None
        try:
            st = os.stat(path)
        except OSError:
            self._forget_resume(path)
            return None
        if entry.get("size") is not None:
            same = (entry["size"] == st.st_size
                    and abs(entry.get("mtime", 0.0) - st.st_mtime) <= 1e-6)
            if not same:
                self._forget_resume(path)
                return None
        sec, dur = entry["sec"], entry["dur"]
        if sec < RESUME_MIN_POSITION_SEC or sec >= dur - RESUME_TAIL_SEC:
            self._forget_resume(path)
            return None
        return entry

    def _ask_resume_choice(self, file_path, sec, dur):
        """问用户"接着看还是从头"；返回 `"resume"` / `"restart"` / `None`（取消）。

        单独抽成一个方法是为了**可替换**：测试把它换成一个固定返回值的函数，
        就能走遍三条分支，不必去点真实对话框 —— 模态等待会把无人值守的回归
        整轮挂死（见 ResumeDialog 的说明）。
        """
        return ResumeDialog(self.root, file_path, sec, dur).show()

    def open_video(self, file_path=None, record=True, resume=True, ask=True):
        """打开（并开始播放）一个视频文件。

        `record=True` 时会把这个文件记进右侧播放列表 —— 这是需求里"将要播放的
        视频记录在播放器右侧列表中"的落点。唯一的例外是**转换产生的临时产物**
        （`on_conversion_done` 传 record=False）：它躺在 %TEMP% 里，下次转换就
        会被删掉，记进列表只会留下永远点不开的死条目。

        【第 17 轮·新】`resume` / `ask` 两个参数管"记忆播放位置"：

          · `resume=True`（默认）—— 这个文件上次没看完的话，按记录决定从哪儿起播。
          · `resume=False`      —— 完全不理历史记录，一律从头播。转换产物走这条。
          · `ask=True`（默认）  —— 有记录时弹窗问"继续 / 从头"。
          · `ask=False`         —— 有记录就**静默**接着上次的位置播，不问。
                                   片尾自动连播走这条：那是"往下连播"，中途
                                   弹窗会把连播打断，而那个文件上次没看完、
                                   这次接着看，本来就是连播该有的样子。

        返回 True = 已经开始播放这个文件；False = 用户取消了（关掉文件对话框，
        或者点了续播询问框的"取消"），或者文件打不开。片尾自动续播靠这个返回值
        判断"要不要把这一项标记成播不动"。
        """
        if file_path is None:
            file_path = filedialog.askopenfilename(filetypes=self._video_filetypes())
            if not file_path:
                return False

        # 【第 17 轮·新】① 把"上一个视频停在哪儿"记下来。
        # ⚠️ 必须在换掉 cap **之前**做 —— 位置只能从还活着的那一个读出来。
        self._settle_current_position()

        # 【第 17 轮·新】② 查这个文件有没有"上次没看完"的记录，有就问一句。
        #
        # ⚠️ **必须在 _stop_playback_loop() 之前**。用户可能点"取消"，那时当前
        #    正在播的东西必须原封不动 —— 所以这一段只做"查表 + 弹窗"，一个播放
        #    状态都不碰，取消时直接 return False 走人。放后面的话，"取消"会变成
        #    "当前视频已经停了，新视频又没开"。
        start_sec = 0.0
        if resume and self.resume_enabled:
            entry = self._lookup_resume(file_path)
            if entry is not None:
                sec, dur = entry["sec"], entry["dur"]
                choice = self._ask_resume_choice(file_path, sec, dur) if ask else "resume"
                if choice == "restart":
                    # 用户明确要从头看：把记录清掉。不清的话下次又问、又续 ——
                    # 而"从头播放"这句话的意思正是"那条记录我不要了"。
                    self._forget_resume(file_path)
                elif choice == "resume":
                    start_sec = sec
                else:
                    return False            # 取消：当前播放原样不动

        # 停止旧视频的播放循环，防止定时器叠加导致加速
        # 【P2 修复】原来这里是手写两行（is_playing=False + 取消定时器），
        # 与 on_close 里的收尾逻辑重复，现统一走 _stop_playback_loop()
        self._stop_playback_loop()

        self.current_video_path = file_path
        if self.cap is not None: self.cap.release()

        self.cap = cv2.VideoCapture(file_path)
        if not self.cap.isOpened():
            messagebox.showerror("错误", "无法打开视频文件")
            return False

        # 【第 17 轮·新】record=False 的文件（转换临时产物）不进"记忆位置"体系。
        # 放在"确实能打开"之后：打不开的文件没必要往这个集合里塞。
        if record:
            self._resume_ignored.discard(resume_store.make_key(file_path))
        else:
            self._resume_ignored.add(resume_store.make_key(file_path))

        # 【第 17 轮·新】帧率/总帧数**提前到这里读** —— 原来在"音频播放器建好
        # 之后"，现在要用它把续播起点从"秒"换算成"帧"，必须先用上。
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.video_fps = fps if fps > 0 else 30.0
        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.scale.config(to=self.total_frames)

        # 把续播起点落到 cap 上。放在这里而不是等 update_frame 之后：后面建音频
        # 播放器、发第一帧都要用同一个位置，先定位再起播，一次到位，不会出现
        # "画面还在第 0 帧、音频已经跳到第 12 秒"的错位。
        start_frame = self._clamp_seek_frame(int(start_sec * self.video_fps))
        if start_frame > 0:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

        # 【第 16 轮·新】先探测这个文件有没有音轨（结论在上面 _create_audio_player
        # 那里用得上：没有音轨就根本不建播放器）。
        #
        # ⚠️ **必须在"摘掉旧播放器"之前**做。探测要起一个 ffmpeg 子进程（实测
        #    ~120ms）。放在"摘旧 → 建新"之间的话，这 120ms 里旧音频已经摘掉、
        #    新音频还没建，会白断一小段声音；放在这里，这 120ms 旧音频照常出声，
        #    后面那段"摘旧 → 建新"的间隙仍然是原来的 ~2ms。
        has_audio = self._probe_audio_track(file_path)

        # 处理旧的音频播放器 —— 【第 15 轮·重写】不再同步 close_player()
        #
        # 原来这里是 _cancel_audio_handoff() + _close_audio_player()，后者会调
        # close_player() 等读线程退出：实测 **440~810ms**（中位数其实只有 0.2ms
        # —— 只有"上一个视频有音轨且真的在播"时才这么慢），而每点一次列表就卡
        # 一次。实测连续切换 8 次共 1565ms，其中 1304ms（83%）花在这一句上。
        #
        # 现在改走"先建后关"，拆成两步（中间隔着"建新播放器"，顺序不能变）：
        #   ① _cancel_audio_handoff(join=False)：作废在飞的交接 —— 这是必须的，
        #      那次交接里可能还有一个旧实例等着被关，不作废就会和新流程抢同一个
        #      实例。join=False 是因为退场线程关的东西主线程已经放手了，等它
        #      只是白卡（连续换文件时这个"白等"会一次次叠加）。
        #   ② _detach_audio_player()：摘下当前实例并立刻止声，但**不关**。
        self._cancel_audio_handoff(join=False)
        old_audio = self._detach_audio_player()

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
        # 音频交接用的"当前视频位置"。【第 17 轮】从续播点起算而不是 0 ——
        # 交接建新播放器时取的就是它（见 _last_media_sec 的说明），
        # 从 0 起算会让"续播后又跳转一次"的音频落到片头。
        self._last_media_sec = start_sec
        self.audio_failed = False  # 换文件后允许重试一次音频
        # 【第 16 轮·新】把上面探测的结论落地（为什么必须连 audio_failed 一起置，
        # 见 _probe_audio_track 的说明；探测不了 = None 时**不置**，
        # 宁可多建一个播放器也不能把真有音轨的文件搞成永久静音）。
        self.current_has_audio = has_audio
        if has_audio is False:
            self.audio_failed = True
        # 【第 11 轮修复】换了文件，"为拖动挂起"的豁免不能跨文件残留
        self._mark_audio_live()
        self.btn_play.config(text="暂停")

        # 创建新的音频播放器（与视频同一文件）。
        # 【第 17 轮】起点从 0 改成 start_sec —— 视频在 cap.set 上定位了，
        # 音频必须落到同一个位置，否则续播一开头就是音画错位。
        # ⚠️ 必须赶在**旧实例还活着**的时候建（上面只是摘下、并没有关）——
        # "先建后关"是硬约束：旧实例还占着一条混音通道，SDL 混音器才不会被销毁，
        # 随后对新播放器调 set_volume 才安全（见 _apply_volume_now 的实测）。
        self.audio_player = self._create_audio_player(file_path, start_sec)
        # 【第 15 轮·修复】新的已就位，现在才把旧的交出去 —— 它会被留到
        # "新播放器的音频确认在跑"之后，再由后台线程关掉（见 _retire_audio_player）。
        self._retire_audio_player(old_audio)

        # 【第 17 轮】进度条也直接落到续播点（原来无条件写 0），免得第一帧
        # 渲染之前滑块还指着片头。
        self.seek_var.set(start_frame)

        # 【第 18 轮·新】装配这个视频的字幕（自动找同名 .srt / .vtt）。
        # ⚠️ 必须在 `update_frame()` 之前 —— 第一帧就要把字幕一起画上去，
        #    放后面的话会看到"第一帧没字幕、第二帧才冒出来"，像卡了一下。
        self._load_subtitles(file_path)

        self.update_frame()

        # 【第 14 轮·新】记进播放列表（放在"已经真的能播"之后 —— 打不开的文件
        # 不该进列表），并把它高亮成当前项。
        if record:
            self._playlist_add([file_path])
            self._refresh_playlist_ui(select_path=file_path)

        # 【第 17 轮·新】续播时明确告诉用户"刚才那个选择生效了，从这儿开始"。
        # 不吭声的话，画面直接跳到中间会让人怀疑是播放器坏了。
        #
        # 【第 18 轮】状态行只有一条，两次 set 会互相覆盖（后写的赢），而"续播了"
        # 和"上了字幕"都是用户该知道的事 —— 所以攒成一个列表拼起来写一次。
        notices = []
        if start_sec > 0:
            notices.append("从 %s 继续播放" % format_hms(start_sec))
        if self.subtitle_set.available:
            notices.append(self.subtitle_set.describe() if self.subtitle_font_path
                           else "找到字幕但本机缺少中文字体")
        if notices:
            self._set_playlist_status(
                "%s：%s" % (" · ".join(notices),
                           os.path.basename(file_path) or file_path))

        # 【第 11 轮·修正 2】刚选完文件的用户，下一个动作几乎一定是播放/暂停。
        # 而此刻焦点还留在"打开视频"那个按钮上 —— 空格会被按钮吃掉（重新打开
        # 文件对话框），"按空格没反应"的观感就是这么来的。这里主动把焦点收回来。
        self._restore_shortcut_focus()
        return True

    def start_conversion(self, file_path=None):
        """把源视频转成 3D（左右分屏 SBS）。

        【第 22 轮·改】`file_path=None` 时**不再无条件弹文件对话框**，而是先看
        "当前正在播放的是哪个视频" —— 于是「打开视频 → 转换为3d」成了一条清楚
        的两步流程，也顺带让"先看看 2D 效果再决定转不转"成为可能。
        显式传 `file_path` 时完全按传进来的来（测试与程序内部调用走这条）。

        真正的源文件选择逻辑在 `_conversion_source()` 里。
        """
        if self.is_converting:
            messagebox.showwarning("提示", "正在转换中，请稍候...")
            return

        if file_path is None:
            file_path = self._conversion_source()
        if not file_path:
            return

        self.is_converting = True
        self.conversion_cancel_event.clear()

        # 显示转换进度面板
        self.conversion_frame.grid()
        # 把源文件名写进首行：转换要跑几分钟，用户得能看出"转的是哪一个"
        self.conv_label.config(text="正在加载模型，请稍候...（%s）"
                                    % os.path.basename(file_path))
        self.conv_progress_var.set(0)
        self.btn_cancel_convert.config(text="取消转换", state=tk.NORMAL)

        self.btn_convert.config(text="转换中...", state=tk.DISABLED)

        # 【P2 修复】留下线程引用，关窗时给它一点时间去删掉写到一半的临时文件
        self.conversion_thread = threading.Thread(
            target=self.process_video_to_3d, args=(file_path,)
        )
        self.conversion_thread.daemon = True
        self.conversion_thread.start()

    def _conversion_source(self):
        """这次转换该拿哪个文件当输入。返回 None = 没得转（用户取消）。

        优先级：**当前正在播放的视频** → 文件对话框。

        ⚠️ 当前播着的就是**本次转换的产物**时不能拿它当源：产物是左右分屏
        （SBS），再转一次只会得到"四格"画面，白烧几分钟算力。而那一刻用户
        真正想做的事其实是"把这份产物另存下来" —— 所以就在【转换为3d】上
        把它做了（问一句再存，不搞静默的行为切换）。
        """
        current = self.current_video_path
        if current and current == self.session_output_path:
            if messagebox.askyesno(
                    "当前播放的是转换产物",
                    "当前这个视频就是上一次转换出来的 3D 产物（左右分屏）。\n\n"
                    "要把它另存为正式文件吗？\n"
                    "（想转换别的视频，请先点【打开视频】）"):
                self.export_3d_video()
            return None
        if current and os.path.exists(current):
            return current
        return filedialog.askopenfilename(
            title="选择要转换为 3D 的视频",
            filetypes=[("MP4 Video", "*.mp4"), ("All Files", "*.*")],
        ) or None

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

        【第 12 轮·重构】实现搬到 `media_tools.mux_audio_into_video`（纯函数）。
        这里保留同名薄封装，理由有二：
          ① 调用点（`process_video_to_3d` 收尾）与测试都按这个名字调用，不动最安全；
          ② **ffmpeg 的查找仍留在本文件** —— 这里靠 `find_ffmpeg()` 拿到路径再传进去，
             于是测试改写 `_FFMPEG_PATH` 伪造"本机没有 ffmpeg"的接缝继续有效。
             顺带还去掉一个隐性依赖：原来 mux 自己偷偷调 `find_ffmpeg()`，
             "补音轨能不能跑"取决于一个它签名上看不见的全局状态。

        返回 True = 音轨已并进去；False = 没补（产物仍是可播放的无声文件），
        三种情况都**不抛异常**：找不到 ffmpeg / 源文件本来没音轨 / ffmpeg 失败或超时。
        """
        return media_tools.mux_audio_into_video(
            video_path, audio_source_path, find_ffmpeg())

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
        self.btn_convert.config(text="转换为3d", state=tk.NORMAL)
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

        # 【第 17 轮】resume=False：临时产物没有"上次看到哪儿"可言
        # （每转一次都是一个新路径），显式说清楚比"碰巧没有记录"可靠。
        self.open_video(output_path, record=False, resume=False)

        # 【第 22 轮·改】原来这里只留一句"点【导出3D视频】可另存为正式文件"的
        # 提示，用户看完还得再去点一次那个按钮。现在底栏没有导出按钮了 ——
        # **转换这一步的自然收尾就是问一句"要不要存下来"**，这正是"三个按钮
        # 合成两个"的落点。
        #
        # 问的顺序（并存/切模式是两个互相独立的问题，合成一个对话框只会让
        # 语义变糊 —— 比如"想另存但不想切红蓝3D"的人就没法表达）：
        #   ① 先问另存 —— 转换刚跑完，最自然的收尾是"把这几分钟的成果存下来"；
        #   ② 再问要不要切红蓝3D —— 保持 P2 定下的行为，绝不假设用户有眼镜。
        want_save = messagebox.askyesno(
            "转换完成",
            "3D 视频已生成（左右分屏格式）。\n"
            "%s\n\n"
            "要把它另存为正式文件吗？\n"
            "（选【否】就只在这里预览；临时文件会在下次转换时自动清理）\n\n"
            "临时文件位置：\n%s"
            % ("已保留原视频的音轨。" if self._last_output_has_audio else
               "产物未包含音轨（没找到 ffmpeg，或源视频本身没有声音）。",
               output_path)
        )
        if want_save:
            self.export_3d_video()

        # 【P2 修复】原来这里无条件把模式切成"红蓝3D"，提示语也直接假设
        # 用户有红蓝眼镜 —— 没有眼镜的人会看到一张奇怪的叠影图，还以为
        # 转换坏了。现在改成先问一句：要就切，不要就保持 2D 左右分屏
        # （转换产物本身就是 SBS 格式，2D 下也能正常看）。
        switch_3d = messagebox.askyesno(
            "显示方式",
            "现在切换到【红蓝3D】模式吗？\n"
            "（需要戴红蓝眼镜才能看出立体感；选'否'则保持 2D 左右分屏显示）"
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
        elif key == "v":
            self.cycle_subtitle_mode()
        elif key == "b":
            self.toggle_subtitle_bar()
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
            "V             切换字幕（关闭 → 中文 → 英文 → 中英双语）\n"
            "B             开关独立字幕栏（字幕挪到画面之外，不再遮画面）\n"
            "F  /  F11     全屏开关\n"
            "F1            显示这份说明\n\n"
            "底栏那两个主按钮：\n"
            "· 【打开视频】选一个视频就播。2D 的、已经是 3D 的都能开 ——\n"
            "  本程序是**实时**把 2D 画面转成 3D 显示的，跟源文件是不是 3D 无关\n"
            "· 【转换为3d】把**当前正在播的这个视频**转成一个左右分屏的 3D 文件\n"
            "  （还没打开任何视频就会让你选一个）；转完会问你要不要另存下来。\n"
            "  想事后补存：当前播的就是上次转出来的那份产物时，再点它一次即可，\n"
            "  那一下会被理解成「另存」（产物本身是左右分屏，再转只会得到四格）\n\n"
            "字幕（把 .srt / .vtt 放在视频旁边、与视频同名即可自动加载）：\n"
            "· 认 `电影.srt`、`电影.zh.srt`、`电影.en.srt`、`电影.中英.srt` 这些写法，\n"
            "  也认视频同目录下的 subs / 字幕 子目录；名字看不出语种就按内容判断\n"
            "· 一个文件里中文一行、英文一行的（字幕组最常见的那种），选\n"
            "  【中英双语】就是中文在上、英文在下两行一起显示\n"
            "· 名字对不上号的（下载下来叫 1.srt 之类）点【载入字幕】手选\n"
            "· 字幕默认画在画面内底部（所有播放器都这样）。觉得遮挡画面 —— 尤其\n"
            "  是看 3D 的时候 —— 就按 B 或勾上【字幕栏】：窗口底部会切出一条\n"
            "  纯色横条专放字幕，字幕再也不压画面，代价是画面小一点\n\n"
            "没有字幕文件？【在线匹配】可以直接去 OpenSubtitles 下一份：\n"
            "· 它先按视频文件的指纹精确匹配（比按片名搜准得多），再按片名+年份，\n"
            "  优先挑中英双语，其次简体。下好的字幕存到视频旁边并自动加载\n"
            "· 对结果不满意就**再点一次**：会换下一条候选，不会重复下同一份\n"
            "· **文件名里带上年份**（阿凡达.2009.mkv）能准得多 —— 同名片和同系列\n"
            "  （《阿凡达》/《阿凡达2》）靠年份才分得开，没有年份它也认不出来\n"
            "· 认不出是哪部片时它**不下错的那份**，直接打开列表让你挑（不花额度）\n"
            "· 想自己搜自己挑：【搜索…】。填片名和年份，列表里每条都写清属于\n"
            "  哪部片（原名+年份）、什么语种、多少帧、多少人下过\n"
            "· **绝不覆盖**你已有的字幕文件 —— 重名就退成 电影.zh.1.srt\n"
            "· 第一次用需要填一个免费的 API key（右键点【在线匹配】可以改）\n\n"
            "播放列表（右侧那一栏）：\n"
            "· 可以直接把视频文件从资源管理器拖进窗口，拖进来就开始播\n"
            "· 双击列表条目播放它；播完自动播下一项（列表末尾回到第一项）\n"
            "· 一次拖多个文件、甚至整个文件夹都行，会全部加进列表\n\n"
            "万一按键没反应：点一下画面即可。\n"
            "（焦点停在按钮/滑块/下拉框/播放列表上时，那几个键在该控件上有自己\n"
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
        # 【第 15 轮·新】播放列表栏也是"控件"，跟着一起收 —— 全屏下右边还挂着
        # 一条 236px 的列表栏就不叫全屏了。它比底栏多一步：还要把第 1 列的
        # 宽度约束归零，否则视频区右边会留一条空带（见那个方法）。
        self._sync_playlist_visibility()
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
        # 【第 15 轮·新】列表栏同理，只按"用户的意愿"决定要不要一起唤回来 ——
        # 用户自己点过"隐藏列表"的话，晃一下鼠标不该把它强行拉出来。
        self._sync_playlist_visibility()
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

    def _sync_playlist_visibility(self):
        """按「用户的意愿 + 这一刻该不该显示控件」决定右侧列表栏的显隐。

        两个条件**都**成立才显示：

          · `_playlist_visible` —— 用户的意愿（"隐藏列表"按钮切换的那个开关）；
          · `_controls_visible` —— 当前不在"全屏自动隐藏"那一段里。

        【第 15 轮·新】在这之前只看第一个条件，于是全屏下鼠标一停、底栏收起来了，
        右边却还挂着一条 236px 的列表栏 —— 全屏等于白叫。现在把两个条件拆开：
        "用户想不想看列表"和"这一刻该不该显示控件"互不干扰。
        全屏自动隐藏期间不显示；鼠标一晃唤回控件时，只有用户没主动关过列表，
        它才跟着回来。

        隐藏时**必须连第 1 列的 minsize 一起归零** —— `grid_remove` 只是
        "不管理这个控件"，列宽约束还在，视频区右边会留下一条 236px 的空带。
        """
        if self._playlist_visible and self._controls_visible:
            self.playlist_frame.grid()
            self.root.grid_columnconfigure(1, minsize=PLIST_MIN_WIDTH)
        else:
            self.playlist_frame.grid_remove()
            self.root.grid_columnconfigure(1, minsize=0)

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

    def _screenshot_frame(self):
        """截图用的图像：**默认用"上一次真正上屏的那一屏"**。

        【第 18 轮·改】原来是直接存 `current_frame_image`（`_process_3d` 的输出），
        它只是"视频本身"，没有黑边、也没有字幕 —— 于是截出来的图跟屏幕上看到
        的不是一回事：明明在看双语字幕，截图里一个字都没有。这属于"早晚会被当
        bug 报上来"的差异，所以顺手让它所见即所得。

        `_last_display_image` 是 `render_current_frame` 收尾时留下的整屏 RGBA。
        极少数情况下它可能是空的（比如刚打开、一帧都还没渲染），那就退回旧行为。
        """
        image = self._last_display_image
        if image is not None:
            # PIL 是 RGBA（红绿蓝 + alpha），OpenCV 要 BGR。用花式索引取反序，
            # 顺手把 alpha 丢掉；这一步会产出新数组（连续内存），imencode 收得下。
            return np.asarray(image)[:, :, [2, 1, 0]]
        return self.current_frame_image

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
            ok, buf = cv2.imencode(ext, self._screenshot_frame())
            if not ok:
                raise RuntimeError("图像编码失败")
            with open(save_path, "wb") as f:
                f.write(buf.tobytes())
        except Exception as e:
            messagebox.showerror("截图失败", "保存失败：\n%s" % e)
            return

        messagebox.showinfo("截图成功", "截图已保存到：\n%s" % os.path.abspath(save_path))

    def export_3d_video(self):
        """把本次转换的产物另存为一个正式文件。返回 True = 确实存下去了。

        【P1 修复】只认 `session_output_path` 记录的那个文件，**绝不看**
        "工作目录里有没有同名文件" —— 否则用【打开视频】载入别的文件之后
        再导出，导出的会是上一次转换遗留的残留产物（甚至可能是中途报错
        留下的残缺文件）。

        【第 22 轮·改】底栏那个常驻的「导出3D视频」按钮去掉了（三个按钮合成
        两个），但这条能力一点没少，只是换成两个更贴场景的入口：
          · 转换刚结束 —— `on_conversion_done` 问"要另存吗"，走的就是这里；
          · 事后想补存 —— 当前正在播的就是产物时，再点【转换为3d】也走这里
            （见 `_conversion_source`：那个状态下"再转一次"没有意义，
            产物本身就是左右分屏，再转只会得到四格画面）。
        """
        temp_file = self.session_output_path
        if not temp_file or not os.path.exists(temp_file):
            messagebox.showwarning(
                "提示",
                "还没有可另存的3D视频。\n"
                "请先点【转换为3d】完成一次转换。"
            )
            return False

        return self._save_converted_as(temp_file)

    def _save_converted_as(self, temp_file):
        """弹"另存为"对话框，把 `temp_file` 复制过去。返回是否存成。

        `temp_file` 必须**由调用方显式传进来** —— 这是 P1 那条教训的结构化
        落地：要导出的路径永远来自"我们记下的那一个"，而不是再查一次磁盘。
        """
        save_path = filedialog.asksaveasfilename(
            defaultextension=".mp4",
            filetypes=[("MP4 Video", "*.mp4")],
            title="选择保存位置",
            initialfile="我的3D视频.mp4"
        )

        if not save_path:
            return False

        try:
            shutil.copy(temp_file, save_path)
        except Exception as e:
            messagebox.showerror("导出错误", f"导出失败：\n{str(e)}")
            return False
        messagebox.showinfo("导出成功", f"3D视频已成功导出到：\n{save_path}\n\n可以直接发送给朋友，或使用支持3D的播放器观看。")
        return True

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
            # 【第 17 轮·新】"这一遍播到头了" —— 顺手把"记忆播放位置"清掉。
            # 下次再点开这个文件就是从头播、不再问"要不要继续"：片子都看完了，
            # 再问一句"上次看到 19:58，要接着看吗"只会让人莫名其妙。
            # 之所以是显式告诉它"finished"，而不是让它自己去量位置：此刻
            # `cap.read()` 已经读不出帧，位置读数不可信（停住不动、或者停在
            # 某个解码出错的地方）。
            self._settle_current_position(finished=True)

            # 【第 14 轮·新】先问播放列表：还有下一项就切过去，而不是在原地绕圈。
            #   ⚠️ 必须**立刻 return** —— open_video 内部已经停掉旧的播放循环、
            #   重新排好了下一帧；这里再往下走就会再排一次，两套定时器叠加
            #   = 画面以两倍速跑（第 1 轮踩过的那种"加速"）。
            #   `force=True`（暂停态重绘一帧）**不续播**：那不是"播完了"，
            #   只是用户改了视差/切了模式要刷新一张画面，不能顺手把文件换掉。
            next_path = self._advance_playlist() if not force else None
            if next_path is not None:
                # 【第 17 轮】ask=False：这是"往下连播"，不该在中途弹窗打断。
                # 但如果那个文件上次也没看完，就静默接着上次的位置 —— 连播
                # 本来就是这个意思（像追剧，一集接一集）。
                if not self.open_video(next_path, ask=False):
                    # 文件在、但解不开（编码坏了等）：记下来，下一轮续播会跳过它，
                    # 本帧到此为止 —— 不要再往下走和 3D 处理抢状态。
                    self._playlist_failed.add(next_path)
                return

            # 没有可续播的项 → 保持原来的"原地循环回绕"。
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
        # 0. 【第 17 轮·新】把"这个视频停在哪儿"存下来。
        #    必须是**第一件事** —— 下面第 5 步会 release 掉 cap，位置只能从
        #    还活着的那个读；而且第 2 步之后 play_state 就成 IDLE 了。
        #    （用户关了播放器、过几天再打开，能接着上次看，靠的就是这一句。）
        self._settle_current_position()

        # 0.5 【第 20 轮】让在线匹配的轮询停下来。它在主线程上**自我续期**，
        #     窗口都销毁了还继续排 after，只会换来一串
        #     "invalid command name ..._poll_online"。
        self._online_busy = False

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
        """**同步**关闭并清空音频播放器（会等读线程退出，≈0.4~0.8 秒）。

        注意：**不要在后台音频交接进行中调用** —— 交接期间旧实例可能还留在
        _audio_handoff_old / 工作线程手里，先关当前这个会把混音器拆掉，
        让那次交接里的抢发音量撞上已销毁的混音器。调用前必须先
        _cancel_audio_handoff()。

        【第 15 轮·新】它现在**只剩关窗路径**在用了（on_close 里那一次：
        进程要退出了，同步等干净是应该的）。换文件改走
        `_detach_audio_player()` + `_retire_audio_player()` —— 那条路径
        不能同步等（每点一次列表就卡 0.4~0.8 秒）。
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

    def _probe_audio_track(self, file_path):
        """探测文件有没有音轨。返回 True / False / None（None = 探测不了）。

        【第 16 轮·新】为什么提前探测，而不是"先建播放器、再看时钟死不死"
        （原来就是那么干的，代价记在文件顶部第 5 条）：

        1. **那行让人以为"打开失败"的报错就是从这儿来的。** 给没有音轨的文件建
           MediaPlayer，ffpyplayer 的读线程必然配不起 filtergraph，于是往 stderr
           打一条 FATAL：`Failed to open file '...' or configure filtergraph`。
           它**产生于打开文件那一刻**，但 Windows 上 stderr 一旦不是终端就是
           **块缓冲**（4KB），这几十个字节填不满，于是一直扣在 C 运行时的缓冲里，
           直到进程退出、os._exit 触发 CRT 的 flush 才冲出来。表现出来就是
           "一关播放器就冒出一行打开失败"，看着像关闭动作把播放搞坏了，其实两者
           毫无关系（实测：开文件时就有，只是当时看不见）。
           要让它彻底不出现，唯一的办法就是**不建那个播放器**。

        2. **那个播放器从头到尾没有用处** —— 文件里没有音频流，它只会白占一条
           SDL 混音通道和一颗 ffpyplayer 线程，还得在换文件时陪着走一遍 close
           （那正是第 15 轮花大力气挪去后台的操作）。

        3. **省掉一条 1.5 秒的弯路**：原来要等死时钟检测攒够
           AUDIO_CLOCK_STALL_SEC 才判死、才转用墙钟主时钟，这期间画面按帧率盲走。
           提前知道没有音轨，第一帧就能直接用墙钟（见 open_video 里设
           audio_failed 那两行）。

        代价是起一个 ffmpeg 子进程，实测 ~120ms，所以结论按文件指纹缓存。
        只缓存**有结论**的结果：探测失败多半是"这一次读不到"，缓存下来等于把
        偶然失败固化成结论（到时候真有音轨的文件会被永久静音，那是本末倒置）。
        """
        if not file_path:
            return None
        try:
            key = (os.path.abspath(file_path), os.path.getsize(file_path),
                   os.path.getmtime(file_path))
        except OSError:
            key = None                      # 拿不到指纹就不缓存，照常探测
        if key is not None and key in self._audio_probe_cache:
            return self._audio_probe_cache[key]

        result = media_tools.probe_audio_stream(find_ffmpeg(), file_path)

        if key is not None and result is not None:
            if len(self._audio_probe_cache) >= AUDIO_PROBE_CACHE_MAX:
                self._audio_probe_cache.clear()
            self._audio_probe_cache[key] = result
        return result

    def _warm_audio_probe_cache(self):
        """趁空闲把播放列表里各文件的音轨结论先算出来。

        【第 16 轮·新】探测要起一个 ffmpeg 子进程（实测 ~120ms）。用户点列表
        常常是连着点的，每次都现探就会一卡一卡 —— 而列表在启动时就确定了，
        完全可以先算好，之后点哪个都是 0ms（缓存命中）。

        三条纪律：
          · 线程里只调 `_probe_audio_track`（只读文件 + 写缓存），**不碰任何
            播放状态**，所以与主线程并行是安全的。缓存是普通 dict，CPython 下
            单个 get/set 原子；最坏情况不过是同一条目被探两次。
          · **先在这边（主线程）调一次 `find_ffmpeg()`** —— 它背后那对全局缓存
            （`_FFMPEG_PATH` / `_FFMPEG_SEARCHED`）是懒搜索的，放在后台线程里
            首次命中会触发一次文件系统全盘查找。
          · 最少 2 个文件才值得预热（一个文件的探测成本反正躲不掉，还白起线程），
            只预热确实存在的文件，线程是 daemon：列表再长、某个文件在慢盘上，
            也不会拖住退出。
        """
        if not self._audio_probe_warm_enabled:
            return
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            return
        targets = [p for p in self.playlist[:AUDIO_PROBE_WARM_MAX]
                   if p and os.path.isfile(p)]
        if len(targets) < 2:
            return

        def work():
            # ⚠️ 这里**只**做探测。别在后台线程里碰 Tk（连 winfo_exists 这种
            #    "只读"调用也不行 —— Tk 不是线程安全的）。要到"窗口没了就停"
            #    的效果，用主线程能看见的普通标志，而不是问 Tk。
            for path in targets:
                try:
                    self._probe_audio_track(path)
                except Exception:
                    pass

        threading.Thread(target=work, name="audio-probe-warm", daemon=True).start()

    def _create_audio_player(self, file_path, start_sec=0.0):
        """**同步**创建音频播放器（构造本身只要 ~1.6ms）。

        两条路径调用：① 打开文件；② _begin_audio_handoff 启动后台交接时
        （先建新的、后关旧的，见那一段说明）。**不要**在拖动进度条这类高频
        路径上直接调用它 —— 那类路径要先走 _begin_audio_handoff()，
        否则又回到"关旧播放器卡主线程"的老问题。
        返回创建的 MediaPlayer；失败返回 None 并置 audio_failed。
        """
        # 【第 16 轮】多一条 `current_has_audio is False` 的保险。open_video 那时
        # 已经把 audio_failed 一并置上了（两处是同一个判断的两道门），这里再拦一次
        # 是为了防止将来的新调用点漏掉那步 —— 代价只是多一个判断，而漏掉的后果是
        # 白建一个没用的播放器 + 又冒出那行误导人的报错。
        if not file_path or self.audio_failed or self.current_has_audio is False:
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
        self._spawn_audio_retire(old)
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

    def _spawn_audio_retire(self, old):
        """起一个后台线程关掉旧播放器 `old`，并登记下来（关窗时有界地 join）。

        统一的出口：`_cancel_audio_handoff` 与 `_poll_audio_handoff_ready`
        原来各自起一遍线程，现在都走这里 —— 免得漏登记，或者将来只改了其中
        一处。顺带把已经结束的线程从列表里剔掉，否则连点几十次文件会把列表
        一直撑大。
        """
        if old is None:
            return None
        thread = threading.Thread(
            target=self._audio_handoff_worker, args=(old,),
            name="audio-retire", daemon=True,
        )
        self._audio_retire_threads = [t for t in self._audio_retire_threads
                                      if t.is_alive()]
        self._audio_retire_threads.append(thread)
        self._audio_handoff_thread = thread      # 兼容既有的"交接线程"判据
        thread.start()
        return thread

    def _join_audio_retire(self, timeout):
        """有界地等待所有"关旧播放器"的后台线程结束（关窗 / 换文件时用）。

        总预算是 `timeout` 秒，**不是每个线程各等这么久** —— 连续换了好几次
        文件时可能有多个退场线程在飞，等不起随次数线性增长。

        等不到就放弃：它们是 daemon，进程退出时随之一并结束；而且此刻主线程
        手上已经没有任何播放器实例，不存在"两个 SDL 音频设备并存"的问题。
        """
        deadline = time.time() + max(0.0, float(timeout))
        for t in list(self._audio_retire_threads):
            remain = deadline - time.time()
            if remain <= 0:
                break
            try:
                t.join(remain)
            except Exception:
                pass
        self._audio_retire_threads = [t for t in self._audio_retire_threads
                                      if t.is_alive()]

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

    def _cancel_audio_handoff(self, timeout=2.0, join=True):
        """作废正在进行的音频交接（换文件 / 关窗前必须先调用）。

        两种"在飞"的状态都要收：① 旧播放器已经交给工作线程在关；
        ② 旧播放器还留在 `_audio_handoff_old` 里等着"新播放器音频就绪"——
        这里不等了，直接把它交给工作线程关掉。此后再没有人持有那个实例。

        `join=True`（默认）会在 `timeout` 秒内等退场线程结束 —— **关窗时要这个**，
        否则进程退出时还有线程在拆 SDL 音频设备。等待有界：超时就放弃
        （daemon 线程，随进程退出）。

        【第 15 轮·改】换文件走 `join=False`。那里**不需要等**：退场线程关的是
        一个主线程已经完全放手的实例，等它只是白卡主线程 —— 而连续换文件时
        这个"白等"会一次叠一次，正是"连点几下就一顿一顿"的第二个来源
        （第一个是同步 close_player，见 open_video 里的说明）。
        """
        self._cancel_audio_handoff_poll()
        self._audio_volume_resume = False       # 抢发窗口即刻关闭
        self._stop_volume_resume_poll()
        self._audio_handoff_seq += 1

        old = self._audio_handoff_old
        self._audio_handoff_old = None
        if old is not None:
            self._spawn_audio_retire(old)

        # 交接就此作废：_audio_handoff_thread 归 None 表示"没有交接在进行"，
        # update_frame 的每帧兜底（挂起的重建请求）因此重新放行。
        # 线程本身仍登记在 _audio_retire_threads 里，关窗时等得到。
        self._audio_handoff_thread = None
        self._audio_handoff_pending = False
        if join:
            self._join_audio_retire(timeout)

    # ============================================================
    # 【第 15 轮·修复】换文件时的音频"先建后关"
    # ------------------------------------------------------------
    # 打开新视频原来是这样：同步 close_player() → 建新播放器。
    # close_player() 要等读线程退出，实测 440~810ms（中位数其实只有 0.2ms ——
    # 只有"上一个视频有音轨且真的在播"时才这么慢），于是每点一次列表就卡一次。
    # 现在拆成两半：先摘下并止声（不阻塞）→ 建新播放器 → 旧的交出去，等新播放器
    # 的音频确认在跑之后由后台线程关。顺序不能反，"先建后关"是硬约束
    # （见 _apply_volume_now 的段错误分析）。
    # ============================================================
    def _detach_audio_player(self):
        """把当前音频播放器"摘下来"并立刻止声（**不阻塞**），返回被摘下的实例。

        这是"先建后关"的前半步：摘掉引用但**不关它** —— 此刻新播放器还没建，
        现在关掉会销毁 SDL 混音器，随后对新播放器调 set_volume 就是段错误
        （读 NULL+0x18，见 _apply_volume_now）。调用方必须在**建好新播放器之后**
        调 `_retire_audio_player(old)` 收尾。

        顺手做两件事：
          ① `set_pause(True)` 立刻止声 —— 旧实例还要活几百毫秒，不能指望它自己
             停（换文件时新旧声音叠在一起会很明显）；这一步安全且便宜。
          ② `_mark_audio_live()` —— "当前没有播放器"了，"为拖动挂起"的豁免
             不该跨文件残留（与 _close_audio_player 里那句同理）。
        """
        old = self.audio_player
        self.audio_player = None
        if old is not None:
            try:
                old.set_pause(True)
            except Exception:
                pass
        self._mark_audio_live()
        return old

    def _retire_audio_player(self, old):
        """换文件后把摘下来的旧播放器交出去 —— 等新播放器音频确认在跑，再后台关。

        【为什么不在这里直接起线程关】关掉最后一个占着混音通道的播放器会销毁
        SDL 混音器，此后 `set_volume` 就可能段错误。所以旧的必须一直活到
        "新播放器的音频通道确认建立"为止 —— 这正是 `_audio_handoff_old` 的语义，
        于是这里复用同一套机制，不另造一套状态：

            _retire_audio_player → _audio_handoff_old 有值
                → update_frame 每帧问 _poll_audio_handoff_ready
                → 新音频确认在跑（或确认没音轨 / 超时兜底）
                → _spawn_audio_retire 后台关，_poll_audio_handoff 收尾

        这样做还顺带得到两个本来就有的一致行为：这期间墙钟接管画面
        （`_audio_handoff_pending`），以及"交接期间来的跳转请求会被补上"。
        """
        if old is None:
            return
        if self._audio_handoff_old is not None:
            # 理论上到不了这里（调用前刚 _cancel_audio_handoff 清过）。
            # 真撞上就起线程关掉它 —— 不能被顶掉：没人关的播放器会一直占着
            # 混音通道，后面所有 set_volume 的判据都会失准。
            self._spawn_audio_retire(self._audio_handoff_old)
        self._audio_handoff_old = old
        self._audio_handoff_started = time.time()
        self._audio_handoff_pending = True

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

    def _process_3d(self, frame):
        """把 SBS 原始帧处理成当前显示模式的帧。

        【第 12 轮·重构】实现搬到 `depth3d.process_3d`（纯函数）；原来它专用的
        `_get_mesh` 也随之变成 `depth3d.MeshCache`（实例是 `self._mesh`）——
        `_mesh_hw` / `_mesh_x` / `_mesh_y` 三个字段因此从本类消失。

        保留同名薄封装，三个调用点（update_frame / change_display_mode /
        _refresh_paused_frame）就一行都不用改。
        """
        return depth3d.process_3d(frame, self.display_mode, self.swap_eyes,
                                  self.parallax_value, self._mesh)

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