# -*- coding: utf-8 -*-
"""打包成 .exe 套件（第 24 轮）。

打包有一类特有的失败：**包能启动、能播放、能截屏，但某个功能一跑到就报错**，
而且报错点在别人的机器上、没有控制台、堆栈也看不见。这个套件就是把这类问题
挪到本机、挪到提交之前。

四节的分工（前两节"真函数 × 假环境"，第四节"假函数 × 真环境"，合起来覆盖整条链）：

  ① 包内目录布局 × find_ffmpeg()  —— 按 build_exe 的约定搭出一个"包"，
     再**真调** find_ffmpeg()，看它能不能原地命中。这条最值钱：ffmpeg 找不到
     不会报任何错，只会**静默**产出没有声音的 3D 视频（见 media_tools 注释）。
  ② transformers 的动态导入名单  —— AutoModel / AutoImageProcessor 走的是
     importlib 动态导入，PyInstaller 的静态分析看不见。名单写错的表现同样是
     "只有点了【转换为3d】才炸"。这里把 build_exe 的兜底名单和**现场解析**
     的结果对起来算了又算。
  ③ 冻结垫片（sys.frozen）        —— --windowed 下 sys.stdout 是 None，
     一句 print() 就是 AttributeError，而且发生在启动阶段（表现成"双击一下
     闪一下就没了"）。用子进程伪造冻结环境，验证垫片真的把这事办了。
  ④ 真建一个迷你包                —— 唯一走**真实冻结路径**的一节：现场跑一次
     PyInstaller，把 sys.prefix / _MEIPASS / --add-data 的落点验清楚。
     第 24 轮就是靠它确认 `sys.prefix == _MEIPASS == <包>/_internal` 的。

⚠️ 第四节为什么要另写一个 `os.path.isfile` 而不是调 find_ffmpeg()：
   把 final_player 塞进这个迷你包会把 torch/cv2/tkinter 全拖进来，那就是正式
   打包本身（几分钟 + 1GB），不适合放进套件。所以拆成两半：
     ① = 真 find_ffmpeg + 假包（照包内布局摆的文件）
     ④ = 真包 + 一行等价的路径判断
   两者覆盖的一起，正好是"包内 ffmpeg 能不能被找到"这件事。
"""

import os
import subprocess
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import PROJ, Summary, work_dir  # noqa: E402

sys.path.insert(0, PROJ)
import build_exe  # noqa: E402
import final_player as fp  # noqa: E402

RUN_TAG = "%d_%s" % (os.getpid(), uuid.uuid4().hex[:6])
EXE_NAME = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
SEP = os.pathsep            # --add-data 的 src/dest 分隔符（Windows 是分号）
PACK_DIR = os.path.join(work_dir("packaging"), RUN_TAG)


# ============================================================
# ① 包内目录布局
# ============================================================
def section_layout(s):
    s.section("① 包内目录布局 × find_ffmpeg()（真函数）")

    s.check(build_exe.FFMPEG_DEST == os.path.join("share", "ffpyplayer", "ffmpeg", "bin"),
            "FFMPEG_DEST 就是 find_ffmpeg() 认的那段相对路径")

    # 照 build_exe 的 dest 摆出一个"包内"ffmpeg
    bundle = os.path.join(PACK_DIR, "bundle")
    bin_dir = os.path.join(bundle, *[p for p in build_exe.FFMPEG_DEST.split(os.sep) if p])
    os.makedirs(bin_dir, exist_ok=True)
    open(os.path.join(bin_dir, EXE_NAME), "wb").close()

    old_prefix = sys.prefix
    old_flag, old_path = fp._FFMPEG_SEARCHED, fp._FFMPEG_PATH
    old_which = fp.shutil.which
    old_env = os.environ.pop("FFMPEG_BINARY", None)
    try:
        # 冻结态实测：sys.prefix == sys._MEIPASS == <包>/_internal
        sys.prefix = bundle
        fp._FFMPEG_SEARCHED, fp._FFMPEG_PATH = False, None
        fp.shutil.which = lambda *a, **k: None      # 把 PATH 上的 ffmpeg 挡开
        got = fp.find_ffmpeg()
    finally:
        sys.prefix = old_prefix
        fp._FFMPEG_SEARCHED, fp._FFMPEG_PATH = old_flag, old_path
        fp.shutil.which = old_which
        if old_env is not None:
            os.environ["FFMPEG_BINARY"] = old_env

    want = os.path.join(bin_dir, EXE_NAME)
    s.check(got == want,
            "按包内布局摆好的 ffmpeg，find_ffmpeg() 原地命中"
            "（命中不了不会报错，只会静默产出无声 3D 视频）")

    # 随包模型缓存的目录名必须跟着 MODEL_ID 走
    with open(os.path.join(PROJ, "final_player.py"), encoding="utf-8") as fh:
        src = fh.read()
    s.check(build_exe.MODEL_ID in src,
            "build_exe.MODEL_ID 就是 final_player 真正请求的那个模型 id"
            "（换模型时两边必须一起动）")
    s.check(os.path.dirname(build_exe.HF_DEST) == "hf_cache",
            "HF_DEST 的根目录叫 hf_cache —— 与 final_player 冻结垫片里"
            "找的 <_MEIPASS>/hf_cache 对得上")
    s.check(build_exe.HF_DEST == os.path.join(
                "hf_cache", "models--" + build_exe.MODEL_ID.replace("/", "--")),
            "缓存子目录是 HuggingFace 的 models--org--name 形状"
            "（写成别的形状，local_files_only=True 就找不到）")


# ============================================================
# ② transformers 的动态导入名单
# ============================================================
def section_transformers(s):
    s.section("② transformers 动态导入名单（与现场解析对齐）")

    try:
        from transformers import (AutoConfig, AutoImageProcessor,
                                  AutoModelForDepthEstimation)
        cfg = AutoConfig.from_pretrained(build_exe.MODEL_ID, local_files_only=True)
        proc = AutoImageProcessor.from_pretrained(build_exe.MODEL_ID, local_files_only=True)
        model = AutoModelForDepthEstimation.from_pretrained(build_exe.MODEL_ID,
                                                            local_files_only=True)
    except Exception as exc:
        s.note("本机解析不了这个模型（%s: %s），跳过本节 —— "
               "打包脚本此时会退回兜底名单" % (type(exc).__name__, exc))
        return

    mods = {type(cfg).__module__, type(proc).__module__}
    backbone = getattr(cfg, "backbone_config", None)
    if backbone is not None:
        mods.add(type(backbone).__module__)
    for _name, sub in model.named_modules():
        mods.add(type(sub).__module__)
    live = sorted({m.rsplit(".", 1)[0] for m in mods
                   if m.startswith("transformers.models.")})

    s.check(bool(live), "现场解析出了模型真正落在哪些 transformers 子包里")
    s.note("现场解析：%s" % "、".join(live))

    for mod in live:
        s.check(mod in build_exe.TRANSFORMERS_FALLBACK,
                "%s 在打包名单里（漏了 = 点【转换为3d】才报 ImportError）" % mod)

    fallback_models = sorted(set(build_exe.TRANSFORMERS_FALLBACK)
                             - {"transformers.models.auto"})
    s.check(fallback_models == live,
            "兜底名单和现场解析结果一字不差（多收不致命，少收才是事故）")

    # 钉住那个反直觉的事实，免得后人"顺手改回去"
    s.check("transformers.models.depth_anything" in live,
            "模型 id 叫 Depth-Anything-V2，实现却在 V1 的 depth_anything 包里"
            "（它的 config.json 里 model_type = \"depth_anything\"）")
    s.check("transformers.models.depth_anything_v2" not in live,
            "按名字猜出来的 depth_anything_v2 是个不存在的包 —— 手写名单必错")


# ============================================================
# ③ 冻结垫片
# ============================================================
SHIM_CHILD = r'''
import os, sys
real_out = sys.stdout
sys._MEIPASS = os.environ["SHIM_BUNDLE"]
if os.environ.get("SHIM_FROZEN") == "1":
    sys.frozen = True
    sys.stdout = None
    sys.stderr = None
sys.path.insert(0, os.environ["SHIM_PROJ"])
import final_player                                    # noqa: F401
out_ok = sys.stdout is not None
err_ok = sys.stderr is not None
log_path = os.path.join(os.environ.get("APPDATA", ""), "final_player", "player.log")
if getattr(sys, "frozen", False) and sys.stdout is not None:
    sys.stdout.write("SHIM-PROBE-LINE\n")
    try:
        sys.stdout.flush()
    except Exception:
        pass
sys.stdout = real_out
try:
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        logged = fh.read()
except Exception:
    logged = ""
print("OUT_OK=%s" % out_ok)
print("ERR_OK=%s" % err_ok)
print("LOG_HAS_MARKER=%s" % ("SHIM-PROBE-LINE" in logged))
print("HF=%s" % os.environ.get("HF_HUB_CACHE", "<unset>"))
'''

# 子进程里的 %APPDATA% 一律改到临时目录 —— 否则套件会往用户真实的
# %APPDATA%/final_player/player.log 里写东西（虽然无害，但测试不该碰用户的家）。


def _run_child(script, extra_env):
    env = dict(os.environ)
    env.update(extra_env)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([sys.executable, script], capture_output=True,
                          text=True, env=env, cwd=PACK_DIR, timeout=600)
    out = {}
    for line in (proc.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    if proc.returncode != 0 or "HF" not in out:
        out["_err"] = (proc.stderr or "")[-400:]
    return proc.returncode, out


def section_shim(s):
    s.section("③ 冻结垫片（--windowed 下 sys.stdout 是 None）")

    os.makedirs(PACK_DIR, exist_ok=True)
    script = os.path.join(PACK_DIR, "shim_child.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(SHIM_CHILD)

    with_cache = os.path.join(PACK_DIR, "bundle_with_cache")
    os.makedirs(os.path.join(with_cache, "hf_cache", "models--probe"), exist_ok=True)
    without = os.path.join(PACK_DIR, "bundle_without_cache")
    os.makedirs(without, exist_ok=True)

    def appdata(tag):
        d = os.path.join(PACK_DIR, "appdata_" + tag)
        os.makedirs(d, exist_ok=True)
        return d

    # (a) 冻结 + 包内有模型缓存
    rc, out = _run_child(script, {"SHIM_PROJ": PROJ, "SHIM_BUNDLE": with_cache,
                                  "APPDATA": appdata("a"), "SHIM_FROZEN": "1"})
    s.check(rc == 0 and out.get("OUT_OK") == "True",
            "冻结态下 import 完 final_player，sys.stdout 不再是 None"
            "（否则启动阶段任何一句 print 都会崩）")
    s.check(out.get("ERR_OK") == "True", "sys.stderr 同样被兜住")
    s.check(out.get("LOG_HAS_MARKER") == "True",
            "兜底目标是 %APPDATA%/final_player/player.log，且立刻可见"
            "（发给别人的程序，出事得有日志可看）")
    s.check(out.get("HF") == os.path.join(with_cache, "hf_cache"),
            "包内有模型缓存时，HF_HUB_CACHE 被指到 <_MEIPASS>/hf_cache"
            "（否则对方机器上第一次转换必须联网）")
    if rc != 0:
        s.note("子进程尾部输出：%s" % out.get("_err", ""))

    # (b) 冻结 + 包内没带模型缓存 —— 保持"联网下载"的老行为
    rc, out = _run_child(script, {"SHIM_PROJ": PROJ, "SHIM_BUNDLE": without,
                                  "APPDATA": appdata("b"), "SHIM_FROZEN": "1"})
    s.check(rc == 0 and out.get("HF") == "<unset>",
            "包内没有 hf_cache 时不去动 HF_HUB_CACHE（退回联网，不制造假路径）")

    # (c) 非冻结（平时跑源码 / 套件）—— 垫片必须一字节都不执行
    rc, out = _run_child(script, {"SHIM_PROJ": PROJ, "SHIM_BUNDLE": without,
                                  "APPDATA": appdata("c"), "HF_HUB_CACHE": "<哨兵>",
                                  "SHIM_FROZEN": "0"})
    s.check(rc == 0 and out.get("HF") == "<哨兵>",
            "非冻结态下垫片完全不生效（跑源码/跑套件的行为与打包前一致）")


# ============================================================
# ④ 真建一个迷你包
# ============================================================
MINI_PROBE = r'''
import os, sys
base = getattr(sys, "_MEIPASS", "")
rel = os.path.join("share", "ffpyplayer", "ffmpeg", "bin", "ffmpeg.exe")
print("FROZEN=%s" % bool(getattr(sys, "frozen", False)))
print("PREFIX_EQ_MEIPASS=%s"
      % (os.path.normcase(os.path.abspath(sys.prefix))
         == os.path.normcase(os.path.abspath(base))))
# ⚠️ 括号不能省：`%` 的优先级**高于** `==`，少了括号那句算出来是
#    ("PREFIX_IS_INTERNAL=_internal" == "_internal") → 永远 False。
#    第 24 轮第一次跑套件就是在这里假红了一条。
print("PREFIX_IS_INTERNAL=%s"
      % (os.path.basename(os.path.abspath(sys.prefix)) == "_internal"))
print("ADD_DATA_HIT=%s" % os.path.isfile(os.path.join(base, rel)))
'''


def section_real_bundle(s):
    s.section("④ 真建一个迷你包（唯一走真实冻结路径的一节）")

    try:
        import PyInstaller  # noqa: F401
    except Exception:
        s.note("本环境没装 PyInstaller，跳过第 ④ 节")
        return

    root = os.path.join(PACK_DIR, "mini")
    payload = os.path.join(root, "payload")
    os.makedirs(payload, exist_ok=True)
    # 只验落点，不搬那 140MB 真 ffmpeg —— 真件由 ① 与正式打包负责
    open(os.path.join(payload, "ffmpeg.exe"), "wb").close()

    script = os.path.join(root, "mini_probe.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(MINI_PROBE)

    dist = os.path.join(root, "dist")
    work = os.path.join(root, "work")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--onedir", "--console", "--name", "mini",
           "--distpath", dist, "--workpath", work, "--specpath", work,
           "--add-data", "%s%s%s" % (payload, SEP, build_exe.FFMPEG_DEST),
           script]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                          cwd=root)
    if proc.returncode != 0:
        s.check(False, "PyInstaller 建迷你包成功（退出码 %d）" % proc.returncode)
        s.note("尾部输出：%s" % (proc.stdout or proc.stderr or "")[-500:])
        return

    exe = os.path.join(dist, "mini", "mini.exe" if os.name == "nt" else "mini")
    s.check(os.path.isfile(exe), "迷你包产出可执行文件")
    run = subprocess.run([exe], capture_output=True, text=True, timeout=300)
    out = {}
    for line in (run.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()

    s.check(out.get("FROZEN") == "True", "包里的程序确实处于冻结态")
    s.check(out.get("PREFIX_EQ_MEIPASS") == "True",
            "冻结态下 sys.prefix == sys._MEIPASS —— find_ffmpeg() 靠这一条"
            "才找得到包内的 ffmpeg")
    s.check(out.get("PREFIX_IS_INTERNAL") == "True",
            "onedir 包的解包目录就叫 _internal（--add-data 的落点基准）")
    s.check(out.get("ADD_DATA_HIT") == "True",
            "--add-data 的 dest 写 share/ffpyplayer/ffmpeg/bin 时，"
            "文件确实落在 find_ffmpeg() 会去翻的地方")


# ============================================================
# ⑤ 自检入口
# ============================================================
def section_selftest(s):
    s.section("⑤ 自检入口 final_player.py --selftest")

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, os.path.join(PROJ, "final_player.py"), "--selftest"],
        capture_output=True, text=True, env=env, cwd=PROJ, timeout=900)
    text = (proc.stdout or "") + (proc.stderr or "")

    s.check(proc.returncode == 0,
            "源码态下 --selftest 正常退出（退出码 %d）" % proc.returncode)
    s.check("自检报告" in text,
            "--selftest 打出了报告（证明 __main__ 里那个分支真的接上了）")
    hits = [ln for ln in text.splitlines()
            if "ffmpeg" in ln and ":" in ln and "**" not in ln]
    s.check(bool(hits), "报告里 ffmpeg 那一行给出的是找到的路径，而不是「找不到」")
    s.check("深度模型" in text, "报告覆盖了深度模型那一项"
                               "（打包后另一半最容易静默失效的东西）")
    if proc.returncode != 0:
        s.note("尾部输出：%s" % text[-400:])


def body(s):
    section_layout(s)
    section_transformers(s)
    section_shim(s)
    section_real_bundle(s)
    section_selftest(s)


def main():
    summary = Summary("打包成 exe")
    try:
        body(summary)
    finally:
        pass
    return summary.finish()


if __name__ == "__main__":
    sys.exit(main())
