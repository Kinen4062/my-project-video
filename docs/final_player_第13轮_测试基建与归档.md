# final_player 第 13 轮 · 测试基建（语法检查不落盘 + 输出统一 UTF-8）+ 归档早期脚本

> 日期：2026-10-14
> 起点提交：`ab60554`（第 12 轮②，拆开 `__init__`）
> 本轮提交：`b61b2c7`（归档早期脚本）→ `05ed6c1`（测试基建）
> 验证：`run_tests.py --full` → **11 组全 OK、202 项断言、0 FAIL、退出码 0、333.2s**
> （干净环境 + 输出重定向）

> 这份记录是事后补写的。起因是用户说"过程记录每次都记得写，方便我之后理解学习
> 你完成的代码" —— 本轮当时按"不主动新建文档"的默认没写，现在补上。

---

## 一、这一轮修的两个问题，都属于同一类

它们有共同的形状：**代码本身完全正确，但换一个运行环境就报"失败"。**

| 问题 | 表现 | 真实原因 |
|---|---|---|
| 语法检查落盘失败 | 报"语法检查失败"、退出码 1 | 语法没错，是 `__pycache__` 目录不可写 |
| 输出编码崩溃 | 报套件失败、退出码 1 | 一个用例都没跑，是 `UnicodeEncodeError` 把进程打死了 |

**这类问题最阴的地方在于"它骗你"。** 退出码 1 看起来就是用例失败，你会去翻用例
逻辑；而真正的原因在完全无关的地方（文件权限、终端编码）。两处都复现过、定位到
根因之后修的。

---

## 二、问题 1：`py_compile` 会写盘，目录不可写时误报失败

### 原来的写法

```python
plan = [
    ("语法检查", ["-m", "py_compile", os.path.join(HERE, "final_player.py")]),
    ...
]
```

### 复现

`python -m py_compile` 会写 `__pycache__/*.pyc`。把字节码缓存重定向到一个
不可写的目录：

```bash
PYTHONPYCACHEPREFIX="C:/Windows/System32/zzz_nope" python -m py_compile final_player.py
# → [WinError 5] 拒绝访问。: '...'
# → EXIT=1
```

**语法完全正确，却报"语法检查失败"。** 触发条件在真实场景里很常见：
只读检出（CI 挂载、容器里的只读层）、被杀毒软件锁住的目录。

### 改法：内存里编译（`run_tests.py:41`）

```python
SYNTAX_TARGETS = ("final_player.py", "media_tools.py", "depth3d.py", "viewfit.py",
                  "filedrop.py", "playlist_store.py")     # 第 14 轮又加了两个
SYNTAX_CHECK_CODE = (
    "import sys\n"
    "for p in sys.argv[1:]:\n"
    "    compile(open(p, 'rb').read(), p, 'exec')\n"
    "    print('syntax OK: %s' % p)\n"
)
```

三个要点：

1. **`compile()` 只读源码、一个字节都不写盘** —— `__pycache__` 不可写这件事
   从此与语法检查无关；
2. **源码按 `'rb'` 读**（bytes 而不是 str）—— 这样 PEP 263 的编码声明
   （`# -*- coding: utf-8 -*-`）仍然生效，含非 ASCII 的源文件不会被误判；
3. **检查力一点没减**：真有语法错、缩进错，`compile()` 照样抛 `SyntaxError`
   + 非 0 退出码。

顺带把检查范围从 1 个文件扩到当时全部 4 个源文件（第 14 轮又扩到 6 个）。

### 验证（正例 + 负例都跑了）

| 场景 | 期望 | 实测 |
|---|---|---|
| 真的有缩进错的文件 | 非 0 | `EXIT=1` ✅ |
| 括号没闭合的文件 | 非 0 | `EXIT=1` ✅ |
| 正常源码 + `PYTHONPYCACHEPREFIX` 指向不可写目录 | 0 | `EXIT=0` ✅ |
| 跑完后 `__pycache__/*.pyc` 的 mtime | 不变 | 前后完全一致 ✅ |

---

## 三、问题 2：重定向 / CI 下输出按 gbk 编码，把套件打死

### 复现

```bash
env -u PYTHONIOENCODING -u PYTHONUTF8 python tests/test_shortcuts.py > log 2>&1
# → UnicodeEncodeError: 'gbk' codec can't encode character '\u24ea'
# → EXIT=1
```

`\u24ea` 就是那个 `⓪`（"⓪ 绑定注册方式"那一节）。

### 为什么会这样

Windows 上 Python 启动时会看 **stdout 是不是终端**：

- 是终端 → 用 `WindowsConsoleIO`，能输出 UTF-8；
- **是文件或管道（也就是重定向、CI 日志采集）→ 退回系统 ANSI 代码页**，
  简体中文机器上就是 `gbk`。

测试输出里混着 `✅` `⓪` `⇒` 这类装饰字符，`gbk` 编不出来 → `UnicodeEncodeError`
在**打印那一行**就抛出，把整个套件进程打死。**退出码 1，看起来像用例失败，
其实一个字都没测。**

这一条特别值得记住：**在终端里手动跑是绿的，一串管道到文件就红** ——
而 CI 永远是后者的形态。

### 改法：双保险

**① 子进程统一带环境变量**（`run_tests.py`）：

```python
CHILD_ENV = dict(os.environ)
CHILD_ENV["PYTHONUTF8"] = "1"
CHILD_ENV["PYTHONIOENCODING"] = "utf-8"
```

**② 自己也在导入处把流切成 UTF-8** —— `run_tests.py` 与 `tests/_harness.py`
各有一份同样的动作，**这样单跑某一个套件（不经过 `run_tests.py`）也不会踩坑**：

```python
def _force_utf8_streams():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

_force_utf8_streams()          # 模块导入时就执行
```

`errors="replace"` 是**最后一道保险**：万一真撞上编不出的字符，宁可打成 `?`
也不要让整轮回归陪葬。注意它是 `reconfigure` 而不是包一层 wrapper ——
前者对 `print`、`traceback`、子进程继承都生效，后者只挡自己写的输出。

**③ 顺带把仅有的三个 gbk 编不出的字符换成 ASCII**（等于再加一道保险）：

| 文件 | 原字符 | 换成 |
|---|---|---|
| `run_tests.py` | `全部通过 ✅` | `全部通过 [OK]` |
| `test_shortcuts.py` | `⓪ 绑定注册方式` | `0 绑定注册方式` |
| `tests/_harness.py` | `只写视频流 ⇒ 没有音轨` | `只写视频流 -> 没有音轨` |

换完扫描确认：**`run_tests.py` + `tests/` 100% gbk 可编码**。

> 注：`final_player.py` 里的 `⚠️ ⇒ − 🔊 🔇` 只出现在**注释和 Tk 按钮文本**里，
> 不进 stdout，所以没有动它。

### 验证

```bash
env -u PYTHONIOENCODING -u PYTHONUTF8  python run_tests.py --full  > log 2>&1
```

- 11 组全 OK、202 项断言、0 FAIL、**退出码 0**、333.2s；
- 零 `Traceback`、零 `UnicodeEncodeError`；
- 日志**只能按 UTF-8 解码**（gbk 解不了）—— 这正是修复生效的证明：
  以前它会被写成 gbk 字节，现在是纯 UTF-8。

---

## 四、问题 3：根目录堆着 6 个早期试验脚本

`gui_player.py` / `my_player.py` / `main.py` / `depth_test.py` / `dibr_test.py` /
`make_3d_video.py` —— 都是 `final_player.py` 之前的探索产物。

**动手前的核查**（这一步不能省）：

1. **mtime 全部早于主程序** —— 确认是历史产物，不是"其实还在用"；
2. **全仓没有任何 import 引用** —— 包括 `tests/` 与 `docs/`；
3. **git 有完整历史** —— 移走不等于丢失，随时能 `git log --follow` 找回来。

**手法：`git mv`（不是普通移动）**，这样 git 记作 100% 重命名、历史跟得上：

```
depth_test.py    => archive/depth_test.py      | 0
dibr_test.py     => archive/dibr_test.py       | 0
gui_player.py    => archive/gui_player.py      | 0
main.py          => archive/main.py            | 0
make_3d_video.py => archive/make_3d_video.py   | 0
my_player.py     => archive/my_player.py       | 0
6 files changed, 0 insertions(+), 0 deletions(-)
```

`0 insertions / 0 deletions` 就是"纯重命名、内容一个字没改"的机器级证明。

移动后仓库根目录只剩**成品 + 模块**共 5 个 `.py`。

### 一个如实的副作用

`.idea/workspace.xml` 里 **5 个 run configuration + 1 个 `main.py` 断点**
仍指向旧路径（该文件不进库），PyCharm 会提示找不到脚本 —— 需要在 IDE 里重新指定。

---

## 五、顺手做的事：`MEMORY.md` 重新整理

当时的项目记忆文件已经涨到约 250 行，注入时被截断（等于后半段没生效）。
压到 **约 3850 字符 / <100 行**：

- 删掉与 `docs/` 分轮记录重复的过程叙述（那些细节放在本目录的轮次文档里更合适）；
- 合并 Tk 焦点相关的两节；
- **10 条 ffpyplayer 硬约束与第 11 轮两条不变量的语义一字未改** —— 那些是
  "不写下来就会再踩一次"的内容，压缩时优先保留。

原则：**记忆文件只留"不写就会踩坑"的结论，过程留在 `docs/`。**

---

## 六、规模与验证

```
run_tests.py --full（干净环境 env -u PYTHONIOENCODING -u PYTHONUTF8 + 输出重定向）
→ 11 组全 OK、202 项断言、0 FAIL、退出码 0、333.2s
```

| | 数值 |
|---|---|
| 改动文件 | `run_tests.py`（+53/-5）、`tests/_harness.py`（+27）、`tests/test_shortcuts.py`（1 字符） |
| 重命名 | 6 个脚本 → `archive/`（0 插入 / 0 删除） |
| 语法检查覆盖 | 1 个文件 → 4 个（第 14 轮再扩到 6 个） |
| 本轮提交 | `b61b2c7`、`05ed6c1`（均本地，未 push） |

---

## 附：这一轮最值得带走的两条经验

1. **退出码 1 不等于用例失败。** 先看输出里有没有 `Traceback` —— 有 traceback
   就说明是**基建**挂了（编码、导入、依赖），不是断言挂了。这两个的排查方向完全不同。
2. **"只有某种运行方式下才红"要先怀疑环境。** 本地终端绿、重定向红 → 编码问题；
   有权限的目录绿、只读目录红 → 落盘问题。**修基建，别改用例去迁就环境。**
