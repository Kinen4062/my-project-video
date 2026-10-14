# final_player 第 15 轮 · 全屏隐藏列表栏 + 切换视频卡顿修复

> 日期：2026-10-14
> 起点提交：`993aaf7`（第 13 轮过程记录）
> 本轮提交：见文末
> 验证：`run_tests.py --full` → 数据见「五、验证与规模」
> 截图：`docs/final_player_第15轮_全屏隐藏列表栏.png`

---

## 一、两个需求，一个共同点

用户原话：

> 右侧的视频播放列表也需要像底部的控件一样，在全屏状态下会隐藏而且能够唤出。
> 然后，似乎多个视频多次点击会发生卡顿，你检查一下，如果存在此问题，解决它。

两件事看起来无关，其实共享同一个前提：**第 14 轮新加的东西，都没有接进既有的机制里。**

| | 第 14 轮做了什么 | 漏了什么 |
|---|---|---|
| 列表栏 | 一个能显隐的控件（自己的 `toggle_playlist`） | 没接进"全屏自动隐藏"那套（`_hide_controls` / `_show_controls`） |
| 播放列表的"播放"按钮 | 调用了 `open_video()` | `open_video` 里那句**同步** `close_player()` 从来没被质疑过 —— 直到它被高频调用 |

所以这一轮的工作可以概括成：**把新东西接进旧机制，并把新暴露出来的旧问题修掉。**

---

## 二、任务 1：全屏时隐藏列表栏（并能唤出）

### 问题：一个布尔量被当成两件事用

原来 `toggle_playlist()` 直接操作 grid，显隐判断只有一个条件 —— `_playlist_visible`。
而全屏自动隐藏走的是另一条完全独立的路径（`_hide_controls` 里逐个 `grid_remove`）。

于是全屏下鼠标停 2 秒：底栏收起来了，**右边那条 236px 的列表栏还在** ——
"全屏"等于白叫。

### 改法：把"要不要显示"拆成两个条件

```python
def _sync_playlist_visibility(self):
    if self._playlist_visible and self._controls_visible:
        self.playlist_frame.grid()
        self.root.grid_columnconfigure(1, minsize=PLIST_MIN_WIDTH)
    else:
        self.playlist_frame.grid_remove()
        self.root.grid_columnconfigure(1, minsize=0)
```

- `_playlist_visible` —— **用户的意愿**（点"隐藏列表"按钮切换的那个开关）；
- `_controls_visible` —— **这一刻该不该显示控件**（全屏自动隐藏期间为 False）。

这两个语义必须分开。合成一个布尔量的话，"鼠标一晃"就会把用户主动关掉的列表
又拉出来 —— 那是另一种 bug。

这个写法其实是抄了旁边 `_sync_parallax_visibility()` 的现成模式（那里是
"2D 模式 或 控件隐藏期间 → 不显示视差栏"），保持同一套风格。

### 三个接入点

| 位置 | 加什么 |
|---|---|
| `_hide_controls()` | 设完 `_controls_visible = False` 后调一次 → 列表跟着收起 |
| `_show_controls()` | 设完 `_controls_visible = True` 后调一次 → 列表跟着回来 |
| `toggle_playlist()` | 只翻转 `_playlist_visible` + 换按钮文字，然后调它 |

`toggle_playlist` 改成这样之后**短了一大截**：grid 操作与 minsize 归零只留在
`_sync_playlist_visibility` 一处。原来自成一套的时候，那一步（minsize 归零）
是容易漏的 —— 少了它，`grid_remove` 之后视频区右边照样留一条空带。

### 截图时踩到的坑（值得记一笔）

第一次截出来的两张图**一模一样**，都是纯画面。排查下来是两个叠加的原因：

1. `PrintWindow` 只重绘目标窗口自身的绘制，**Tk 的子 HWND 控件不会被画进去**
   （底栏、列表栏都抓不到）→ 改用抓"窗口所在的屏幕区域"。
2. 更隐蔽的一个：`_show_controls()` **不重置**那个 2 秒的自动隐藏计时器 ——
   重置计时是 `_on_mouse_move` / `_schedule_hide_controls` 的职责。
   脚本里"手动显示 → 等 2 秒 → 抓图"，抓到的其实又被收回去了。

第 2 条**不是 bug**（真实交互里鼠标一动就会 `_schedule_hide_controls`），
但写自动化脚本很容易踩。

教训：**别靠肉眼判断"这张图应该是那个状态"**。截图脚本里同时打印
`_controls_visible` / `winfo_ismapped()` / 第 1 列 minsize 三个值，
它们才是"图里应该看到什么"的依据 —— 否则两张一样的图会让你以为功能没生效。

---

## 三、任务 2：切换视频卡顿 —— 先测，别猜

用户说的是"似乎"，所以第一步不是改代码，是**量**。

### 探针：给每一段打时间戳

思路：把 `open_video` 中间隔调用的几个方法包一层计时 wrapper，然后连续切换
列表里的多个视频（其中 city.mp4 有音轨，短片无音轨），打印各段耗时。

第一次实测结果（切换 8 次，每次等 0.5s）：

```
  _stop_playback_loop    n= 8  中位    0.0  最大    0.0
  _cancel_audio_handoff  n= 8  中位    0.0  最大    0.0
  _close_audio_player    n= 8  中位    0.1  最大  467.7     ← 就是它
  _create_audio_player   n= 8  中位    0.2  最大    0.3
  open_video             n= 8  中位   34.0  最大  500.2
```

单次切换的耗时列表：446 / 40 / 34 / 29 / 500 / 32 / 31 / 27 ms —— **只有"上一个
视频有音轨且真的在播"时才慢**，因为 `close_player()` 要等音频读线程退出。

不等稳定、连续猛点 8 次（更接近"手快点"）：总耗时 **1565ms**，其中
`_close_audio_player` 合计 **1304ms —— 占 83%**。

> **结论：卡顿 100% 来自 `open_video` 里那句同步 `_close_audio_player()`。**
> 这与本文件顶部早就记录过的实测完全一致（`close_player()` ≈ 400~580ms），
> 只是那条经验当初只被用在"拖动进度条"的路径上，换文件这条路径漏了。

### 为什么不能直接删掉它

因为**顺序有硬约束**：`_apply_volume_now()` 的注释里记着一组实测：

```
关掉旧播放器 → 建新 → 立刻 set_volume      → 4/4 段错误（读 NULL+0x18）
关掉旧播放器 → 建新 → 等 0.2s / 0.6s 再调  → 4/4 安全
两个播放器并存（谁都没关）+ 建好立刻调      → 4/4 安全
```

原因是 SDL_mixer 在**最后一个占着通道的播放器被关掉时**会
`Mix_CloseAudio()` 销毁混音器，此后 `Mix_Volume` 就是踩 NULL。
所以"先建后关"是结构性的要求，不能改成"先关后建"，也不能靠"等 0.2 秒"这种
时间猜测。

于是改法只能是：**保留顺序，但把"关"这一步挪到主线程之外。**

### 改法：把"换文件"也接进已有的后台交接机制

原来（阻塞）：

```
_cancel_audio_handoff() → _close_audio_player()   ← 等 0.44~0.81 秒
                        → 一堆状态重置
                        → 建新播放器
```

现在（不阻塞）：

```
_cancel_audio_handoff(join=False)   → old = _detach_audio_player()   ← 只止声，不等
                                    → 一堆状态重置
                                    → 建新播放器（此刻旧实例还活着 ⇒ 混音器有效）
                                    → _retire_audio_player(old)       ← 交给机制，不等
```

三个新方法，各管一件事：

| 方法 | 做什么 | 为什么这么切 |
|---|---|---|
| `_detach_audio_player()` | 摘下引用 + `set_pause(True)` 止声 + `_mark_audio_live()`，**返回旧实例但不关** | 摘下来是便宜的；关它是贵的。止声不能省 —— 旧实例还要活几百毫秒，不按住就会和新视频的声音叠在一起 |
| `_retire_audio_player(old)` | 把旧实例登记进 `_audio_handoff_old`，交给既有的"等确认再关"流程 | **复用**已有机制，不另造一套状态 |
| `_spawn_audio_retire(old)` | 起 daemon 线程关它 + 登记到 `_audio_retire_threads` | 原来 `_cancel_audio_handoff` 和 `_poll_audio_handoff_ready` 各自起一遍线程，现在收敛成一个出口 |

旧的实例随后由**已有的** `update_frame` 每帧检查走完剩下的路：

```
_poll_audio_handoff_ready()   ← 每帧问一次"新播放器的音频确认在跑了吗"
    → 确认（或确认无音轨 / 超时兜底）
    → _spawn_audio_retire(old)      # 这时候才真正关，0.5 秒都在后台
```

这里最关键的一点是**"等确认再关"不是多余的小心**：它正是"混音器全程不被销毁"
的保证。旧实例多活那两三百毫秒，换来的是新播放器 `set_volume` 一定安全。

顺带得到两个本来就有的一致行为（不用额外写代码）：

- 这期间墙钟接管画面（`_audio_handoff_pending`）—— 换文件后画面严格按实时走；
- 交接期间来的跳转请求会被记住并补上（`_pending_audio_pos` 那条兜底）。

### 还有第二个"白等"

`_cancel_audio_handoff()` 原来是 `join(timeout)` —— 等退场线程结束。
关窗时**需要**这个等待（进程要退出，不能让线程还在拆 SDL 设备）。
但换文件时不需要：那个线程关的实例，主线程早就放手了，等它只是白卡；
而连续换文件时这个"白等"会一次叠一次。

所以加了个参数：

```python
def _cancel_audio_handoff(self, timeout=2.0, join=True):
    ...
    if join:
        self._join_audio_retire(timeout)
```

换文件传 `join=False`，关窗保持默认 `True`。

`_join_audio_retire(timeout)` 的预算是**总时长**而不是"每个线程各等一次" ——
连续换了好几次文件时可能有多个退场线程在飞，不能让等待时间随次数线性增长。

---

## 四、验证：速度只是及格线，正确性才是

改完先量速度（切换 8 次，与上面同一场景）：

| | 修复前 | 修复后 |
|---|---|---|
| 单次切换最大耗时 | **500ms**（其中 close 467ms） | **39ms** |
| 连续猛点 8 次总耗时 | **1565ms** | **261ms**（平均 33ms/次） |
| `_cancel_audio_handoff` | 0~2ms | 1.2~2.1ms（不 join 了） |

但"变快了"不能作为完成的理由，必须验证没弄坏东西。写了第二个探针，四层判据：

**① 旧实例有没有被漏关？** 统计 `_detach_audio_player()` 返回非 None 的次数
与被后台关掉的次数：**摘下 36 次 = 关掉 36 次**，一个不漏、也不重复关。

> 踩过的小坑：第一版用 `id(instance)` 去重来判断"有没有漏"，结果假失败 ——
> 对象回收后 `id` 会被复用，而且第一次 `open_video` 时本来就没有旧播放器
> （`None` 也被我计进去了）。**改用次数比对**才对。

**② 切换后音频还活着吗？** 切回有音轨文件后 `_audio_calibrated` 为 True
（说明新播放器的时钟真的在走 = 音频通道已建立、正在出声）。

**③ 音量还发得下去吗？** 把音量设成 0.5，用混音通道探针读真实值 ——
切来切去之后通道值始终是 64（0.5 × 128）。这一条最关键，因为
"先建后关"要保护的正是 `set_volume` 的安全性。

**④ 极端情况会不会崩？** 极快连点 20 次（间隔 30ms，比"等新音频确认"快得多）：
线程不堆积、实例不泄漏、切回有音轨文件后仍能出声。全程没有段错误。

### 一个如实的发现

极快连点（30ms 间隔）时，**未确认的旧实例会被 `_cancel_audio_handoff` 提前
交出去关**，于是混音器可能被拆装一次。这不是 bug，也不会崩 ——

- 那一刻不会调 `set_volume`（判据是"新实例的时钟已校准"，而新实例刚建、还没校准，
  下发会被推迟）；
- 换到有音轨文件后混音器会重新建立（④ 已验证）。

**真正要守住的是"有音轨文件之间互切"这条路**（现实里最常见：列表里都是正常视频）：
两个有音轨文件互切 8 次，混音器全程有效、音量始终正确 —— 因为这条路上
确认来得及，"等确认再关"全程生效。

### 反证：这些用例真的能抓住 bug 吗

一个用例必须能抓住它要防的 bug 才算有用。所以把行为改回"同步 close"再跑一遍
同样的判据：

```
【模拟修复前：同步 close_player】
  每次切换的同步耗时: 458 / 563 / 613 / 561 ms
  其中 close_player 本身: 425 / 535 / 586 / 533 ms
```

⑪ 节的判据是 `max < 150ms` → 这一轮会 **RED** ✅ 用例有效。

（顺带说明为什么阈值取 150ms：修复后实测最大 39ms，修复前最小 446ms，
中间留了一倍以上的余量给 CI 机器的抖动。）

---

## 五、验证与规模

```
run_tests.py --full（干净环境 env -u PYTHONIOENCODING -u PYTHONUTF8 + 输出重定向）
→ 12 组全 OK、273 项断言、0 FAIL、退出码 0、332.1s
```

（断言分布：功能用例 40 / 定时器与挂起请求 23 / 快捷键与逐帧步进 86 /
**播放列表与拖放 71（本轮 54 → 71，新增 17 项）** / 转换补音轨 15 /
稳态速度 3 / 乱序压力 seed=1..5 各 7。）

| | 改动前 | 改动后 |
|---|---|---|
| `final_player.py` | 3399 行 | **3556 行**（+157） |
| `tests/test_playlist.py` | 336 行 | **439 行**（+103） |
| 播放列表组的断言 | 54 项 | **71 项** |
| diff | — | `2 files changed, 311 insertions(+), 51 deletions(-)` |

那 51 行删除里，大部分是 `toggle_playlist` 的旧实现（被 `_sync_playlist_visibility`
取代）和两处"各自起一遍线程"的重复代码（收敛到 `_spawn_audio_retire`）。

---

## 六、改动清单（想看代码按这个顺序）

**任务 1（全屏隐藏列表栏）**

1. `_sync_playlist_visibility()` —— 新增，两个条件决定显隐（照 `_sync_parallax_visibility` 的写法）；
2. `_hide_controls()` / `_show_controls()` —— 各加一次调用；
3. `toggle_playlist()` —— 改成只翻转用户意愿，grid 操作全部交给上面那个方法。

**任务 2（切换不卡顿）**

4. `_detach_audio_player()` —— 摘下 + 止声（不阻塞）；
5. `_retire_audio_player(old)` —— 登记进 `_audio_handoff_old`，等确认后后台关；
6. `_spawn_audio_retire(old)` / `_join_audio_retire(timeout)` —— 线程的统一出口与有界等待；
7. `_cancel_audio_handoff(timeout, join=True)` —— 新增 `join` 参数，换文件走 `False`；
8. `_poll_audio_handoff_ready()` —— 起线程改用 `_spawn_audio_retire`；
9. `open_video()` —— 那个接缝：`_cancel_audio_handoff(join=False)` +
   `old_audio = self._detach_audio_player()` … `self._retire_audio_player(old_audio)`。

**测试**：`tests/test_playlist.py` 的 ⑨ 节扩写（全屏收/唤 + 用户意愿不被覆盖）、
新增 ⑪ 节（速度 + 混音器 + 账目 + 极端连点）。
