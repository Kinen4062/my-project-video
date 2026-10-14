# final_player 第 14 轮 · 右侧播放列表 + 拖放打开

> 日期：2026-10-14
> 起点提交：`05ed6c1`（第 13 轮·测试基建）
> 本轮提交：`a1f87c1`
> 验证：`run_tests.py --full` → **12 组全 OK、256 项断言、0 FAIL、退出码 0、340.2s**
> （干净环境 + 输出重定向，即最苛刻的 CI 场景）
> 截图：`docs/final_player_第14轮_播放列表与拖放.png`

---

## 一、需求，以及它实际被拆成了什么

原话两条：

1. **"将要播放的视频记录在视频播放器右侧列表中"** —— 一个看得见、能持久化的播放列表；
2. **"可以直接拖动视频文件到播放器进行播放"** —— 从资源管理器拖进窗口就开始播。

需求本身不难，难的是**别弄坏已经稳定的播放逻辑**。这个程序的
`update_frame` / 音频交接 / 定时器已经反复调过十几轮，任何"顺手改一下"都可能
把音量、同步、倍速再弄坏一次。所以本轮所有设计都围着一个原则转：

> **新功能只在三个明确的接缝上碰老代码**，其余全部是新文件、新方法、新控件。

三个接缝是：

| 接缝 | 改动 |
|---|---|
| `open_video()` | 加 `record=True` 参数 + 返回 `True/False` |
| `update_frame()` 的"读不到帧"分支 | 加 6 行：先问列表有没有下一项 |
| `__init__` | 加两次调用（`_init_playlist_state` / `_build_playlist_panel`） |

老代码里其它任何地方**一行都没动**。

---

## 二、文件分工

| 文件 | 行数 | 职责 |
|---|---|---|
| `filedrop.py` | 250 | 拖放（纯 ctypes，零第三方依赖） |
| `playlist_store.py` | 80 | 列表落盘（JSON + 原子写） |
| `final_player.py` | 3002 → **3399** | 面板、列表操作、续播、两个接缝 |
| `tests/test_playlist.py` | 336 | 新增第 7 组套件（54 项断言） |

前两个都**不碰 `VideoPlayerApp` 的状态**，与第 12 轮抽出的
`media_tools` / `depth3d` / `viewfit` 是同一类东西：可以脱离 Tk 单独读、单独测。

---

## 三、拖放：`filedrop.py`

### 为什么不用现成的库

| 方案 | 为什么不 |
|---|---|
| `tkinterdnd2` | 要额外安装，还是个带二进制的包；**它要求用 `TkinterDnD.Tk()` 当根窗口** —— 整个程序都得换根，而根窗口是 Tk 里最不能乱动的东西 |
| `windnd` | 早就不维护了 |
| 自己写 | 要的其实只是"拿到一份拖进来的文件路径列表"，Windows 的**旧式拖放 API** 几十行就够 |

### 它怎么工作（`filedrop.py:231` `enable_file_drop`）

```
DragAcceptFiles(hwnd, True)          把窗口声明成"接受文件拖放"
        ↓  用户拖文件进来
系统给那个窗口发 WM_DROPFILES
        ↓
我们把窗口过程（WNDPROC）换成了自己的 —— 只有这一条消息自己处理，
其余全部 CallWindowProcW 原样转发
        ↓
DragQueryFileW 取出路径 → DragFinish 释放
        ↓
root.after(0, ...) 把路径丢回 Tk 事件队列
        ↓
_on_files_dropped() 在 Tk 队列里跑，可以安全操作控件
```

### 三个必须这么写的点

**① `after(0, ...)` 不是可选的。**
WNDPROC 是在 Tk 的消息循环里被调用的。在里面直接操作 Tk 控件属于**重入**
（控件可能正被 Tk 更新到一半），很容易变成偶发崩溃。`after(0, ...)` 只是把回调
排到队尾，等于"等这轮消息处理完再说"。见 `filedrop.py:189` `_dispatch`。

**② 回调对象必须被强引用钉住。**
WNDPROC 是 C 层持有的函数指针；Python 这边一旦被 GC 回收，系统下一次回调就是
**野指针**，直接崩。所以 `_TARGETS` 是模块级全局，专门用来钉住 `_DropTarget`
实例（`_DropTarget.__init__` 里那句 `self._proc = ...`，注释标了 ⚠️）。
别为了"优雅"把它改成局部变量。

**③ 所有不认识的窗口消息都要转发。**
漏一条（比如 `WM_NCCALCSIZE`）窗口就画不出来。这是"换窗口过程"这种做法的通用代价。

### 一个容易忽略的细节：64 位句柄

`_load_windows_api()` 里**每个函数都显式写了 `argtypes` / `restype`**：

```python
long_ptr = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
setter = user32.SetWindowLongPtrW if ... 8 字节 ... else user32.SetWindowLongW
```

不声明的话 ctypes 默认按 32 位 `int` 传，**句柄会被截断** —— 而且只是"有时"崩，
属于最难查的那类问题。测试里合成 HDROP 时同样踩过一次（`GlobalAlloc` 的返回值
必须声明成 `c_void_p`），见 `tests/test_playlist.py:48` 的注释。

### 挂两个窗口句柄

`_window_chain()` 返回 `[顶层窗口, 客户区窗口]`，两个都挂。Tk 在 Windows 上给
**每个控件**都建了真 HWND，所以拖到视频画面上时命中的是某个子窗口；系统的旧式
拖放会沿父链往上找第一个"声明接受拖放"的窗口，因此给顶层挂上就够了 ——
多挂客户区是防某些系统版本不往上找。
**两个都挂不会重复触发**：一条消息只会投给其中一个窗口。

### 纯函数部分（可单独测）

`is_video_file` / `expand_dropped_paths` 不碰任何 Windows API，因此能在测试里
跑得飞快：

- **目录递归展开**，每个目录内按文件名排序（资源管理器的拖放顺序本身不可预期，
  不能依赖它）；
- **扩展名白名单** `.mp4 .m4v .mkv .avi .mov .wmv .flv .webm .mpg .mpeg .ts .m2ts .3gp`；
- **去重按 `os.path.normcase`** —— Windows 路径大小写不敏感，`C:\a.mp4` 和
  `c:\A.MP4` 是同一个文件，但字符串不相等；
- **上限 500 个**（`MAX_DROP_FILES`），防手滑把整个盘拖进来把界面卡死。

这份扩展名清单**同时也是"添加文件"对话框的过滤器**（`_video_filetypes()`），
免得出现"能拖进来却选不了"这种两处不一致。

---

## 四、播放列表的数据模型

### 唯一的事实来源

```python
self.playlist        # list[str]  —— 唯一的数据
self.playlist_box    # tk.Listbox —— 只是它的**视图**
```

所有增删一律走同一条路：**先改 `self.playlist` → 存盘 → 再刷新视图**
（`_playlist_add` / `playlist_remove_selected` / `playlist_clear` 都是这个形状）。
两边不会各改各的，也就不存在"界面和文件不一致"的状态。

### 刻意不缓存"当前是第几项"

`_playlist_index_of(path)` 每次从 `current_video_path` 现算，**不存下标**：

> 列表会被增删、顺序会变，缓存出来的下标迟早和列表对不上。
> 每次现算，永远自洽。

这条在被移除的项正好是"正在播的那一项"时尤其明显 —— 缓存下标那一刻就已经错了。

### 路径比较一律走 `normcase`

Windows 上 `D:/v/a.mp4` 和 `d:\V\A.MP4` 是同一个文件。所以"去重""找下标"
"判断是不是同一个文件"全部用 `os.path.normcase()` 归一后再比。

### 落盘：`playlist_store.py`

- 位置：`%APPDATA%\final_player\playlist.json`（其它平台 `~/.config/...`）。
  不放程序目录（可能是只读的安装位置），也不放视频旁边（列表里的视频本来就在
  不同盘里，没有"共同的所在"）。
- **原子写**：先写同目录的临时文件，再 `os.replace`。直接 `open(path, "w")`
  写一半时断电/崩溃会留下**语法不完整的 JSON**，下次启动读不回来，用户的列表
  就整个没了。原子替换的结果只有"旧内容"或"新内容"，不存在"半个文件"。
- **读写失败一律静默降级**（返回 `False` / `[]`，不抛）。播放列表是锦上添花，
  没有它播放器照样得能跑。读的时候还会顺手清掉空串、非字符串、重复项。

---

## 五、面板布局：三个踩过的坑

`_build_playlist_panel()`（`final_player.py:695`）建右侧那一栏，236px 全高。

### 坑 1：`rowspan=6` 让底栏在列表左边收住

顶层 grid 本来是"1 列 6 行"。这里开出**第 1 列**，列表栏挂
`(row=0, column=1, rowspan=6, sticky="ns")` 占满右边缘，而进度条 / 按钮 / 转换
进度面板仍然只占第 0 列 —— 于是底栏会**在列表栏左边收住，不会钻到列表下面**。

### 坑 2：`pack_propagate(False)`，不是 `grid_propagate`

面板的**子控件是 pack 进去的**，而 `grid_propagate` 只管"用 grid 管理的子控件"。
第一版写成了 `grid_propagate(False)`，面板照样被里面的 Listbox 按内容撑宽 ——
实测宽度是 **169 而不是 236px**。是尺寸冒烟检查（打印 `winfo_width()`）抓出来的。

### 坑 3：隐藏列表时必须把列宽归零

```python
self.playlist_frame.grid_remove()
self.root.grid_columnconfigure(1, minsize=0)     # ← 这一句不能少
```

`grid_remove` 只是"不管理这个控件"，**列宽约束还在**，视频区右边会留下一条
236px 的空带。恢复显示时再把 `minsize` 设回 `PLIST_MIN_WIDTH`。

### 其它小决定

- **列表项显示文件名**而不是整条路径（路径动辄上百字符，放不下）；完整路径在
  选中时显示到下面的状态行（`_on_playlist_select`）。
- **`exportselection=False`**：否则列表框的选中会和"系统选中区"绑定，别处一选
  东西这边的高亮就没了，看着像"选中丢了"。
- 窗口默认几何从 950×700 **加宽到 1180**，给列表栏让出位置。
- **底栏多了一个"隐藏列表"按钮**，可以随时把列表收起来。

---

## 六、两个接缝的具体形状

### 接缝 1：`open_video(file_path=None, record=True)`

```python
if record:
    self._playlist_add([file_path])
    self._refresh_playlist_ui(select_path=file_path)
...
return True     # 失败时 return False
```

- 记录动作放在 **"已经真的能播"之后**（`final_player.py:1349`）—— 打不开的文件
  不该进列表；
- `record=False` 只有一个调用方：`on_conversion_done`。转换产物躺在 `%TEMP%` 里，
  下次转换就被删掉，记进列表只会留下永远点不开的死条目；
- **返回值**给片尾续播用：`False` 时把这一项记进"播不动"名单。

### 接缝 2：`update_frame()` 的片尾分支

```python
if not ret:
    next_path = self._advance_playlist() if not force else None
    if next_path is not None:
        if not self.open_video(next_path):
            self._playlist_failed.add(next_path)
        return                                   # ⚠️ 必须立刻 return
    ...原来的"原地无缝回绕"...
```

三个要点：

1. **必须立刻 `return`。** `open_video` 内部已经停掉旧的播放循环、重新排好了
   下一帧；这里再往下走就会**再排一次**，两套定时器叠加 = 画面以两倍速跑
   （第 1 轮踩过的那种"加速"）。
2. **`force=True`（暂停态重绘一帧）不续播。** 那不是"播完了"，只是用户改了视差
   或切了显示模式要刷新一张画面，不能顺手把文件换掉。
3. **没有可续播的项时，原来的"原地回绕"逻辑一行未改** —— 包括第 11 轮修好的
   "回绕时音频也必须回 0 秒"。

---

## 七、自动续播：`_advance_playlist()` 刻意保守

返回"下一个要播的路径"，没有可续播的项则返回 `None`。**返回 None 的条件有三个**：

```python
if not self.autoplay_next or n < 2:
    return None
cur = self._playlist_index_of(self.current_video_path)
if cur is None:
    return None
```

`n < 2` 这一条是关键设计：

> **只有列表里还有别的项时**才返回路径。于是"只放一个文件"的老用法完全不受
> 影响 —— 那时列表里只有当前这一项，返回 `None`，走 `update_frame` 原来的
> "原地无缝回绕"（不用重新打开文件，也就没有那一下卡顿）。

找不到的条目会被记进 `self._playlist_failed` 并 `continue` 跳过，
**保证这个循环一定会往前推进**，不会卡在同一项上反复重试（那就是死循环）。
用户手点这一项时照旧尝试播放，等于手动重试。

`_playlist_failed` 只是一个 `set`，不落盘 —— 它描述的是"这次运行期内的现实"，
下次启动重新判定更合理。

---

## 八、测试：四层，最值钱的是第四层

`tests/test_playlist.py`，十个小节、54 项断言，从里到外四层：

| 层 | 测什么 | 为什么放这一层 |
|---|---|---|
| ① 纯函数 | `playlist_store` 的落盘 / 读回 / 坏 JSON 容错 | 不碰 Tk，跑得飞快，坏了最好定位 |
| ② 纯函数 | `expand_dropped_paths` 的过滤 / 递归 / 去重 / 上限 | 同上 |
| ③ 面板 | 增删清空后"列表 ↔ 界面 ↔ 磁盘"三者一致 | 这是数据模型的全部契约 |
| ④ 拖放 | **自己造一个真的 HDROP，投一条真的 `WM_DROPFILES`** | 见下 |

### 为什么第 ④ 层要造真的消息

`test_playlist.py:39-84` 用 `ctypes` 造了一个 `DROPFILES` 结构 + `GlobalAlloc`
一块共享内存，然后 `SendMessageW(hwnd, WM_DROPFILES, handle, 0)`。

> 为什么不"直接调 `_on_files_dropped` 就算了"：那样只能证明回调本身没写错，
> 证不了 ctypes 那一层（句柄宽度、WNDPROC 转发、`DragQueryFileW` 取值）是通的
> —— 而那一层恰恰是这种功能唯一会崩的地方。合成 HDROP 是本套件里最值钱的几行。

于是这条用例走完的是**完整链路**：
窗口消息 → 替换的 WNDPROC → `DragQueryFileW` → 回 Tk 队列 → 进列表并播放。

### 还测了"片尾真的会切过去"

第 ⑥ 节不是调一下 `_advance_playlist()` 看返回值，而是：

```python
app.open_video(short_a)
ok = pump_until(root, lambda: app.current_video_path == short_b, timeout=8.0)
```

用两个 0.6 秒的真短片，**等它自己播完**再断言切过去了。

### 第 ⑧ 节：焦点在列表上时的按键归属

延续第 12 轮的让路矩阵，确认新控件接对了：

- 方向键、空格 → **让**给列表（它的原生含义就是移动选中 / 选中当前项）；
- `M`、`K` → **归播放器**（列表对字母键毫无反应，让路就成哑巴了）。

---

## 九、一个只在 `--full` 下复现的假失败（跨套件串味）

`--full` 第一次跑完，播放列表组 **5 项 FAIL**，而单独跑 `test_playlist.py` 却是全绿。

**根因**：`_harness` 用 `itertools.count(1)` 生成临时播放列表文件名
（`playlist_1.json`、`playlist_2.json`…）。但 **`run_tests.py` 的每组套件是独立
子进程，计数器都从 1 重来** —— 于是所有套件的第一个 app 都落到
`playlist_1.json` 这个文件上。前一组（`test_shortcuts` 也会 `open_video`）留下的
条目，串到了后一组的"新窗口"里，`app.playlist == []` 当场就是假的。

**修法**（`tests/_harness.py:391`）：

```python
path = os.path.join(work_dir("_playlist"),
                    "playlist_%d_%d.json" % (os.getpid(), next(_app_counter)))
try:
    os.remove(path)      # 起手状态一定是空列表，与上一次遗留无关
except OSError:
    pass
```

文件名带 pid，且**交付给 app 之前先删同名文件**。
`--full` 重跑 → 12 组全绿。

这条写进了文档，也写进了记忆文件：**看到"单跑绿、全量红"先怀疑跨进程共享的
临时文件**。

### 另一个隔离：老套件显式关掉续播

`load_app(autoplay_next=False)` 是**默认值**。老套件量的是"单文件"的片尾行为
（片尾回绕卡不卡、测速窗口会不会穿过 EOF），而它们跑的过程中会打开好几个视频、
播放列表因此会攒下半打条目 —— 续播一开，片尾就从"回绕同一个文件"变成"切到列表
里的下一个"，那些用例量的东西就悄悄变了样子。只有 `test_playlist.py` 显式传
`autoplay_next=True`。

---

## 十、验证与规模

```
run_tests.py --full（干净环境 env -u PYTHONIOENCODING -u PYTHONUTF8 + 输出重定向）
→ 12 组全 OK、256 项断言、0 FAIL、退出码 0、340.2s
```

（256 项断言的分布：功能用例 40 / 定时器与挂起请求 23 / 快捷键与逐帧步进 86 /
**播放列表与拖放 54（本轮新增）** / 转换补音轨 15 / 稳态速度 3 / 乱序压力 seed=1..5 各 7。）

| | 改动前 | 改动后 |
|---|---|---|
| `final_player.py` | 3002 行 | **3399 行**（+397） |
| 方法数 | 97 | 117 |
| 新增文件 | — | `filedrop.py` 250 / `playlist_store.py` 80 / `tests/test_playlist.py` 336 |
| `run_tests.py` 的子任务数 | 11 项（语法检查 + 5 个套件 + 5 个压力种子） | **12 项**（多出"播放列表与拖放"） |

本轮 diff：`7 files changed, 1120 insertions(+), 16 deletions(-)` ——
**只删了 16 行**，那 16 行还都是被替换掉的旧写法（`open_video` 的头部、
`SYNTAX_TARGETS`、套件清单）。老播放逻辑一行未动。

---

## 附：想快速看懂这部分代码，按这个顺序读

1. `playlist_store.py` 全文（80 行，最简单，先建立"列表是什么"的概念）；
2. `filedrop.py` 的**纯函数部分**（`expand_dropped_paths`，到第 93 行）；
3. `final_player.py` 的 `_init_playlist_state` → `_build_playlist_panel`；
4. `_playlist_add` / `_refresh_playlist_ui` / `_playlist_index_of` 三个方法
   （数据模型的核心三角）；
5. `_advance_playlist` + `update_frame` 片尾那 6 行（功能与老代码的唯一实质接触）；
6. `filedrop.py` 的 Windows 部分（最后读，也最难，不读也不影响理解主线）。
