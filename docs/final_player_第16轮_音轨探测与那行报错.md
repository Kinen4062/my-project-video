# 第 16 轮：那行「打开失败」到底是谁打的 —— 音轨探测

> 起点：用户问「为什么关掉播放器之后，都会出现
> `Failed to open file '<视频目录>/测试合成3D.mp4' or configure filtergraph`？」
>
> 终态：这行彻底不再出现，而且顺带砍掉了一个从头到尾没用过的音频播放器。

---

## 1. 先看用户的原话

```
失败提示：Failed to open file '...测试合成3D.mp4' or configure filtergraph
出现时机：关掉播放器之后
频率：都会出现（= 每次）
```

这句话里有个**陷阱**：它是用户对自己现象的**归因**，不是事实。

「关掉播放器之后出现」很容易让人往下面几个方向查：

- 关闭流程有 bug？`on_close` 里是不是又去开了什么东西？
- 退出时某个定时器/线程还在跑，去建了新播放器？
- 文件被提前删了，导致关闭时读失败？

**这三条全是错的。** 真实原因和"关闭"这个动作**毫无关系** —— 这一轮花时间最多的
地方，就是一步步把"用户的归因"和"实际因果"拆开。下面把这个拆解过程完整记下来，
因为它比最后的修复更值得学。

---

## 2. 排查过程（本轮最值钱的部分）

### 第 1 步：这行字是谁打的？

先把这句话当成指纹去搜：

```bash
# 在 ffpyplayer 那一堆二进制里找这句字符串
.venv/Lib/site-packages/ffpyplayer/player/core.cp312-win_amd64.pyd -> 有
.venv/share/ffpyplayer/ffmpeg/bin/ffplay.exe                     -> 有
其他 21 个 .dll/.pyd/.exe                                          -> 没有
```

**结论**：它是 `ffpyplayer` 的播放器核心（`ffplay.c` 的移植）打的，
不是我们自己的代码、也不是 ffmpeg 子进程。

**顺带看一眼它旁边的字符串**，能看出它属于哪段逻辑：

```
Failed to open file '%s' or configure filtergraph
%s: error while seeking
Failed to copy packet%s
Reached eof
Exiting read thread
ffpyplayer.player.core.VideoState.read_thread      ← 关键
```

也就是说：它出现在 `read_thread`（读线程）的失败路径上。
再结合本文件顶部早就记着的一条实测（第 5 条）：

> 「没有音轨」的文件**不会**让 MediaPlayer 构造报错 —— ffmpeg 只是打不开文件 /
> 配不起 filtergraph（stderr 打印 "Failed to open file ... or configure
> filtergraph"），而 `get_pts()` 从此**永远返回 0.0**。

**答案已经浮出来了**：给一个**没有音轨**的文件建 `MediaPlayer`，它的读线程配不起
filtergraph，就打这行。而那个文件实测确实只有一条视频流：

```
$ ffprobe ...测试合成3D.mp4
0,mpeg4,video          ← 只有视频，没有 Audio
```

### 第 2 步：那为什么最小实验里它一个字都不打？

写了个最小脚本，直接 `MediaPlayer(那个文件)`，把 fd1/fd2 都重定向到文件：

```python
MediaPlayer(path, ff_opts={})
time.sleep(1.2)
get_pts()          # -> 0.0   （确认真的没有音轨）
close_player()
```

```
[stdout] 0 字节
[stderr] 0 字节        ← 什么都没打！
```

而走 `app.open_video()` 就出现。**这就是第一个分歧点。**

当时我以为差异在"混音器有没有被初始化过"或"是不是关得太快"，于是做了下一步。

### 第 3 步：场景穷举（7 个场景，各跑一个独立子进程）

`on_close()` 会 `os._exit(0)`，所以每个场景必须独立进程跑、把 stdout+stderr 合并捕获：

| 场景 | 做了什么 | 命中 |
|---|---|---|
| `plain` | 只建 app 然后关窗 | **0 次** |
| `open_noaudio_close` | 开无音轨文件 → 立刻关 | 1 次 |
| `play_noaudio_close` | 开无音轨 → 播 3 秒 → 关 | 1 次 |
| `switch` | 有音轨 → 无音轨 → 有音轨 → 关 | 1 次 |
| `speed_noaudio` | 无音轨 + 切 1.5x 倍速 → 关 | 1 次 |
| `seek_noaudio` | 无音轨 + 拖动进度 → 关 | 1 次 |
| `multiple_noaudio` | 同一个无音轨文件开 **4 次** → 关 | **1 次** |

两条硬结论：

1. **必须发生过"打开无音轨文件"这个动作**。不开文件的 `plain` 场景一次都没有 ——
   所以它和"关闭动作"确实无关。
2. **同一进程里开 4 次同一个文件，只打 1 次** —— ffmpeg 对重复日志会去重
   （不然刷屏）。这也解释了为什么用户觉得"每次都会"（每次都只多一行）。

### 第 4 步：它到底在哪一刻打印？

用两个标记夹住关键节点，并把 Python 的 stdout/stderr + C 运行时的流**分开 flush**：

```python
os.write(2, "@@@@ 1) 准备打开无音轨文件")     # 直写 fd，绕开一切缓冲
app.open_video(TARGET)
os.write(2, "@@@@ 2) 只 flush Python 的 stdout/stderr")
sys.stdout.flush(); sys.stderr.flush()
os.write(2, "@@@@ 3) 只 flush C 运行时的所有流 fflush(NULL)")
ctypes.CDLL("msvcrt").fflush(None)
```

输出：

```
@@@@ 1) 准备打开无音轨文件（下面这行如果立刻出现，就是无缓冲直写）
@@@@ 2) 只 flush Python 的 stdout/stderr
@@@@ 3) 只 flush C 运行时的所有流 fflush(NULL)
Failed to open file '<视频目录>\测试合成3D.mp4' or configure filtergraph
@@@@ 4) 两者都再 flush 一次
```

**两个关键事实**：

1. 它夹在 **`open_video()` 内部**（在 `@@@@ 1)` 和 `@@@@ 2)` 之间调用发生了），
   **不是**在关闭时。
2. 只有 `fflush(NULL)`（C 运行时的全局刷新）能把它逼出来 —— 说明它躺在
   **C 运行时（MSVCRT）的输出流缓冲**里。

（还有一个当时没解开、但不影响结论的谜：把 fd1/fd2 重定向到**文件**时完全抓不到它，
重定向到**管道/终端**却能抓到。ffpyplayer 那边对输出流有自己的处理，抓不到就抓不到，
反正"它在 C 缓冲里"这条已经被 `fflush` 证实了。）

### 第 5 步：于是"关掉之后才出现"就解释通了

Windows 上，**stdout/stderr 一旦不是终端**（PyCharm 的控制台管线、重定向、CI 日志都算），
C 运行时默认给它们**块缓冲**（4KB 量级）。那行只有 60 多个字节 —— **填不满**，
于是一直扣在缓冲区里不出来。

什么时候冲出来？**进程退出时**。而 `on_close()` 的最后一件事是：

```python
sys.stdout.flush()
sys.stderr.flush()
os._exit(0)          # CRT 的 _exit 会 flush 并关闭所有流
```

于是：

```
你打开文件            → 日志被写进 C 缓冲（你看不见）
你正常播放            → 缓冲原封不动（你还是看不见）
你关掉播放器          → os._exit → CRT flush → 那行"唰"地出现 → 控制台关闭
```

**用户看到的「关掉之后出现」，真相是「打开时产生、关闭时才被冲出来」。**
归因和因果在这里差了一整个播放周期。

---

## 3. 修复：让那行根本没有机会被写出来

既然它唯一的来源是"给没有音轨的文件建了 `MediaPlayer`"，那修复就一句话：

> **别建那个播放器。**

它本来就没有任何用处，还有三笔额外开销：

1. 白占一条 SDL 混音通道 + 一颗 ffpyplayer 线程；
2. 换文件时要陪着走一遍 `close`（那正是第 15 轮花大力气挪去后台的操作）；
3. 带出这行**误导了几轮用户**的报错。

### 3.1 探测函数为什么必须是三态

`media_tools.has_audio_stream()` 第 11 轮就有了，但它**不能直接用**：

```python
def has_audio_stream(ffmpeg, path):
    if not ffmpeg:
        return False               # ← 麻烦在这
    ...
    return b"Audio:" in proc.stderr
```

它把"**确定没有音轨**"和"**探测不了**"合并成了 `False`。这在它原本的场景（转换收尾时
决定"要不要补音轨"）是对的 —— 判断不了就别动人家的音轨。但换到播放器这边就**正好相反**：

| 调用方 | 问的问题 | "不确定"时该按什么处理 |
|---|---|---|
| mux（补音轨） | 要不要补？ | **不需要**（少做一件事没副作用） |
| 播放器 | 要不要建播放器？ | **需要**（宁可白建，也不能把有声音的文件永久静音） |

两种相反的默认值没法共用同一个二态返回值。所以拆成：

```python
def probe_audio_stream(ffmpeg, path):
    """True / False / None（None = 探测不了，下不了结论）"""

def has_audio_stream(ffmpeg, path):
    """二态的老接口，语义原样保留：只有确认有才 True"""
    return probe_audio_stream(ffmpeg, path) is True
```

老接口变成一行薄封装，`tests/test_audio_mux.py` 里那些接缝一行都不用改。

### 3.2 结论放哪：`open_video` 里的两处

```python
# open_video 里，建 cap 之后、**摘掉旧播放器之前**：
has_audio = self._probe_audio_track(file_path)

...  # 摘旧播放器、状态就位

self.audio_failed = False
self.current_has_audio = has_audio
if has_audio is False:
    self.audio_failed = True
```

**位置很讲究**：探测要起一个 ffmpeg 子进程（实测 ~120ms）。如果放在"摘掉旧播放器"
和"建新播放器"之间，这 120ms 里旧音频已经摘掉、新音频还没建 —— 会**白断一小段声音**。
放在摘旧之前，这 120ms 里旧音频照常出声，后面那段间隙还是原来的 ~2ms。

### 3.3 为什么"顺手置上 `audio_failed`"不是偷懒

代码里看起来是"用一个叫 failed 的标记表达'没音轨'"，有点别扭。但它有个**非这样不可**的理由 ——
在 `_silent_clock_media_seconds()`（无音轨文件用的墙钟主时钟）的准入条件里：

```python
if not (self.audio_failed or self._audio_clock_dead
        or self._audio_handoff_pending):
    return None
```

如果**只**记一个自己的标记、不置 `audio_failed`，而播放器又没建（`audio_player is None`），
那么：

- `_audio_clock_media_seconds()` 因为 `p is None` → 返回 None；
- `_silent_clock_media_seconds()` 因为三个条件全 False → 也返回 None；
- → **主时钟永远是 None → 永久盲调度**
- → 正好撞上文件顶部第 5 条记的那个坑：**画面越播越慢，直到近乎卡死**
  （实测无音轨版 6 秒只推进 0.88 秒，0.147x）。

而且更糟：判死时钟的代码本身就在 `_audio_clock_media_seconds()` 里面，
而那里已经提前 return 了 —— **连"等 1.5 秒后自愈"都不会发生**。

所以置 `audio_failed` 是**必须**的。顺带还有个好处：原来无音轨文件的头 1.5 秒是按帧率
盲走的（要等 `AUDIO_CLOCK_STALL_SEC` 攒够才判死），现在**第一帧就直接用墙钟**。

### 3.4 探测结果要缓存

探测一次 120ms。用户点列表是**连着点**的，每次都现探就会一顿一顿。所以结论按
**文件指纹** `(绝对路径, 大小, 修改时间)` 缓存：

- 同一个文件第二次打开：0ms；
- 文件被重新转换/替换（大小或时间变了）：缓存自动失效，不会拿旧结论骗人；
- **只缓存有结论的结果** —— 探测失败多半是"这一次读不到"，缓存下来等于把偶然失败
  固化成结论，到时候有音轨的文件会被永久静音，那是本末倒置。

### 3.5 再往前一步：趁空闲预热

`open_video` 首次 174ms（含 ~120ms 探测），缓存命中后 45ms。列表在启动时就确定了，
完全可以先算好。所以 `_finish_init` 里挂一个延后的预热：

```python
self.root.after(AUDIO_PROBE_WARM_DELAY_MS, self._warm_audio_probe_cache)
```

三条纪律写在方法 docstring 里：

- 线程里**只调探测**，不碰任何播放状态（缓存是普通 dict，CPython 下单个 get/set 原子，
  最坏情况是同一项被探两次）；
- **先在主线程调一次 `find_ffmpeg()`** —— 它背后那对全局缓存是懒搜索的，放到后台线程
  首次命中会触发一次全盘文件系统查找；
- **最小 2 个文件才预热**（一个文件的探测成本反正躲不掉，还白起线程），
  只预热确实存在的文件，daemon 线程。

> ⚠️ 这里踩了一个**当场就发现**的坑：最初写的"窗口没了就停"用了
> `self.root.winfo_exists()` —— **Tk 不是线程安全的，后台线程调 Tk API 会出事**
> （连"只读"的 `winfo_exists` 也不行）。删掉，改成只做探测。

测试基架会把这个预热关掉（`_harness.load_app` 里设 `_audio_probe_warm_enabled = False`）：
后台 ffmpeg 子进程会跟 `test_playlist.py` ⑪ 节那条"连续切换 <150ms"的断言抢磁盘和 CPU，
**一个不确定的耗时断言等于没有断言**。预热本身另有专门用例覆盖。

---

## 4. 测试（`tests/test_audio_probe.py`，7 节 36 项）

| 节 | 守的是什么 |
|---|---|
| ① 探测函数的三态语义 | True / False / None 三种输入各自正确；**中文 + 空格路径**（用户的文件就是中文名，Windows 路径编码是真实风险点）；老接口 `has_audio_stream` 的二态语义没变 |
| ② 无音轨：不建播放器 | `audio_player is None`、`audio_failed is True`、`_audio_clock_dead is False`（说明拿墙钟靠的是"已经知道没音轨"而不是"等判死"）、**墙钟主时钟在推进**（不是冻住的读数） |
| ③ 有音轨：一切照旧 | 照常建、照常校准 —— 修一个噪音不能把正常音频搞坏 |
| ④ 缓存 + 代价上界 | 第二次打开不重复探测；**改动文件时间戳后必须重新探测**；首次打开 < 600ms（实测 148ms）、缓存命中 < 200ms（实测 33ms） |
| ⑤ 不再做无用的音频动作 | 无音轨文件上 `_begin_audio_handoff()` 直接被挡（省掉那 0.5 秒）；切倍速、拖动之后依然没有音频播放器，而视频照常工作 |
| ⑥ 预热 | 两个文件时预热生效；一个文件时不预热；开关关掉时完全不启动 |
| ⑦ 探测不了时不许误静音 | 伪造"本机没有 ffmpeg" → 探测 None → **仍然建播放器** |

### 4.1 回归抓到了本轮自己引入的 120ms（这才是回归测试的用处）

第一次 `--full` 有一组红：

```
[失败] 播放列表与拖放   18.2s  退出码=1
  FAIL  每次切换的同步耗时都 < 150ms（修复前 440~810ms；本轮实测最大 154ms）
```

154 vs 阈值 150 —— **差 4ms 擦线**。查下来就是本轮新加的那 120ms 探测：

⑪ 节先打开 `city`（缓存命中），然后 4 次切换在 `[city, city2]` 之间来回 ——
**第一次切到 `city2` 时缓存还是空的**，那一次就带上了 ~120ms 探测（120 + 34 = 154）。

**处理方式不是把阈值放宽，而是把两笔账拆开**：

- ⑪ 节量的是"**切换本身**"（第 15 轮那条同步 `close_player`），所以开测前显式把两个
  文件的探测缓存填上 —— 真实使用中列表文件本来就会被 `_warm_audio_probe_cache` 预热，
  这才是它要模拟的状态；
- "探测要花多少时间"是**另一件事**，在 `test_audio_probe.py` ④ 里单独立了两条上界。

**混在一句话里的两笔账，红了根本看不出是哪一笔坏的** —— 这是测试设计问题。
而且直接放宽阈值会削弱第 15 轮好不容易建的防线（458ms 对 150ms 有区分度，
对 250ms 就打折了）。拆开之后 ⑪ 节回到 **28ms**。

> 顺带说明：`_harness.load_app` 会关掉 `_warm_audio_probe_cache`（后台 ffmpeg 子进程会跟
> 耗时断言抢资源）。所以**每个套件都得自己决定要不要填这个缓存** —— ⑪ 节显式填了，
> `test_audio_probe.py` ④ 则故意不填（它量的就是含探测的代价）。

### 4.2 写用例时踩的两个坑（都是我自己写错）

1. **`toggle_play()` 的语义陷阱**：`open_video()` 结束时状态**已经是 PLAYING**，
   测试里再 `toggle_play()` 是**暂停**。而暂停时主时钟按设计返回 None ——
   我的 ②③ 节因此全红。修法是别调它，并把这个原因写进用例注释。
2. **字符串里混进 ASCII 双引号**：写测试消息时顺手打了 `"已经知道没音轨"`，
   直接把 `.py` 打断了。中文内容里一律用「」。

### 反证：用例真的能抓住回归吗？

把探测结论硬改成"永远说有音轨"（等于退回改前行为），跑同一个文件：

```
Failed to open file '<视频目录>\测试合成3D.mp4' or configure filtergraph
  audio_player is None  -> False     ← ② 节的断言会红
  audio_failed          -> False     ← 退回改前
```

**那行报错原样回来了，断言也红了。** 因果链和用例的有效性一起被证实。

---

## 5. 验证与诚实数字

```
run_tests.py --full（干净环境 env -u PYTHONIOENCODING -u PYTHONUTF8 + 输出重定向）
→ 13 组全 OK、309 项断言、0 FAIL、退出码 0、350.1s
```

| | 数值 |
|---|---|
| 套件数 | 12 → **13 组** |
| 断言总数 | 273 → **309**（新增套件 36 项；原有 273 项一条没删、一条没改判据） |
| 新增套件 | `tests/test_audio_probe.py`（7 节 36 项），`run_tests.py` 里排第 6 组 |
| 端到端场景复验 | 7 个场景：改前各命中 1 次 → **改后 0 次** |
| 反证 | 拆掉修复 → 用例变红 + **那行报错原样回来** |
| 探测耗时 | 115~123ms；缓存命中后 0ms |
| `open_video` 耗时 | 无音轨文件首次 148ms（含探测）、缓存命中 33ms |
| `final_player.py` | 3690 行 |
| 本轮改动量 | `final_player.py` +136 行、`media_tools.py` +30 行、测试 +208 行 |

**老实说**：这一轮**没有让代码变短**，反而长了 136 行（其中约一半是注释和 docstring）。
换来的是一行让用户疑惑了几轮的报错消失，外加一个从头到尾没用的播放器
（和它的 SDL 通道、ffpyplayer 线程、每次换文件的 close 开销）不再被创建。

**还有一笔要如实交代**：新增的探测给 `open_video` 加了 ~120ms —— 这是第 15 轮
刚优化完的路径，所以专门做了缓存 + 启动预热（列表文件 0ms），并把它挪到
"摘旧播放器之前"避免白断声音。剩下的部分（拖进来的新文件）确实多 120ms，
这个代价已经用用例立了上界（首次 < 600ms）。

---

## 6. 想快速看懂这一轮，按这个顺序读

1. **本文件第 2 节第 5 步** —— 先接受这个事实：那行日志产生于打开文件时，
   关闭时才被 C 缓冲冲出来。后面的代码都是围着这条来的。
2. `media_tools.py` 的 `probe_audio_stream` —— 二十来行，看三态为什么必要
   （表格：两个调用方对"不确定"的默认值正好相反）。
3. `final_player.py` 的 `_probe_audio_track` —— 看缓存键为什么是
   `(路径, 大小, mtime)`，以及为什么**只缓存有结论的结果**。
4. `final_player.py` 的 `open_video` 里那两个注释块 —— 看探测**为什么必须放在
   摘旧播放器之前**（不然白断 120ms 声音）。
5. `_silent_clock_media_seconds` 的准入条件 —— 理解**为什么非置 `audio_failed` 不可**
   （不置就是永久盲调度，比一行报错严重得多）。
6. `_warm_audio_probe_cache` —— 预热，以及"后台线程里绝不许碰 Tk"这条纪律。
7. `tests/test_audio_probe.py` 第 ② 节 —— 再看一眼反证那两行输出。

## 7. 一条通用经验（本轮最想留下的）

**用户描述的是"他观察到的现象"，不是"因果"。**

「关掉播放器之后出现」是**现象**：关掉那一刻确实看到它了。
但真相是「打开时产生、关闭时才被冲出来」。这两者差了一个完整的播放周期，
**照着现象去查关闭流程，会一无所获**。

同样的形状以前也出现过：用户报「快捷键没反应」，真相是"焦点在下拉框上时让路规则太粗"；
报「多个视频点击卡顿」，真相是"`close_player()` 阻塞在主线程"。
**先把现象翻译成可观测的因果，再动手改代码** —— 这一轮花在翻译上的时间比改代码多，
但改代码只花了十分钟。
