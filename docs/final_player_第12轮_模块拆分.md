# final_player 第 12 轮 · 模块拆分（① 抽工具模块 + ② 拆开 `__init__`）

> 日期：2026-10-14
> 起点提交：`58a2916`（第 11 轮收尾）
> 本轮提交：`1b53af0`（①）、② 见下一次提交
> 验证：`run_tests.py --full` 跑了 **两次**（每块一次），202 项断言 + 5 个乱序压力种子，全绿

---

## 一、为什么现在才做这件事

拆分的风险从来不在"文件长"，而在**改动本身可能悄悄破坏行为**。这个项目前 10 轮
吃过三次回归漏测的亏（播放变慢 / 暂停拖动不刷新 / 拖动后音量丢失），全是"改 A 弄坏 B"。

第 11 轮把回归**固化**成 `tests/` + `run_tests.py`（202 项断言、6 组套件、5 个随机
压力种子、每组独立子进程），这件事才从"不能做"变成"可以做"。

真正的问题形状也不是长度，而是**90 个方法读写同一份 `self`**：
音频交接那组方法与 `update_frame` 互相穿透五六个字段
（`_audio_calibrated` / `_audio_handoff_old` / `_pending_audio_pos` /
`_audio_suspended_for_seek` …）。所以本轮只拆**不碰这些共享状态**的部分。

### 拆之前定下的三条纪律

1. **`import final_player` 的公开面不能变**。测试只引用 5 个名字：
   `VideoPlayerApp`、`should_yield_shortcut`、`find_ffmpeg`、`has_audio_stream`、
   `PlayState`（另加两个内部变量 `_FFMPEG_PATH` / `_FFMPEG_SEARCHED`）。
2. **一次只拆一块，每块跑一遍 `--full`**。不是拆完再跑 —— 否则红了不知道是哪块引入的。
3. **纯搬运，不改行为**。过程中看到"这里可以顺手优化"就记下来，不混进同一次改动。

---

## 二、① 抽出不碰 `self` 的纯函数

| 搬到哪 | 搬了什么 | 搬走后从 `VideoPlayerApp` 消失的东西 |
|---|---|---|
| `media_tools.py`（115 行） | `has_audio_stream` / `remove_quietly` / `mux_audio_into_video`，外加常量 `FFMPEG_MUX_TIMEOUT_SEC`、`FFMPEG_AUDIO_BITRATE` | 方法内的 ffmpeg 调用细节；`import subprocess` 也不再需要 |
| `depth3d.py`（114 行） | `process_3d`（SBS 原始帧 → 当前显示模式）+ `MeshCache` | 字段 `_mesh_hw` / `_mesh_x` / `_mesh_y`，以及专用的 `_get_mesh()` |
| `viewfit.py`（93 行） | `fit_min`（min 缩放几何）+ `LetterboxBgCache`（四周模糊背景及其缓存），常量 `BG_REFRESH_INTERVAL` | 字段 `_bg_cache` / `_bg_cache_size` / `_bg_age` |

**三个调用点各保留一个同名薄封装方法**，所以 `update_frame`、
`change_display_mode`、`_refresh_paused_frame`、`process_video_to_3d` 等调用点
**一行都没有改**：

```python
def _process_3d(self, frame):
    return depth3d.process_3d(frame, self.display_mode, self.swap_eyes,
                              self.parallax_value, self._mesh)

def _mux_audio_into_video(self, video_path, audio_source_path):
    return media_tools.mux_audio_into_video(
        video_path, audio_source_path, find_ffmpeg())

def _get_letterbox_bg(self, frame, win_w, win_h):
    return self._bg.get(frame, win_w, win_h)
```

### 刻意**没有**搬走的东西（重要）

`find_ffmpeg()` 和它的全局缓存 `_FFMPEG_PATH` / `_FFMPEG_SEARCHED` **留在原处**。

原因：`tests/test_audio_mux.py` 第 ④ 节靠改写那对全局变量来伪造
"这台机器上没有 ffmpeg"，从而验证优雅降级：

```python
fp._FFMPEG_PATH = ""          # 伪装成"找不到"
fp._FFMPEG_SEARCHED = True
s.check(fp.find_ffmpeg() is None, ...)
s.check(app._mux_audio_into_video(silent, city) is False, ...)
```

**这个接缝就是拆分的边界线。** 把缓存一起搬走，测试要么失效（漏掉回归）、
要么必须跟着改（削弱防线）—— 两种都不划算。所以改成让 `media_tools` 的 mux
**由调用方传入 ffmpeg 路径**，自己不做查找：

- 接缝原样保留，`test_audio_mux.py` 一个字没改；
- 顺带去掉一个隐性依赖 —— 原来 `_mux_audio_into_video` 内部偷偷调 `find_ffmpeg()`，
  "补音轨能不能跑"取决于一个它签名上看不见的全局状态。

`has_audio_stream` 则用**再导出**保留（`from media_tools import has_audio_stream`），
测试继续能按 `final_player.has_audio_stream` 调用。

---

## 三、② 拆开 `__init__` 的 391 行

`__init__` 原来一口气干七件事。现在按"职责 + 依赖顺序"拆成 8 段，
**逐行搬移、逻辑一行没改**：

| 段 | 行数 | 负责 |
|---|---|---|
| `__init__` | 28 | 只留主题、标题、几何尺寸 + 8 次调用 |
| `_init_playback_state` | 66 | 播放状态、进度条变量、定时器句柄、3D 调节量、显示模式、模型句柄 |
| `_build_video_area` | 41 | 顶层 grid 布局 + `video_container` / `video_label`（含点画面收回焦点） |
| `_build_progress_bar` | 16 | 进度条（row 1）与三个交互事件 |
| `_build_control_bars` | 131 | 底栏两行、转换进度面板、视差与左右眼栏 |
| `_bind_global_shortcuts` | 60 | `bind_all` / `bind_class` / `<FocusIn>` 兜底 |
| `_init_audio_state` | 72 | 播放器句柄、主时钟基准、后台交接状态、抢发音量窗口 |
| `_init_session_state` | 14 | `session_output_path` / `_last_output_has_audio` |
| `_finish_init` | 28 | 同步视差栏显隐、接关窗回调、建背景缓存 |

### 为什么这次编辑是安全的（操作细节）

搬运时**代码缩进没有变**：`__init__` 的方法体是 8 空格，方法体也是 8 空格。
所以整个操作只是在块边界插入 `    def _xxx(self):` + 文档字符串，
**一行代码都没有重新缩进** —— 这是让"纯搬运"在机械层面成立的关键，
也是这次改动能靠 diff 一眼审完的原因。

### 三条顺序依赖（写进了 `__init__` 的文档字符串）

1. `_init_playback_state` 必须在建控件之前跑完 —— 控件要拿 `seek_var` /
   `mode_var` 当 `variable` 传进去。
2. `_build_control_bars` 必须在 `_finish_init` 之前 —— 后者要同步
   `parallax_frame` 的显隐。
3. 全局键的注册要等控件都建好 —— `bind_class` 会作用于之后创建的按钮。

---

## 四、验证

每一块都单独跑了一次 `run_tests.py --full`：

```
① 抽工具模块后   202 项断言，10 组全绿，0 FAIL，331.2s
② 拆 __init__ 后  202 项断言，10 组全绿，0 FAIL，329.2s
```

（断言分布：功能用例 40 / 定时器与挂起请求 23 / 快捷键与逐帧步进 86 /
转换补音轨 15 / 稳态速度 3 / 乱序压力 seed=1..5 各 7。）

另加一个 79 项的**初始化冒烟检查**（拆 ② 后单独跑的）：确认 8 段建出来的
属性/控件一个不缺、两个缓存都能用、`bind_all` 与按钮类空格替换都在、
关窗回调已接上、视差栏初始仍是隐藏。

---

## 五、规模变化（诚实的数字）

| | 拆分前 | 拆分后 |
|---|---|---|
| `final_player.py` | **3085 行** | **3002 行** |
| 方法数 | 90 | 97 |
| 最大的方法 | `__init__` 391 行 | `update_frame` 145 行（`__init__` 降到 28） |
| 新增文件 | — | `media_tools.py` 115 / `depth3d.py` 114 / `viewfit.py` 93 |

**总行数只减了 83 行，甚至方法数还多了 7 个**（因为新加了 8 个方法头与文档字符串）。
这说明"拆模块"的价值**不是让代码变短**，而是：

- `__init__` 从"没人能一口气看懂"变成"一眼看清 8 个阶段"；
- 改 3D 合成不必在 3000 行里翻，去 `depth3d.py`；
- 取景数学与 ffmpeg 调用可以脱离 Tk 单独读、单独测。

想真正让总行数下降，得做下一节的 ③。

---

## 六、本轮**没做** ③（抽音频引擎成独立类），以及理由

`2194~2901` 那 700 行（现在是 `_close_audio_player` 到 `_current_video_seconds`）
是最诱人的一块，但**收益/风险比最差**：

- `_audio_calibrated`、`_audio_handoff_old`、`_pending_audio_pos`、
  `_audio_suspended_for_seek`、`_audio_volume_resume` 这些字段要和
  `update_frame`、`PlayState`、`root.after` 定时器来回穿插，还有后台线程在碰它们；
- 搬出去必须把这些**隐式耦合全部显式化**（定接口、传回调、定谁拥有定时器）；
- 而这块恰好是整个程序**唯一会段错误**的部分（混音器被销毁后 `Mix_Volume`、
  无音轨实例上 `set_volume`）。

花大力气换来"文件短了 700 行"，但最脆的地方被重新翻译了一遍。
**结论：等真的要频繁改音频时再做，那时候拆更有依据。**
