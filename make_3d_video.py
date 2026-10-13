import os

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

import cv2
import torch
import numpy as np
from transformers import AutoImageProcessor, AutoModelForDepthEstimation
from PIL import Image

# 1. 准备设备与模型
device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"正在使用设备: {device}")
model_id = "depth-anything/Depth-Anything-V2-Small-hf"
image_processor = AutoImageProcessor.from_pretrained(model_id)
model = AutoModelForDepthEstimation.from_pretrained(model_id).to(device)

# 2. 打开原视频
cap = cv2.VideoCapture('city.mp4')
width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
fps = cap.get(cv2.CAP_PROP_FPS)

# 为了性能，我们限制只处理前 100 帧（大约 3-4 秒），方便你测试
MAX_FRAMES = 100

# 3. 准备输出视频 (宽度变为两倍，因为左右拼接)
fourcc = cv2.VideoWriter_fourcc(*'mp4v')
out = cv2.VideoWriter('output_3d_video.mp4', fourcc, fps, (width * 2, height))

max_disparity = 20  # 视差强度，太大容易晕
frame_count = 0

# 4. 预先计算映射网格 (避免在循环里重复计算，节省时间)
x_base, y_base = np.meshgrid(np.arange(width), np.arange(height))
x_base, y_base = x_base.astype(np.float32), y_base.astype(np.float32)

print("开始处理视频，这可能需要几分钟，请耐心等待...")

while cap.isOpened() and frame_count < MAX_FRAMES:
    ret, frame = cap.read()
    if not ret:
        break

    frame_count += 1
    if frame_count % 10 == 0:
        print(f"正在处理第 {frame_count} 帧...")

    # --- 第1步：AI 推理深度 ---
    image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    image_pil = Image.fromarray(image)
    inputs = image_processor(images=image_pil, return_tensors="pt").to(device)
    with torch.no_grad():
        outputs = model(**inputs)
        predicted_depth = outputs.predicted_depth

    prediction = torch.nn.functional.interpolate(
        predicted_depth.unsqueeze(1),
        size=image_pil.size[::-1],
        mode="bicubic",
        align_corners=False,
    )
    depth_output = prediction.squeeze().cpu().numpy()
    depth_map = (depth_output * 255 / np.max(depth_output)).astype("uint8")

    # --- 第2步：视点合成 (DIBR) ---
    depth_normalized = depth_map.astype(np.float32) / 255.0
    shift = depth_normalized * max_disparity

    map_x_left = x_base + shift
    map_x_right = x_base - shift

    left_eye = cv2.remap(frame, map_x_left, y_base, cv2.INTER_LINEAR)
    right_eye = cv2.remap(frame, map_x_right, y_base, cv2.INTER_LINEAR)

    # --- 第3步：拼接并写入 ---
    sbs_frame = np.hstack((left_eye, right_eye))
    out.write(sbs_frame)

# 5. 释放资源
cap.release()
out.release()
print("处理完成！请查看 output_3d_video.mp4")