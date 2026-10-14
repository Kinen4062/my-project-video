# final_player.py 第 11 条 —— 音画主时钟同步（架构层）修复记录

**日期**：2026-10-13
**改动文件**：`final_player.py`
**对应审查条目**：`final_player_代码审查.md` 第 11 条
**用户授权**："我手动备份了，继续进行下一步吧"（备份为 `test_videos/final_player.py.bak`）

---

## 一、结论先行

**问题**：视频画面靠 `after(1000/fps)` 自力更生"数帧"，音频却由 ffpyplayer 独立线程按真实时钟播放。两者是**两个互不相干的时基**，一旦单帧处理耗时超过帧间隔（3D 模式下几乎必然），视频就会自动掉速，而音频照常前进 —— 误差只增不减，播放越久漂移越大。

**修复**：改用 **音频主时钟（audio master clock）**。每帧读一次 ffpyplayer 的主时钟（本构建默认即音频时钟），反推出"此刻画面应该在第几帧"：

- 视频落后于音频 → **丢帧追赶**（位置仍然正确）；
- 视频超前于音频 → **多等一会儿**。

处理慢时表现为"掉帧"，而不再是"越播越不同步"。

**实测效果**：

| 场景 | 修复前漂移 | 修复后漂移 |
|---|---|---|
| 模拟 3D 每帧慢处理 60ms，播放 12 秒 | **−4.96 s（43.3%）** | **−0.17 s（1.5%）** |
| 连续播放 45 秒（跨 2 次片尾回绕） | 线性累积，不可用 | 恒定 **−0.013 ~ −0.050 s**（均值 −0.034 s） |

---

## 二、实测 ffpyplayer 主时钟语义（本构建）

这几条是本次修复的地基，全部实机验证过，并已写入源码顶部注释，避免以后重复踩坑。

| # | 事实 | 依据 |
|---|---|---|
| 1 | `get_pts()` 返回**主时钟**（默认即音频时钟），**自带 `ss` 偏移**——从第 12 秒起播则 pts 从 12 起算 | 起播实验 |
| 2 | `get_pts()` 的**增量是墙钟速度**：atempo 加快的是"媒体消耗速度"，不是时钟本身。四种速率下斜率都是 ~1.0；2.0x 时 eos 落在 pts≈10，1.0x 时落在 pts≈20 | eos 判别实验 |
| 3 | **新建播放器后立刻调用 `get_pts()` 会段错误（SIGSEGV，退出码 139）**：`delay=0` 两次全崩，`delay≥0.02s` 全正常 | 最小对照实验 |
| 4 | 暂停时 pts 冻结（读它是安全的，但暂停态本来就不推进画面） | — |
| 5 | 无音轨 / 音频初始化失败时主时钟不可用 | 无音轨探针 |

**由 #1 + #2 推出的核心公式**（不能把 pts 当媒体绝对位置用，因为 `ss` 是非精确 seek 会回退到关键帧）：

```
clock_sec = 校准时的视频位置 + (pts − 校准读数) × playback_speed
```

即**只取音频"推进了多少"**，以校准那一刻的视频位置为零点。

**由 #3 推出的防护**：`AUDIO_CLOCK_WARMUP_SEC = 0.25`，播放器创建不足 0.25 秒时不读 pts，本帧回退盲调度。

---

## 三、丢帧追赶的实现方式（也是实测选型）

| 方式 | 实测开销 | 结论 |
|---|---|---|
| `cap.grab()` 顺序跳过 | **≈ 0.3 ms/帧**（线性、可预期） | ✅ 小跨度用这个 |
| `cap.set(POS_FRAMES, n)` | **20 ~ 90 ms/次**（要回到前一关键帧再解码到目标帧，关键帧越稀疏越慢） | 仅大跨度才划算 |

所以设 `FRAME_SYNC_MAX_GRAB = 60`：落后 ≤60 帧用 `grab` 逐个跳过，>60 帧才退回 `set`。

> 附带收益：上一轮（拖动卡顿）里为对齐音频做的大跨度 `set` 被换成了 `grab`，**丢帧累计开销从 1.5 秒降到 5.9 ms**；正常处理速度下丢帧 0 次/秒。

---

## 四、改了哪些代码

### 4.1 顶部常量与说明（约第 96–125 行）

```python
AUDIO_CLOCK_WARMUP_SEC = 0.25     # 播放器创建后多久才允许读 pts
FRAME_SYNC_TOLERANCE_FRAMES = 2   # 落后不超过这么多帧时只微调等待，不丢帧
FRAME_SYNC_MAX_GRAB = 60          # 小跨度 grab、大跨度 set 的阈值
```

并新增整段注释，把第二节的 4 条实测语义与公式写进源码。

### 4.2 新增状态字段（`__init__`）

```python
self._audio_progress_ref = None   # 音频时钟的参考读数（None = 尚未校准）
self._audio_clock_base = 0.0      # 校准时视频所在位置（秒）
self._audio_started_at = 0.0      # 播放器创建时刻（用于跳过 warmup 窗口）
```

### 4.3 新增方法 `_audio_clock_media_seconds()`

返回"此刻应该播到第几秒"（浮点秒），不可用时返回 `None` 表示本帧回退盲调度。返回 `None` 的情形：

- 无播放器 / 音频失败
- 暂停中、正在 seek、鼠标按在进度条上
- 有挂起的音频重建请求
- 播放器创建不足 `AUDIO_CLOCK_WARMUP_SEC`
- `get_pts()` 抛异常 / 非有限数 / 负数
- **首次调用**：此时建立校准基准，返回 `None`（校准当帧不参与同步）

### 4.4 `_create_audio_player()` 结尾重置校准

```python
self._audio_progress_ref = None
self._audio_started_at = time.time()
```

每次重建播放器都会重新校准，因此跳转 / 切倍速 / 片尾回绕后不会残留旧基准。

### 4.5 `update_frame` 主循环

读帧前按主时钟**丢帧追赶**，渲染后按主时钟算**下一帧等待**：

```python
clock_sec = None if force else self._audio_clock_media_seconds()
if clock_sec is not None and self.total_frames > 1:
    target_frame = clamp(int(clock_sec * fps), 0, total_frames - 1)
    behind = target_frame - current_frame
    if behind > FRAME_SYNC_TOLERANCE_FRAMES:
        if behind <= FRAME_SYNC_MAX_GRAB:   # 小跨度：grab 顺序跳过
            for _ in range(behind):
                if not self.cap.grab(): break
        else:                               # 大跨度：退回 set
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
# ... 原有 read / 渲染 不变 ...
now_sec = None if force else self._audio_clock_media_seconds()
if now_sec is not None:
    next_media_sec = int(self.cap.get(cv2.CAP_PROP_POS_FRAMES)) / fps
    wait_sec = (next_media_sec - now_sec) / self.playback_speed
    delay = max(1, int(wait_sec * 1000))
else:                                        # 无主时钟：退回原行为
    process_time = (time.time() - start_time) * 1000
    delay = max(1, int(1000.0 / fps / self.playback_speed - process_time))
```

`force=True`（暂停态重绘一帧）不参与同步 —— 用户就是要看当前位置。

**这个等待式自带两件事**，无需额外代码：倍速（媒体前进速度 = `speed`）；处理耗时补偿（`clock` 是渲染之后才读的，本帧多花的时间已经算进去了）。

---

## 五、验证

### 5.1 桩测试（25+ 项，全 PASS）

覆盖：warmup 窗口内返回 `None`、暂停/seek/按进度条时返回 `None`、首次调用只校准不参与、公式在 0.5x/1.0x/1.5x/2.0x 下的正确性、`get_pts` 抛异常/NaN/负数时回退、无音轨回退、丢帧阈值分支（≤60 用 grab、>60 用 set）、片尾回绕后重新校准、倍速切换后重新校准。

### 5.2 端到端回归（真 ffpyplayer + 真 cv2 + 真 Tk，音量归零保持静音）

| # | 场景 | 结果 |
|---|---|---|
| 1 | 正常起播 | PASS |
| 2 | 拖动到 12 秒（落点 12.96s，受关键帧间隔限制） | PASS |
| 3 | 长时连续播放 45 秒（跨 2 次回绕） | PASS，漂移恒定 −0.013~−0.050s |
| 4 | 暂停时画面停住 | PASS |
| 5 | 倍速 1.5x（实测 1.48x） | PASS，无被主时钟拽住 |
| 6 | 倍速 0.5x（实测 0.48x） | PASS |
| 7 | 倍速回到 1.0x（实测 0.93x） | PASS |
| 8 | 片尾回绕到 0 秒并重新校准 | PASS |
| 9 | 进程健康 | 无段错误（退出码 0） |

### 5.3 漂移量化对比

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 慢处理 12 秒播放 | −4.96 s（43.3%） | **−0.17 s（1.5%）**，不累积 |
| 45 秒长时 | 累积式，不可用 | **−0.034 s（均值）**，跨 2 次回绕稳定 |

---

## 六、过程中否证的错误方案（备忘，避免复发）

| 曾经的尝试 | 实测结论 |
|---|---|
| `媒体位置 = ss + pts × speed` | ❌ pts 已自带 ss，再叠加 → 时钟给出 24.4s（应为 12.4s），目标帧越界触发片尾回绕 |
| `媒体位置 = base + (pts − base) × speed`（把 base 当"请求位置"） | ❌ 倍速重建时 `ss` 会回退到关键帧，base 不可信 → 主时钟硬拽视频，漂移 +1.675s |
| **最终：以"校准时刻的视频位置"为零点，只取推进量** | ✅ 上一轮端到端与长时测试全 PASS |

此外，测试脚本自身曾有若干缺陷并已修正（`FakeCap.read` 永远成功导致回绕分支进不去、`verify_e2e2.py` 缺 `import cv2`、`measure` 未识别回绕被记 FAIL、用 `time.sleep(0.4)` 制造出人为 −0.35s 偏移）——这些是测试代码的问题，不是播放器的问题。

---

## 七、遗留限制（底层库硬约束，非本次引入）

1. **跳转精度受关键帧间隔限制**：音频定位靠创建时的 `ss`（非精确 seek，回退到前一个关键帧）。常见素材 1~2 秒偏差（`city.mp4` 关键帧只有 0s/12s，所以偏得明显）。
2. **每次跳转 / 切倍速仍有 0.2~0.4 秒音频间断**（重建播放器的固有代价）。
3. **播放态松手后仍有一次约 0.5 秒的音频对齐停顿**（不随拖动次数叠加），根因是 `close_player()` 固定 400~580ms 阻塞。要彻底消除只能换掉 ffpyplayer 音频通道（PyAV 解码 + sounddevice 输出），属架构级改动，且会引入新依赖（影响 PyInstaller 打包）。

---

## 八、本轮之后仍未做的（P2，等你点头）

审查报告第 12–23、26–32 条，纯清理与体验项：

- 死代码：`_hide_controls_id` 重复初始化、`current_video_path` 只写不读、`_process_3d` 里尺寸相同的空 `cv2.resize`、注释掉的 `propagate(False)`、`conv_progressbar` 的 `length=300`、多个只赋值的 widget 属性
- `is_playing` / `is_paused` 状态合并
- `btn_frame` 单行 12 个控件拆行
- 2D 模式下禁用视差滑块与"交换左右眼"
- 截图画质 / 路径
- `os._exit(0)` 兜底规范化
- `bind_all("<Motion>")` 节流

另外还有一个待你决定的事：项目根目录那个 **36.9MB 的旧 `temp_3d_output.mp4`**（新逻辑已不再往那儿写）是否删除。
