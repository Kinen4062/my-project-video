# -*- coding: utf-8 -*-
"""一键跑完整回归（第 11 轮固化）。

    python run_tests.py            # 默认：3 个压力种子 × 60 次操作
    python run_tests.py --full     # 5 个种子 × 80 次操作（发布前跑）
    python run_tests.py --quick    # 1 个种子 × 40 次操作（改一行看一眼）

每一组都在**独立子进程**里跑，理由见 tests/_harness.py 顶部说明：
真段错误时能定位到具体套件，而不是整轮回归一起陪葬。

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
        ("语法检查", ["-m", "py_compile", os.path.join(HERE, "final_player.py")]),
        ("功能用例", [os.path.join(TESTS, "test_playback.py")]),
        ("定时器与挂起请求", [os.path.join(TESTS, "test_timers.py")]),
        ("快捷键与逐帧步进", [os.path.join(TESTS, "test_shortcuts.py")]),
        ("转换补音轨", [os.path.join(TESTS, "test_audio_mux.py")]),
        ("稳态速度", [os.path.join(TESTS, "test_speed.py")]),
    ]
    for seed in range(1, seeds + 1):
        plan.append(("乱序压力 seed=%d" % seed,
                     [os.path.join(TESTS, "test_chaos.py"), str(seed), str(ops)]))
    return plan


def main():
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
        proc = subprocess.run([python] + args, cwd=HERE)
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
        print("全部通过 ✅  总耗时 %.1fs" % total)

    # 清理运行期产物（测试素材、临时视频都在这里）
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
