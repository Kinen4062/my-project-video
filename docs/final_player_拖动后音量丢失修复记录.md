# final_player.py — 拖动进度条后音量丢失 修复记录（第 10 轮）

> 对应你的原话：**"现在声音修改后，如果我拖动了进度条，重新播放的视频声音大小会变，
> 没有保持拖动前那个声音大小，但是音量大小条是在原位置的。"**
>
> 修改文件：`final_player.py`
> 行数：2261 → **2433**
> 本节结论：**已修复并全量回归通过**（37 项功能用例 0 失败 / 5 个种子随机压力测试 0 崩溃 / 三素材稳态速度 1.000x）。

---

## 1. 现象与定位

| 环节 | 表现 |
| --- | --- |
| 拖动前 | 音量滑块在 30%，实际声音也是 30% |
| 拖动后 | **声音变成 100%（满音量）**，而滑块仍然停在 30% |
| 关键特征 | 声音"变大"而不是"变小"或"消失" → 新播放器根本没收到音量值，按默认满音量在放 |

用 ctypes 直接读 SDL2_mixer 的**每通道音量**（`Mix_Volume(ch, -1)`，128 = 满音量）复现：

```
① 起播后（音量还没动过）      非默认音量的通道: {}
② 把音量调到 0.3 之后          非默认音量的通道: {0: 38}        <- 期望 ≈38
③ 拖动进度条之后（新播放器）   非默认音量的通道: {}   ← 修复前：音量被丢掉了
                               （修复后：{0: 38, 1: 38}）
```

---

## 2. 根因：两个独立的事实叠在一起

### 根因 A — `ff_opts['volume']` 在本构建里**不进混音**

本构建的 ffpyplayer 编入了 **SDL2_mixer**（`USE_SDL2_MIXER`，
库文件 `.venv/share/ffpyplayer/sdl/bin/SDL2_mixer.dll`）。

- 音频实际响度 = **每个混音通道的 volume**，只有 `Mix_Volume(通道, 音量)` 能改它；
- ffpyplayer 只在 **`set_volume()`** 里调用 `Mix_Volume`（`player.pyx`）；
- 构造参数 `ff_opts['volume']` **只写进一个不参与混音的字段**
  `settings.audio_volume`（`player.pyx:349`），**全工程只有 `get_volume()` 会读它**。

实测（`probe_mvol.py`）：

```
MediaPlayer(city.mp4, ff_opts={'volume': 0.3}) 建好后
  get_volume() 报 0.296875          <- 撒谎：它读的是那个不参与混音的字段
  混音通道全是 128（满音量）        <- 真实情况
调过 set_volume(0.3) 之后，通道才变成 38
```

> **结论：任何新建的播放器都必须补一次 `set_volume()`，否则它一定按满音量播。**
> 拖动会重建播放器 → 新播放器没补 `set_volume` → 满音量出声，而滑块位置由
> `self.audio_volume` 单独保存，所以纹丝不动。**这就是你看到的现象。**

### 根因 B — 补 `set_volume` 会段错误，且**不能用"等一会儿"绕开**

`set_volume()` 在"没有真实音轨"的播放器上会 **SIGSEGV**（access violation，
Python 的 `except` 拦不住）。机制已经用 ctypes 定位到具体现场：

```
stream_component_close（core.pyx:1860-1881）关闭最后一个音频流时执行
    Mix_UnregisterEffect → Mix_HaltChannel → Mix_FreeChunk → audio_count -= 1
    if not audio_count: Mix_CloseAudio()          # ← 混音器被整个销毁
此后 mix_channel 变 NULL，再 Mix_Volume(mix_channel[i]) 就是读 NULL + 0x18
（0x18 正是 struct 里 volume 字段的偏移）
→ OSError: access violation reading 0x0000000000000018
```

二分实验（`probe_mvol2.py`，每个变体跑 4 轮）：

| 场景 | 结果 |
| --- | --- |
| 两个播放器并存（全程没关过谁）+ 建好立刻 `set_volume` | **4/4 安全** |
| 关掉旧播放器 → 建新 → **立刻** `set_volume` | **4/4 段错误** |
| 关掉旧播放器 → 建新 → 等 0.2s / 0.6s 再 `set_volume` | 4/4 安全 |
| 关旧的动作在主线程还是工作线程做 | 结果一样 |

也就是说崩溃条件是：**"旧播放器被关掉 ⇒ 混音器被销毁"与"新播放器重新打开混音器"
之间有一段空档**，任何落在这段空档里的 `set_volume` 都会崩。

> **所以不能用"等 0.2 秒"这种时间猜测**：机器一忙、文件一大，读线程 0.2 秒内
> 开不出混音器，照样崩。**判据也不能是"这个文件有没有音轨"** —— 文件有音轨
> 不代表这个**新实例**的混音器已经就绪（实测立刻调同样 100% 崩）。

---

## 3. 修复设计

核心思路：**在结构上不让混音器被销毁**，从而让"给新播放器补音量"这件事永远安全。

### 3.1 后台音频交接改成"先建后关"三步走

原来的顺序是 **关旧 → 建新**（中间混音器必然空档）。改成：

```
① 先按住旧的          old.set_pause(True)
                      —— 它此刻还在主线程手里，动它是安全的，且很便宜（不阻塞）
                      —— 不这么做的话，在它被真正关掉之前会和新声音叠在一起

② 主线程先建新的       MediaPlayer(...)  ← 只要 1.6ms，不可见
                      ss 用**现在**的视频位置（self._last_media_sec）
                      此刻旧播放器仍占着一条混音通道
                      ⇒ SDL 混音器必然有效 ⇒ set_volume 安全
                      ⇒ 音量能在新播放器**出声之前**就位

③ 等新播放器音频真的在跑，再把旧的交给 daemon 工作线程关掉
                      —— 整个交接期间混音器都不会被销毁
                      —— 连带的收益：声音回来快了约 0.5 秒
                         （不用等混音器被销毁再重开）
```

**"抢发窗口"（`_audio_volume_resume`）**就是第 ②~③ 步之间那段窗口：混音器一定有效，
所以允许立刻、反复调 `set_volume`。窗口关闭的时刻 = 旧播放器被交出去关的时刻
（之后混音器可能出现空档，绝不能再调）。

三处同类阻塞（切倍速 / 片尾回绕 / 暂停态按继续）也一起走了这条非阻塞路径。

### 3.2 判据收敛到一个函数 `_apply_volume_now()`

只有**两者之一**成立时才真正下发 `set_volume()`：

1. `_audio_calibrated` —— 这个**实例**的音频时钟被观测到真的在走
   （说明混音器已重新工作、音频回调在跑）；
2. `_audio_volume_resume` —— 抢发窗口开着（旧播放器还活着占着通道）。

两者都不成立就只登记，隔 `AUDIO_VOLUME_SYNC_DELAY = 0.4s` 重试；
一旦确认这个文件**没有音轨**（`audio_failed` 或时钟判死），**彻底放弃**
—— 那里没声音可调，调了就是踩已释放内存。

### 3.3 抢发用 10ms 独立定时器，不搭帧的顺风车

第一版把"每帧试一次"写在 `update_frame()` 里。实测下来出声（+40ms）到音量到位
（+88ms）之间还差 ~45ms，**正好是 25fps 的一帧间隔** —— 音频通道就绪的时刻
落在两帧中间，那一帧就整帧错过了。

改为独立定时器 `AUDIO_VOLUME_RESUME_POLL_MS = 10`（比一帧细 4 倍），
窗口一关就停：

```python
def _start_volume_resume_poll(self): ...    # _begin_audio_handoff 里开
def _on_volume_resume_tick(self): ...       # 每 10ms 试一次，幂等
def _stop_volume_resume_poll(self): ...     # 交接就绪 / 换文件 / 关窗 时停
```

该定时器 ID 已加入 `on_close()` 的取消列表，并且有专门的用例
（`check_timer.py`）验证它**不泄漏**。

---

## 4. 实测时间轴（`measure_out.py`，直接读混音通道）

口径：ctypes 每 3ms 采一次 `Mix_Volume(ch,-1)` 与 `Mix_Playing(ch)`，
记录"通道 1 开始出声"和"通道 1 音量变成用户值"两个事件相对**松手**的时刻。
用户设的音量 0.3 → 通道值 38。

| 版本 | 开始出声 | 音量到位 | **满音量暴露窗口** |
| --- | --- | --- | --- |
| 修复前（第 9 轮） | +40 ms | +88 ms | **~45 ms**（听得见的"先响一下大的"） |
| **修复后 · 第 1 次** | +40 ms | +40 ms（同时） | **0 ms** |
| **修复后 · 第 2 次** | +41 ms | +52 ms | **11 ms** |
| **修复后 · 第 3 次** | +41 ms | +41 ms（同时） | **0 ms** |

另：旧播放器通道 0 在 +402~412ms 停止出声（工作线程把旧实例关掉），
整个过程中**没有任何一次"混音器被销毁"事件** —— 这正是 `set_volume` 得以安全的原因。

---

## 5. 改动清单

### 新增常量（文件顶部）

```python
AUDIO_HANDOFF_POLL_MS = 30          # 后台音频交接的取回轮询间隔
AUDIO_HANDOFF_KEEPALIVE_MAX = 3.0   # 最多让旧播放器多活这么久（等新播放器音频就绪）
AUDIO_VOLUME_RESUME_POLL_MS = 10    # 抢发窗口里的音量重试间隔
AUDIO_VOLUME_SYNC_DELAY = 0.4       # 还没确认"音频真的在跑"时，隔多久重试一次音量
AUDIO_CLOCK_REWIND_EPS = 0.01
```

### 新增 / 改写的方法

| 方法 | 说明 |
| --- | --- |
| `_begin_audio_handoff()` | 三步交接（先建后关），开抢发窗口 + 启动 10ms 重试定时器 |
| `_poll_audio_handoff_ready()` | 主线程每帧问一次"新播放器音频确认在跑吗"，确认了才把旧的交出去关 |
| `_audio_handoff_worker(old)` | 工作线程：**只负责 `close_player()`**（这一步固定 400~580ms） |
| `_start/_on/_stop_volume_resume_poll()` | 抢发窗口的 10ms 音量重试定时器 |
| `_apply_volume_now()` | 音量下发的**唯一出口**，含完整的判据与实测记录（docstring 很长，是特意留的证据） |
| `_cancel_audio_handoff()` | 换文件 / 关窗时作废并等待在飞的交接（同时收回 `_audio_handoff_old`） |
| `update_frame()` | 开头只做交接收尾（`_poll_audio_handoff_ready`）与"推迟音量"的到点应用 |
| `on_close()` | 取消列表加上 `_audio_volume_resume_timer` |

### 顶部注释

已重写为"三步交接 + 混音器销毁机制"的说明，`_apply_volume_now` 的 docstring 里
保留了 ctypes 实证与二分结果，避免以后有人把"先建后关"改回"先关后建"。

---

## 6. 验证（全部实跑，非推断）

### 6.1 功能用例 `verify2.py` — **37 PASS / 0 FAIL**

| 分组 | 覆盖点 |
| --- | --- |
| 拖动后音量保持（**你的 bug**） | 混音器没被销毁 / 新播放器通道值 = 用户值 / 无 >200ms 卡顿（帧间隔中位 47ms、最大 63ms）/ 音频已交接回来 |
| 拖动后静音保持 | 静音后所有发声通道都是 0 / 拖动后仍静音 / 取消静音回到 0.5 |
| 连续快速拖动 6 次 | 全程无 >200ms 卡顿 / 没堆积出多个并行交接 / 结束时音量仍是 0.4 |
| 暂停态拖动 → 恢复播放 | 按继续立刻开播（速度 0.995x）/ 恢复后音量 0.35 |
| 切倍速 1x→2x→1x | 无 >200ms 卡顿 / 音量两处都保持 0.45 |
| 片尾回绕 | 回绕处不卡（原来每圈卡 0.5s）/ 回绕后音量 0.3 |
| 无音轨 3D 产物 | 被判定为无音频 / 拖动+改音量+静音都不崩 / **不会被误调 set_volume** |
| 交接中改音量 / 静音（历史崩溃点） | 不崩 / 静音生效 / 取消静音后新播放器 0.25 |
| 交接中换文件 | 不崩 / state 干净 / 能正常播 |
| 冷启动后**立刻**拖动 | 抢发窗口来不及开也仍能把音量补上 |

### 6.2 随机交错压力测试 `chaos2.py` — **5 个种子（各 80 轮）全部 PASS**

拖动 / 倍速 / 暂停 / 音量 / 静音 / 换文件随机混合，判据"不崩 + 播放中操作最大间隔 ≤400ms + 结束时无残留"：

```
seed 1  125 次操作  seek 51 speed 18 pause 14 volume 14 mute 16 open 12   PASS
seed 2  118 次操作  seek 47 speed 11 pause 13 volume 17 mute 16 open 14   PASS
seed 3  125 次操作  seek 47 speed 14 pause 15 volume 13 mute 26 open 10   PASS
seed 4  124 次操作  seek 49 speed 19 pause 14 volume 15 mute 12 open 15   PASS
seed 5  120 次操作  seek 44 speed 10 pause 22 volume 17 mute 15 open 12   PASS
```

各操作最大间隔里唯一的 >400ms 全部出现在 `open`（装新文件，判据已排除），
其余 `pause/seek/speed/volume/mute` 最大 296~328ms。**零崩溃。**

### 6.3 定时器泄漏检查 `check_timer.py` — **0 FAIL**

播放态拖动 / 暂停态拖动 / 交接中换文件 / 关窗，四条路径都确认
`_audio_volume_resume_timer is None`（不泄漏）、抢发窗口已关、无待关旧实例，
`root.destroy()` 之后 `update()` 不抛 `invalid command name`。

### 6.4 稳态播放速度 `speed3.py`

> 这是第 7 轮的教训：**每次改动后都必须拿"程序自己转换出来的文件"测一遍**
> —— 当时正是因为端到端只测了有音轨的 `city.mp4`，把"转换产物播放变慢"的
> 回归漏掉了。

```
PASS  city.mp4                1.000x  (25fps, 20s)
PASS  output_3d_video.mp4     1.020x  (25fps,  4s)
PASS  output_bad_3d.mp4       1.000x  (25fps, 20s)
```

（`output_3d_video.mp4` 只有 100 帧，早先的测量脚本用固定 4 秒窗口会穿过片尾
回绕而误报 0.010x；已改成按片长自适应窗口。）

### 6.5 静态检查

`python -m py_compile final_player.py` → OK，2433 行。

---

## 7. 已知边界与遗留

- 交接期间（约 0.4 秒）走墙钟主时钟、画面略早于新音频起跑，然后软对齐；
  这段时间**旧播放器被按住静音**，所以是"静音 ~0.4 秒"而不是"响一下大的"
  —— 拖动时音频本来就被压住，实测无感。
- 交接期间如果用户把视频**暂停**了，音频时钟不会走，此时由
  `AUDIO_HANDOFF_KEEPALIVE_MAX = 3.0s` 兜底把旧实例交出去关掉（否则旧播放器
  会一直留着）。
- `_request_audio_restart` 的 `SEEK_DEBOUNCE_MS = 500` 未变；拖动松手后仍会有
  一次 94~125ms 的"跳转落地"（`cap.set` + 解码目标帧），那是换位置本身的成本。
- **待你确认的历史项**（此前几轮问过、还没定）：
  1. 暂停态拖动要不要"实时预览"（现在是停手 500ms 或松手才刷新）；
  2. `test_videos/output_bad_3d.mp4`（47MB，9 月旧产物）是否删掉；
  3. 现有备份 `test_videos/final_player.py.bak` 是 **11:03 的 P2 之前版本**，
     本轮之后所有改动都没有回滚点 —— 是否复制一份当前版本做新备份。
     **建议做**（项目不是 git 仓库）。

---

## 8. 回滚方法

改动集中在"后台音频交接"与音量下发两处：

1. 把 `_begin_audio_handoff` 恢复成"先 `close_player()` 旧的、再建新的"；
2. 删掉抢发窗口相关的三个方法、`AUDIO_VOLUME_RESUME_POLL_MS`、
   `_audio_volume_resume` / `_audio_volume_resume_timer` 两个字段，
   以及 `on_close` 取消列表里的 `_audio_volume_resume_timer`；
3. `_apply_volume_now` 的判据退回"只认 `_audio_calibrated`"。

⚠️ 注意：只做第 3 步是**不够**的（音量还是会丢），因为根因 A 说明"新建播放器必须
补 `set_volume`"；而只做第 1 步会退回第 9 轮的"拖一下卡一下"。
**这两件事是绑在一起的** —— 这也是为什么这里的 docstring 写得特别长。
