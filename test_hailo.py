#!/usr/bin/env python3
"""Quick test to verify Hailo-8 inference and inspect output format."""

import time
import cv2
import numpy as np
from hailo_platform import HEF, VDevice, HailoStreamInterface
from hailo_platform import InferVStreams, ConfigureParams, InputVStreamParams, OutputVStreamParams

HEF_PATH = "/usr/share/hailo-models/yolov8s_h8.hef"
CAMERA_INDEX = 0
PERSON_CLASS_ID = 0
CONFIDENCE_THRESHOLD = 0.5

print("Loading HEF model...")
hef = HEF(HEF_PATH)

input_info = hef.get_input_vstream_infos()[0]
output_info = hef.get_output_vstream_infos()[0]
print(f"Input: {input_info.name} {input_info.shape} {input_info.format.type}")
print(f"Output: {output_info.name} {output_info.shape} {output_info.format.type}")

# Capture a frame with countdown
print("\nCapturing frame in...")
for i in range(5, 0, -1):
    print(f"  {i}...")
    time.sleep(1)
print("  Snap!")
cap = cv2.VideoCapture(CAMERA_INDEX)
ret, frame = cap.read()
cap.release()

if not ret:
    print("ERROR: Could not capture frame")
    exit(1)

frame_h, frame_w = frame.shape[:2]
print(f"Frame: {frame_w}x{frame_h}")

# Preprocess
h, w = input_info.shape[0], input_info.shape[1]
resized = cv2.resize(frame, (w, h))

# Run inference
print("\nRunning inference...")
with VDevice() as vdevice:
    configure_params = ConfigureParams.create_from_hef(
        hef, interface=HailoStreamInterface.PCIe
    )
    network_group = vdevice.configure(hef, configure_params)[0]

    input_params = InputVStreamParams.make(network_group)
    output_params = OutputVStreamParams.make(network_group)

    input_data = {input_info.name: np.expand_dims(resized, axis=0)}

    # Activate the network group, then run inference
    with network_group.activate():
        with InferVStreams(network_group, input_params, output_params) as pipeline:
            # Warm up
            _ = pipeline.infer(input_data)

            # Timed inference
            t0 = time.time()
            results = pipeline.infer(input_data)
            t1 = time.time()

print(f"\nInference time: {(t1-t0)*1000:.1f}ms")

# Explore the output structure
raw = results[output_info.name]
print(f"\nOutput type: {type(raw)}")
if isinstance(raw, list):
    print(f"Output length: {len(raw)}")
    if len(raw) > 0:
        print(f"First element type: {type(raw[0])}")
        if isinstance(raw[0], list):
            print(f"First element length: {len(raw[0])}")
            if len(raw[0]) > 0:
                item = raw[0][0]
                print(f"First class type: {type(item)}")
                if isinstance(item, np.ndarray):
                    print(f"First class shape: {item.shape}, dtype: {item.dtype}")
                    print(f"First class data: {item}")
                elif isinstance(item, list):
                    print(f"First class length: {len(item)}")
                    if len(item) > 0:
                        print(f"First det type: {type(item[0])}")

# Try to parse person detections
print(f"\nPerson detections (class {PERSON_CLASS_ID}):")
try:
    # Structure might be: raw[batch][class] = array of detections
    if isinstance(raw, list) and len(raw) > 0:
        batch = raw[0] if isinstance(raw[0], list) else raw
        person_data = batch[PERSON_CLASS_ID]
        if isinstance(person_data, np.ndarray):
            print(f"  Shape: {person_data.shape}")
            print(f"  Data:\n{person_data}")
            # Try parsing as rows of [y_min, x_min, y_max, x_max, score]
            if person_data.ndim == 2:
                for i in range(person_data.shape[0]):
                    row = person_data[i]
                    print(f"  Det {i}: {row}")
                    if len(row) >= 5:
                        y_min, x_min, y_max, x_max, score = row[:5]
                        if score > CONFIDENCE_THRESHOLD:
                            x1 = int(x_min * frame_w)
                            y1 = int(y_min * frame_h)
                            x2 = int(x_max * frame_w)
                            y2 = int(y_max * frame_h)
                            print(f"    -> Person: ({x1},{y1})-({x2},{y2}) conf={score:.2f}")
        else:
            print(f"  Type: {type(person_data)}, value: {person_data}")
except Exception as e:
    print(f"  Parse error: {e}")
    import traceback
    traceback.print_exc()

print("\nDone!")
