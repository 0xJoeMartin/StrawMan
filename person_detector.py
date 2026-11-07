"""
Person Detection Module
-----------------------
Uses YOLOv8-pose to detect people in camera frames, returning bounding boxes
plus 17 COCO keypoints per person (nose, eyes, ears, shoulders, elbows, wrists,
hips, knees, ankles).

Supports Hailo-8 accelerator (fast) with ultralytics CPU fallback (slow).
"""

import os
import cv2
import time
import base64
import math
import numpy as np
import config
from config import (
    CAMERA_INDEX, CAMERA_WIDTH, CAMERA_HEIGHT, CAMERA_FPS,
)

# --- Hailo support ---
HAILO_AVAILABLE = False
HAILO_HEF_PATH = "/usr/share/hailo-models/yolov8s_pose_h8.hef"

# --- COCO 17-keypoint indices (standard for YOLOv8-pose) ---
KP_NOSE = 0
KP_LEFT_EYE = 1
KP_RIGHT_EYE = 2
KP_LEFT_EAR = 3
KP_RIGHT_EAR = 4
KP_LEFT_SHOULDER = 5
KP_RIGHT_SHOULDER = 6
KP_LEFT_ELBOW = 7
KP_RIGHT_ELBOW = 8
KP_LEFT_WRIST = 9
KP_RIGHT_WRIST = 10
KP_LEFT_HIP = 11
KP_RIGHT_HIP = 12
KP_LEFT_KNEE = 13
KP_RIGHT_KNEE = 14
KP_LEFT_ANKLE = 15
KP_RIGHT_ANKLE = 16

KEYPOINT_CONFIDENCE = 0.4   # Min per-keypoint visibility to trust its (x, y)
NMS_IOU_THRESHOLD = 0.7     # IoU above which overlapping detections get merged

# Standard COCO 17-keypoint skeleton edges (pairs of keypoint indices).
# Used for debug overlay drawing and any pose-based reasoning that cares about
# limb connectivity (e.g. "wrist near opposite shoulder = arm crossed").
SKELETON_EDGES = (
    # Face
    (KP_NOSE, KP_LEFT_EYE), (KP_NOSE, KP_RIGHT_EYE),
    (KP_LEFT_EYE, KP_LEFT_EAR), (KP_RIGHT_EYE, KP_RIGHT_EAR),
    # Shoulders / arms
    (KP_LEFT_SHOULDER, KP_RIGHT_SHOULDER),
    (KP_LEFT_SHOULDER, KP_LEFT_ELBOW), (KP_LEFT_ELBOW, KP_LEFT_WRIST),
    (KP_RIGHT_SHOULDER, KP_RIGHT_ELBOW), (KP_RIGHT_ELBOW, KP_RIGHT_WRIST),
    # Torso
    (KP_LEFT_SHOULDER, KP_LEFT_HIP), (KP_RIGHT_SHOULDER, KP_RIGHT_HIP),
    (KP_LEFT_HIP, KP_RIGHT_HIP),
    # Legs
    (KP_LEFT_HIP, KP_LEFT_KNEE), (KP_LEFT_KNEE, KP_LEFT_ANKLE),
    (KP_RIGHT_HIP, KP_RIGHT_KNEE), (KP_RIGHT_KNEE, KP_RIGHT_ANKLE),
)

# YOLOv8 DFL projection vector (used to turn 16-bin distance distribution into a scalar)
_DFL_PROJ = np.arange(16, dtype=np.float32)


# --- Body language description from keypoints ---

def _kp_visible(kp: np.ndarray, idx: int) -> bool:
    """True iff keypoint `idx` is visible enough to trust its (x, y)."""
    return kp[idx, 2] >= KEYPOINT_CONFIDENCE


def _facing_direction(kp: np.ndarray) -> str:
    """Infer facing direction from which face keypoints are visible.

    Both eyes + nose visible       → facing the camera/scarecrow
    One eye + one ear (+nose)      → in profile (the visible ear is the side
                                     they're turned toward)
    Only ears, no eyes/nose        → facing away
    Otherwise                      → '' (face occluded or ambiguous)
    """
    nose = _kp_visible(kp, KP_NOSE)
    leye = _kp_visible(kp, KP_LEFT_EYE)
    reye = _kp_visible(kp, KP_RIGHT_EYE)
    lear = _kp_visible(kp, KP_LEFT_EAR)
    rear = _kp_visible(kp, KP_RIGHT_EAR)

    if nose and leye and reye:
        return "facing me"
    # Profile: one ear visible without the other, plus a nose or near-eye
    if nose and lear and not rear:
        return "turned to my right"   # their left side is toward us
    if nose and rear and not lear:
        return "turned to my left"
    # Back of head: no nose, no eyes, but ears visible
    if not nose and not leye and not reye and (lear or rear):
        return "facing away from me"
    return ""


def _arm_position(kp: np.ndarray) -> str:
    """Detect raised-arm gestures from wrist Y vs shoulder Y.

    Image coords have y increasing downward, so a *smaller* wrist y means
    the wrist is *above* the shoulder.
    """
    have_lshoulder = _kp_visible(kp, KP_LEFT_SHOULDER)
    have_rshoulder = _kp_visible(kp, KP_RIGHT_SHOULDER)
    if not (have_lshoulder or have_rshoulder):
        return ""

    shoulder_ys = []
    if have_lshoulder: shoulder_ys.append(kp[KP_LEFT_SHOULDER, 1])
    if have_rshoulder: shoulder_ys.append(kp[KP_RIGHT_SHOULDER, 1])
    shoulder_y = float(np.mean(shoulder_ys))

    raised = 0
    visible_wrists = 0
    if _kp_visible(kp, KP_LEFT_WRIST):
        visible_wrists += 1
        if kp[KP_LEFT_WRIST, 1] < shoulder_y:
            raised += 1
    if _kp_visible(kp, KP_RIGHT_WRIST):
        visible_wrists += 1
        if kp[KP_RIGHT_WRIST, 1] < shoulder_y:
            raised += 1

    if visible_wrists == 0:
        return ""
    if raised == 2:
        return "arms raised overhead"
    if raised == 1:
        return "one arm raised"
    return ""


def _stance(kp: np.ndarray) -> str:
    """Detect crouched stance — knees bunched up close to hips relative to torso length."""
    have_lshoulder = _kp_visible(kp, KP_LEFT_SHOULDER)
    have_rshoulder = _kp_visible(kp, KP_RIGHT_SHOULDER)
    have_lhip = _kp_visible(kp, KP_LEFT_HIP)
    have_rhip = _kp_visible(kp, KP_RIGHT_HIP)
    have_lknee = _kp_visible(kp, KP_LEFT_KNEE)
    have_rknee = _kp_visible(kp, KP_RIGHT_KNEE)

    if not ((have_lshoulder or have_rshoulder)
            and (have_lhip or have_rhip)
            and (have_lknee or have_rknee)):
        return ""

    shoulder_ys = ([kp[KP_LEFT_SHOULDER, 1]] if have_lshoulder else []) \
                + ([kp[KP_RIGHT_SHOULDER, 1]] if have_rshoulder else [])
    hip_ys = ([kp[KP_LEFT_HIP, 1]] if have_lhip else []) \
           + ([kp[KP_RIGHT_HIP, 1]] if have_rhip else [])
    knee_ys = ([kp[KP_LEFT_KNEE, 1]] if have_lknee else []) \
            + ([kp[KP_RIGHT_KNEE, 1]] if have_rknee else [])

    torso = abs(float(np.mean(hip_ys)) - float(np.mean(shoulder_ys)))
    hip_to_knee = abs(float(np.mean(knee_ys)) - float(np.mean(hip_ys)))
    if torso < 1:
        return ""
    # Standing: hip→knee ≈ shoulder→hip. Crouched: hip→knee much shorter.
    if hip_to_knee < torso * 0.55:
        return "crouched down"
    return ""


def _head_tilt(kp: np.ndarray) -> str:
    """Detect head-down posture (nose well below the eye line — phone-watcher pose)."""
    if not _kp_visible(kp, KP_NOSE):
        return ""
    if not (_kp_visible(kp, KP_LEFT_EYE) or _kp_visible(kp, KP_RIGHT_EYE)):
        return ""

    eye_ys = ([kp[KP_LEFT_EYE, 1]] if _kp_visible(kp, KP_LEFT_EYE) else []) \
           + ([kp[KP_RIGHT_EYE, 1]] if _kp_visible(kp, KP_RIGHT_EYE) else [])
    eye_y = float(np.mean(eye_ys))
    nose_y = float(kp[KP_NOSE, 1])

    # Use inter-eye distance as scale; fall back to a default if only one eye visible.
    if _kp_visible(kp, KP_LEFT_EYE) and _kp_visible(kp, KP_RIGHT_EYE):
        scale = abs(float(kp[KP_LEFT_EYE, 0] - kp[KP_RIGHT_EYE, 0]))
    else:
        scale = 30.0
    if scale < 1:
        return ""

    diff = nose_y - eye_y
    if diff > scale * 1.4:
        return "head down"
    return ""


def describe_body_language(keypoints: np.ndarray) -> str:
    """
    Turn a (17, 3) keypoint array into a short natural-language description
    suitable for a per-person scene sentence. Returns '' when nothing
    confident enough to say.

    Signals are deliberately conservative — better to stay quiet than to
    feed the LLM a wrong observation.
    """
    if keypoints is None or keypoints.shape != (17, 3):
        return ""

    parts = []
    for fn in (_facing_direction, _arm_position, _stance, _head_tilt):
        s = fn(keypoints)
        if s:
            parts.append(s)
    return ", ".join(parts)

try:
    from hailo_platform import (
        HEF, VDevice, HailoStreamInterface, InferVStreams,
        ConfigureParams, InputVStreamParams, OutputVStreamParams,
    )
    HAILO_AVAILABLE = True
except ImportError:
    pass

# --- Ultralytics fallback ---
YOLO_AVAILABLE = False
try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except ImportError:
    pass

if not HAILO_AVAILABLE and not YOLO_AVAILABLE:
    print("[person_detector] WARNING: No detection backend available!")


RAW_LOG_FLOOR = 0.52  # weakest candidate kept for the diagnostic log (0.50 = quantisation noise floor)
_RAW_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "detections.log")


def _decode_floor() -> float:
    """Score floor for decode + NMS: the real threshold, or lower while raw logging."""
    if config.DETECTION_LOG_RAW:
        return min(config.DETECTION_CONFIDENCE, RAW_LOG_FLOOR)
    return config.DETECTION_CONFIDENCE


def _write_raw_log(entries):
    """One line per frame that had any candidate. Best-effort, never raises."""
    try:
        import datetime as _dt
        ts = _dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        with open(_RAW_LOG_PATH, "a") as f:
            f.write(f"{ts}  min_conf={config.DETECTION_CONFIDENCE:.2f} "
                    f"min_area={config.DETECTION_MIN_AREA*100:.2f}%  | " + " | ".join(entries) + "\n")
    except Exception:
        pass


class PersonDetector:
    """Detects people + pose keypoints using a USB webcam and YOLOv8-pose
    (Hailo or CPU). Pose models are single-class, so no class filter needed."""

    def __init__(self):
        # Initialize camera
        self.cap = cv2.VideoCapture(CAMERA_INDEX)
        # Compressed mode first: uncompressed (YUYV) USB webcams drop to ~10 fps at 1280x720
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
        self.cap.set(cv2.CAP_PROP_FPS, CAMERA_FPS)

        if not self.cap.isOpened():
            raise RuntimeError(f"Could not open camera at index {CAMERA_INDEX}")

        # Detection backend
        self._backend = None
        self._hailo_network_group = None
        self._hailo_vdevice = None
        self._hailo_input_name = None
        self._hailo_input_h = 640
        self._hailo_input_w = 640
        self._stride_groups: dict[int, dict] = {}
        self._yolo_model = None

        if HAILO_AVAILABLE:
            try:
                self._init_hailo()
                self._backend = "hailo"
            except Exception as e:
                print(f"[person_detector] Hailo init failed: {e}")
                print("[person_detector] Falling back to CPU...")

        if self._backend is None and YOLO_AVAILABLE:
            from config import DETECTION_MODEL
            print(f"[person_detector] Loading {DETECTION_MODEL} (CPU)...")
            self._yolo_model = YOLO(DETECTION_MODEL)
            self._backend = "cpu"
            print("[person_detector] CPU model loaded")

        if self._backend is None:
            print("[person_detector] WARNING: No detection backend — running blind")

        # Detection state
        self._consecutive_detections = 0

        fourcc = int(self.cap.get(cv2.CAP_PROP_FOURCC)).to_bytes(4, "little").decode(errors="replace")
        print(f"[person_detector] Camera opened ({int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x"
              f"{int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))} @ {self.cap.get(cv2.CAP_PROP_FPS):g}fps, "
              f"{fourcc}; asked for {CAMERA_WIDTH}x{CAMERA_HEIGHT})")
        print(f"[person_detector] Backend: {self._backend or 'none'}")

    def _init_hailo(self):
        """Initialize Hailo-8 inference pipeline.

        YOLOv8-pose exposes 9 raw output streams (3 detection strides × 3
        tensor types: bbox-DFL, class score, keypoints). We capture every
        stream's name + quantization params and group them by stride for
        the Python-side decoder.
        """
        print(f"[person_detector] Loading Hailo model: {HAILO_HEF_PATH}")
        hef = HEF(HAILO_HEF_PATH)

        input_info = hef.get_input_vstream_infos()[0]
        self._hailo_input_name = input_info.name
        self._hailo_input_h = input_info.shape[0]
        self._hailo_input_w = input_info.shape[1]

        # Group all 9 output streams by detection stride.
        # Each stride has three tensors, identified by channel count:
        #   64ch → bbox DFL    1ch → class score    51ch → keypoints
        self._stride_groups: dict[int, dict] = {}
        for info in hef.get_output_vstream_infos():
            h, w, c = info.shape
            stride = self._hailo_input_h // h
            qi = info.quant_info
            tensor = {
                "name": info.name,
                "shape": info.shape,
                "scale": float(qi.qp_scale),
                "zp": float(qi.qp_zp),
            }
            slot = {64: "bbox", 1: "cls", 51: "kpt"}.get(c)
            if slot is None:
                print(f"[person_detector] WARN: unexpected output channels {c} "
                      f"on stream {info.name} (shape {info.shape})")
                continue
            self._stride_groups.setdefault(stride, {})[slot] = tensor

        # Sanity-check we got bbox/cls/kpt for each stride
        for stride, group in self._stride_groups.items():
            missing = {"bbox", "cls", "kpt"} - group.keys()
            if missing:
                print(f"[person_detector] WARN: stride {stride} missing {missing}")

        self._hailo_vdevice = VDevice()
        configure_params = ConfigureParams.create_from_hef(
            hef, interface=HailoStreamInterface.PCIe
        )
        network_groups = self._hailo_vdevice.configure(hef, configure_params)
        self._hailo_network_group = network_groups[0]
        network_group_params = self._hailo_network_group.create_params()

        input_params = InputVStreamParams.make(self._hailo_network_group)
        output_params = OutputVStreamParams.make(self._hailo_network_group)

        self._hailo_infer_ctx = InferVStreams(
            self._hailo_network_group, input_params, output_params
        )
        self._hailo_infer_ctx.__enter__()

        self._hailo_activate_ctx = self._hailo_network_group.activate(network_group_params)
        self._hailo_activate_ctx.__enter__()

        # Warm up with a blank frame
        blank = np.zeros((self._hailo_input_h, self._hailo_input_w, 3), dtype=np.uint8)
        input_data = {self._hailo_input_name: np.expand_dims(blank, axis=0)}
        self._hailo_infer_ctx.infer(input_data)

        print(f"[person_detector] Hailo ready — input {self._hailo_input_w}x{self._hailo_input_h}, "
              f"strides {sorted(self._stride_groups.keys())}")

    def get_frame(self) -> np.ndarray | None:
        """Capture a single frame from the camera."""
        ret, frame = self.cap.read()
        if ret:
            return frame
        return None

    def detect_people(self, frame: np.ndarray) -> list[dict]:
        """
        Run person+pose detection on a frame.

        Returns a list of detections, each with:
            - bbox: (x1, y1, x2, y2) pixel coordinates
            - center: (cx, cy) center of bounding box
            - head: (hx, hy) head position — nose keypoint when visible,
                    else 15%-from-top fallback
            - confidence: detection confidence
            - area_ratio: fraction of frame area occupied by this person
            - keypoints: ndarray of shape (17, 3) — (x, y, visibility) for each
                         COCO keypoint, in pixel coords. Use KEYPOINT_CONFIDENCE
                         to filter unreliable points.
        """
        if self._backend == "hailo":
            return self._detect_hailo(frame)
        elif self._backend == "cpu":
            return self._detect_cpu(frame)
        return []

    def _detect_hailo(self, frame: np.ndarray) -> list[dict]:
        """Run YOLOv8-pose detection on the Hailo-8 accelerator.

        The Hailo Model Zoo yolov8s_pose .hef exposes 9 raw output streams
        (3 strides × {bbox-DFL, class, keypoints}) — no on-chip NMS. We
        decode them on the CPU side: dequantize, DFL bbox decode, sigmoid
        the class scores and keypoint visibility, decode keypoint offsets
        from anchor centers, concatenate across strides, NMS.
        """
        frame_h, frame_w = frame.shape[:2]
        in_h, in_w = self._hailo_input_h, self._hailo_input_w

        # Resize to model input (640x640). Note: no letterboxing here, so the
        # decoded coords use a simple linear scale back to frame size.
        resized = cv2.resize(frame, (in_w, in_h))
        input_data = {self._hailo_input_name: np.expand_dims(resized, axis=0)}

        results = self._hailo_infer_ctx.infer(input_data)

        # Decode each stride and collect (K, 5+51) candidates.
        per_stride = []
        for stride in sorted(self._stride_groups.keys()):
            group = self._stride_groups[stride]
            if not all(k in group for k in ("bbox", "cls", "kpt")):
                continue
            cand = self._decode_pose_stride(results, group, stride)
            if cand is not None and len(cand) > 0:
                per_stride.append(cand)

        if not per_stride:
            return []

        candidates = np.concatenate(per_stride, axis=0)

        # NMS in input-image space (640x640). cv2 wants (x, y, w, h) ints/floats.
        boxes_xywh = np.column_stack([
            candidates[:, 0], candidates[:, 1],
            candidates[:, 2] - candidates[:, 0],
            candidates[:, 3] - candidates[:, 1],
        ]).tolist()
        scores = candidates[:, 4].tolist()
        # With raw logging on, decode/NMS run at a lower floor so weak candidates
        # can be logged. NMS is greedy by score, so boxes >= DETECTION_CONFIDENCE
        # survive exactly as before; the real threshold is re-applied below.
        keep = cv2.dnn.NMSBoxes(
            boxes_xywh, scores, _decode_floor(), NMS_IOU_THRESHOLD
        )
        if len(keep) == 0:
            return []
        keep = np.asarray(keep).flatten()
        survivors = candidates[keep]

        # Scale back from model input space to actual frame size.
        sx = frame_w / in_w
        sy = frame_h / in_h
        frame_area = frame_h * frame_w

        detections = []
        raw_log = [] if config.DETECTION_LOG_RAW else None
        for row in survivors:
            x1 = max(0, min(int(row[0] * sx), frame_w))
            y1 = max(0, min(int(row[1] * sy), frame_h))
            x2 = max(0, min(int(row[2] * sx), frame_w))
            y2 = max(0, min(int(row[3] * sy), frame_h))
            score = float(row[4])

            cx = (x1 + x2) / 2
            cy = (y1 + y2) / 2
            bbox_h = y2 - y1
            area = (x2 - x1) * bbox_h
            area_ratio = area / frame_area
            if raw_log is not None:
                verdict = ("LOW-CONF" if score < config.DETECTION_CONFIDENCE else
                           "TOO-SMALL" if area_ratio < config.DETECTION_MIN_AREA else "OK")
                raw_log.append(f"conf={score:.2f} area={area_ratio*100:.2f}% "
                               f"h={y2 - y1}px y={y2} {verdict}")
            if score < config.DETECTION_CONFIDENCE:
                continue
            if area_ratio < config.DETECTION_MIN_AREA:
                continue

            kp_block = row[5:5 + 51].reshape(17, 3).astype(np.float32)
            keypoints = np.empty((17, 3), dtype=np.float32)
            keypoints[:, 0] = kp_block[:, 0] * sx   # x in frame pixels
            keypoints[:, 1] = kp_block[:, 1] * sy   # y in frame pixels
            keypoints[:, 2] = kp_block[:, 2]         # visibility (already sigmoid'd)

            nose_x, nose_y, nose_v = keypoints[KP_NOSE]
            if nose_v >= KEYPOINT_CONFIDENCE:
                head = (float(nose_x), float(nose_y))
            else:
                head = (float(cx), float(y1 + bbox_h * 0.15))

            detections.append({
                "bbox": (x1, y1, x2, y2),
                "center": (float(cx), float(cy)),
                "head": head,
                "confidence": score,
                "area_ratio": area_ratio,
                "keypoints": keypoints,
            })

        if raw_log:
            _write_raw_log(raw_log)
        return detections

    def _decode_pose_stride(self, results, group, stride: int):
        """
        Decode one detection stride into (K, 56) confidence-filtered candidates
        in input-image (640x640) coordinates.

        Returns None when nothing survives the score threshold.
        """
        bbox_t = group["bbox"]   # (H, W, 64) — 4 sides × 16 DFL bins
        cls_t = group["cls"]     # (H, W, 1)  — pre-sigmoid logit
        kpt_t = group["kpt"]     # (H, W, 51) — 17 × (x_off, y_off, visibility_logit)

        # Dequantize uint8 outputs. Squeeze batch dim while we're at it.
        bbox = self._dequant(results[bbox_t["name"]][0], bbox_t)
        cls = self._dequant(results[cls_t["name"]][0], cls_t)
        kpt = self._dequant(results[kpt_t["name"]][0], kpt_t)

        H, W = bbox.shape[:2]
        N = H * W

        # Class scores → sigmoid, then conf-filter (cuts ~99% of cells immediately)
        scores = self._sigmoid(cls.reshape(N))
        keep_mask = scores >= _decode_floor()
        if not keep_mask.any():
            return None
        kept = np.where(keep_mask)[0]
        K = kept.size

        scores_k = scores[kept]
        bbox_k = bbox.reshape(N, 64)[kept]
        kpt_k = kpt.reshape(N, 51)[kept]

        # Anchor centers (in grid units, +0.5 = cell midpoint)
        gy, gx = np.divmod(kept, W)
        ax = gx.astype(np.float32) + 0.5
        ay = gy.astype(np.float32) + 0.5

        # DFL bbox decode: reshape (K, 64) → (K, 4, 16), softmax over bins,
        # weighted sum with [0..15] gives the predicted distance to each side.
        bbox_dfl = bbox_k.reshape(K, 4, 16)
        dist = (self._softmax(bbox_dfl, axis=-1) * _DFL_PROJ).sum(axis=-1)  # (K, 4) = l, t, r, b

        x1 = (ax - dist[:, 0]) * stride
        y1 = (ay - dist[:, 1]) * stride
        x2 = (ax + dist[:, 2]) * stride
        y2 = (ay + dist[:, 3]) * stride

        # Keypoint decode. Channels are [k0_x, k0_y, k0_v, k1_x, k1_y, k1_v, ...].
        # Following ultralytics' convention:
        #   x = (raw_x * 2 + (anchor_x - 0.5)) * stride
        #   y = (raw_y * 2 + (anchor_y - 0.5)) * stride
        #   visibility = sigmoid(raw_v)
        kp = kpt_k.reshape(K, 17, 3)
        kp_x = (kp[:, :, 0] * 2.0 + ax[:, None] - 0.5) * stride
        kp_y = (kp[:, :, 1] * 2.0 + ay[:, None] - 0.5) * stride
        kp_v = self._sigmoid(kp[:, :, 2])
        kp_xyz = np.stack([kp_x, kp_y, kp_v], axis=-1).reshape(K, 51)

        out = np.empty((K, 56), dtype=np.float32)
        out[:, 0] = x1
        out[:, 1] = y1
        out[:, 2] = x2
        out[:, 3] = y2
        out[:, 4] = scores_k
        out[:, 5:] = kp_xyz
        return out

    @staticmethod
    def _dequant(arr_uint8: np.ndarray, info: dict) -> np.ndarray:
        """Dequantize a uint8 tensor: (value - zp) * scale → float32."""
        return (arr_uint8.astype(np.float32) - info["zp"]) * info["scale"]

    @staticmethod
    def _sigmoid(x: np.ndarray) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-x))

    @staticmethod
    def _softmax(x: np.ndarray, axis: int = -1) -> np.ndarray:
        e = np.exp(x - x.max(axis=axis, keepdims=True))
        return e / e.sum(axis=axis, keepdims=True)

    def _detect_cpu(self, frame: np.ndarray) -> list[dict]:
        """Run pose detection on CPU using ultralytics YOLOv8-pose."""
        if self._yolo_model is None:
            return []

        # YOLOv8-pose is single-class (person), no `classes=` filter needed
        results = self._yolo_model(frame, conf=config.DETECTION_CONFIDENCE, verbose=False)

        detections = []
        frame_area = frame.shape[0] * frame.shape[1]

        for result in results:
            # result.keypoints.data is (N, 17, 3) with (x, y, conf) per keypoint
            kpts_all = (
                result.keypoints.data.cpu().numpy()
                if result.keypoints is not None else None
            )

            for i, box in enumerate(result.boxes):
                x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
                conf = float(box.conf[0])
                cx = (x1 + x2) / 2
                cy = (y1 + y2) / 2
                bbox_h = y2 - y1
                area = (x2 - x1) * bbox_h
                area_ratio = area / frame_area

                # Skip tiny detections (outdoor false positives)
                if area_ratio < config.DETECTION_MIN_AREA:
                    continue

                # Keypoints already in (x, y, conf) pixel coords — no scaling needed
                if kpts_all is not None and i < len(kpts_all):
                    keypoints = kpts_all[i].astype(np.float32)
                else:
                    keypoints = np.zeros((17, 3), dtype=np.float32)

                # Head: prefer nose keypoint, fall back to bbox heuristic
                nose_x, nose_y, nose_v = keypoints[KP_NOSE]
                if nose_v >= KEYPOINT_CONFIDENCE:
                    head = (float(nose_x), float(nose_y))
                else:
                    head = (float(cx), float(y1 + bbox_h * 0.15))

                detections.append({
                    "bbox": (int(x1), int(y1), int(x2), int(y2)),
                    "center": (float(cx), float(cy)),
                    "head": head,
                    "confidence": conf,
                    "area_ratio": area_ratio,
                    "keypoints": keypoints,
                })

        return detections

    def check_for_people(self) -> tuple[bool, list[dict], np.ndarray | None]:
        """
        Capture a frame, run detection, and apply the hold-frame filter.

        Returns:
            - triggered: True if people have been detected for enough consecutive frames
            - detections: list of person detections
            - frame: the captured frame (for sending to AI)
        """
        frame = self.get_frame()
        if frame is None:
            return False, [], None

        detections = self.detect_people(frame)

        if len(detections) > 0:
            self._consecutive_detections += 1
        else:
            self._consecutive_detections = 0

        triggered = self._consecutive_detections >= config.DETECTION_HOLD_FRAMES
        return triggered, detections, frame

    def frame_to_base64(self, frame: np.ndarray, quality: int = 70) -> str:
        """Encode a frame as base64 JPEG for sending to the AI API."""
        encode_params = [cv2.IMWRITE_JPEG_QUALITY, quality]
        _, buffer = cv2.imencode(".jpg", frame, encode_params)
        return base64.b64encode(buffer).decode("utf-8")

    @staticmethod
    def _radar_distance_desc(target) -> str:
        """
        Bucket a radar target's Y distance (mm) into a scarecrow-perspective
        phrase. Boundaries are tuned for a typical front-yard setup where the
        sidewalk is ~7m from the prop. The LD2450 reliably tracks out to ~6m.
        """
        m = target.y_mm / 1000.0
        if m < 2.0:
            return "very close"
        if m < 4.0:
            return "a few feet away"
        if m < 6.5:
            return "across the yard"
        return "out by the street"

    @staticmethod
    def _radar_motion_desc(target) -> str:
        """
        Bucket a radar target's forward velocity (cm/s) into a phrase.
        The LD2450 reports negative speed = approaching, positive = retreating.
        Standing-still band is ±25 cm/s: limb micro-motion reads non-zero, and
        a fast head turn read up to ±24 cm/s on a still scene (sweep test, Oct 16).
        """
        speed = target.speed_cms
        if speed <= -80:
            return "running toward me"
        if speed <= -40:
            return "walking toward me"
        if speed <= -25:
            return "drifting closer"
        if speed < 25:
            return "standing still"
        if speed < 40:
            return "drifting away"
        if speed < 80:
            return "walking away"
        return "running away"

    @staticmethod
    def _camera_bearing(det) -> float:
        """Degrees right (+) / left (-) of where the camera points."""
        return (det["center"][0] / CAMERA_WIDTH - 0.5) * config.CAMERA_HFOV_DEG

    @staticmethod
    def _radar_bearing(target) -> float:
        """Degrees right (+) / left (-) of where the radar points."""
        return math.degrees(math.atan2(target.x_mm, max(target.y_mm, 1)))

    def _match_radar(self, detections, targets) -> dict:
        """Pair radar targets with camera detections by direction (both sensors
        are on his head, so they point the same way). Closest directions pair
        first; nothing pairs beyond RADAR_MATCH_DEGREES. Returns {det index: target}."""
        pairs = sorted(
            (abs(self._camera_bearing(d) - self._radar_bearing(t)), i, j)
            for i, d in enumerate(detections) for j, t in enumerate(targets))
        out, used = {}, set()
        for diff, i, j in pairs:
            if diff > config.RADAR_MATCH_DEGREES:
                break
            if i in out or j in used:
                continue
            out[i] = targets[j]
            used.add(j)
        return out

    @staticmethod
    def _track_for(det, tracks):
        """The track updated from this detection (tracks aren't in detection order)."""
        if not tracks:
            return None
        for t in tracks:
            if t.bbox is det["bbox"]:
                return t
        for t in tracks:
            if tuple(t.bbox) == tuple(det["bbox"]):
                return t
        return None

    def describe_scene(self, detections: list[dict], tracks=None, tracker=None,
                       radar=None, head_still: bool = True) -> str:
        """
        Create a natural-language description of the scene from the scarecrow's
        perspective. Uses spatial terms like 'to my left' instead of technical
        terms like 'left of frame'. Includes motion (from radar or tracker) and
        body language (from pose keypoints) when available.

        Radar distance + speed are used for a person only when a radar target
        points the same way they do, and only once the head has been still for
        RADAR_SETTLE_SECONDS (head_still) — right after a turn the face-mounted
        radar reports stale targets or slides one target from object to object.
        Everyone else gets the camera's estimates. A one-line summary of the raw
        radar targets and how they were used is left in self.last_radar_note.
        """
        self.last_radar_note = ""
        n = len(detections)
        if n == 0:
            return "Nobody around."

        if n == 1:
            parts = ["There is someone in front of me."]
        else:
            parts = [f"There are {n} people in front of me."]

        radar_enabled = radar is not None and getattr(radar, "enabled", False)
        targets = radar.get_targets(max_age=config.RADAR_TARGET_TIMEOUT) if radar_enabled else []
        matches = self._match_radar(detections, targets) if (targets and head_still) else {}
        if radar_enabled:
            raw = "  ".join(f"s{t.slot} x{t.x_mm:+d} y{t.y_mm} v{t.speed_cms:+d} "
                            f"({self._radar_bearing(t):+.0f}°)" for t in targets) or "no targets"
            used = ", ".join(f"person {i + 1}@{self._camera_bearing(detections[i]):+.0f}°→s{t.slot}"
                             for i, t in sorted(matches.items())) or "none"
            state = "used" if head_still else "ignored (head moved)"
            self.last_radar_note = f"[radar] {raw} | {state}; matched: {used}"

        for i, det in enumerate(detections):
            cx, cy = det["center"]
            if cx < CAMERA_WIDTH * 0.33:
                x_desc = "to my left"
            elif cx > CAMERA_WIDTH * 0.67:
                x_desc = "to my right"
            else:
                x_desc = "right in front of me"

            target = matches.get(i)
            if target is not None:
                dist_desc = self._radar_distance_desc(target)
            elif det["area_ratio"] > 0.15:
                dist_desc = "very close"
            elif det["area_ratio"] > 0.05:
                dist_desc = "a few feet away"
            else:
                dist_desc = "far away"

            # Combine body language (from pose) with motion (from radar or
            # tracker) into a single comma-joined "They are X." sentence.
            observations = []
            body_desc = ""
            kp = det.get("keypoints")
            if kp is not None:
                body_desc = describe_body_language(kp)
                if body_desc:
                    observations.append(body_desc)
            motion_desc = None
            if target is not None:
                motion_desc = self._radar_motion_desc(target)
            elif head_still and tracker:
                # Camera motion is meaningless while the head is turning (the
                # whole picture moves), so only use it once he's held still.
                track = self._track_for(det, tracks)
                if track is not None:
                    motion_desc = tracker.describe_motion(track)
            # Someone looking straight at him is very rarely walking backwards:
            # an "away" reading for them is far more likely a sensor mix-up.
            if motion_desc and "facing me" in body_desc and "away" in motion_desc:
                motion_desc = None
            if motion_desc:
                observations.append(motion_desc)

            head = "They are" if n == 1 else f"Person {i+1} is"
            desc = f"  {head} {x_desc}, {dist_desc}."
            if observations:
                # "They" refers back to the most recent subject — reads
                # naturally for both singular and multi-person variants.
                desc += f" They are {', '.join(observations)}."
            parts.append(desc)

        return "\n".join(parts)

    def annotate_frame(self, frame: np.ndarray, detections: list[dict]) -> np.ndarray:
        """Draw bounding boxes on frame (for debug display)."""
        annotated = frame.copy()
        for det in detections:
            x1, y1, x2, y2 = det["bbox"]
            conf = det["confidence"]
            cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 2)
            cv2.putText(annotated, f"Person {conf:.0%}", (x1, y1 - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
        return annotated

    def reset_detection_count(self):
        """Reset the consecutive detection counter (call after triggering an interaction)."""
        self._consecutive_detections = 0

    def release(self):
        """Release the camera and Hailo resources."""
        if self.cap:
            self.cap.release()
        if self._backend == "hailo":
            try:
                self._hailo_activate_ctx.__exit__(None, None, None)
                self._hailo_infer_ctx.__exit__(None, None, None)
                self._hailo_vdevice.release()
            except Exception:
                pass
        print("[person_detector] Camera released")


# --- Standalone test ---
if __name__ == "__main__":
    print("Testing person detector...")
    detector = PersonDetector()

    print("Press Ctrl+C to quit\n")

    frame_count = 0
    start_time = time.time()

    try:
        while True:
            triggered, detections, frame = detector.check_for_people()
            frame_count += 1

            if detections:
                print(f"[{time.strftime('%H:%M:%S')}] {detector.describe_scene(detections)}")

            if triggered:
                print(">>> TRIGGERED — would start interaction <<<")
                detector.reset_detection_count()

            # Print FPS every 50 frames
            if frame_count % 50 == 0:
                elapsed = time.time() - start_time
                fps = frame_count / elapsed
                print(f"[perf] {fps:.1f} FPS ({frame_count} frames in {elapsed:.1f}s)")

            if config.DEBUG and frame is not None:
                annotated = detector.annotate_frame(frame, detections)
                cv2.imshow("Scarecrow Vision", annotated)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break

    except KeyboardInterrupt:
        print("\nStopping...")

    elapsed = time.time() - start_time
    fps = frame_count / elapsed
    print(f"\nFinal: {fps:.1f} FPS ({frame_count} frames in {elapsed:.1f}s)")

    detector.release()
    if config.DEBUG:
        cv2.destroyAllWindows()
    print("Done!")
