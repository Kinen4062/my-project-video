# 第 24 轮：把这套播放器打包成能直接发给别人的 .exe

- 日期：2026-10-16
- 需求原话：「将这个视频播放器的项目打包成一个.exe发给别人使用，要怎么做」
- 产物：`dist/final_player/`（一个文件夹，双击里面的 `final_player.exe` 即用）
- 新增：`build_exe.py`（打包脚本）、`tests/test_packaging.py`（打包套件）、
  `final_player.py` 的**冻结态启动垫片**与 `--selftest` 自检入口
- 改动到的旧文件：`run_tests.py`（注册新套件 + 语法检查纳入 `build_exe.py`）

---

## 一、需求拆解

「打包成 .exe」听起来是一件事，实际是三个，而且难度完全不在同一个量级：

| # | 问题 | 难度 | 这一轮花的时间 |
|---|------|------|----------------|
| 1 | 用工具把 Python 项目变成 exe | 低，有标准答案 | 一点点 |
| 2 | **哪些东西必须跟着走**，哪些绝对不能跟 | 中 | 一半 |
| 3 | **打包这件事本身改变了程序的哪些行为** | 高，且没有文档 | 另一半 |

第 3 问是这一轮真正的主题。它的恶劣之处在于：**打包不会让程序报错，
只会让程序在某些路径上悄悄变成另一个东西**。三个实例：

- `--windowed` 之后 `sys.stdout` 变成 `None`，而本文件里有大量 `print()`；
- 别人机器上没有 `~/.cache/huggingface`，模型加载的第一个分支直接失效；
- 冻结之后 `__file__` / `sys.prefix` 全部指向解包目录，`find_ffmpeg()` 的四条
  搜索路径里前两条的**含义变了**。

这三条里没有任何一条会在启动时报错。前两条的表现是"双击一下闪一下就没了"，
第三条的表现是"转换出来的 3D 视频没有声音，但界面一切正常"。

---

## 二、四个决定

### 2.1 onedir，不是 onefile

`--onefile` 会把整个包压进一个 exe，**每次双击**都要先把几百 MB 解包到 `%TEMP%`
再启动。这个程序自带 140 MB ffmpeg + 95 MB 模型 + 几百 MB PyTorch，`--onefile`
的启动等待会到十几秒到一分钟，退出还要清理，杀进程还会在 `%TEMP%` 留垃圾。

`--onedir` 是一个文件夹，双击即起。代价是"不能只发一个 exe 文件"——要发整个
文件夹（压成 zip）。这笔交易对这个体量是划算的。

### 2.2 CPU 版 PyTorch，不是 CUDA 版

| | 装完体积 | 谁能用上 |
|---|---|---|
| `torch 2.5.1+cu121`（开发环境现成的） | **4.3 GB**（其中 4.2 GB 是 cudnn/cublas/nvrtc 那一堆） | 只有装了 NVIDIA 驱动 + 有 N 卡的人 |
| `torch 2.5.1+cpu` | **1.2 GB**（其中还有 790 MB 是 `.lib` 链接库，PyInstaller 不收） | 所有人 |

本程序用的深度模型是 Depth-Anything-V2-**Small**，CPU 推理每帧约 0.2 秒。
转换一部几分钟的短片慢几分钟，但完全可用；而**播放功能一个字节都不受影响**
（播放路径根本不碰 torch）。

这一条是问过用户的：他确认接收方"没有 / 不确定"有没有 N 卡，于是选 CPU 版。
需要说明的是**这不改变一个事实**：CUDA 版也不是不能打，只是那 4.2 GB 大概率是
纯陪跑。真要给有 N 卡的人，把 `build/buildvenv` 换成 CUDA 版 torch 重打即可
（`build_exe.py` 的 `preflight()` 会主动警告）。

### 2.3 打包环境与开发环境隔离

打包用 `build/buildvenv`（CPU 版 torch），**开发用的 `.venv` 一个字节都不动**。

- 反过来做（把 `.venv` 里的 torch 换成 CPU 版）会破坏开发环境——那里的 CUDA
  是真的在用的；
- 打包环境是**一次性**的，`build/` 本来就被 `.gitignore` 忽略，删了重建就完事。

### 2.4 用脚本，不用手敲 PyInstaller 命令

`build_exe.py` 是**唯一**的打包入口。理由不是"方便"，是因为这个命令有 20 多个
参数，其中至少 6 个是"漏了它不会报错、只会让某个功能静默失效"。参数多到手敲
一定会漏，而漏了的表现又和"没漏"几乎一样。做成脚本之后，这些参数连同**为什么
是它**的理由一起进了版本库。

---

## 三、打包改变了什么行为（三个必须补的洞）

### 3.1 `--windowed` 下 `sys.stdout` 是 `None`

Windows 上 GUI 子系统的程序如果没有控制台，`sys.stdout` / `sys.stderr` 就是
`None`。往 `None` 上 `write` 得到的是：

```
AttributeError: 'NoneType' object has no attribute 'write'
```

而且它发生在**启动阶段**（本文件顶部就 `print` 的东西不少，transformers 内部
更多），表现是"双击一下闪一下就没了"，没有任何线索。

**补法**：`final_player.py` 顶部加一段只在 `sys.frozen` 为真时才执行的垫片，
把两路输出接到 `%APPDATA%/final_player/player.log`。

三个刻意的地方：

1. **只在 `sys.frozen` 为真时执行**。平时跑源码、跑回归套件时这段一个字节都
   不执行，所以"打包不改变行为"是**结构性成立**的，不是靠自觉。
2. **不是接到 `os.devnull`，而是接到日志文件**。程序是发给别人的，出了事总得
   有东西可看。位置跟着 `playlist_store` / `resume_store` 的老约定走。
3. **`buffering=1`（行缓冲）**。SDL 那边真段错误的时候，最后几行也必须已经
   落盘——否则这份日志等于没有。
4. 日志文件建不出来（只读配置目录、权限受限）就退回 `devnull`，绝不因此崩。

> 第 24 轮实测更正一条想当然：`--windowed` 的程序**从终端启动时
> `sys.stdout` 是有效的**（有控制台可以继承），只有双击时才为 `None`。
> 所以只做"接日志"这一条路的方案会漏掉"命令行里看不到输出"这一半。
> 这就是下面 3.4 那个自检要**同时**打屏幕和写日志的原因。

### 3.2 别人机器上没有 `~/.cache/huggingface`

第 23 轮刚把 `_load_depth_model()` 改成"先试 `local_files_only=True`、缓存不全
才联网"，那次修的是**开发机**上的一屏红字。打包之后问题变了：接收方的机器上
根本没有那个缓存目录，`local_files_only=True` 必然失败，于是必然联网——
而联网正是第 23 轮想避免的事（hf-mirror SSL 断流会重试 5 次刷屏）。

**补法**：把缓存目录随包带上（`--add-data`），并在垫片里设
`HF_HUB_CACHE = <_MEIPASS>/hf_cache`。

三个细节：

1. **目录名必须保持 HuggingFace 的 `models--<org>--<name>` 形状**，
   `--add-data` 的目的地写成 `hf_cache/models--depth-anything--Depth-Anything-V2-Small-hf`
   而不是随便一个名字——`from_pretrained` 是按这个命名规则去缓存里找的。
2. **目录不存在就**不设这个环境变量，保持"没带模型就联网下载"的老行为，
   不去制造一个假路径。
3. ⚠️ **还要带上 `<hub>/version.txt`**（1 个字节）。见坑 #3。

### 3.3 冻结之后，`find_ffmpeg()` 的搜索路径含义变了

`find_ffmpeg()` 的四类候选里，前两类是：

```python
proj_root = os.path.dirname(os.path.abspath(__file__))
for base in (sys.prefix, proj_root):
    for sub in (("share", "ffpyplayer", "ffmpeg", "bin"),
                (".venv", "share", "ffpyplayer", "ffmpeg", "bin")):
```

跑源码时 `sys.prefix` 是 `.venv` 目录，`proj_root` 是项目根——两条都能命中
`.venv/share/ffpyplayer/ffmpeg/bin/ffmpeg.exe`。

冻结之后这两个变量的取值**没有文档说明**。所以这一轮**没有猜**，而是造了一个
探针用真实的 PyInstaller 打出来（`build/probe_find_ffmpeg.py`，跑完即弃）：

```
frozen          = True
sys.prefix      = ...\dist\probe\_internal
sys.exec_prefix = ...\dist\probe\_internal
_MEIPASS        = ...\dist\probe\_internal
dirname(__file__) = ...\dist\probe\_internal
candidates:
  [HIT ] ...\_internal\share\ffpyplayer\ffmpeg\bin\ffmpeg.exe
  [miss] ...\_internal\.venv\share\ffpyplayer\ffmpeg\bin\ffmpeg.exe
  [HIT ] ...\_internal\share\ffpyplayer\ffmpeg\bin\ffmpeg.exe
  [miss] ...\_internal\.venv\share\ffpyplayer\ffmpeg\bin\ffmpeg.exe
```

结论：**`sys.prefix == sys.exec_prefix == sys._MEIPASS == dirname(__file__) ==
<包>/_internal`**。于是 `--add-data <ffmpeg bin>;share/ffpyplayer/ffmpeg/bin`
落到 `_internal/share/ffpyplayer/ffmpeg/bin/`，正好命中。

**这一条的好消息是：业务代码一行都不用改。** `find_ffmpeg()` 的四路查找本来
就是为"找不到就降级"设计的，打包只是让它第一条路真的命中了。

代价是 `--add-data` 那两个路径**必须是绝对路径**，见坑 #2。

### 3.4 加了 `--selftest`：让人能自己报故障

上面三个洞都属于"静默失效"。程序发给别人之后，对方能给你的只有一句"不能用了"，
而这三个洞恰恰不会产生任何提示。

所以加了 `final_player.exe --selftest`：它把两件最容易静默坏掉的事各验一遍
（ffmpeg 在不在、模型能不能离线加载），**同时**打在屏幕上和追加进 player.log。

这个自检不是"顺手加的功能"，它是**验证手段**：第 24 轮就是靠它证明"包里的
`find_ffmpeg()` 真的能找到包里的 ffmpeg"的（见第六节）。

---

## 四、六个坑

### 坑 1（本轮最值钱）：transformers 的模型是**动态导入**的，按名字猜必错

`AutoModelForDepthEstimation.from_pretrained()` 走的是 `importlib` 动态导入，
PyInstaller 的静态分析**看不见**。而 `_pyinstaller_hooks_contrib` 里的
`hook-transformers.py` 只做一件事：**拷贝包的元数据**（`copy_metadata`），
一个子模块都不收。

于是必须自己写隐藏导入名单。而这里有个陷阱：

> 模型 id 叫 `depth-anything/Depth-Anything-V2-Small-hf`，
> 但它的 `config.json` 里写的是
> ```
> "model_type": "depth_anything",              ← V1 的类，不是 depth_anything_v2
> "backbone_config": { "model_type": "dinov2" },
> "image_processor_type": "DPTImageProcessor"
> ```
> 而 `transformers 4.46.3` 里**根本没有** `transformers.models.depth_anything_v2`
> 这个包（实测 `ModuleNotFoundError`）。

照名字写 `"transformers.models.depth_anything_v2"` 会得到一个**恰好是错的**包：
能启动、能播放、能截屏、能加载字幕，直到用户点【转换为3d】的那一刻才
`ImportError`。

**补法**：不手写名单，改成**在打包时问 transformers 自己**——
`build_exe.py: resolve_transformers_modules()` 把程序启动时会走的那条路
（`AutoConfig` / `AutoImageProcessor` / `AutoModelForDepthEstimation` 各
`from_pretrained` 一次）原样走一遍，然后把**实际拿到的对象**所属的模块收出来：

```
模型真实用到的 transformers 子包：
  transformers.models.auto、transformers.models.depth_anything、
  transformers.models.dinov2、transformers.models.dpt
```

这样做还有两个附带好处：模型换代时名单自动跟上；解析失败（本机没缓存）时
退回一份**被测试钉住**的兜底名单，而不是悄悄少收几个模块。

### 坑 2：`--add-data` 的两个语义陷阱

**（a）相对源路径是相对 `--specpath` 解析的，不是相对 cwd。**

```
ERROR: Unable to find '<workpath>\.venv\share\ffpyplayer\ffmpeg\bin'
       when adding binary and data files.
```

——看着像"文件不存在"，其实是它去 `--specpath`（我们的 `build/pyi_work`）下面
找那个相对路径了。**所有源路径写成绝对路径**即可。

**（b）dest 一律当作目录，写"文件路径"会造出一个同名目录。**

想把本机的 `version.txt` 放进包内 `hf_cache/` 下，自然写法是：

```
--add-data C:\...\hub\version.txt;hf_cache/version.txt     # 错
```

实际得到的是 **`hf_cache/version.txt/version.txt`** —— 一个名叫 `version.txt` 的
**目录**。后果和"根本没带这个文件"一模一样（transformers 照样走迁移分支），还
额外多打一句 `There was a problem when trying to write in your cache folder...`
（因为 `version.txt` 是个目录，写不进去）。

正确写法是把 dest 写成**目录**：

```
--add-data C:\...\hub\version.txt;hf_cache                 # 对 → hf_cache/version.txt
```

PyInstaller 文档里对这一条只有一句 "DEST is a directory"，很容易看漏；而
`--add-data some_file;.` 这种最常见的写法（dest 是 `.`）又恰好是对的，
于是"经验"会把你带沟里。**判据不是读文档，是打完之后 `ls` 一眼。**

### 坑 3：随包模型缓存少了 `version.txt`，第一屏就是迁移告警

不带上它，第一次转换会打出：

```
The cache for model files in Transformers v4.22.0 has been updated.
Migrating your old cache. This is a one-time only operation.
```

`transformers` 用 `<hub>/version.txt` 判断"这个缓存是不是老结构"，缺了就走迁移
分支（全目录扫描 + 自己补写这个文件）。功能上能自愈，但用户看到的是一句很像
出错的告警。

实测确认：不带它，`hf_cache/version.txt` 会在第一次运行之后**被程序自己补出来**
（时间戳就是那次运行的时刻）。所以补法很简单：`--add-data` 把本机那份
`version.txt` 一起带上。

### 坑 4：ffmpeg.exe 的 DLL 必须和它同目录，不能靠包根那份

`hook-ffpyplayer.py` 会把 ffpyplayer 依赖的 DLL（`SDL2.dll`、`SDL2_mixer.dll`、
`av*.dll`）复制到**包根**（即 `_internal/`）——那是给 `ffpyplayer` 的 `.pyd`
用的。

但 `ffmpeg.exe` 是个只读 `av*.dll` 的壳，Windows 加载 DLL 时搜索的是
**可执行文件自己所在的目录**，不是进程目录。所以包根那份 DLL **救不了**
`_internal/share/ffpyplayer/ffmpeg/bin/ffmpeg.exe`。

**补法**：把整个 `bin/` 目录（11 个文件、136.7 MB）一起搬过去，让它和 DLL 同进同出。
代价是那 140 MB 的 `av*.dll` 在包里出现了两份（包根一份、bin 一份）。

**为什么接受这个浪费**：另外两条路都更脆——
① 只拷 `ffmpeg.exe` 到包根、靠 `FFMPEG_BINARY` 指过去：能省 140 MB，但依赖
"hook 会把 DLL 放在包根"这个第三方实现细节，它哪天变了，症状是**静默无声**；
② 不打包 ffmpeg：转换产物全都没声音。
在这个项目里，"静默少一段音频"比"包大 140 MB"难受得多。

### 坑 5：`--noconfirm` 删不掉旧 `dist` 时**只是跳过**，不报错

第一次重打时 PyInstaller 直接退出码 1。原因是上一次的 `dist/final_player`
还在，`--noconfirm` 想删但删不掉（被占用 / 权限），于是它**静默跳过**继续跑，
最后在 COLLECT 阶段炸掉——错误信息离真正的原因很远。

危害不只是报错：就算它没炸，**上一次的陈旧文件会留在新包里**（改过名、删过的
模块还在里面），症状是"代码改了但包的行为没变"，比报错难查十倍。

**补法**：`build_exe.py` 在调用 PyInstaller 之前自己 `shutil.rmtree(OUT_DIR)`，
并且**删不掉就直接报错停下**，明确告诉用户手动删。

### 坑 6：`sys.prefix` 到底等于什么——不能猜，要打表

见 3.3。这一条其实是"坑"的反面：这轮一开始就决定**不猜**，造探针实测。
成本 20 秒，收益是整条 ffmpeg 查找链路变成确定的事。

（顺带一提：`PyInstaller/loader/` 下的 Python 代码里 `grep prefix` 一条都没有，
因为它是在 C 引导程序里设的。查文档不如打表。）

---

## 五、接缝

打包给程序引入了三个新的"外部事实"，各自只在一处被读取——这是刻意的，
因为这三件事都是**在别人机器上才知道答案**的：

| 事实 | 唯一读取点 | 谁提供 |
|---|---|---|
| `sys.stdout` 可能是 `None` | `final_player.py` 顶部冻结垫片 | 运行环境 |
| 模型缓存在哪 | 同上，`HF_HUB_CACHE` | `build_exe.py` 的 `--add-data` |
| ffmpeg 在哪 | `find_ffmpeg()`（**没改**） | `build_exe.py` 的 `--add-data` |

第三个尤其值得说：**没有为打包新开一条路**。`--add-data` 的目的地是照着
`find_ffmpeg()` 已有的搜索路径写的，而不是反过来让 `find_ffmpeg()` 多认一个
"打包专用"的位置。这样"包内布局"和"查找逻辑"就只有一个真相来源，
改一边另一边会立刻在套件里红。

---

## 六、测试分层

新增 `tests/test_packaging.py`，五节。设计思路是**把两类只能靠真机才暴露的
失败挪到提交之前**，而这两类各自需要一半是真的一半是假的：

| 节 | 真的一侧 | 假的一侧 | 覆盖的问题 |
|---|---|---|---|
| ① 包内目录布局 | **真** `find_ffmpeg()` | 照约定摆出来的假包 | 找到了吗 |
| ② transformers 名单 | **真** 现场解析 | `build_exe` 的名单常量 | 名单对不对 |
| ③ 冻结垫片 | **真** `import final_player` | 子进程伪造 `sys.frozen` | stdout/缓存 |
| ④ 真建迷你包 | **真** PyInstaller | 一行等价的路径判断 | `sys.prefix` 是啥 |
| ⑤ 自检入口 | **真** 跑一遍 `--selftest` | —— | 入口接上了吗 |

①②④⑤ 合起来正好覆盖"包内 ffmpeg 能不能被找到"这条链：
① 是真函数配假包，④ 是真包配等价判断。**之所以拆开**：把 `final_player` 塞进
那个迷你包会把 torch/cv2/tkinter 全拖进来，那就等于正式打包本身（几分钟 + 1 GB），
不适合放进每次提交都跑的套件。

第 ② 节里有两句**反直觉断言**，专门钉住坑 1，免得后人"顺手改回去"：

```python
s.check("transformers.models.depth_anything" in live,   # it IS the V1 package
        "模型 id 叫 Depth-Anything-V2，实现却在 V1 的 depth_anything 包里")
s.check("transformers.models.depth_anything_v2" not in live,
        "按名字猜出来的 depth_anything_v2 是个不存在的包 —— 手写名单必错")
```

第 ③ 节用**子进程**伪造冻结环境，因为垫片是 import 期执行的代码，没法在
进程内测。三个用例：(a) 冻结 + 有缓存 → 验 stdout 兜底 + `HF_HUB_CACHE` 指对
+ 报告真的落进了 `player.log`；(b) 冻结 + 没缓存 → `HF_HUB_CACHE` 必须**不被设置**；
(c) 非冻结 → 垫片必须完全不生效（用一个哨兵值验）。

`run_tests.py` 的注册表加了这一组，语法检查纳入了 `build_exe.py`。
现在是 **12 具名 + 5 乱序 seed = 17 组**。

### 一条「套件自己写错」的假红

第 ④ 节第一次跑就红了一条 `onedir 包的解包目录就叫 _internal`。查下去发现是**探针脚本自己的
优先级 bug**，不是产品问题：

```python
print("PREFIX_IS_INTERNAL=%s" % os.path.basename(sys.prefix) == "_internal")   # 错
```

`%` 的优先级**高于** `==`，所以那句算的是
`("PREFIX_IS_INTERNAL=_internal" == "_internal")` → 永远 `False`。补上括号即绿。

记在这里是因为它正好撞上本项目的另一条老规矩：**断言失败时要先怀疑断言**。
这一条的「真值」其实在正式包里已经手工确认过（`sys._MEIPASS = ...\dist\final_player\_internal`），
所以是「事实为真、判据写错」，而不是反过来。

---

## 七、诚实数字

```
打包环境           build/buildvenv（CPU 版 torch 2.5.1+cpu）
打包耗时           141.9 秒（共打过 6 次，范围 140~164 秒；含模型解析那 ~30 秒）
产物 exe           46.4 MB
产物整个文件夹     934.5 MB
分发包             dist/final_player_win64.zip   409.3 MB（压 78 秒）
```

**发出去的就是那个 zip**：对方解压后双击 `final_player.exe`。`build_exe.py` 默认会顺手压好
（`--no-zip` 可跳过），zip 里还带了一份自动生成的 `使用说明.txt`（含"怎么用 / 第一次打开会
遇到什么 / 出问题怎么自检并报障"）。

体积构成（实测）：

| 项 | 大小 | 备注 |
|---|---|---|
| ffmpeg bin（11 个文件） | 136.7 MB | 与包根那份 `av*.dll` 有重复，见坑 4 |
| 深度模型（含 version.txt） | 94.6 MB | |
| PyTorch（CPU） | 约 400 MB | venv 里是 1.2 GB，其中 790 MB 的 `.lib` 被 PyInstaller 排除 |
| opencv / numpy / PIL 等 | 约 200 MB | |

**这个数字比一开始估的大。** 起因是低估了 CPU 版 torch：它在 venv 里占 1.2 GB
（`dnnl.lib` 一个就 653 MB），虽然 `.lib` 不进包，`torch_cpu.dll` 自己也有 249 MB。

进一步瘦身的可能（**这一轮没做**，留给需要的人）：

- `--exclude-module sympy` 省约 30 MB，但 `torch.fx` 会用到它，有风险；
- 把深度模型换成 ONNX Runtime 推理，能省掉整个 torch（约 400 MB），
  但要改 `_load_depth_model` 和 `process_video_to_3d` 里的推理代码；
- `--onefile` 不谈，见 2.1。

回归：**17 组（12 具名 + 5 乱序 seed）全部通过，0 FAIL，总耗时 416.5 秒**。
其中新增的「打包成 exe」组 **27 项全绿**（含第 ④ 节真跑一次 PyInstaller 建迷你包，单组 28.9 秒）。

第 23 轮时是 16 组 794 PASS；本轮多了这一组，语法检查也多了 `build_exe.py` 一个文件。
（各组的逐项 PASS 数在套件自己的输出里，这里只汇总到"组"这一层。）

---

## 八、想快速看懂按这个顺序读

1. **`build_exe.py` 顶部的注释** —— 为什么 onedir、为什么 CPU 版 torch。三分钟。
2. **`final_player.py` 第 8~60 行那段冻结垫片** —— 打包改变行为的地方全在这
   五十行里，每一行都写了"不这么写会怎样"。
3. **本文 第四节 坑 1** —— transformers 动态导入那个，本轮最值钱的一条。
4. **`tests/test_packaging.py` 顶部注释** —— 五节为什么这么分，以及
   "真函数配假包 / 真包配等价判断"这个拆法。
5. 其余按需回查。

## 九、留给后来人

- **换模型时**：改 `build_exe.py` 的 `MODEL_ID` 之后，`HF_DEST` 的目录名要跟着
  改（套件第 ① 节会红），`TRANSFORMERS_FALLBACK` 可能也要改（套件第 ② 节会红）。
  这两处红了不是麻烦，是它在替你拦住坑 1。
- **换 transformers 版本时**：套件第 ② 节会现场重新解析，如果新版把
  `depth_anything` 挪了地方，它会立刻说出来。
- **要发更新版时**：把 `dist/final_player` 整个文件夹重新压 zip。别只发 exe ——
  见使用说明第一句。
- **用户的 API key 不会跟着走**：`online.json` 在 `%APPDATA%/final_player/`，
  在项目目录之外，打包脚本也没有带它。接收方要自己申请一个。
- **`build/probe_find_ffmpeg.py` 是一次性探针**，跑完就该删（它在
  `.gitignore` 覆盖的 `build/` 里，不会入库）。
