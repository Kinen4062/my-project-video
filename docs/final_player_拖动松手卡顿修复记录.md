# final_player.py — 拖动进度条松手后卡顿的修复记录

> 目标问题（用户原话）：“现在拖动进度条后有明显的卡顿，然后视频才进行播放，怎么解决这个卡顿问题，或者说让人不容易一眼看出问题”
>
> 结论：**不是“掩饰”，是彻底解决**。实测把松手后那次 609ms 的冻结合并到 110ms 的“跳转落地解码”里，落地后全程 47ms 一帧、>200ms 卡顿 0 次。
> 过程中还借压力测试揪出并修掉了一个**约 25%~100% 概率复现的段错误**（`set_volume` 在无音轨播放器上会崩）。

---

## 一、问题现象与定位

### 现象
拖进度条松手后，画面先正常播约 0.25 秒，**然后冻住约 0.6 秒**，之后才继续。

### 实测时间线（`diag_lag.py`，city.mp4，拖 20% → 60%）

| 时刻 | 发生了什么 |
|---|---|
| t=250ms | 松手 |
| — | `finish_seek()` → `cap.set()` 78ms |
| t=344ms | 画面跳到新位置，开始正常播 |
| t=578ms | 音频重建防抖到期 → `_restart_audio_at()` |
| — | **`_close_audio_player()` 在主线程同步阻塞 547ms** |
| t=1141ms | 才继续播 |

逐帧看就是：帧 307 →（间隔 **563ms**）→ 帧 308。

### 根因
本构建的 `MediaPlayer.close_player()` **固定要 400~580ms**（等它内部读线程退出；与已播时长无关，清空队列、先 `set_pause`/`set_volume` 都无效）。
而它原本是在**主线程**里同步执行的 —— 所以“拖一下就卡一下”。

---

## 二、修复方案：后台音频交接（background audio handoff）

把“关旧播放器”那 0.5 秒移出主线程，拆成两半、由线程和轮询自然隔开：

```
主线程                                             工作线程
  │                                                   │
  ├─ _begin_audio_handoff(pos)                        │
  │    old = self.audio_player                        │
  │    self.audio_player = None      ← 交出去         │
  │    self._audio_handoff_pending = True             │
  │    thread.start() ────────────────────────────────┤ close_player()  ← ~0.5s
  │    return（立刻返回，不阻塞）                      │
  │                                                   │
  ├─ _poll_audio_handoff() 每 30ms 一次 ……            │
  │    线程结束了吗？没有 → 再等 30ms                  │
  │    结束了 ↓                                        │
  ├─ self.audio_player = _create_audio_player(        │
  │       当前文件, self._last_media_sec)  ← 1.6ms     │
  └─ self._audio_handoff_pending = False
```

关键纪律与配套改动：

- **交接期间 `self.audio_player` 必须是 `None`**，主线程绝不触碰那个正在被关闭的实例。
- **建新播放器放主线程**，用“此刻的视频位置”（每帧发布的 `_last_media_sec`）做 `ss` —— 交接那 0.5 秒画面还在往前走，用请求时的旧位置会让音频落后半秒、口型对不上。
- 交接期间没有音频时钟，**改由墙钟主时钟驱动画面**（`_silent_clock_media_seconds()` 新增 `_audio_handoff_pending` 条件），画面照常按实时速度播、可丢帧追赶。
- 鼠标已松开就**立刻开工**，不再等 250ms 音频重建防抖 —— 声音反而比原来早约 0.25 秒回来。

顺带一起解决的三处同类阻塞（原来都是同步重建）：
1. **切换倍速**（`change_speed` → `_restart_audio_at`）
2. **片尾回绕**（原来每循环一圈卡 0.5 秒）
3. **暂停态按“继续”**（`_flush_pending_audio_restart`）

---

## 三、过程中发现并修掉的两个段错误

### 1. 早先的“关旧 + 建新同一个工作线程”
反复 `access violation`。改成“**关旧在后台线程、建新在主线程**”后消失，且两个动作之间天然隔着一次 30ms 轮询。

### 2. 【本次压测新发现】`set_volume()` 在无真实音轨的播放器上会崩

乱序压力测试（随机交错拖动/倍速/暂停/音量/静音/换文件）连续 3 次都在同一行崩：
`update_frame → _apply_volume_now → set_volume`，退出码 139。

用 `probe_vol2.py` 做变量二分，结论如下（每个变体各跑一遍）：

| 变体 | 结果 |
|---|---|
| 进程里第一个播放器就是**无音轨**文件，0.5s 后调 `set_volume` | OK |
| 先建过**有音轨**的播放器（city.mp4）再建无音轨的，然后调 | **崩** |
| 无音轨播放器建好后反复 `get_pts`，再调 `set_volume` | **崩** |
| 有音轨的（city→city、单个 city）怎么调 | **从不崩** |

即 `set_volume` 只对“确实有音轨、音频线程在跑”的播放器可靠；无音轨时播放线程会退出，之后往它投递音量命令就踩到已释放的内存。
上一版按“播放器太新”设的 0.4 秒闸门**拦不住**这件事（0.45 秒后照样崩）。

**修法**：`_apply_volume_now()` 的判据改成“**音频时钟被观测到真的在走**”（`_audio_calibrated`）—— 它同时蕴含“有音轨”+“音频线程活着”，正是 `set_volume` 需要的条件：

- 满足 → 下发 `set_volume`（有音轨，实测安全）；
- 不满足 → 只登记，由 `update_frame` 每 0.4s 重试。

无音轨文件则永远不会下发（**那里本来也没有声音可以调，跳过是正确行为**）。新播放器的音量本来就在创建时通过 `ff_opts['volume']` 带进去了。

---

## 四、验证结果

### 4.1 直观对比（同一脚本、同一次拖动、同一台机器实测）

见同目录 `final_player_拖动卡顿_修复前后对比.png`。

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 松手时刻 | t=266ms | t=266ms |
| **最大帧间隔** | **609ms** | **110ms** |
| **>200ms 的卡顿** | **1 次** | **0 次** |
| 跳转落地后帧间隔 | —— | 中位 32~46ms，p95 47ms |
| 那 110ms 花在哪 | —— | `cap.set` + 解码目标帧（每次跳转都要付一次） |

### 4.2 自动化回归

| 套件 | 内容 | 结果 |
|---|---|---|
| `verify_handoff.py` | 14 个用例：拖动/连拖/暂停恢复/切倍速/片尾回绕/无音轨/交接中改音量/交接中换文件/交接中关窗/4 个文件的稳态测速 | **40/40 PASS** |
| `verify_pause_seek.py` | 上一轮“暂停态拖动画面正确”回归（显示帧 vs 独立解码同一帧，像素级比对） | **17/17 PASS** |
| `stress_chaos.py` | 随机交错拖动/倍速/暂停/音量/静音/换文件，80 次操作/轮 | **14 轮零崩溃** |
| `probe_seq.py` | 崩溃序列的 5 个变体，各 4 轮 | **零崩溃** |

稳态播放速度（上一轮“播放变慢”事故的把关项，未被破坏）：
city.mp4 **0.990x** / output_3d_video.mp4 **0.976x** / output_bad_3d.mp4 **0.990x** / temp_3d_output.mp4 **0.996x**。

---

## 五、代价与已知边界（如实说明）

1. **交接那约 0.5 秒没有声音。** 原来拖动时音频也是被压住的，所以不是新增损失；且声音比原来早约 0.25 秒回来。
2. **跳转落地要 94~125ms。** 这是 `cap.set` + 解码目标帧的固定开销，每一次跳转都要付一次，属于“换位置”本身的成本，不是卡顿（画面这段时间仍停在旧帧上）。
3. **偶发一次 ~140~156ms 的额外等待**，来自同步逻辑的兜底上限 `FRAME_SYNC_MAX_WAIT_FRAMES = 4`（25fps 下 160ms），属设计值。
4. **交接进行中换文件**时，`_cancel_audio_handoff()` 会 join 工作线程（正常 0.3~0.5s，极端到 2s 上限）。换文件本身要装载新视频，这点等待可接受。
5. **无音轨文件上拖音量滑块不再“实时生效”**（该播放器不会被下发 `set_volume`）；但音量状态照常记录，切到有音轨文件时按新音量创建。

---

## 六、改动清单（`final_player.py`）

**新增常量**：`AUDIO_HANDOFF_POLL_MS`、`AUDIO_CLOCK_REWIND_EPS`、`AUDIO_VOLUME_SYNC_DELAY`

**新增状态**：`_audio_handoff_thread` / `_audio_handoff_pending` / `_audio_handoff_seq` / `_audio_handoff_poll_id` / `_audio_volume_sync_at` / `_last_media_sec`

**新增方法**
- 交接主体：`_begin_audio_handoff`、`_audio_handoff_worker`、`_schedule_audio_handoff_poll`、`_cancel_audio_handoff_poll`、`_poll_audio_handoff`、`_cancel_audio_handoff`
- 播放器构造拆分：`_audio_ff_opts`、`_new_audio_player`、`_create_audio_player`、`_reset_audio_clock_state`
- 音量：`_apply_volume_now`

**修改**
- `_restart_audio_at` / `_request_audio_restart` / `_do_audio_restart` / `_flush_pending_audio_restart` → 全部改为非阻塞交接
- `_audio_clock_media_seconds` → 新增 pts 回跳检测（实测出现过 12.412 → 12.285，不处理会让画面白等约 0.16s）
- `_silent_clock_media_seconds` → 交接期间也走墙钟
- `update_frame` → 到点重试音量 + 每帧发布 `_last_media_sec`；`is_seeking` 轮询链登记 ID 以便取消
- `open_video` → 换文件前先 `_cancel_audio_handoff()`，并归零 `_last_media_sec`
- `on_close` → 取消交接轮询 + 有界等待交接收尾（0.3s）
- `change_volume` / `toggle_mute` → 走 `_apply_volume_now`

---

## 七、遗留 / 未做

- `test_videos/final_player.py.bak` 是 11:03 的版本（P2 清理之前）。此后各轮改动都没有回滚点，本轮改动前由用户自行备份。
- 上一轮遗留项：**暂停态拖动进度条时没有“实时预览”**（现在要停手 500ms 或松手才刷新画面），用户尚未决定是否要做。
- 历史遗留：`test_videos/` 下的 36.9MB 旧 `temp_3d_output.mp4` 是否删除，用户未答。
