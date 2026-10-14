# 第 17 轮：记忆播放位置 —— 关掉播放器、切走之后，还能接着上次看

> 需求原文：
> 「使得播放器关闭后或者播放其他视频后，又点击被播放过的未完全播完的视频时，
> 可以选择从头播放还是从未播放完的地方继续播放的功能」

---

## 1. 先把需求拆成几个能判断的问题

"记住播放位置"听起来是一句话，落到代码里是四件不同的事，每件都可能做错：

| 问题 | 本轮的做法 | 在哪 |
|---|---|---|
| **什么时候算"离开"？** | 换文件时、关窗口时、播到片尾时 | `_settle_current_position()` 的三个调用点 |
| **什么才算"没看完"？** | 两条 5 秒阈值，见第 2 节 | 模块级常量 `RESUME_MIN_POSITION_SEC` / `RESUME_TAIL_SEC` |
| **记在哪儿、怎么不记坏？** | 用户配置目录下的小 JSON，带文件指纹 | 新模块 `resume_store.py` |
| **再打开时问什么、怎么起播？** | 三选一对话框；起点同时落到 cap 和音频播放器 | `ResumeDialog` + `open_video` 的 `start_sec` |

---

## 2. 最要紧的一个判断：什么算"没看完"

整个功能的地基就是这一句。两条阈值，都在 `final_player.py` 顶部：

```python
RESUME_MIN_POSITION_SEC = 5.0   # 停在这么靠前 → 当作"还没开始看"
RESUME_TAIL_SEC = 5.0           # 距末尾这么近 → 当作"看完了"
```

中间那一大段才记下来，下次问一句。

**为什么不按比例判"看完"**：想过"过了 95% 就算完"，但 2 小时的片子剩 6 分钟
（95%）明明还想接着看 —— 按比例判会把记录清掉，用户下次点开发现从头开始了，
而且完全不知道为什么。按**绝对秒数**判，长片短片一视同仁，行为可解释。

**为什么两个值都取 5 秒**：纯属省得记两套数。偏头的 5 秒和偏尾的 5 秒，观感上
都是"基本没看"和"基本看完了"。取大了不会出错，只会让询问变多（用户被问烦了
自然会点"从头"），所以宁可保守。

**"看完了"要顺手清记录**：不清的话，下次点开会问一句
「上次看到 19:58，要接着看吗？」—— 片子都看完了，这一问只会让人莫名其妙。
清除发生在两个地方：贴着片尾离开时（`_settle_current_position`），以及播到
`cap.read()` 读不出帧时（`finished=True`）。

---

## 3. 数据放在哪儿：新模块 `resume_store.py`

跟 `playlist_store.py` 是一对：同样的位置（用户配置目录，Windows 是
`%APPDATA%\final_player\`）、同样的原子写（临时文件 + `os.replace`）、同样的
"读写失败一律静默降级"。**记录"看到哪儿了"是锦上添花，绝不能因为它让播放器
本身出问题** —— 配置目录只读、磁盘满、文件被锁，全部按"没有这条记录"处理。

数据结构：

```json
{
  "version": 1,
  "positions": [
    {"path": "D:/片子/a.mp4", "sec": 512.3, "dur": 1840.0,
     "size": 1234567890, "mtime": 1699999999.5, "at": 1700000000.1}
  ]
}
```

### 为什么除秒数外还要存 (大小, 修改时间)

记录是**按路径**查的，而路径会指向不同的东西。最实在的一种：用户把片子重新
转换了一遍（或拿别的文件覆盖了同名文件），旧记录的"512 秒"在新文件上毫无意义，
最坏情况是拿 2 小时长片的位置去播一个 30 秒短片，一开局就冲到中段。

所以顺手存下 `(大小, 修改时间)` 当指纹，查询时对不上就当**没有这条记录**，
并就地把它清掉（免得下次还拿它来问）。见 `_lookup_resume`。

### 坏条目一律丢弃，不猜默认值

`resume_store._clean_entry` 的规则是"不合法就丢"，而不是"缺字段就补 0"。
手改坏的 JSON、别的版本写的文件 —— 唯一安全的处理是当作没有，**绝不能凭空
造一个位置出来把用户送到片子中间**。测试里覆盖了 6 种坏法（空路径、路径不是
字符串、秒数不是数字、时长 0、负位置、根本不是对象）。

一个细节：`bool` 是 `int` 的子类，`{"size": true}` 会通过
`isinstance(x, (int, float))` 检查并被当成 1.0 用。所以单独写了 `_is_number()`
把 `bool` 挡掉 —— 挡掉的是**那个字段**，不牵连整条记录。

---

## 4. 四次改动，各在哪一步

### 4.1 `open_video` 开头：结算上一个 + 问一句

顺序是硬约束，写反了会出两种很难查的问题：

```python
if file_path is None:
    file_path = filedialog.askopenfilename(...)   # ① 文件对话框
    if not file_path:
        return False

self._settle_current_position()                   # ② 结算上一个（cap 还活着）

start_sec = 0.0                                   # ③ 查记录 + 问一句
if resume and self.resume_enabled:
    entry = self._lookup_resume(file_path)
    if entry is not None:
        choice = self._ask_resume_choice(...) if ask else "resume"
        if choice == "restart":
            self._forget_resume(file_path)
        elif choice == "resume":
            start_sec = sec
        else:
            return False                          # ★ 取消

self._stop_playback_loop()                        # ④ 到这里才开始拆旧的
...
```

**② 必须在换 cap 之前** —— 位置只能从还活着的那一个读。

**③ 必须在 `_stop_playback_loop()` 之前** —— 用户在询问框上点"取消"时，
当前正在播的东西必须**原封不动**。这一段因此只做"查表 + 弹窗"，一个播放状态
都不碰。放到后面的话，"取消"会变成"当前视频已经被停了、新视频又没开" ——
两个都不播。测试里专门有一条守着这件事（④ 节"取消"那三条断言）。

**"从头播放"要顺手清记录** —— 不清的话下次又问、又续，而"从头播放"这句话的
意思正是"那条记录我不要了"。

参数设计（两个布尔，语义正交）：

| 调用场景 | 传参 |
|---|---|
| 用户点列表 / 拖文件 / 关文件对话框选文件 | 默认 `resume=True, ask=True` |
| 片尾自动连播下一项 | `ask=False`（静默续播，见 4.4） |
| 转换产物（`on_conversion_done`） | `record=False, resume=False` |

### 4.2 `fps` / `total_frames` 提前读

原来它们在"音频播放器建好之后"才读，现在挪到 `cap.isOpened()` 成功后立刻读 ——
因为续播起点是**秒**，而 `cap` 要的是**帧号**：

```python
fps = self.cap.get(cv2.CAP_PROP_FPS)
self.video_fps = fps if fps > 0 else 30.0
self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
self.scale.config(to=self.total_frames)

start_frame = self._clamp_seek_frame(int(start_sec * self.video_fps))
if start_frame > 0:
    self.cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
```

**起点必须同时给 cap 和音频播放器**：

```python
self.audio_player = self._create_audio_player(file_path, start_sec)   # 原来是 0.0
...
self._last_media_sec = start_sec                                      # 原来是 0.0
```

第二行容易被漏掉，但漏了会真出问题：`_last_media_sec` 是"当前视频位置"，
后台音频交接建新播放器时取的就是它。续播到第 8 秒之后用户再拖一下进度条，
如果它是 0，新音频会落到片头 —— 音画错位，而且只在这种组合操作下才复现。

### 4.3 `on_close` 第一步

```python
def on_close(self):
    # 0. 必须是第一件事 —— 下面第 5 步会 release 掉 cap，
    #    而且第 2 步之后 play_state 就成 IDLE 了。
    self._settle_current_position()
    ...
```

用户关了播放器、过几天再打开能接着看，靠的就是这一句。

### 4.4 `update_frame` 撞到片尾时

```python
ret, frame = self.cap.read()
if not ret:
    self._settle_current_position(finished=True)      # 看完了 → 清记录
    next_path = self._advance_playlist() if not force else None
    if next_path is not None:
        if not self.open_video(next_path, ask=False):   # 静默续播，不弹窗
            self._playlist_failed.add(next_path)
        return
```

**为什么是显式 `finished=True` 而不是让它自己去量位置**：此刻 `cap.read()`
已经读不出帧，位置读数不可信（停住不动，或者停在某个解码出错的地方）。

**为什么自动连播走静默续播（`ask=False`）而不是也弹窗**：那是"往下连播"，
弹窗会把连播打断；而那个文件上次没看完、这次接着看，本来就是连播该有的样子
（像追剧，一集接一集）。这一条明确写在 `open_video` 的 docstring 里。

---

## 5. 对话框：为什么不用 messagebox

三选一（继续 / 从头 / 取消）在 `messagebox.askyesnocancel` 里只能映射成
"是 / 否 / 取消"，按钮文字跟含义对不上 —— 用户得先读懂问题、再去猜哪个按钮
是"从头"。所以自建一个 Toplevel，三个按钮把动作写在脸上：

![续播询问框](final_player_第17轮_续播询问.png)

```
这个视频上次没有看完
a.mp4
上次看到 8:04 / 20:00，还剩 11:56
要接着看，还是从头开始？
[继续播放]  [从头播放]                          [取消]
```

回车 = 继续（默认动作，这是用户点回来时更可能想要的）、Esc = 取消、
点标题栏的 × = 取消。三条路径都显式绑了 —— 即使不绑 `WM_DELETE_WINDOW`，
`wait_window` 返回后 `choice` 为 `None`、照旧按"取消"处理，行为也对；绑它是
为了让三条路径都在代码里看得见。

### `__init__` 只建、`show()` 才等

这个拆分不是为了好看，是**为了能被测试**：`show()` 里的 `grab_set` +
`wait_window` 是模态等待，无人值守的回归点不到按钮，会整轮挂死。拆开之后
测试可以只建不等，直接 `_pick("resume")` 验证三个返回值；`⑨` 节还额外用
`root.after(60, ...)` 自动点掉它，真的跑了一次 `show()`（既验证它会等，
也验证它会返回）。

`_pick()` 写成幂等的（窗口已经销毁时不抛）—— 因为"回车 + 关窗"之类的组合
真的可能连着触发两次。

### 询问逻辑单独抽成 `_ask_resume_choice()`

```python
def _ask_resume_choice(self, file_path, sec, dur):
    return ResumeDialog(self.root, file_path, sec, dur).show()
```

抽出来的唯一理由是**可替换**：测试把它换成一个固定返回值的 lambda，就能走遍
"继续 / 从头 / 取消"三条分支，不必去点真实窗口。

---

## 6. 测试隔离：这一步不做，整套回归都会歪

`_harness.load_app` 新增一个参数，**默认关掉整个功能**：

```python
app = fp.VideoPlayerApp(root, playlist_path=make_playlist_path(),
                        resume_path=make_resume_path())
app.resume_enabled = resume_enabled          # 默认 False
```

两条理由，第二条比第一条硬：

1. **不能写用户的真实配置**。跟播放列表一样，`resume_path` 换成 `%TEMP%` 下的
   临时文件，名字带 pid（跨进程不串味）。观看进度比播放列表还更要紧：列表少
   几条用户看得见，而"莫名其妙从片子中间开始播"是件很难自己想明白的事。

2. **不关掉的话，老套件的语义会悄悄变**。`test_chaos` 一轮要开几十次文件，
   记录一开，第二次打开就变成"静默续播到上次停的位置" —— 位置不再是 0，
   那些量"片尾回绕""测速""逐帧步进"的用例**全部失去意义**，而且它们大概率
   还是绿的（只是量的东西变了）。这种"绿着的假测试"比红着的更危险。

自己那一组（`tests/test_resume.py`）显式传 `resume_enabled=True`。

---

## 7. 本轮踩的坑

### 7.1 测试里"改文件来验证指纹校验"走不通

第一版是这么写的：拷一份视频 → 记一条位置 → `os.utime()` 改时间 → 断言记录
作废；再 `open(path, "ab")` 追加几个字节 → 断言大小变了也作废。

结果 `PermissionError: [Errno 13]`。查下来有两层原因：正在播的文件在 Windows
上被持有句柄；而这个沙箱环境对"修改已存在文件的内容"本来就有限制（连刚
`shutil.copy` 出来的新文件、从没被播放器打开过，`open(..., "ab")` 也是拒绝）。

**改法**：不去动文件，**直接构造一个"指纹对不上"的记录**喂给 `_lookup_resume`：

```python
app._resume_positions[key] = {"path": city, "sec": 7.0, "dur": 20.0,
                              "size": real_size, "mtime": real_mtime + 120}
s.check(app._lookup_resume(city) is None, "修改时间对不上 → 记录作废")
```

这反而**更精确**：要测的本来就是那个校验分支，直接喂边界比"真去动文件、
再期望程序发现"更可控。顺带把"位置越界"（太靠前 / 贴片尾）两条也一并测了。

### 7.2 测试用例自己把记录覆盖掉了，看着像"功能没生效"

⑤ 节第一版：塞一条 `other = 7.0 秒` 的记录 → `open_video(other, ask=False)` →
断言起播位置 ≈ 7.0。**实测 6.08**。

不是程序坏了：`open_video` 开头那次结算记的是 `current_video_path`，而当时
当前就是 `other`（上一节留下的），所以它先把 other 现在的真实位置（6.04）写进去，
把刚塞的 7.0 覆盖了，然后读到的是 6.04。

**改法**：先 `open_video(city)` 把"当前"换成别的文件，再塞 other 的记录，
断言也收紧到 `< 0.5`。改完实测请求 7.0 → 起播 **7.04**（一帧误差）。
这一条写进了测试注释 —— 它是"结算会覆盖"这个行为的真实证据，不是测试的瑕疵。

### 7.3 中文内容里又混进 ASCII 双引号

写断言消息时随手写了 `"没看完"`、`"继续"`，直接打断字符串常量
（`SyntaxError: invalid syntax. Perhaps you forgot a comma?`）。

规则重申一遍（第 16 轮就踩过）：**断言消息 / 注释 / 文档里的中文引号一律用「」**，
不要碰 ASCII 双引号。本轮用脚本一次性替换了 19 处。

### 7.4 凑数的断言要当场删掉

写的过程中为了"先跑通"塞过两条像这样的：

```python
s.check("d.mp4" in str(...) or True, "")        # ← 恒真，等于没测
s.check(True, "（上面那条只用来确认整体没有抛异常）")
```

它们会**抬高 PASS 计数却什么也没验证**，正是"绿着的假测试"的雏形。当场换成
真断言（"只留 2 条合法的" + "bool 字段被丢掉"）或者删掉。

### 7.5 `teardown` 漏了一步，日志里多出一行吓人的 Tk 报错

⑧ 节"同进程再开一个 app"跑完之后，日志里冒出：

```
invalid command name "2315037711744_finish_resize"
    while executing "2315037711744_finish_resize"  ("after" script)
```

**它不是坏在功能上**（断言全过、退出码 0），但看着像坏了，得查清楚。定位过程
本身有个可复用的教训：**stdout 是块缓冲、stderr 是无缓冲**，所以这行 Tk 报错
（stderr）在日志里的位置是假的 —— 它看起来"在 ① 节之前"，实际上发生在 ⑨ 节。
用 `python -u` 重跑（强制 stdout 无缓冲）才看到它真正的位置。

根因：`after` 会在 Tcl 里注册一个命令名，而 `window.destroy()` 会把这个窗口
注册过的命令**全删掉**，但**定时器本身还留在 Tcl 的事件队列里**。之后任何一次
事件泵跑到它，Tcl 找不到命令就报这么一句。

`on_close()` 里本来就有"取消所有挂起的 after"这一步（排第 3 步），但
`tests/_harness.py` 的 `teardown()` 一直漏了 —— 只因为老套件"拆完窗口就不再
泵事件"才没暴露。补上之后噪音消失。

**顺带说明**：`python -u` 这个手法值得记住。本轮之前有两处"输出位置对不上"
的困惑都源于块缓冲，看日志时先确认一眼是不是这个原因，比瞎猜快得多。

---

## 8. 测试分层（`tests/test_resume.py`，9 节）

顺序是从下往上，下面绿了上面才不会假绿：

| 节 | 测什么 | 关键断言 |
|---|---|---|
| ① | `format_hms` 的边界 | 0 / 59 / 60 / 3599 / 3600 / 3725 / 负数 / 非数字 |
| ② | `resume_store` 存取 | 中文+空格路径、坏文件、6 种坏条目、条数封顶、不留临时文件 |
| ③ | 记录时机 | 播放中**不写盘**；切走记 8.04；片头 5 秒内不记；贴片尾清记录 |
| ④ | 三种选择 | 继续→8.04 / 从头→0.04 且清记录 / 取消→返回 False 且当前播放分毫未动 |
| ⑤ | 静默续播 | `ask=False` 完全不问；仍续播到 7.04；记录不被当成"选了从头" |
| ⑥ | 片尾清记录 | 真的把 14 秒素材播到 EOF，记录被清 |
| ⑦ | 失效校验 | mtime / size 对不上 → 作废；位置越界 → 不当"没看完"；`record=False` 不记 |
| ⑧ | 落盘 | 记录真的在磁盘上；新开一个 app 指向同一份 JSON 能读到并通过校验 |
| ⑨ | 对话框 | 三个返回值；窗口真的建/真的关；`show()` 会等也会返回 |

⑥ 用**无音轨**素材是有意的：没有音频线程来搅时序，走墙钟主时钟、帧调度干净，
把 14 秒素材播到片尾只要两秒多。

---

## 9. 本轮改了哪些文件

| 文件 | 改动 |
|---|---|
| `resume_store.py` | **新增**（131 行）。进度落盘的纯存储模块 |
| `final_player.py` | 4086 行（本轮 +396）：`format_hms`、`ResumeDialog`、`_init_resume_state`、五个 `_*_resume` / `_settle_current_position`，以及 `open_video` / `on_close` / `update_frame` / `on_conversion_done` 四处接入 |
| `tests/test_resume.py` | **新增**（376 行，9 节 60 项） |
| `tests/_harness.py` | `make_resume_path()`、`load_app(resume_enabled=)`、`teardown` 补取消挂起 after |
| `run_tests.py` | 语法检查加 `resume_store.py`；套件清单加"记忆播放位置" |
| `docs/final_player_第17轮_记忆播放位置.md` | 本文 |

### 实测数字

| 项目 | 结果 |
|---|---|
| `run_tests.py --full`（干净环境 + 重定向） | 14 组全 OK、退出码 0、362.6s |
| 记忆播放位置组 | **60 PASS / 0 FAIL**，11.4s |
| 续播精度（请求 8.0 秒） | 起播 8.04（差一帧，因为 `POS_FRAMES` 读的是"下一帧序号"） |
| 静默续播（请求 7.0 秒） | 起播 7.04 |
| "从头播放" 分支 | 位置回到 0.04，记录同步清掉 |
| "取消" 分支 | 返回 False，当前视频的路径/状态/位置**分毫未动** |

---

## 10. 留给后来人

- **改 `_settle_current_position` 之前先读它的 docstring**：它不碰任何播放状态
  （只读位置 + 写记录表），正是这一点让它可以安全地放在 `open_video` 最开头 ——
  那里用户随时可能点"取消"。
- **加新的"打开视频"入口时，想一下 `resume` / `ask` 该传什么。** 默认值是
  "问一句"，对用户手点的入口是对的；对任何**自动**入口（连播、将来可能有的
  播放全部/定时播放）都应该传 `ask=False`，否则弹窗会把自动流程卡住。
- **别在播放过程中写盘。** 有意只在"离开那一刻"结算。想加"崩溃也能记住进度"
  的话，得另设计一个远低频的检查点，不要每帧写。
- `resume_enabled=False` 是老套件的安全阀，**新增套件时保持默认关**，
  只有确实在测这个功能的套件才打开。
