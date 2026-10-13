import os
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

import cv2
import torch
import numpy as np
from transformers import AutoImageProcessor, AutoModelForDepthEstimation
from PIL import Image

# 1. 确认使用 NVIDIA 显卡
device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"正在使用设备: {device}")

# 2. 加载 AI 模型 (Depth Anything V2 Small - 极小极快)
print("正在加载深度估计模型 (大约100MB)...")
model_id = "depth-anything/Depth-Anything-V2-Small-hf"
image_processor = AutoImageProcessor.from_pretrained(model_id)
model = AutoModelForDepthEstimation.from_pretrained(model_id).to(device)

# 3. 读取你的视频的第一帧
cap = cv2.VideoCapture('city.mp4')
ret, frame = cap.read()
cap.release()

if not ret:
    print("无法读取视频，请检查 city.mp4 是否在项目文件夹内。")
    exit()

# 4. 预处理图像格式
image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
image_pil = Image.fromarray(image)

# 5. 让 AI 推理画面的深度
print("AI 正在理解画面远近...")
inputs = image_processor(images=image_pil, return_tensors="pt").to(device)
with torch.no_grad():
    outputs = model(**inputs)
    predicted_depth = outputs.predicted_depth

# 6. 把 AI 输出的深度图放大回原图大小
prediction = torch.nn.functional.interpolate(
    predicted_depth.unsqueeze(1),
    size=image_pil.size[::-1],
    mode="bicubic",
    align_corners=False,
)
output = prediction.squeeze().cpu().numpy()

# 7. 归一化：让最近的地方变白(255)，最远的地方变黑(0)
formatted = (output * 255 / np.max(output)).astype("uint8")

# 8. 保存深度图
cv2.imwrite("depth_output.jpg", formatted)
print("处理完成！请在左侧项目文件夹中查看 depth_output.jpg 文件。")