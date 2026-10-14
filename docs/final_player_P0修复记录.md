# final_player.py — P0 修复记录

修改文件：`final_player.py`
范围：仅 P0（会崩 / 会错），共 5 项 + 1 处等价收敛。P1、P2 未动。
日期：2026-10-13

---

## 1. 进度条拖动除零（原第 730-731 行）

**问题**：`on_scale_move` 直接拿 `self.cap.get(cv2.CAP_PROP_FPS)` 当除数。某些容器/流返回 0，一拖进度条就 `ZeroDivisionError`。而 `open_video` 里早就算好了带兜底的 `self.video_fps`，只是没人用。

```python
# 改前
current_sec = int(float(value) / self.cap.get(cv2.CAP_PROP_FPS))
total_sec = int(self.total_frames / self.cap.get(cv2.CAP_PROP_FPS))

# 改后
fps = self.video_fps if self.video_fps > 0 else 30.0
current_sec = int(float(value) / fps)
total_sec = int(self.total_frames / fps)
```

## 2. seeking 轮询定时器未登记（原第 775-777 行）

**问题**：`update_frame` 里 `self.root.after(30, self.update_frame)` 没有存进 `self.after_id`，`on_close` 的取消清单漏掉它；窗口 destroy 之后回调触发会抛 `invalid command name`。

```python
# 改前
if self.is_seeking and not force:
    self.root.after(30, self.update_frame)
    return

# 改后（走统一调度出口）
if self.is_seeking and not force:
    self._schedule_next_frame(30)
    return
```

## 3. 定时器叠加导致视频加速（同一处，顺带根治）

**问题**：`update_frame` 有两个入口——定时器自身回调，以及 `toggle_play` / `on_scale_click` 里的直接调用。直接调用时不会取消上一次遗留的挂起定时器，于是产生两条并行的 `after` 链，帧率翻倍且不可恢复（文件里第 77 行的注释其实已经点出过这个隐患）。

新增两个方法，把"取消 + 重新调度"收敛到唯一出口：

```python
def _cancel_pending_frame(self):
    if self.after_id is not None:
        try:
            self.root.after_cancel(self.after_id)
        except Exception:
            pass
        self.after_id = None

def _schedule_next_frame(self, delay_ms):
    self._cancel_pending_frame()
    self.after_id = self.root.after(max(1, int(delay_ms)), self.update_frame)
```

- 帧末调度：`self.after_id = self.root.after(delay, ...)` → `self._schedule_next_frame(delay)`
- 播放停止分支补上 `self._cancel_pending_frame()`，清掉残留引用
- `open_video` 里手写的 3 行取消逻辑换成 `self._cancel_pending_frame()`（等价收敛，顺带覆盖 P2 第 22 条）

## 4. 空格键与按钮双重触发（原第 249 行）

**问题**：`root.bind("<space>", ...)` 无焦点判断。焦点落在按钮上时，空格既激活按钮、又切换播放/暂停。

新增模块级常量与回调：

```python
SPACE_CONFLICT_WIDGETS = (
    tk.Button, tk.Entry, tk.Text, tk.Listbox,
    ttk.Button, ttk.Entry, ttk.Combobox,
    ttk.Checkbutton, ttk.Radiobutton,
)

def _on_space_key(self, event=None):
    focused = self.root.focus_get()
    if isinstance(focused, SPACE_CONFLICT_WIDGETS):
        return None          # 交给控件自己处理
    self.toggle_play()

# 绑定改为
self.root.bind("<space>", self._on_space_key)
```

刻度条（ttk.Scale）刻意不在清单里：它本身不响应空格，用户拖完音量后按空格仍应能暂停。

## 5. 深度图归一化除零 → NaN（原第 492 行）

**问题**：`depth_output * 255 / np.max(depth_output)`，纯色/全黑等平坦帧最大深度为 0，除零产生 NaN，`astype("uint8")` 后是一张垃圾图，左右眼都会被污染。

```python
# 改前
depth_map = (depth_output * 255 / np.max(depth_output)).astype("uint8")

# 改后
depth_output = np.nan_to_num(depth_output, nan=0.0, posinf=0.0, neginf=0.0)
d_max = float(np.max(depth_output)) if depth_output.size else 0.0
if d_max > 1e-6:
    depth_map = (depth_output * 255.0 / d_max).astype("uint8")
else:
    depth_map = np.zeros(depth_output.shape, dtype="uint8")
```

## 6. 渲染函数越权修改防抖状态（原第 295 行）

**问题**：`render_current_frame` 开头有一句 `self.resize_after_id = None`。它只丢掉定时器 ID，并不取消那个还活着的 `after(60, _finish_resize)`，于是 `on_window_resize` 之后再也取消不掉，防抖回调重复排队、多渲染若干次。

处理：删掉该行。`_finish_resize` 自己开头的 `self.resize_after_id = None` 保留（那是回调正常结束时的清理）。渲染函数现在不再碰任何定时器状态。

---

## 验证结果

| 验证项 | 方式 | 结果 |
|---|---|---|
| 语法 | `python -m py_compile final_player.py` | COMPILE_OK |
| 静态检查 | 6 项断言（旧写法已无残留、`after_id` 赋值点收敛为 1 处） | 全部 OK |
| 真实环境导入 | `.venv` 下 `import final_player`（含 cv2/torch/transformers/ffpyplayer） | IMPORT_OK |
| 定时器单链 | 连续调度两次 → 旧 ID 已取消、只剩一个挂起 | PASS |
| 停止播放清引用 | 暂停态调用 `update_frame` → `after_id is None` 且无挂起 | PASS |
| 空格焦点判断 | 焦点在按钮 → 不切换；焦点在画面 → 切换 | PASS |
| 深度归一化 | 桩模型驱动 `process_video_to_3d` 跑真实转换，4 种深度（全零/全NaN/恒定/随机） | 全部写出正常帧、无 NaN、全零与 NaN 时左右眼一致 |

深度测试在临时目录内进行（该函数输出名固定为 `temp_3d_output.mp4`），未触碰项目里的素材与既有文件。

---

## 遗留问题（属于 P1 / P2，未在本轮处理）

- **P1**：倍速不改音频速率；片尾回绕时音频不 seek；`export_3d_video` 无法溯源，项目根目录里现有的 `temp_3d_output.mp4`（36.9MB，10-12 生成）在"打开已有3D视频"场景下会被错误导出；`os._exit(0)` 兜底；`bind_all("<Motion>")` 与每像素 after 抖动。
- **另发现**：`process_video_to_3d` 的 except 分支只在"用户取消"时删临时文件，转换中途报错会留下一个残缺的 `temp_3d_output.mp4`，之后同样能被导出按钮导出去（我这次用错张量维度做桩时复现了）。建议与 P1 的溯源问题一并修：用一个 `_session_output` 变量记录"本文件由本程序本次转换生成"。
- **P2**：死代码清理、按钮栏拆行、2D 模式下禁用视差控件等（见 `final_player_代码审查.md`）。
