# -*- coding: utf-8 -*-
"""把"从资源管理器把文件拖进窗口"接到 Tk 上 —— 纯 ctypes，不依赖第三方库。

## 为什么不用现成的库
`tkinterdnd2` 要额外安装（还是带二进制的包），而且它要求用 `TkinterDnD.Tk()`
当根窗口 —— 整个程序都得换根，代价远大于收益；`windnd` 早就不维护了。
这里要的其实只是"拿到一份拖进来的文件路径列表"，Windows 的**旧式拖放 API**
（`DragAcceptFiles` + `WM_DROPFILES`）几十行就够，而且零新依赖。

## 它怎么工作
1. `DragAcceptFiles(hwnd, True)` 把窗口声明成"接受文件拖放"；
2. 拖放发生时，系统给那个窗口发 `WM_DROPFILES` —— 所以要把窗口过程
   （WNDPROC）取出来换成我们自己的，处理完再用 `CallWindowProcW` 原样转发；
3. 用 `DragQueryFileW` 取出路径、`DragFinish` 释放，然后**用 `root.after(0, ...)`
   把结果丢回 Tk 的事件队列**再处理。

第 3 步不是可选的。WNDPROC 是在 Tk 的消息循环里被调用的，在里面直接操作 Tk
控件属于**重入**（控件可能正被 Tk 更新到一半），很容易变成偶发崩溃。
`after(0, ...)` 只是把回调排到队尾，等于"等这轮消息处理完再说"。

## 两个必须记住的坑
- **回调对象必须被强引用住**。WNDPROC 是 C 层持有的函数指针，Python 这边
  一旦被 GC 回收，系统下一次回调就是野指针 → 直接崩。所以 `_TARGETS` 是
  模块级全局，专门用来"钉住"这些对象，别改成局部变量。
- **必须转发所有不认识的窗口消息**。漏一条（比如 WM_NCCALCSIZE）窗口就画不
  出来。除了 WM_DROPFILES，其余一律 `CallWindowProcW` 交还给 Tk。
"""

import ctypes
import os
import sys

# 拖放时认哪些扩展名（打开文件对话框也用这一份，保证两处一致）
DEFAULT_VIDEO_EXTS = (".mp4", ".m4v", ".mkv", ".avi", ".mov", ".wmv", ".flv",
                      ".webm", ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp")

# 拖一整个目录进来时的数量上限：防止手滑把 C:\ 拖进来把界面卡死
MAX_DROP_FILES = 500

WM_DROPFILES = 0x0233
GWLP_WNDPROC = -4

# 强引用登记处 —— 见模块文档"两个必须记住的坑"第一条
_TARGETS = []


# ============================================================
# 纯函数部分（不碰 Windows API，可单独测试）
# ============================================================
def is_video_file(path, extensions=DEFAULT_VIDEO_EXTS):
    return os.path.splitext(path)[1].lower() in tuple(e.lower() for e in extensions)


def expand_dropped_paths(paths, extensions=DEFAULT_VIDEO_EXTS, max_files=MAX_DROP_FILES):
    """把拖进来的路径整理成"能播的视频文件"列表。

    - **目录**：递归收集其中的视频；每个目录内按文件名排序后再往下，所以
      顺序是稳定的（资源管理器的拖放顺序本身不可预期，不能依赖）；
    - **文件**：扩展名在白名单里才收；
    - **去重**：按 `os.path.normcase` 判重 —— Windows 路径大小写不敏感，
      `C:\\a.mp4` 和 `c:\\A.MP4` 是同一个文件，但字符串不相等；
    - 最多 `max_files` 个。
    """
    exts = tuple(e.lower() for e in extensions)
    out, seen = [], set()
    for raw in paths:
        if not raw:
            continue
        p = os.path.abspath(raw)
        if os.path.isdir(p):
            candidates = []
            for root, dirs, files in os.walk(p):
                dirs.sort()
                for name in sorted(files):
                    fp = os.path.join(root, name)
                    if os.path.splitext(fp)[1].lower() in exts:
                        candidates.append(fp)
                if len(out) + len(candidates) >= max_files:
                    break
        elif os.path.splitext(p)[1].lower() in exts:
            candidates = [p]
        else:
            candidates = []

        for c in candidates:
            key = os.path.normcase(c)
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
            if len(out) >= max_files:
                return out
    return out


# ============================================================
# Windows 部分
# ============================================================
def _load_windows_api():
    """按需加载并声明函数签名。非 Windows 返回 None。

    每个函数都显式写 `argtypes` / `restype`：Windows 是 64 位句柄，
    不声明的话 ctypes 默认按 32 位 int 传，句柄会被截断 —— 只是"有时"
    崩，属于最难查的那类问题。
    """
    if sys.platform != "win32":
        return None
    from ctypes import wintypes

    long_ptr = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    user32.GetParent.restype = wintypes.HWND
    user32.GetParent.argtypes = [wintypes.HWND]

    setter = user32.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 \
        else user32.SetWindowLongW
    setter.restype = long_ptr
    setter.argtypes = [wintypes.HWND, ctypes.c_int, long_ptr]

    user32.CallWindowProcW.restype = long_ptr
    user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM]

    shell32.DragAcceptFiles.restype = None
    shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]

    shell32.DragQueryFileW.restype = wintypes.UINT
    shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                       wintypes.LPWSTR, wintypes.UINT]

    shell32.DragFinish.restype = None
    shell32.DragFinish.argtypes = [ctypes.c_void_p]

    wndproc = ctypes.WINFUNCTYPE(long_ptr, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)
    return {
        "user32": user32, "shell32": shell32, "wintypes": wintypes,
        "long_ptr": long_ptr, "setter": setter, "wndproc": wndproc,
    }


_API = _load_windows_api()


def _query_dropped_files(hdrop):
    """从一个 HDROP 里取出全部路径（UTF-16 长路径，缓冲区给到 32768）"""
    buf = ctypes.create_unicode_buffer(32768)
    handle = ctypes.c_void_p(hdrop)
    count = _API["shell32"].DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
    out = []
    for i in range(int(count)):
        if _API["shell32"].DragQueryFileW(handle, i, buf, len(buf)):
            out.append(buf.value)
    return out


class _DropTarget:
    """一个窗口的拖放接线：换掉它的 WNDPROC，只拦 WM_DROPFILES。"""

    def __init__(self, root, hwnd, on_files):
        self.root = root
        self.hwnd = hwnd
        self.on_files = on_files
        # ⚠️ self._proc 必须一直活着（_TARGETS 钉住了 self），否则传到 C 层的
        #    函数指针会被 GC 掉，下一次拖放就是野指针。
        self._proc = _API["wndproc"](self._wndproc)
        self._old = _API["setter"](_API["wintypes"].HWND(hwnd), GWLP_WNDPROC,
                                   ctypes.cast(self._proc, ctypes.c_void_p).value)
        _API["shell32"].DragAcceptFiles(_API["wintypes"].HWND(hwnd), True)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_DROPFILES:
            try:
                paths = _query_dropped_files(wparam)
            except Exception:
                paths = []
            try:
                _API["shell32"].DragFinish(ctypes.c_void_p(wparam))
            except Exception:
                pass
            if paths:
                self._dispatch(paths)
            return 0                      # 已处理，不再往下传
        return _API["user32"].CallWindowProcW(
            ctypes.c_void_p(self._old), hwnd, msg, wparam, lparam)

    def _dispatch(self, paths):
        """把路径交给调用方 —— 但**排到 Tk 事件队列尾部**，别在消息循环里直接改界面。"""
        try:
            self.root.after(0, lambda: self.on_files(list(paths)))
        except Exception:
            # after 都失败了（窗口正在销毁）→ 至少别把这次拖放丢掉
            try:
                self.on_files(list(paths))
            except Exception:
                pass


def _window_chain(root):
    """要挂拖放的窗口句柄，顺序 = [顶层窗口, 客户区窗口]。

    Tk 在 Windows 上给**每个控件**都建了真 HWND，所以拖到视频画面上时命中的
    是某个子窗口。系统的旧式拖放会从命中窗口沿父链往上找第一个"声明接受拖放"
    的窗口，因此给顶层挂上就够了；这里把客户区窗口也一起挂上，是防某些系统
    版本不往上找。两个都挂**不会**重复触发：一条消息只会投给其中一个窗口。
    """
    try:
        root.update_idletasks()
        child = int(root.winfo_id())
    except Exception:
        return []
    out = []
    try:
        parent = _API["user32"].GetParent(_API["wintypes"].HWND(child))
        if parent:
            out.append(int(parent))
    except Exception:
        pass
    out.append(child)

    seen, uniq = set(), []
    for h in out:
        if h and h not in seen:
            seen.add(h)
            uniq.append(h)
    return uniq


def enable_file_drop(root, on_files):
    """让 `root`（Tk 根窗口）接受"把文件拖进来"。

    `on_files(paths)` 会在**主线程**、且是在 Tk 事件队列里被调用
    （见 `_DropTarget._dispatch`），可以直接操作界面。

    返回 True = 挂上了；False = 平台不支持或挂接失败。
    这是可选增强 —— 失败只是没有拖放，不影响播放器其它功能，所以不抛异常。
    """
    if _API is None:
        return False
    try:
        hwnds = _window_chain(root)
        if not hwnds:
            return False
        for hwnd in hwnds:
            _TARGETS.append(_DropTarget(root, hwnd, on_files))
        return True
    except Exception:
        return False
