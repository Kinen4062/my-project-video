# final_player.py P2 清理 —— 修复记录

**日期**：2026-10-13
**改动文件**：`final_player.py`（1819 行 → **1826 行**）
**范围**：审查报告第 **12–23、26–32** 条，外加第 9、10 条（此前未归类，一并处理）
**授权**：用户已手动备份（`test_videos/final_player.py.bak`，45KB，11:03）

---

## 一、结论先行

- P2 清单 **26 项**：**24 项已改**，2 项经核对**已不成立/早已修复**（第 13、17 条，见下）。
- 顺手清掉了审查报告里没归类的第 **9、10** 条（`os._exit` 兜底、`bind_all("<Motion>")`）。
- **过程中发现并修复了一个真实的旧 bug**：转换报错时错误提示框**根本弹不出来**（详见第五节）。
- 验证：**81 项检查全部 PASS**，含真 Tk + 真 cv2 + 真 ffpyplayer 的端到端回归，进程正常退出（退出码 0，无段错误）。

---

## 二、逐条对照

### 2.1 纯冗余 / 死代码（第 12–23 条）

| # | 原问题 | 本次处理 |
|---|---|---|
| 12 | `self._hide_controls_id = None` 初始化两次 | 删掉 `__init__` 末尾那一次，只保留声明处 |
| 13 | `current_video_path` 只写不读 | **已不成立**。P1 起它被 `change_speed` / `_restart_audio_at` / `_request_audio_restart` 三处读取（音频重建必须知道当前文件）。保留，未动 |
| 14 | `current_frame` 只是跳转中转变量 | 内联成一行 `self.seek_var.set(int(...))`，删掉实例属性 |
| 15 | `is_playing` / `is_paused` 语义重叠 | 收敛成单一枚举 `PlayState`（IDLE/PLAYING/PAUSED），见第三节 |
| 16 | `_process_3d` 里尺寸相同的空 `cv2.resize` | 只在宽度不一致时 resize，见第四节 |
| 17 | `render_current_frame` 越权清 `resize_after_id` | **P0 阶段已修**（并已在 docstring 里写明原因），本次未再动 |
| 18 | 注释掉的 `grid_propagate(False)` 死代码 | 删除 |
| 19 | 取消转换的两段重复清理逻辑 | 提取 `_cleanup_partial_output()`（静态方法），两处调用；顺带让报错分支也打印删除失败原因 |
| 20 | `conv_progressbar` 的 `length=300` 被 `fill=X+expand` 覆盖 | 删除该参数，并把控件降为局部变量 |
| 21 | 多个只赋值、从不引用的 widget 属性 | `volume_label` / `volume_scale` / `speed_label` / `speed_combo` / `mode_label` / `mode_combo` / `btn_open` / `btn_screenshot` / `btn_export` / `conv_progressbar` 共 **10 个**改为局部变量 |
| 22 | `open_video` 与 `on_close` 各写一遍"停播" | 提取 `_stop_playback_loop()` |
| 23 | `change_parallax` / `toggle_swap_eyes` 结尾三行完全相同 | 提取 `_refresh_paused_frame()` |

> 保留为属性的只有真正被引用的：`btn_convert` / `btn_cancel_convert` / `btn_play` / `btn_mute` / `btn_swap_eyes` / `conv_label` / `parallax_label` / `video_label` / `scale`，以及所有 `tk.*Var`（**变量对象不能降为局部变量** —— `tkinter.Variable.__del__` 会 unset 掉 Tcl 变量，控件随即失去绑定）。

### 2.2 界面与交互（第 24、25、26、27、28、29、30、31、32 条）

| # | 原问题 | 本次处理 |
|---|---|---|
| 24 | 底栏单行挤 12 个控件 | **拆成两行**，见第三节 |
| 25 | 2D 模式下视差滑块 / 交换左右眼无效却仍可交互 | 2D 时**整行隐藏**，切到任意 3D 模式才出现 |
| 26 | `_show_controls` 无条件恢复视差栏 | 改由 `_sync_parallax_visibility()` 按模式 + 底栏可见性决定 |
| 27 | ttk.Scale 自己会跳到点击位置，`on_scale_click` 又手算一遍 ratio | 两处的公共部分（压住音频 + 置 seeking + 重置防抖）提取为 `_begin_seek_interaction()`；**保留**手算 ratio，理由写在代码注释里（见下） |
| 28 | 绑在 root 上的 `<Configure>` 靠 `event.widget` 过滤，很脆弱 | **删掉 root 上的绑定**，只由 `video_container` 驱动 |
| 29 | 每帧都重算模糊背景 | 加缓存 `_get_letterbox_bg()`，每 `BG_REFRESH_INTERVAL=3` 帧重算一次 |
| 30 | root 与 container 双绑 Configure，一次拖拽反复切换 `is_resizing` | 同 28（只剩一条事件链） |
| 31 | 截图写死文件名 + cwd | 改 `asksaveasfilename` + `imencode` 落盘 + 提示**绝对路径** |
| 32 | 转换完成硬编码切"红蓝3D"、提示语假设有眼镜 | 改成先问一句，选"是"才切 |

**关于第 27 条为什么"没有全删"**：`on_scale_click` 是**控件绑定**，而 ttk 自己的跳转逻辑是**类绑定（class bindtag）**；Tk 严格按 bindtags 顺序执行，控件绑定先跑。所以在 `on_scale_click` 里执行时 `seek_var` 还是旧值，不自己算就没法在暂停态立刻渲染目标帧。两处算出的位置一致，也不存在"跳两次"（实测：程序写 `seek_var` 不会触发 command 回调）。本次把重复的**状态处理**收敛掉了，保留了这段有实际作用的计算，并在代码里写了原因。

---

## 三、两处结构性改动

### 3.1 播放状态：两个布尔量 → 单一枚举

```python
class PlayState:
    IDLE = "idle"        # 没有打开中的视频（初始 / 关闭 / 正在换文件）
    PLAYING = "playing"
    PAUSED = "paused"
```

- 写侧**全部**收敛到 `self.play_state`（共 4 处：`__init__`、`open_video`、`toggle_play`、`on_close`→`_stop_playback_loop`）。
- 读侧保留同名**只读属性**：
  ```python
  @property
  def is_playing(self): return self.play_state == PlayState.PLAYING
  @property
  def is_paused(self):  return self.play_state == PlayState.PAUSED
  ```
  这样 20 多处读取点一行都不用改，同时"给它们赋值"会直接抛 `AttributeError` —— 非法组合从语法层就不可能产生。

### 3.2 底栏拆两行

| 行 | 容器 | 内容 |
|---|---|---|
| row 0 | `video_container` | 视频（唯一带 weight 的行） |
| row 1 | `progress_frame` | 播放进度条 |
| **row 2** | `btn_frame` | 打开已有3D视频 / 导入2D视频一键转3D / 播放 / 截图 / 导出3D视频 |
| **row 3** | `aux_frame`（新增） | 3D模式 / 倍速 / 音量 + 静音 |
| row 4 | `conversion_frame` | 转换进度（默认隐藏） |
| row 5 | `parallax_frame` | 视差 / 交换左右眼（仅 3D 显示） |

实测请求宽度：`btn_frame` **471px**、`aux_frame` **439px**，容器 950px —— 都有近一倍余量，不再拥挤。截图：`final_player_P2底栏_2D模式.png`、`final_player_P2底栏_3D模式.png`。

---

## 四、两处性能相关改动

### 4.1 `_process_3d` 的空 resize（第 16 条）

原来无条件 `cv2.resize(left_eye, (w_half, h))`：**w 为偶数时**（绝大多数视频）切片宽度恰好等于 `w_half`，目标尺寸与源尺寸完全相同 → 纯空操作，每帧白做两次全画幅拷贝；**w 为奇数时**右眼比左眼宽 1 像素，这时才真的需要裁。

现在只在宽度不一致时 resize；一致时用 `np.ascontiguousarray`（纯 memcpy）——因为切片本身是**非连续视图**，而下游 `cv2.split` / `cv2.remap` 更喜欢连续内存。

### 4.2 模糊背景缓存（第 29 条）

`render_current_frame` 里最贵的一步是"四周那圈暗背景"：整幅放大到 cover 尺寸 → 降采样 → 高斯模糊 → 放大回窗口 → 整体压暗。它是 0.45 倍压暗 + 强模糊的陪衬，逐帧差异肉眼看不出来，所以按 `BG_REFRESH_INTERVAL = 3` 帧重算一次。

两个关键细节：
1. 缓存**以窗口尺寸为键**，尺寸一变立刻作废（否则会在旧尺寸的图上取错区域）；
2. 取到缓存后必须 `.copy()` 再贴前景 —— 否则前景会被"烙"进背景里，下一次复用就会露出上一帧的影子。

实测：连续取 8 次背景只重算 **2 次**；并且做了**逐像素等价性验证**——冷缓存下的输出与 P2 之前的实现**完全一致**。

---

## 五、测试中发现并修复的真实旧 bug（未在审查报告里）

**现象**：转换过程中如果抛异常（比如模型出错、磁盘满），用户**什么提示都看不到**，转换窗口莫名消失，只在控制台留一条 `Exception in Tkinter callback`。

**原因**：

```python
except Exception as e:
    ...
    self.root.after(0, lambda: messagebox.showerror("转换错误", str(e)))
```

Python 会在 `except` 块结束时**删除变量 `e`**；而这个 lambda 是"稍后"由事件循环调用的，那时 `e` 已经不存在，直接 `NameError: cannot access free variable 'e'`。

**修法**：先把消息取成一个普通局部变量，闭包只捕获它。

这是**之前几轮一直没被覆盖的路径**（P0/P1 的测试都没模拟"转换中途抛异常"），本轮补了用例。

---

## 六、顺带处理的两条（此前未归类）

| # | 问题 | 处理 |
|---|---|---|
| 9 | `os._exit(0)` 会跳过线程收尾、不 flush、跳过临时文件清理 | 保留强制退出（SDL 残留线程问题无解），但：① 关窗前 `join` 转换线程最多 1 秒，让它响应取消并删掉残缺 mp4（新增 `CONVERSION_JOIN_TIMEOUT_SEC`）；② 退出前 flush `stdout/stderr`；③ 在注释里写明"这是最后手段、后面不许再放需要收尾的逻辑" |
| 10 | `bind_all("<Motion>")` 是全应用级，且每像素移动都 `after_cancel + after` | 改为只绑主窗口（子控件的 Motion 会经 bindtags 冒泡到 toplevel，行为不变，但不再收到对话框等其它窗口的事件），并按 `MOTION_THROTTLE_MS = 100` 节流 |

---

## 七、验证

### 7.1 桩测试（真 Tk + 真 cv2）——44/44 PASS

覆盖：状态机（含"只读属性赋值必抛 AttributeError"、`toggle_play` 互翻、IDLE 不调度）、界面结构（各行 row 号、控件数、2D 隐藏 / 3D 显示视差栏、全屏隐藏底栏时不冒出来）、`_process_3d`（4 种模式 × 偶/奇宽、红蓝合成取色正确性 `(左B, 左G, 右R)`、视差 remap、交换左右眼）、背景缓存（重算次数、尺寸失效、不污染缓存）、缩放去重、进度条公共入口去重、`_stop_playback_loop`、`_cleanup_partial_output`、`_refresh_paused_frame`、截图（中文路径 + 取消静默）、转换完成后询问。

其中一条**对照实验**很有说服力：

| 写法 | 中文路径结果 |
|---|---|
| 旧：`cv2.imwrite("C:\...\Temp\截图测试_中文名.png", img)` | 返回 **False**（静默失败） |
| 新：`cv2.imencode` + `open(path,"wb")` | 成功，读回尺寸正确 |

也就是说，原来的截图在**中文文件名**下会假装成功（提示"已保存为 screenshot.jpg"），实际什么都没写。

### 7.2 背景缓存等价性 —— 5/5 PASS

把 P2 之前的实现原样抄成参照函数，与缓存版**逐像素比对**：冷缓存完全一致、热缓存命中画面不变、换帧后前景更新而背景按设计最多滞后 3 帧、年龄到阈值后背景随新帧刷新。

### 7.3 转换链路（桩模型，不下载深度模型）——8/8 PASS

正常转换（产物带唯一前缀、落在 `%TEMP%`、宽 = 2×源宽）、中途取消（残缺产物被删除、走取消回调、不误报完成）、报错路径（弹错误提示、不误报完成/取消 —— **就是第五节修的那个 bug**）。

### 7.4 端到端（真 ffpyplayer + 真 cv2 + 真 Tk，音量归零）——24/24 PASS

起播推进 / 音频播放器建立 / 切红蓝3D 后视差栏出现且继续推进 / 播放中调视差与交换左右眼 / 暂停画面停住 / 恢复继续 / 拖动落点 63.4%（关键帧限制内）且**松手仅 83ms 不阻塞** / 1.5x 不被时钟拽住 / 全屏进出 + 底栏显隐 + 鼠标移动不报错 / 中文路径截图成功 / **`on_close` 后退出码 0，无段错误**。

合计 **81 项检查全 PASS**。

---

## 八、仍未处理 / 保留现状

1. **第 29 条的"独立 Canvas 背景层"方案**没做（只做了缓存）。要做到"背景永不重算"得把视频区和背景拆成两层 Canvas，属于渲染架构调整，收益不如风险。
2. **跳转精度受关键帧间隔限制**、**每次跳转/切倍速有 0.2~0.4 秒音频间断**、**播放态松手后一次约 0.5 秒音频对齐停顿** —— 都是底层库硬约束（前两轮已记录，非本轮引入）。
3. `test_videos/` 下还有一个 **36.9MB 的旧 `temp_3d_output.mp4`**（10 月 10 日，与项目根目录那个不同）。项目根目录那个你已删除；`test_videos/` 里的这个我没动（那是测试素材目录），需要的话说一声。
