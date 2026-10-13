import cv2
import numpy as np

# 1. 读取视频 (请确保视频文件名为 city.mp4，并且和 main.py 在同一个文件夹)
cap = cv2.VideoCapture('city.mp4')

# 获取视频的基本参数
width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
fps = cap.get(cv2.CAP_PROP_FPS)

print(f"成功读取视频！尺寸: {width}x{height}, 帧率: {fps}")

# 2. 准备写入新视频 (生成劣质3D视频)
fourcc = cv2.VideoWriter_fourcc(*'mp4v')
out = cv2.VideoWriter('output_bad_3d.mp4', fourcc, fps, (width, height))

# 3. 循环处理每一帧
frame_count = 0
while cap.isOpened():
    ret, frame = cap.read()
    if not ret:
        break  # 视频读完了，退出循环

    frame_count += 1
    if frame_count % 30 == 0:
        print(f"正在处理第 {frame_count} 帧...")

    # ================= 核心处理区 =================
    # 将画面切成左右两半（模拟左右眼）
    left_half = frame[:, 0:width // 2]
    right_half = frame[:, width // 2:width]

    # 放大左右两半，填满整个画面
    left_resized = cv2.resize(left_half, (width, height))
    right_resized = cv2.resize(right_half, (width, height))

    # 创建一个黑色画布
    combined = np.zeros_like(frame)

    # 隔行交织 (这就是产生横纹的原因)
    combined[0::2, :] = left_resized[0::2, :]
    combined[1::2, :] = right_resized[1::2, :]
    # ==============================================

    # 写入新的视频帧
    out.write(combined)

# 4. 释放资源
cap.release()
out.release()
print("处理完成！请在左侧项目文件夹里查看 output_bad_3d.mp4 文件。")