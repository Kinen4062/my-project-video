# 修复记录：暂停时拖动进度条，画面不跟着变

**问题**：暂停状态下拖动进度条，松手后进度条位置对了、但画面仍停在暂停前那一帧。

**结论**：这是我上一轮 **P2 清理**（把 `is_playing` / `is_paused` 两个布尔量合并成 `PlayState` 枚举）时引入的**语义回归**。`update_frame()` 的守卫条件没跟着改，导致暂停态的强制重绘被提前 `return` 掉。已修复并完整回归。

---

## 一、根因

`update_frame(force=True)` 的设计意图是"不管当前是不是在播，都立刻读一帧并渲染"，暂停态点/拖进度条就是靠它刷新画面。

守卫条件在两个布尔量时代是这样写的：

```python
if not self.is_playing or (self.is_paused and not force):
    return                              # 不画
```

当时"暂停"只是把 `is_paused` 置真，**`is_playing` 仍然是 True**，所以暂停 + force 能顺利往下走。

P2 合并状态后：

```python
@property
def is_playing(self):
    return self.play_state == PlayState.PLAYING      # 暂停时这里是 False
```

于是 `not self.is_playing` 在暂停态**恒为真** → 无论 force 是不是 True，函数都在第 3 行直接返回：

- 位置其实**跳对了**（`cap.set(POS_FRAMES, n)` 执行了），
- 但**一帧都没读、没渲染**，画面自然纹丝不动。

受影响的就是所有走 `update_frame(force=True)` 的路径：暂停态**单击**进度条、暂停态**拖动**进度条松手、暂停态"按住停手"的防抖跳转。视差滑块和"交换左右眼"是直接调 `render_current_frame()` 的，所以它们一直是好的——这也解释了为什么只有进度条出问题。

## 二、复现证据

修之前，用真实 Tk + OpenCV 复现（`city.mp4`，500 帧，暂停在第 44 帧，拖到第 300 帧）：

```
按下(20%): frame=100  seek_var=100
拖动结束: frame=100  seek_var=300      ← 位置一直没动
松手后:   frame=300 (目标 300)
位置: PASS (差 0 帧)   画面: FAIL (平均像素差 34.012)
        （当前画面实际是暂停前那一帧）
```

位置读数是对的（300），但画面与"真正的第 300 帧"平均像素差 **34.0** —— 根本没重绘。

## 三、三处修改

### 1. 核心修复：守卫条件改成按"状态"判断

```python
# 正确语义：只有 IDLE（压根没在放）才不画；暂停态带 force 要照画
if self.play_state == PlayState.IDLE or (self.is_paused and not force):
    self._cancel_pending_frame()
    return
```

各状态行为对照（改前 → 改后）：IDLE 不画（不变）、PLAYING 画（不变）、**PAUSED+force 从"不画"变成"画"**（这正是修复点）、PAUSED 无 force 不画（不变）。

### 2. 顺带修掉"松手会多走一帧"

`update_frame()` 会把读到的位置回写进 `seek_var`，而 OpenCV 的 `POS_FRAMES` 给的是"**下一帧**的序号"（比请求值大 1）。原来的 `finish_seek` 渲染完一律把 `_last_jump_frame` 清成 `None`，于是：

按住拖动 → 停手够久（防抖已经跳转并渲染）→ 松手时又拿这个 **+1 的读数**再跳一次，画面悄悄多走一帧。实测 `松手后仍是目标帧: 像素差 15.367`。

现在改成记下**真实落点**，让紧随其后的松手成为空操作（下一次按进度条时清空）：

```python
landed = None
if target_frame != self._last_jump_frame:
    self.cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
    if self.is_paused:
        self.update_frame(force=True)
        landed = int(self.seek_var.get())     # 真实落点，不是请求值
self._last_jump_frame = landed
```

### 3. 顺带修掉"拖到最右端画面跳回开头"

进度条刻度是 `0..total_frames`，拖到最右端得到帧号 `== total_frames`，而最后一帧序号是 `total_frames - 1`。越界后 `cap.read()` 必然失败，被 `update_frame` 里"读不到就当放完了"的分支接走 → **绕回第 0 帧**（还会白付一次 0.5 秒的音频重建）。实测：

```
拖到 100.0% (请求帧 500) → 位置读数 1，画面显示 第 0 帧（开头）    ← 修前
拖到 100.0% (请求帧 500) → 位置读数 500，画面显示 第 499 帧（末尾）  ← 修后
```

新增 `_clamp_seek_frame()`，在 `on_scale_click` / `finish_seek` 两处夹取到 `[0, total_frames-1]`。**播放态**的自然片尾回绕（放完自动从头循环）是设计行为，未受影响。

## 四、验证

### 对照图

`final_player_暂停拖动_修复前后对比.png`：

| 面板 | 内容 | 与"真正的第 300 帧"的平均像素差 |
|---|---|---|
| 修复前（旧行为） | 画面停在暂停前那一帧 | **34.41** |
| 修复后（现在） | 画面就是第 300 帧 | **0.00** |
| 参照 | 视频里真正的第 300 帧 | 0.00 |

### 自动化测试：51 项全 PASS

对 3 个文件（`city.mp4` 有音轨 / `temp_3d_output.mp4`、`我的3D视频1.mp4` 是本程序导出的**无音轨**视频）各跑 17 项，全部 PASS，每个进程退出码 0、无段错误：

| 场景 | 检查点 |
|---|---|
| 暂停态单击进度条 | 落点到位、**画面即目标帧** |
| 暂停态按住拖到 60% 再松手 | 落点到位、**画面即目标帧**、画面确实变了（不是旧帧） |
| 暂停态按住不松、停手超过防抖时间 | 还按着就已显示目标帧（防抖兜底生效）、松手后仍是目标帧 |
| 暂停态跳转后恢复播放 | 从落点继续（不是从 0、也不是原位置） |
| 暂停态调视差 / 交换左右眼 | 立刻重绘、且用的仍是跳转后的帧 |
| 播放态拖动（回归） | 落点到位、仍在播放、按实时速度推进 |
| 未播放（IDLE）时 force 重绘 | 不会把播放循环重新拉起来、状态保持 IDLE |
| 暂停态在窗口外松手（root 兜底绑定） | 也能正确跳帧 |

判据用的是**画面对比**（`cap.set(n)+read` 独立取出目标帧，与界面当前帧做逐像素比较），而不是内部计数器，避免"位置对了就算过"的假通过。

### 播放速度无回归

媒体位置推进量 / 墙钟（绕片尾回绕按正增量累加）：

| 文件 | 音轨 | 3D | 速度 |
|---|---|---|---|
| `city.mp4` | 有 | 否 | 0.999x |
| `temp_3d_output.mp4`（2560x720@60fps） | 无 | 否 | 0.988x |
| `我的3D视频1.mp4` | 无 | 否 | 0.986x |
| `output_3d_video.mp4` | 无 | 否 | 0.986x |

上一轮"播放变慢"的修复未受影响。

### 回绕回归

播放态拖到最末尾松手后，仍能正常循环继续播放（`city.mp4`、`temp_3d_output.mp4` 均 PASS，退出码 0）。

## 五、遗留（都是底层库硬约束，非本次引入）

- 跳转精度受关键帧间隔限制（`ss` 非精确 seek）；
- 有音轨文件每次跳转/切倍速仍有 0.2~0.4 秒音频间断（`close_player()` 固定 400~580ms 阻塞，实测无法消除，除非换音频通道并引入新依赖）；
- 无音轨文件跳转已无此代价（没有音频重建，拖动仅阻塞约 11ms）。

## 六、影响文件

- `final_player.py`
  - `update_frame()`：守卫条件改为 `play_state == PlayState.IDLE`
  - `finish_seek()`：记录真实落点，避免松手多走一帧；跳转目标夹取
  - `on_scale_click()`：跳转目标夹取；按下时清空上一次的落点记录
  - 新增 `_clamp_seek_frame()`
  - 1994 行，`py_compile` 通过
