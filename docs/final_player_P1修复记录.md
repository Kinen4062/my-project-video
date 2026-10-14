# final_player.py — P1 修复记录

修改文件：`final_player.py`
范围：P1 中约定的两项 —— ① 音频同步（倍速 / 片尾回绕 / 进度条）② 导出溯源 + 转换产物落点。
日期：2026-10-13

---

## 一、先说结论：动了这个功能之后，最值钱的是下面这组实测事实

这个功能没法按"教科书写法"实现，因为项目用的 ffpyplayer 构建有三个硬约束。三条都是实测出来的，写在文件顶部注释里，避免以后有人再踩：

| 事实 | 实测证据 | 后果 |
|---|---|---|
| **`MediaPlayer.seek()` 定位不了音频** | `seek(7.5)` 后 ffpyplayer 自己的帧 pts 跳到 7.52，但**音频时钟仍从 0.35 继续走** | seek 对这个应用是纯无用功（画面由 OpenCV 输出，ffpyplayer 的视频流根本没人用） |
| **暂停态调用 `seek()` 会段错误** | 暂停态 `seek(2.0)` → 进程退出码 139 (SIGSEGV)；`set_pause(False)` 后立即 `seek()` 同样崩 | 原来那句"同步音频位置"的代码如果被"修好"，会直接把程序搞崩 |
| **创建时传 `ff_opts['ss']` 是唯一能定位音频的手段** | `ss=12` → 音频时钟 12.27；`ss=17` → 12.26（回退到关键帧） | 定位精度受视频关键帧间隔限制 |

另外两条接口事实：

- **本构建没有 `set_playback_rate`**（已核对 pyx 导出符号：只有 `close_player/get_frame/get_metadata/get_mute/get_pts/get_volume/request_channel/seek/set_mute/set_pause/set_size/set_volume/toggle_pause` 等）。倍速只能靠 ffmpeg 的 **`atempo` 滤镜在创建时**指定 —— 实测可用：传 `af='atempo=1.5'` 无任何报错，传一个不存在的滤镜名会立刻报 `Failed to open file or configure filtergraph`。
- **`MediaPlayer.seek()` 的参数里没有 `whence`**（签名是 `seek(pts, relative=False, seek_by_bytes='auto', accurate=True)`）。

> 也就是说：**改动前那份代码里，"同步音频位置"这段从来就没生效过** —— 每次调用都抛 `TypeError: unexpected keyword argument 'whence'`，又被 `except Exception: pass` 静默吞掉。进度条拖动、倍速、片尾回绕三处的音频从来没有跟画面同步过。

---

## 二、改动清单

### 1. 音频同步：统一收敛到"重建播放器 + `ss` 定位"

新增/改写的方法：

```python
def _close_audio_player(self)                      # 统一的安全关闭
def _create_audio_player(self, file_path, start_sec=0.0)
def _restart_audio_at(self, start_sec=0.0)         # 唯一的音频跳转入口
def _current_video_seconds(self)                   # 当前视频位置（秒）
```

`_create_audio_player` 按当前状态组装 `ff_opts`：

```python
ff_opts = {
    'paused': bool(self.is_paused),                 # 与视频状态一致
    'volume': 0.0 if self.audio_muted else self.audio_volume,
}
if self.playback_speed and self.playback_speed != 1.0:
    ff_opts['af'] = "atempo=%g" % self.playback_speed   # 倍速
if start_sec and start_sec > 0.01:
    ff_opts['ss'] = float(start_sec)                # 定位
```

三个调用点：

| 场景 | 改前 | 改后 |
|---|---|---|
| 切倍速 `change_speed` | 只改视频帧间隔，音频恒 1.0x，越播越偏 | 按当前视频位置重建音频，带 `atempo` 同速率 |
| 片尾回绕 `update_frame` | 视频跳回第 0 帧，音频不动，第二遍起永久错位 | 同步重建音频到 0 秒 |
| 进度条 `finish_seek` | `seek(..., whence='frame')` → 每次都 TypeError 被吞 | 换算成秒后走 `_restart_audio_at` |

代码里**不再有任何 `seek()` 调用**（全文仅剩两行注释引用了旧写法）。

### 2. 导出溯源 + 转换产物落点

- 新增 `self.session_output_path`：记录"本次会话由本程序转换生成"的文件。
- 新增 `_make_temp_output_path()`：产物写到**系统临时目录**、文件名带 `uuid` 前缀，不再固定写工作目录的 `temp_3d_output.mp4`。
  （改前的问题：从任何目录启动都往那儿写、目录不可写就失败、多次转换互相覆盖、导出只能靠"有没有同名文件"猜。）
- `on_conversion_done` 登记产物路径，并清掉上一次的临时文件；完成提示里带上临时文件位置。
- `export_3d_video` 只认 `session_output_path`；文件不存在（含"没有产物"与"产物被删"）一律给明确提示，并且**不会再弹保存框**。
  （改前：用【打开已有3D视频】载入别的文件后点导出，导出的会是上一次转换的残留文件；甚至可能是中途报错留下的残缺文件。）
- 顺带把 `on_close`、`open_video` 里重复的音频释放逻辑换成 `_close_audio_player()`。

---

## 三、验证结果

| 验证项 | 方式 | 结果 |
|---|---|---|
| 语法 | `python -m py_compile final_player.py` | COMPILE_OK |
| 真实环境导入 | 项目 `.venv`（cv2/torch/transformers/ffpyplayer 齐全） | IMPORT_OK |
| 倍速进 `ff_opts` | 桩替换 MediaPlayer，捕获真实参数字典 | 1.5x→`atempo=1.5`、2.0x→`atempo=2`、0.5x→`atempo=0.5`、1.0x→不带 af；静音→`volume=0.0`；start=5→`ss=5.0`；start=0→不带 ss。全 PASS |
| 产物落点 | 两次调用 | 均在 `%TEMP%`、`.mp4` 后缀、名字不重名。PASS |
| 导出溯源 | 桩替换 messagebox/filedialog | 无产物→警告且不弹保存框；有产物→文件正确复制且内容一致；产物被删→警告。PASS |
| 重建语义 | 桩播放器 | 暂停态/播放态都能重建、旧播放器被关闭、`ss` 与 `paused` 选项正确、回绕到 0 时不带 ss、无文件时不重建。PASS |
| eos 回绕 | 假摄像头 read() 失败一次 | `update_frame` 触发 1 次音频重建，且不带 ss。PASS |
| **真播放器定位（核心）** | 真 ffpyplayer + 真 Tk 事件循环，读音频时钟 | 拖到 12s → 时钟 12.56；拖到 5s → 0.6（关键帧回退，符合预期）；暂停态重建 12s → 恢复后 12.63；恢复后正常推进 12.63→13.24。全 PASS |
| **真实句柄切倍速** | 真 cv2 句柄，跳到第 300 帧(=12.00s) 后切 1.5x | 位置换算 12.00；音频时钟落在 12.57。PASS |
| 稳定性 | 上述真播放器测试全过程 | 无段错误、无异常退出 |

测试均在临时目录内进行，未触碰 `test_videos/` 素材，也未覆盖项目里已有的文件。

---

## 四、已知限制（属于底层库，不在本轮范围内可解）

1. **音频跳转精度受视频关键帧间隔限制。** `ss` 是"非精确 seek"，会回退到目标之前的关键帧。`test_videos/city.mp4` 的关键帧只有 0s 和 12s，所以拖到 5 秒时音频会落在 0 秒附近。常见手机/相机素材的关键帧间隔是 1~2 秒，偏差也在这个量级。要彻底解决需要换掉 ffpyplayer 的音频通道（例如 `sounddevice` + PyAV 自己喂 PCM），那是架构级改动。
2. **每次跳转/切倍速会重建音频播放器**，有约 0.2~0.4 秒的音频间断。这是"没有 set_playback_rate、seek 又定位不了音频"逼出来的唯一可行解。
3. **音画主时钟仍是两套**（视频按 `after()` 数帧、音频走 ffpyplayer 自己的线程）。本轮只是让三个"跳转动作"不再留下永久错位；**长时间播放的渐进漂移仍未解决**，那属于审查报告里的第 11 条（架构级），建议单开一轮。

---

## 五、遗留与建议

- **审查报告第 11 条**（音画主时钟）——建议下一轮单独做。
- **P2 项**：死代码清理、按钮栏拆行（现在是单行 12 个控件，950px 必然挤）、2D 模式下禁用视差控件、`os._exit(0)` 兜底、`bind_all("<Motion>")` 节流等。
- **可选优化（已实测，未改）**：音频播放器创建时加 `'vn': 1` 可禁掉 ffpyplayer 用不到的视频解码（画面由 OpenCV 输出），省一份解码开销。实测加与不加，音频时钟都按实时推进（5 秒采样到 4.83），所以**不影响音频**；是否采用看你更在意 CPU 还是保守。
- **项目根目录残留**：`temp_3d_output.mp4`（36.9MB，10-12 生成）是旧逻辑遗留的产物，现在的新逻辑不会再往那儿写。要我删掉吗？（没有你确认我不会动它）
