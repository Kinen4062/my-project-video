# -*- coding: utf-8 -*-
"""一键跑完整回归（第 11 轮固化）。

    python run_tests.py            # 默认：3 个压力种子 × 60 次操作
    python run_tests.py --full     # 5 个种子 × 80 次操作（发布前跑）
    python run_tests.py --quick    # 1 个种子 × 40 次操作（改一行看一眼）

每一组都在**独立子进程**里跑，理由见 tests/_harness.py 顶部说明：
真段错误时能定位到具体套件，而不是整轮回归一起陪葬。

### 两条"只在重定向 / CI 下才暴露"的坑（第 13 轮修）

1. **语法检查不落盘**。原来这一步是 `python -m py_compile final_player.py`，它会写
   `__pycache__/*.pyc` —— 目录一不可写（只读检出、CI 挂载、被杀软锁住），
   `py_compile` 就抛 `[WinError 5] 拒绝访问` 并返回码 1：**语法明明没问题却报"语法检查失败"**。
   现在改成在内存里 `compile()`，只读源码、一个字节都不写盘；真有语法错误照样
   `SyntaxError` + 非 0 退出码，检查力不减。
2. **子进程统一 UTF-8**。输出里混着 emoji / 圈码 / 箭头这类装饰字符，而 Windows 上 Python
   一旦发现 stdout 是**文件/管道**（也就是重定向、CI 日志）就用系统 ANSI 代码页（简中 = gbk）
   编码，遇到 gbk 编不出的字符直接 `UnicodeEncodeError` 把套件打死 —— 退出码 1，
   **看起来像"用例失败"，其实一个字都没测**。现在两条保险：
   ① 子进程统一带 `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8`；
   ② `run_tests.py` 与 `tests/_harness.py` 都把自己的 stdout/stderr `reconfigure` 成 UTF-8，
   单跑某个套件也不会踩坑。
   （顺带把测试输出里仅有的三个 gbk 编不出的字符换成了 ASCII，等于再加一道保险。）

退出码：0 = 全绿；非 0 = 有失败（或某个套件崩了，见下面单独列出）。
"""

import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.join(HERE, "tests")
TMP_ROOT = os.path.join(os.environ.get("TEMP", "/tmp"), "fp_tests")

# 语法检查只读源码、在内存里编译 —— 不写 __pycache__（见文件头第 1 条）
SYNTAX_TARGETS = ("final_player.py", "media_tools.py", "depth3d.py", "viewfit.py",
                  "filedrop.py", "playlist_store.py")
SYNTAX_CHECK_CODE = (
    "import sys\n"
    "for p in sys.argv[1:]:\n"
    "    compile(open(p, 'rb').read(), p, 'exec')\n"
    "    print('syntax OK: %s' % p)\n"
)

# 子进程统一 UTF-8（见文件头第 2 条）。这两条也正是本机 IDE 的既有约定，
# 显式写出来只是让 CI / 重定向场景也拿到同样的确定性。
CHILD_ENV = dict(os.environ)
CHILD_ENV["PYTHONUTF8"] = "1"
CHILD_ENV["PYTHONIOENCODING"] = "utf-8"


def force_utf8_streams():
    """把自己的 stdout/stderr 也切到 UTF-8。

    重定向时 Python 用系统 ANSI 代码页（简中 = gbk）编码输出，汇总里那几个装饰字符会
    `UnicodeEncodeError` 把汇总打印打断。`errors="replace"` 是最后一道保险：
    万一真撞上编不出的字符，宁可打成 `?` 也不要让整轮回归陪葬。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def venv_python():
    """优先用项目自己的 .venv（torch / ffpyplayer 都装在那里）。

    直接 `python run_tests.py` 而当前解释器不是 venv 时，会自动切到 venv，
    免得报一堆 ModuleNotFoundError。
    """
    exe = "python.exe" if os.name == "nt" else "python"
    cand = os.path.join(HERE, ".venv", "Scripts" if os.name == "nt" else "bin", exe)
    if os.path.isfile(cand) and os.path.abspath(sys.executable) != os.path.abspath(cand):
        return cand
    return sys.executable


def build_plan(full=False, quick=False):
    """返回 [(标签, [参数...]), ...]，每一项是一次独立的子进程调用。"""
    seeds, ops = (3, 60)
    if full:
        seeds, ops = (5, 80)
    if quick:
        seeds, ops = (1, 40)
    plan = [
        ("语法检查", ["-c", SYNTAX_CHECK_CODE] +
                     [os.path.join(HERE, name) for name in SYNTAX_TARGETS]),
        ("功能用例", [os.path.join(TESTS, "test_playback.py")]),
        ("定时器与挂起请求", [os.path.join(TESTS, "test_timers.py")]),
        ("快捷键与逐帧步进", [os.path.join(TESTS, "test_shortcuts.py")]),
        ("播放列表与拖放", [os.path.join(TESTS, "test_playlist.py")]),
        ("转换补音轨", [os.path.join(TESTS, "test_audio_mux.py")]),
        ("稳态速度", [os.path.join(TESTS, "test_speed.py")]),
    ]
    for seed in range(1, seeds + 1):
        plan.append(("乱序压力 seed=%d" % seed,
                     [os.path.join(TESTS, "test_chaos.py"), str(seed), str(ops)]))
    return plan


def main():
    force_utf8_streams()
    full = "--full" in sys.argv
    quick = "--quick" in sys.argv
    python = venv_python()
    plan = build_plan(full=full, quick=quick)

    if os.path.abspath(python) != os.path.abspath(sys.executable):
        print("使用解释器：%s\n" % python, flush=True)
    print("共 %d 组，逐组独立进程执行。\n" % len(plan), flush=True)

    results = []
    t_start = time.time()
    for label, args in plan:
        print("=" * 68, flush=True)
        print(">>> %s" % label, flush=True)
        print("=" * 68, flush=True)
        t0 = time.time()
        proc = subprocess.run([python] + args, cwd=HERE, env=CHILD_ENV)
        dt = time.time() - t0
        results.append((label, proc.returncode, dt))
        print(flush=True)

    print("=" * 68, flush=True)
    print("汇总", flush=True)
    print("=" * 68, flush=True)
    failed = []
    for label, code, dt in results:
        if code == 0:
            mark = "OK  "
        elif code < 0:
            # 负值 = 被信号杀掉；Tk/SDL 层段错误的典型表现
            mark = "崩溃"
            failed.append(label)
        else:
            mark = "失败"
            failed.append(label)
        print("  [%s] %-22s %6.1fs  退出码=%s" % (mark, label, dt, code), flush=True)

    total = time.time() - t_start
    print(flush=True)
    if failed:
        print("有 %d 组未通过：%s" % (len(failed), "、".join(failed)))
        print("（某一组『崩溃』通常意味着音频/SDL 层的段错误 —— 该组前面的输出"
              "就是最后执行到的用例。）")
    else:
        print("全部通过 [OK]  总耗时 %.1fs" % total)

    # 清理运行期产物（测试素材、临时视频都在这里）
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
