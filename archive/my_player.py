import cv2
import numpy as np

video_path = 'output_3d_video.mp4'
cap = cv2.VideoCapture(video_path)

if not cap.isOpened():
    print("无法打开视频文件")
    exit()

fps = cap.get(cv2.CAP_PROP_FPS)
delay = int(1000 / fps) if fps > 0 else 33

print("播放器已启动！")
print("操作说明：按 'q' 退出 | 按 '空格' 暂停/播放 | 按 'r' 切换红蓝3D | 按 'l' 开启/关闭循环")

is_paused = False
red_blue_mode = False
loop_play = True
frame = None  # 初始化 frame 为空

while True:
    # 1. 读取画面（只有在非暂停状态下才读取）
    if not is_paused:
        ret, frame = cap.read()

        if not ret:
            if loop_play:
                # 倒带重播
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ret, frame = cap.read()
                if not ret:
                    print("无法读取视频，可能是文件损坏，退出。")
                    break
            else:
                # 播完了，且不循环，自动暂停
                print("视频播放完毕。按 '空格' 重新播放，按 'q' 退出，按 'l' 开启循环。")
                is_paused = True

    # 2. 🛡️ 终极安全气囊（如果空帧，生成一张黑底白字的提示图）
    if frame is None or frame.size == 0:
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        cv2.putText(frame, "No Frame / Video End", (50, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)

    # 3. 实时 3D 处理区
    if red_blue_mode:
        h, w = frame.shape[:2]
        if w >= 2:
            left_eye = frame[:, 0:w // 2]
            right_eye = frame[:, w // 2:w]
            left_eye = cv2.resize(left_eye, (w // 2, h))
            right_eye = cv2.resize(right_eye, (w // 2, h))
            b_l, g_l, r_l = cv2.split(left_eye)
            b_r, g_r, r_r = cv2.split(right_eye)
            display_frame = cv2.merge((b_l, g_l, r_r))
        else:
            display_frame = frame
    else:
        display_frame = frame

    # 4. 显示画面
    cv2.imshow('My 3D Player', display_frame)

    # 5. 键盘控制（无论如何都会执行，绝对不会死锁）
    # 注意：暂停时，waitKey(0) 会无限等待按键；播放时，waitKey(1) 是每1毫秒刷新一次
    key = cv2.waitKey(0 if is_paused else 1) & 0xFF

    if key == ord('q'):
        break
    elif key == ord(' '):  # 空格键
        if is_paused and not loop_play:
            # 如果是因为播完暂停的，按空格可以重置到开头重播
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            is_paused = False
            print("重新播放")
        else:
            is_paused = not is_paused
            print("暂停" if is_paused else "继续播放")
    elif key == ord('r'):
        red_blue_mode = not red_blue_mode
        print("切换红蓝3D模式" if red_blue_mode else "切换回普通模式")
    elif key == ord('l'):
        loop_play = not loop_play
        print("循环播放已开启" if loop_play else "循环播放已关闭")

cap.release()
cv2.destroyAllWindows()