import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import cv2
import numpy as np
from PIL import Image, ImageTk
import time


class VideoPlayerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("我的 AI 3D 播放器 - 进阶版")
        self.root.geometry("850x650")

        # --- 播放器状态 ---
        self.cap = None
        self.is_playing = False
        self.is_paused = False
        self.red_blue_mode = False
        self.playback_speed = 1.0
        self.is_fullscreen = False  # 【新增】全屏状态标志

        # --- 进度条核心变量 ---
        self.total_frames = 0
        self.current_frame = 0
        self.is_seeking = False
        self.seek_var = tk.DoubleVar()
        self.seek_timer = None

        # 供截图使用
        self.current_frame_image = None

        # --- 创建界面组件 ---
        # 1. 视频显示区
        self.video_label = tk.Label(self.root, bg="black")
        self.video_label.pack(expand=True, fill=tk.BOTH)

        # 2. 进度条区域
        self.progress_frame = tk.Frame(self.root)
        self.progress_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=5)

        self.scale = ttk.Scale(
            self.progress_frame,
            from_=0,
            to=100,
            orient=tk.HORIZONTAL,
            variable=self.seek_var,
            command=self.on_scale_move
        )
        self.scale.pack(fill=tk.X)

        self.scale.bind("<ButtonRelease-1>", self.on_scale_release)
        self.scale.bind("<Button-1>", self.on_scale_click)

        # 3. 按钮区域
        self.btn_frame = tk.Frame(self.root)
        self.btn_frame.pack(side=tk.BOTTOM, fill=tk.X)

        self.btn_open = tk.Button(self.btn_frame, text="打开视频", command=self.open_video)
        self.btn_open.pack(side=tk.LEFT, padx=10, pady=10)

        self.btn_play = tk.Button(self.btn_frame, text="播放", command=self.toggle_play)
        self.btn_play.pack(side=tk.LEFT, padx=10, pady=10)

        self.btn_3d = tk.Button(self.btn_frame, text="切换红蓝3D", command=self.toggle_3d)
        self.btn_3d.pack(side=tk.LEFT, padx=10, pady=10)

        self.btn_screenshot = tk.Button(self.btn_frame, text="截图", command=self.save_screenshot)
        self.btn_screenshot.pack(side=tk.LEFT, padx=10, pady=10)

        self.speed_label = tk.Label(self.btn_frame, text="倍速:")
        self.speed_label.pack(side=tk.LEFT, padx=(20, 5))

        self.speed_var = tk.StringVar(value="1.0x")
        self.speed_combo = ttk.Combobox(
            self.btn_frame,
            textvariable=self.speed_var,
            values=["0.5x", "1.0x", "1.5x", "2.0x"],
            width=5,
            state="readonly"
        )
        self.speed_combo.pack(side=tk.LEFT)
        self.speed_combo.bind("<<ComboboxSelected>>", self.change_speed)

        # --- 键盘快捷键绑定 ---
        self.root.bind("<space>", lambda event: self.toggle_play())
        self.root.bind("<F11>", self.toggle_fullscreen)  # 【新增】F11全屏
        self.root.bind("<Escape>", self.toggle_fullscreen)  # 【新增】Esc退出全屏

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def open_video(self):
        file_path = filedialog.askopenfilename(filetypes=[("MP4 Video", "*.mp4"), ("All Files", "*.*")])
        if not file_path:
            return

        if self.cap is not None:
            self.cap.release()

        self.cap = cv2.VideoCapture(file_path)
        if not self.cap.isOpened():
            messagebox.showerror("错误", "无法打开视频文件")
            return

        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.scale.config(to=self.total_frames)
        self.seek_var.set(0)

        self.is_playing = True
        self.is_paused = False
        self.btn_play.config(text="暂停")
        self.update_frame()

    def toggle_play(self):
        if self.cap is None: return
        self.is_paused = not self.is_paused
        self.btn_play.config(text="继续" if self.is_paused else "暂停")
        if not self.is_paused:
            self.update_frame()

    def toggle_3d(self):
        self.red_blue_mode = not self.red_blue_mode

    def change_speed(self, event):
        speed_str = self.speed_var.get()
        self.playback_speed = float(speed_str.replace("x", ""))
        print(f"当前播放速度已调整为: {self.playback_speed}x")

    # ================= 【新增】全屏切换逻辑 =================
    def toggle_fullscreen(self, event=None):
        """切换全屏状态，并隐藏/显示底部操作栏"""
        self.is_fullscreen = not self.is_fullscreen

        if self.is_fullscreen:
            # 进入全屏
            self.root.attributes('-fullscreen', True)
            # 隐藏底部组件
            self.progress_frame.pack_forget()
            self.btn_frame.pack_forget()
            print("已进入全屏模式，按 Esc 或 F11 退出")
        else:
            # 退出全屏
            self.root.attributes('-fullscreen', False)
            # 恢复底部组件布局 (按原来的顺序)
            self.progress_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=5)
            self.btn_frame.pack(side=tk.BOTTOM, fill=tk.X)
            print("已退出全屏模式")

    def save_screenshot(self):
        if self.current_frame_image is None:
            messagebox.showwarning("提示", "当前没有可截图的画面，请先打开视频。")
            return

        # 保持单张截图覆盖，不带时间戳
        filename = "screenshot.jpg"
        success = cv2.imwrite(filename, self.current_frame_image)

        if success:
            print(f"截图已成功保存为 {filename}")
            messagebox.showinfo("截图成功", f"画面已保存为 {filename}")
        else:
            messagebox.showerror("错误", "截图保存失败，请检查磁盘权限或空间。")

    # ================= 进度条交互（防抖优化版） =================
    def on_scale_move(self, value):
        self.is_seeking = True
        if self.cap is None: return
        current_sec = int(float(value) / self.cap.get(cv2.CAP_PROP_FPS))
        total_sec = int(self.total_frames / self.cap.get(cv2.CAP_PROP_FPS))
        self.root.title(f"我的 AI 3D 播放器 - {current_sec}秒 / {total_sec}秒 (速度: {self.playback_speed}x)")

        if self.seek_timer is not None:
            self.root.after_cancel(self.seek_timer)
        self.seek_timer = self.root.after(200, self.finish_seek)

    def on_scale_click(self, event):
        if self.cap is None: return
        width = self.scale.winfo_width()
        if width <= 1: return

        ratio = max(0.0, min(1.0, event.x / width))
        target_frame = int(ratio * self.total_frames)
        self.seek_var.set(target_frame)
        self.is_seeking = True

        if self.seek_timer is not None:
            self.root.after_cancel(self.seek_timer)
        self.seek_timer = self.root.after(200, self.finish_seek)

        if self.is_paused:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
            self.update_frame(force=True)

    def on_scale_release(self, event):
        if self.cap is None: return
        if self.seek_timer is not None:
            self.root.after_cancel(self.seek_timer)
        self.finish_seek()

    def finish_seek(self):
        if self.cap is None: return
        target_frame = int(self.seek_var.get())
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
        self.is_seeking = False

        if self.is_paused:
            self.update_frame(force=True)
        self.seek_timer = None

    # ================= 核心循环 =================
    def update_frame(self, force=False):
        if self.cap is None: return
        if self.is_seeking and not force:
            self.root.after(30, self.update_frame)
            return

        if not self.is_playing or (self.is_paused and not force):
            return

        # 记录开始时间
        start_time = time.time()

        ret, frame = self.cap.read()
        if not ret:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret, frame = self.cap.read()
            if not ret: return

        if not self.is_seeking:
            self.current_frame = int(self.cap.get(cv2.CAP_PROP_POS_FRAMES))
            self.seek_var.set(self.current_frame)

        if self.red_blue_mode:
            h, w = frame.shape[:2]
            left_eye = frame[:, 0:w // 2]
            right_eye = frame[:, w // 2:w]
            left_eye = cv2.resize(left_eye, (w // 2, h))
            right_eye = cv2.resize(right_eye, (w // 2, h))
            b_l, g_l, r_l = cv2.split(left_eye)
            b_r, g_r, r_r = cv2.split(right_eye)
            frame = cv2.merge((b_l, g_l, r_r))

        self.current_frame_image = frame.copy()

        # 【优化】用 OpenCV 进行缩放（比 PIL 快得多）
        win_w = self.root.winfo_width()
        win_h = self.root.winfo_height()
        if win_w <= 1 or win_h <= 1:
            win_w, win_h = 850, 520

        # 计算缩放比例，保持宽高比
        h, w = frame.shape[:2]
        scale = min(win_w / w, win_h / h)
        new_w, new_h = int(w * scale), int(h * scale)

        # 使用 cv2.resize 压缩画面
        resized_frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

        # 转回 PIL 格式用于 Tkinter 显示
        cv2image = cv2.cvtColor(resized_frame, cv2.COLOR_BGR2RGBA)
        current_image = Image.fromarray(cv2image)
        imgtk = ImageTk.PhotoImage(image=current_image)
        self.video_label.imgtk = imgtk
        self.video_label.config(image=imgtk)

        # 【优化】动态计算延迟
        # 记录处理这一帧总共花了多少毫秒
        process_time = (time.time() - start_time) * 1000
        target_delay = int(30 / self.playback_speed)  # 我们期望的延迟
        actual_delay = max(1, target_delay - int(process_time))  # 实际延迟 = 目标延迟 - 处理耗时

        self.root.after(actual_delay, self.update_frame)

    def on_close(self):
        self.is_playing = False
        if self.cap is not None:
            self.cap.release()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = VideoPlayerApp(root)
    root.mainloop()