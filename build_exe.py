#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 final_player.py 打包成一个可以直接发给别人的 Windows 程序。

用法（在项目根目录执行）：

    build\\buildvenv\\Scripts\\python.exe build_exe.py              # 正式包（不带控制台）
    build\\buildvenv\\Scripts\\python.exe build_exe.py --console    # 调试包（留一个黑窗口看输出）
    build\\buildvenv\\Scripts\\python.exe build_exe.py --no-zip     # 不压 zip

产出：dist/final_player/  —— 整个文件夹就是"发出去的东西"，压成 zip 发给别人即可。

--------------------------------------------------------------------------------
为什么必须是 --onedir 而不是 --onefile
--------------------------------------------------------------------------------
这个程序自带 140MB 的 ffmpeg、100MB 的深度模型和几百 MB 的 PyTorch。
--onefile 会把它们全压进一个 exe，**每次双击**都要先把这几百 MB 解包到 %TEMP%
再启动（十几秒起，慢盘上一分钟），退出时还要清理，杀进程还会留一堆垃圾。
onedir 是一个文件夹，双击即起。代价只是"不能只发一个 exe 文件"。
--------------------------------------------------------------------------------
为什么 torch 必须是 CPU 版
--------------------------------------------------------------------------------
CUDA 版 torch 装完 4.3GB，其中 4.2GB 是 CUDA 运行库（cudnn/cublas/nvrtc 那一堆
dll）。它只有在**对方也装了 NVIDIA 驱动 + 有 N 卡**时才有用；对方没有的话，
这 4.2GB 是纯陪跑，而程序照样能用（torch 自动退回 CPU）。
本程序用的深度模型是 Depth-Anything-V2-**Small**，CPU 推理每帧约 0.2 秒，
转换一部几分钟的短片慢几分钟，但完全可用。
⇒ 打包用单独的 build/buildvenv（CPU 版 torch），开发用的 .venv 一个字节都不动。
"""

import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
BUILD_VENV = os.path.join(ROOT, "build", "buildvenv")
APP_NAME = "final_player"
DIST_DIR = os.path.join(ROOT, "dist")
WORK_DIR = os.path.join(ROOT, "build", "pyi_work")
OUT_DIR = os.path.join(DIST_DIR, APP_NAME)

# 随包走的 ffmpeg：find_ffmpeg() 会在 sys.prefix 下按
# share/ffpyplayer/ffmpeg/bin 找，所以 dest 这一段不能改。
FFMPEG_SRC = os.path.join(BUILD_VENV, "share", "ffpyplayer", "ffmpeg", "bin")
FFMPEG_DEST = os.path.join("share", "ffpyplayer", "ffmpeg", "bin")

# 随包走的深度模型缓存：目录名必须保持 HuggingFace 的
# models--<org>--<name> 形状，代码里靠 HF_HUB_CACHE 指过来（见 final_player.py 的冻结垫片）。
HF_HUB_DIR = os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")
HF_SRC = os.path.join(HF_HUB_DIR, "models--depth-anything--Depth-Anything-V2-Small-hf")
HF_DEST = os.path.join("hf_cache", "models--depth-anything--Depth-Anything-V2-Small-hf")

# ⚠️ 这个 1 字节的小文件必须一起带上，否则第一次转换会打一屏
#    "The cache for model files in Transformers v4.22.0 has been updated.
#     Migrating your old cache."
#    —— transformers 用 <hub>/version.txt 判断"这个缓存是不是老结构"，
#    缺了它就走迁移分支（扫描 + 自己补写这个文件）。功能上能自愈，但
#    用户看到的是一句很像出错的告警，而且多一次全目录扫描。
#    实测：不带上它，包内 hf_cache/version.txt 会在第一次运行后被程序自己补出来。
#
# ⚠️⚠️ dest 这里写的是**目录** hf_cache，不能写 "hf_cache/version.txt"：
#    --add-data 的 dest **一律当作目录**用，写成文件路径会造出一个**同名目录**
#    （hf_cache/version.txt/version.txt），于是 transformers 照样找不到标记文件、
#    还因为"version.txt 是个目录"而写不进去，多打一句
#    "There was a problem when trying to write in your cache folder..."。
#    第 24 轮实测踩过 —— 症状和"根本没带这个文件"一模一样。
HF_VERSION_SRC = os.path.join(HF_HUB_DIR, "version.txt")
HF_VERSION_DEST = "hf_cache"

# 程序里用的深度模型。
MODEL_ID = "depth-anything/Depth-Anything-V2-Small-hf"

# transformers 的 AutoModel/AutoImageProcessor 走的是 **动态 importlib**，
# PyInstaller 的静态分析看不见（hook-transformers.py 只拷包元数据、不收子模块）。
# 漏了它们，包能启动，但一点【转换为3d】就 ImportError —— 只有真跑转换才暴露。
#
# ⚠️ 所以不能手写死名单。第 24 轮实测：模型 id 里明明写着 Depth-Anything-**V2**，
#    但它的 config.json 里
#        model_type          = "depth_anything"     ← V1 的类，不是 depth_anything_v2
#        backbone_config     = "dinov2"
#        image_processor_type= "DPTImageProcessor"
#    照名字写 "transformers.models.depth_anything_v2" 会得到一个**恰好是错的**包：
#    能启动、能播放、能截屏，直到用户点【转换为3d】才炸。
#    正确做法是在打包时**问 transformers 自己**（下面这个函数）。
#
# 这一份兜底只在"本机没有模型缓存 / transformers 装不上"时用。
# tests/test_packaging.py 第 ② 节会断言它跟现场解析结果一致 —— 所以它不会
# 随时间悄悄失真：模型换了、名单没跟，套件会红。
TRANSFORMERS_FALLBACK = [
    "transformers.models.auto",            # Auto* 那几个类自己
    "transformers.models.depth_anything",  # 模型主体（model_type = "depth_anything"）
    "transformers.models.dinov2",          # backbone（backbone_config = "dinov2"）
    "transformers.models.dpt",             # 图像处理器（DPTImageProcessor）
]


def resolve_transformers_modules():
    """现场问 transformers：这个模型真正会 import 哪几个子包。

    做法就是把程序启动时走的那条路原样走一遍（AutoConfig / AutoImageProcessor /
    AutoModelForDepthEstimation 各 from_pretrained 一次），然后把**实际拿到的
    对象**所属的模块收出来。比任何手写名单都准，而且模型换代时自动跟上。
    """
    try:
        from transformers import (AutoConfig, AutoImageProcessor,
                                  AutoModelForDepthEstimation)
        cfg = AutoConfig.from_pretrained(MODEL_ID, local_files_only=True)
        proc = AutoImageProcessor.from_pretrained(MODEL_ID, local_files_only=True)
        model = AutoModelForDepthEstimation.from_pretrained(MODEL_ID, local_files_only=True)
    except Exception as exc:
        log("⚠️  解析模型的真实模块失败（%s: %s），改用兜底名单。"
            % (type(exc).__name__, exc))
        return list(TRANSFORMERS_FALLBACK)

    mods = {type(cfg).__module__, type(proc).__module__}
    backbone_cfg = getattr(cfg, "backbone_config", None)
    if backbone_cfg is not None:
        mods.add(type(backbone_cfg).__module__)
    for _name, sub in model.named_modules():
        mods.add(type(sub).__module__)

    pkgs = sorted({m.rsplit(".", 1)[0] for m in mods
                   if m.startswith("transformers.models.")})
    found = ["transformers.models.auto"] + pkgs
    log("模型真实用到的 transformers 子包：%s" % "、".join(found))
    return found

# 明确用不上的大件。
# ⚠️ 这里故意**很短**：`unittest` / `torch.distributed` / `torch.testing` 看着也没用，
#    但 torch 与 transformers 会在运行时按需 import 它们（`torch.distributed`
#    更是 transformers 判断"要不要走分布式"的探针）。排掉它们能省几十兆，
#    换来的却是一个"某个分支一跑到就 ImportError"的包 —— 不值。
#    还有 imageio_ffmpeg 也**不能**排：find_ffmpeg() 会试着 import 它。
EXCLUDES = [
    "matplotlib", "scipy", "pandas", "IPython", "pytest", "notebook",
    "torchaudio", "tkinter.test",
]


def log(msg):
    print("[build] %s" % msg, flush=True)


def die(msg):
    print("[build] 失败：%s" % msg, file=sys.stderr, flush=True)
    sys.exit(1)


def python_in_venv():
    return os.path.join(BUILD_VENV, "Scripts", "python.exe")


def human(nbytes):
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024 or unit == "GB":
            return "%.1f %s" % (nbytes, unit)
        nbytes /= 1024.0


def dir_size(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


def preflight():
    """开工前把"会白跑十分钟"的情况全部挡在前面。"""
    py = python_in_venv()
    if not os.path.isfile(py):
        die("打包环境不存在：%s\n"
            "      先建它：\n"
            "        .venv\\Scripts\\python.exe -m venv build/buildvenv\n"
            "        build\\buildvenv\\Scripts\\python.exe -m pip install "
            "torch==2.5.1+cpu torchvision==0.20.1+cpu "
            "--index-url https://download.pytorch.org/whl/cpu\n"
            "        build\\buildvenv\\Scripts\\python.exe -m pip install "
            "opencv-python==4.10.0.84 ffpyplayer==4.5.3 pillow==12.3.0 "
            "numpy==2.5.3 transformers==4.46.3 safetensors==0.8.0 "
            "requests==2.34.2 pyinstaller==6.22.3" % BUILD_VENV)

    # torch 是不是 CPU 版 —— 这是唯一会让成品大 4GB 的开关
    probe = subprocess.run(
        [py, "-c", "import torch;print(torch.__version__);print(torch.version.cuda)"],
        capture_output=True, text=True)
    if probe.returncode != 0:
        die("打包环境里的 torch 装不上/装坏了：\n%s" % probe.stderr.strip())
    version, cuda = (probe.stdout.strip().splitlines() + ["", ""])[:2]
    if cuda and cuda != "None":
        log("⚠️  打包环境里是 CUDA 版 torch（%s，CUDA %s）——"
            "成品会大到 5GB 左右，且只有对方有 N 卡时才用得上。" % (version, cuda))
        log("⚠️  想换成 CPU 版：删掉 build/buildvenv，按上面 preflight 里的三条命令重建。")
    else:
        log("torch %s（CPU 版，正常）" % version)

    if not os.path.isfile(os.path.join(FFMPEG_SRC, "ffmpeg.exe")):
        die("找不到 ffmpeg：%s\n"
            "      它是 ffpyplayer 的 FULL-BUILD 轮子自带的；"
            "换成 ffpyplayer==4.5.3 重装一次。" % FFMPEG_SRC)
    log("ffmpeg 自带完整（%d 个文件，%s）"
        % (len(os.listdir(FFMPEG_SRC)), human(dir_size(FFMPEG_SRC))))

    if not os.path.isdir(HF_SRC):
        log("⚠️  本机没有深度模型缓存（%s）——跳过，"
            "包会小 100MB，但对方第一次转换必须联网下载。" % HF_SRC)
        return py, None
    log("深度模型缓存齐全（%s）" % human(dir_size(HF_SRC)))
    if not os.path.isfile(HF_VERSION_SRC):
        log("⚠️  缺少 %s ——第一次转换时 transformers 会打一屏"
            "「正在迁移旧缓存」的告警（能自愈，但吓人）。" % HF_VERSION_SRC)
    return py, HF_SRC


def build_options(console, hidden):
    opts = [
        "--noconfirm", "--clean", "--onedir",
        "--name", APP_NAME,
        "--distpath", DIST_DIR,
        "--workpath", WORK_DIR,
        "--specpath", WORK_DIR,
        "--console" if console else "--windowed",
        # ffmpeg 整目录：ffmpeg.exe 是个只读 av*.dll 的壳，
        # 少一个 dll 就是"补音轨静默失效"。所以整个 bin 目录一起搬。
        # 注意两个路径都必须是**绝对路径**：--add-data 的相对源路径是相对
        # --specpath（而不是 cwd）解析的，写成相对会得到
        #   ERROR: Unable to find '<workpath>\<相对路径>'
        # 这个坑第 24 轮实测踩过一次。
        "--add-data", "%s;%s" % (FFMPEG_SRC, FFMPEG_DEST),
    ]
    for name in hidden:
        opts += ["--collect-submodules", name]
    for name in EXCLUDES:
        opts += ["--exclude-module", name]
    return opts


def run_build(console, hf_src, hidden):
    py = python_in_venv()
    opts = build_options(console, hidden)
    if hf_src:
        opts += ["--add-data", "%s;%s" % (hf_src, HF_DEST)]
        if os.path.isfile(HF_VERSION_SRC):
            opts += ["--add-data", "%s;%s" % (HF_VERSION_SRC, HF_VERSION_DEST)]

    # 显式清掉旧产物再交给 PyInstaller。
    # ⚠️ 不能只靠 --noconfirm：它删不掉旧目录时**只是跳过**，于是这一次打出来的
    #    包会混着上一次的陈旧文件（改过名/删过的模块还在里面），症状是
    #    "代码改了但包的行为没变"，极难查。这里删不掉就直接报错停下。
    if os.path.exists(OUT_DIR):
        log("清掉上一次的产物：%s" % OUT_DIR)
        shutil.rmtree(OUT_DIR, ignore_errors=True)
        if os.path.exists(OUT_DIR):
            die("删不掉 %s —— 先手动删掉它再重试"
                "（可能是被资源管理器/杀软占用）。" % OUT_DIR)

    cmd = [py, "-m", "PyInstaller"] + opts + [os.path.join(ROOT, "final_player.py")]
    log("开始打包（%s 模式）……" % ("console" if console else "windowed"))
    t0 = time.time()
    rc = subprocess.call(cmd, cwd=ROOT)
    if rc != 0:
        die("PyInstaller 退出码 %d，往上翻日志看第一个 ERROR。" % rc)
    log("打包完成，用时 %.1f 秒" % (time.time() - t0))


README = """\
============================================================
 3D 视频播放器 —— 使用说明
============================================================

怎么用
------
1. 把整个文件夹解压到任意位置（比如 D:\\软件\\3D播放器）。
   **不要只把 final_player.exe 拖出来单独用** —— 它旁边的 _internal
   文件夹里装着播放器需要的一切（ffmpeg、深度模型、运行库）。
2. 双击 final_player.exe 就能用。

第一次打开可能出现的
--------------------
* 如果 Windows 弹出"已保护你的电脑"（蓝色窗口）：
  点【更多信息】→【仍要运行】。这是 Windows 对"没有花钱买数字
  签名的新程序"的统一提示，和有没有病毒无关。
* 如果杀毒软件报警：是 PyInstaller 打包的程序常见的误报，
  加个信任即可。

【转换为3d】要多久
------------------
用的是 CPU 推理，按视频长短大约每分钟片子要一两分钟。
转的时候界面不会卡，可以慢慢等；已经转换过一次的视频
（左右分屏那种）程序会拦住并提醒你不要再转，点【否】就行。

需要联网吗
----------
* 播放、转换：**完全离线**可用（深度模型已经随包带上了）。
* 只有在用【在线匹配字幕】时才需要联网，且需要你自己去
  OpenSubtitles 申请一个 API key（右键【在线匹配】按钮填进去）。

快捷键
------
空格 = 播放/暂停      ← → = 快退/快进      ↑ ↓ = 音量
F = 全屏              B  = 字幕栏开关      截图 = 底栏按钮
把视频文件直接拖进窗口也可以打开。

出问题了怎么办（请把这个也转给对方）
------------------------------------
双击运行时，这个程序**不显示任何提示**，这是正常的（它没有控制台窗口）。
遇到"不对劲"，请跑一次自检，把结果发回给我：

  1. 在放 final_player.exe 的那个文件夹里，按住 Shift 点右键
  2. 选【在终端中打开】或【在此处打开 PowerShell 窗口】
  3. 输入（前面是"点加斜杠"，别漏了）：
         .\\final_player.exe --selftest
  4. 等十几秒，窗口里会打印一份报告，它检查最要紧的两件事：
         · ffmpeg 在不在 —— 不在的话转换出来的 3D 视频**会没有声音**，而且不报错
         · 深度模型能不能离线加载 —— 不行的话第一次转换会去联网
  5. 把窗口里那段文字复制发给我。它同时也追加保存在
         %APPDATA%\\final_player\\player.log
     窗口里的中文要是显示成乱码，就直接把上面这个 player.log 发我（它是 UTF-8）。

如果**双击时一闪就没了**，同样是把 %APPDATA%\\final_player\\player.log 发我 ——
里面的日志会说明它停在哪一步。

你的设置存在哪
--------------
%APPDATA%\\final_player\\  （播放列表、上次看到哪儿、字幕 API key、日志）
删掉这个文件夹就等于恢复出厂设置。
"""


def post_build(hf_src):
    exe = os.path.join(OUT_DIR, APP_NAME + ".exe")
    if not os.path.isfile(exe):
        die("找不到产物：%s" % exe)

    with open(os.path.join(OUT_DIR, "使用说明.txt"), "w", encoding="utf-8") as fh:
        fh.write(README)
    if not hf_src:
        log("⚠️  包内没有深度模型：对方第一次点【转换为3d】必须联网。")

    log("")
    log("产物：%s" % OUT_DIR)
    log("  final_player.exe      %s" % human(os.path.getsize(exe)))
    total = dir_size(OUT_DIR)
    log("  整个文件夹            %s" % human(total))
    log("")
    log("发出去的方式：把 dist/final_player 整个文件夹压成 zip。")
    return OUT_DIR, total


def make_zip(out_dir):
    zip_base = os.path.join(DIST_DIR, APP_NAME + "_win64")
    log("正在压 zip（约 1GB，要一会儿）……")
    path = shutil.make_archive(zip_base, "zip", DIST_DIR, APP_NAME)
    log("zip 完成：%s（%s）" % (path, human(os.path.getsize(path))))
    return path


def main():
    argv = sys.argv[1:]
    console = "--console" in argv
    _py, hf_src = preflight()
    hidden = resolve_transformers_modules()
    run_build(console, hf_src, hidden)
    out_dir, _total = post_build(hf_src)
    if "--no-zip" not in argv:
        make_zip(out_dir)
    log("全部完成。")


if __name__ == "__main__":
    main()
