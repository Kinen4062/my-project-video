import cv2
import numpy as np

# 1. 读取原视频的一帧 和 刚才生成的深度图
cap = cv2.VideoCapture('city.mp4')
ret, frame = cap.read()
cap.release()

if not ret:
    print("无法读取视频")
    exit()

# 读取深度图，并转为灰度图
depth_map = cv2.imread('depth_output.jpg', cv2.IMREAD_GRAYSCALE)
# 把深度图缩放到原视频大小
depth_map = cv2.resize(depth_map, (frame.shape[1], frame.shape[0]))

# 2. 把深度图归一化到 0.0 到 1.0 之间
depth_normalized = depth_map.astype(np.float32) / 255.0

# 3. 设置最大视差（数值越大，3D效果越强，但也越容易变形）
max_disparity = 30  # 试试这个数值，你可以之后自己改
shift = depth_normalized * max_disparity

# 4. 生成映射网格 (用于cv2.remap)
h, w = frame.shape[:2]
x, y = np.meshgrid(np.arange(w), np.arange(h))
x, y = x.astype(np.float32), y.astype(np.float32)

# 5. 生成左眼和右眼画面
# 近处像素移动多，远处移动少
map_x_left = x + shift
map_x_right = x - shift

# 用cv2.remap进行像素重映射
left_eye = cv2.remap(frame, map_x_left, y, cv2.INTER_LINEAR)
right_eye = cv2.remap(frame, map_x_right, y, cv2.INTER_LINEAR)

# 6. 把左右眼拼在一起 (SBS - Side by Side 格式)
sbs_3d = np.hstack((left_eye, right_eye))
cv2.imwrite('sbs_3d_test.jpg', sbs_3d)
print("处理完成！请查看 sbs_3d_test.jpg")