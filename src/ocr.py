"""
PaddleOCR module for Minus using RKNN NPU.

Detects text in frames and checks for ad-related keywords.
"""

import os
import time
import numpy as np
import cv2
from pathlib import Path
from rknnlite.api import RKNNLite

try:
    import pyclipper
    from shapely.geometry import Polygon
    HAS_POSTPROCESS = True
except ImportError:
    HAS_POSTPROCESS = False
    print("[OCR] Warning: pyclipper/shapely not installed. Install with: pip3 install --break-system-packages pyclipper shapely")


class DBPostProcessor:
    """Post-processor for text detection using DB (Differentiable Binarization)."""

    def __init__(self, thresh=0.3, box_thresh=0.5, max_candidates=1000,
                 unclip_ratio=1.5, min_size=3):
        self.thresh = thresh
        self.box_thresh = box_thresh
        self.max_candidates = max_candidates
        self.unclip_ratio = unclip_ratio
        self.min_size = min_size

    def __call__(self, pred, src_h, src_w):
        if len(pred.shape) == 3:
            pred = pred[0]

        segmentation = pred > self.thresh
        boxes, scores = self.boxes_from_bitmap(pred, segmentation, src_w, src_h)
        return boxes, scores

    def boxes_from_bitmap(self, pred, bitmap, dest_width, dest_height):
        height, width = bitmap.shape
        bitmap_uint8 = (bitmap * 255).astype(np.uint8)
        contours, _ = cv2.findContours(bitmap_uint8, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

        num_contours = min(len(contours), self.max_candidates)
        boxes = []
        scores = []

        for i in range(num_contours):
            contour = contours[i]
            points, sside = self.get_mini_boxes(contour)
            if sside < self.min_size:
                continue

            score = self.box_score_fast(pred, points.reshape(-1, 2))
            if self.box_thresh > score:
                continue

            box = self.unclip(points, self.unclip_ratio)
            if box is None:
                continue

            box, sside = self.get_mini_boxes(box.reshape(-1, 1, 2).astype(np.int32))
            if sside < self.min_size + 2:
                continue

            box[:, 0] = np.clip(box[:, 0] / width * dest_width, 0, dest_width)
            box[:, 1] = np.clip(box[:, 1] / height * dest_height, 0, dest_height)

            boxes.append(box.astype(np.int32))
            scores.append(score)

        return boxes, scores

    def get_mini_boxes(self, contour):
        bounding_box = cv2.minAreaRect(contour)
        points = sorted(list(cv2.boxPoints(bounding_box)), key=lambda x: x[0])

        index_1, index_2, index_3, index_4 = 0, 1, 2, 3
        if points[1][1] > points[0][1]:
            index_1 = 0
            index_4 = 1
        else:
            index_1 = 1
            index_4 = 0
        if points[3][1] > points[2][1]:
            index_2 = 2
            index_3 = 3
        else:
            index_2 = 3
            index_3 = 2

        box = np.array([points[index_1], points[index_2],
                       points[index_3], points[index_4]])
        return box, min(bounding_box[1])

    def box_score_fast(self, bitmap, box):
        h, w = bitmap.shape
        box = box.copy()
        xmin = np.clip(np.floor(box[:, 0].min()).astype(np.int32), 0, w - 1)
        xmax = np.clip(np.ceil(box[:, 0].max()).astype(np.int32), 0, w - 1)
        ymin = np.clip(np.floor(box[:, 1].min()).astype(np.int32), 0, h - 1)
        ymax = np.clip(np.ceil(box[:, 1].max()).astype(np.int32), 0, h - 1)

        mask = np.zeros((ymax - ymin + 1, xmax - xmin + 1), dtype=np.uint8)
        box[:, 0] = box[:, 0] - xmin
        box[:, 1] = box[:, 1] - ymin
        cv2.fillPoly(mask, box.reshape(1, -1, 2).astype(np.int32), 1)
        return cv2.mean(bitmap[ymin:ymax + 1, xmin:xmax + 1], mask)[0]

    def unclip(self, box, unclip_ratio):
        try:
            poly = Polygon(box)
            distance = poly.area * unclip_ratio / poly.length
            offset = pyclipper.PyclipperOffset()
            offset.AddPath(box, pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
            expanded = offset.Execute(distance)
            if len(expanded) == 0:
                return None
            return np.array(expanded[0])
        except Exception:
            return None


class CTCLabelDecode:
    """CTC decoder for text recognition."""

    def __init__(self, character_dict_path):
        self.character = ['blank']
        with open(character_dict_path, 'r', encoding='utf-8') as f:
            for line in f:
                char = line.strip('\n')
                if char:
                    self.character.append(char)
        self.character.append(' ')

    def __call__(self, preds):
        if len(preds.shape) == 3:
            preds = preds[0]

        preds_idx = preds.argmax(axis=1)
        preds_prob = preds.max(axis=1)

        result = []
        prev_idx = -1
        conf_list = []

        for i, idx in enumerate(preds_idx):
            if idx != 0 and idx != prev_idx:
                if idx < len(self.character):
                    result.append(self.character[idx])
                    conf_list.append(preds_prob[i])
            prev_idx = idx

        text = ''.join(result)
        confidence = np.mean(conf_list) if conf_list else 0.0

        return text, float(confidence)


class PaddleOCR:
    """PaddleOCR using RKNN models for NPU acceleration."""

    # Ad-related keywords to detect (must be distinct/clear ad indicators)
    # Note: 'ad' alone is too generic - only match 'skip ad', 'ad:', etc.
    # Note: 'learn more' removed - too common in YouTube UI (recommended videos, etc.)
    AD_KEYWORDS_EXACT = [
        'skip ad', 'skip ads', 'skipad', 'skipads',
        'sponsored', 'advertisement', 'ad break',
        'shop now', 'buy now',
        'promoted',  # Twitter/social media promoted ads
        # Note: 'promo' removed - too broad, matches 'Promote' button
    ]
    # Keywords that need word boundary matching (avoid matching inside words)
    AD_KEYWORDS_WORD = [
        'skip', 'sponsor',
    ]

    # Phrases that should NOT trigger ad detection (false positives)
    # These override keyword matches when the full phrase is detected
    AD_EXCLUSIONS = [
        'skip recap', 'skiprecap',  # Netflix "Skip Recap" button
        'skip intro', 'skipintro',  # Streaming "Skip Intro" button
    ]

    def __init__(self, det_model_path, rec_model_path, dict_path,
                 cls_model_path=None):
        self.det_model_path = det_model_path
        self.rec_model_path = rec_model_path
        self.cls_model_path = cls_model_path
        self.dict_path = dict_path

        self.det_rknn = None
        self.rec_rknn = None
        self.cls_rknn = None

        self.det_input_h = 960
        self.det_input_w = 960
        self.rec_input_h = 48
        self.rec_input_w = 320

        self.db_postprocess = DBPostProcessor() if HAS_POSTPROCESS else None
        self.ctc_decode = None
        self.initialized = False

    def load_models(self):
        """Load all RKNN models."""
        if not HAS_POSTPROCESS:
            print("[OCR] Cannot load models without pyclipper/shapely")
            return False

        print("[OCR] Loading PaddleOCR models...")

        # Load detection model
        print(f"[OCR]   Loading detection model...")
        self.det_rknn = RKNNLite()
        ret = self.det_rknn.load_rknn(self.det_model_path)
        if ret != 0:
            print(f"[OCR]   Failed to load detection model: {ret}")
            return False
        ret = self.det_rknn.init_runtime()
        if ret != 0:
            print(f"[OCR]   Failed to init detection runtime: {ret}")
            return False

        # Load recognition model
        print(f"[OCR]   Loading recognition model...")
        self.rec_rknn = RKNNLite()
        ret = self.rec_rknn.load_rknn(self.rec_model_path)
        if ret != 0:
            print(f"[OCR]   Failed to load recognition model: {ret}")
            return False
        ret = self.rec_rknn.init_runtime()
        if ret != 0:
            print(f"[OCR]   Failed to init recognition runtime: {ret}")
            return False

        # Initialize CTC decoder
        if os.path.exists(self.dict_path):
            self.ctc_decode = CTCLabelDecode(self.dict_path)
            print(f"[OCR]   Dictionary loaded: {len(self.ctc_decode.character)} characters")
        else:
            print(f"[OCR]   Dictionary not found: {self.dict_path}")
            return False

        self.initialized = True
        print("[OCR] Models loaded successfully")
        return True

    def preprocess_det(self, img):
        """Preprocess image for detection."""
        if len(img.shape) == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
        elif img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2RGB)
        elif img.shape[2] == 3:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

        h, w = img.shape[:2]
        img_resized = cv2.resize(img, (self.det_input_w, self.det_input_h))
        img_input = np.expand_dims(img_resized, 0).astype(np.uint8)
        return img_input, h, w

    def preprocess_rec(self, img):
        """Preprocess cropped text region for recognition."""
        if len(img.shape) == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
        elif img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2RGB)
        elif img.shape[2] == 3:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

        h, w = img.shape[:2]
        ratio = self.rec_input_h / h
        new_w = int(w * ratio)
        new_w = min(new_w, self.rec_input_w)

        img_resized = cv2.resize(img, (new_w, self.rec_input_h))

        if new_w < self.rec_input_w:
            pad_w = self.rec_input_w - new_w
            img_resized = np.pad(img_resized,
                                ((0, 0), (0, pad_w), (0, 0)),
                                mode='constant', constant_values=0)

        img_input = np.expand_dims(img_resized, 0).astype(np.uint8)
        return img_input

    def crop_text_region(self, img, box):
        """Crop text region from image using perspective transform."""
        box = np.array(box).astype(np.float32)

        width = int(max(
            np.linalg.norm(box[0] - box[1]),
            np.linalg.norm(box[2] - box[3])
        ))
        height = int(max(
            np.linalg.norm(box[0] - box[3]),
            np.linalg.norm(box[1] - box[2])
        ))

        if width < 3 or height < 3:
            return None

        dst = np.array([
            [0, 0],
            [width, 0],
            [width, height],
            [0, height]
        ], dtype=np.float32)

        M = cv2.getPerspectiveTransform(box, dst)
        cropped = cv2.warpPerspective(img, M, (width, height))
        return cropped

    def detect(self, img):
        """Run text detection."""
        img_input, src_h, src_w = self.preprocess_det(img)

        start = time.time()
        outputs = self.det_rknn.inference(inputs=[img_input])
        det_time = (time.time() - start) * 1000

        pred = outputs[0]
        if len(pred.shape) == 4:
            pred = pred[0, 0]
        elif len(pred.shape) == 3:
            pred = pred[0]

        boxes, scores = self.db_postprocess(pred, src_h, src_w)
        return boxes, scores, det_time

    def recognize(self, img_crop):
        """Run text recognition on cropped region."""
        img_input = self.preprocess_rec(img_crop)

        start = time.time()
        outputs = self.rec_rknn.inference(inputs=[img_input])
        rec_time = (time.time() - start) * 1000

        pred = outputs[0]
        text, confidence = self.ctc_decode(pred)
        return text, confidence, rec_time

    def ocr(self, img):
        """
        Run full OCR pipeline on image.

        Returns:
            List of dicts with 'text', 'confidence', 'box'
        """
        if not self.initialized:
            return []

        results = []

        # Detection
        boxes, det_scores, det_time = self.detect(img)

        # Recognition for each box
        for box in boxes:
            cropped = self.crop_text_region(img, box)
            if cropped is None:
                continue

            text, confidence, rec_time = self.recognize(cropped)

            if text.strip():
                results.append({
                    'text': text,
                    'confidence': confidence,
                    'box': box.tolist()
                })

        return results

    # Patterns that indicate terminal/development content
    TERMINAL_INDICATORS = [
        # Shell/Terminal patterns
        r'\$\s*$',           # Shell prompt
        r'radxa@',           # Username prompt
        r'/home/',           # Unix paths
        r'\.py\b',           # Python files
        r'\.log\b',          # Log files
        r'\[I\]|\[W\]|\[E\]', # Log level indicators
        r'Exit code',        # Command exit
        r'ctrl\+',           # Keyboard shortcuts
        r'minus',            # Our own script
        r'OCR #\d+',         # Our log output
        r'^\d{4}-\d{2}-\d{2}', # Timestamps
        r'Error:|Warning:',  # Error messages
        r'python3?\s',       # Python command
        r'nohup|grep|cat|tail|cd\s', # Common commands

        # Claude Code / AI Assistant patterns (OCR may misread)
        r'dangerously.*skip.*perm',  # --dangerously-skip-permissions flag
        r'angerously.*skip.*perm',   # OCR misread without 'd'
        r'claude.*code',     # Claude Code UI
        r'anthropic',        # Company name
        r'opus|sonnet|haiku', # Model names
        r'welco[nm]e\s*back',  # Welcome back (Claude greeting)

        # Keyword list patterns (our own keywords displayed on screen)
        r"'skip.*sponsor",   # Keyword list showing
        r'skip.*promo.*sponsor', # Multiple keywords together
        r"skip\s*ad.*skip\s*ads", # Multiple ad keywords listed
        r'AD_KEYWORDS',      # Variable name
        r'TERMINAL_INDICATORS', # This variable name

        # Code patterns
        r'def\s+\w+\s*\(',   # Python function definitions
        r'class\s+\w+',      # Class definitions
        r'import\s+\w+',     # Import statements
        r'pip3?\s+install',  # pip commands
        r'sudo\s+',          # sudo commands
        r'git\s+(status|commit|push|pull)', # git commands
    ]

    def is_terminal_content(self, all_texts):
        """
        Check if the detected text appears to be terminal/development content.

        Returns:
            True if terminal content is detected, False otherwise
        """
        import re

        terminal_matches = 0
        total_texts = len(all_texts)

        if total_texts == 0:
            return False

        combined_text = ' '.join(all_texts)
        combined_lower = combined_text.lower()

        for pattern in self.TERMINAL_INDICATORS:
            if re.search(pattern, combined_text, re.IGNORECASE):
                terminal_matches += 1

        # If we match 3+ terminal indicators, it's likely terminal content
        if terminal_matches >= 3:
            return True

        # Also check for high density of code-like characters
        code_chars = sum(1 for c in combined_text if c in '{}[]();:=></')
        if code_chars > len(combined_text) * 0.05 and total_texts > 20:
            return True

        # Check if multiple ad keywords appear together (likely showing our code)
        # Note: Don't double-count related keywords (e.g., "sponsored" contains "sponsor")
        matched_keywords = set()
        for kw in self.AD_KEYWORDS_EXACT + self.AD_KEYWORDS_WORD:
            if kw in combined_lower:
                # Skip if a longer keyword already matched this same text
                already_matched = any(kw in mk or mk in kw for mk in matched_keywords if mk != kw)
                if not already_matched:
                    matched_keywords.add(kw)
        # If 4+ distinct keywords visible, it's probably our code or documentation
        if len(matched_keywords) >= 4:
            return True

        # Check for Python-like syntax (OCR may mangle it)
        python_patterns = [
            r"'\w+',\s*['\"]",   # 'word', ' or 'word', "
            r'exit\s*code',      # exit code (lenient)
            r'step.*ed',         # stopped, stepped
            r'\[\s*\]',          # [] brackets
            r'renovedI|Added.*Lin', # OCR misreads of "removed" and "Added lines"
        ]
        python_matches = sum(1 for p in python_patterns
                            if re.search(p, combined_text, re.IGNORECASE))
        if python_matches >= 2:
            return True

        return False

    def check_ad_keywords(self, ocr_results):
        """
        Check OCR results for ad-related keywords.

        Returns:
            Tuple of (found_ad, matched_keywords, all_texts, is_terminal)
        """
        import re
        matched = []
        all_texts = []

        for result in ocr_results:
            text = result['text']
            all_texts.append(text)

            text_lower = text.lower()
            text_clean = ''.join(c for c in text_lower if c.isalnum())

            # Check exact phrase keywords (can appear anywhere)
            for keyword in self.AD_KEYWORDS_EXACT:
                keyword_clean = ''.join(c for c in keyword if c.isalnum())
                if keyword in text_lower or keyword_clean in text_clean:
                    matched.append((keyword, text))
                    break

            # Check word-boundary keywords (must be whole word)
            # But first check if text matches any exclusion patterns (e.g., "Skip Recap")
            is_excluded = any(excl in text_lower or excl.replace(' ', '') in text_clean
                              for excl in self.AD_EXCLUSIONS)

            if not is_excluded:
                for keyword in self.AD_KEYWORDS_WORD:
                    # Use word boundary regex
                    pattern = r'\b' + re.escape(keyword) + r'\b'
                    if re.search(pattern, text_lower):
                        matched.append((keyword, text))
                        break

            # Fuzzy matches for common OCR misreads of "Skip Ad"
            if 'skipad' in text_clean or 'skipads' in text_clean:
                if ('skipad', text) not in matched and ('skipads', text) not in matched:
                    matched.append(('skip ad (fuzzy)', text))
            # Common OCR misreads
            if 'spad' in text_clean and len(text_clean) < 10:  # Short text with spad
                matched.append(('skip ad (fuzzy-spad)', text))
            if 'foad' in text_clean and len(text_clean) < 10:  # Short text with foad
                matched.append(('skip ad (fuzzy-foad)', text))

            # Fuzzy matches for "Shop now" - frequently misread
            if 'shopnow' in text_clean or 'shpnow' in text_clean:
                matched.append(('shop now (fuzzy)', text))
            # "Shan ngw", "Shon ngw", "Shap now" etc.
            if re.search(r'sh[ao][np]\s*n[gwo]w', text_lower):
                matched.append(('shop now (fuzzy-shan)', text))
            # "go to [site].io" or "go to [site].com" indicates ad CTA
            if re.search(r'go\s*to\s+\w+\.(io|com|net|org)', text_lower):
                matched.append(('go to site (ad CTA)', text))

            # "Ad 1 of 2", "Ad2of2", "ad 2 of 3" - video ad progress indicator
            if re.search(r'ad\s*\d+\s*of\s*\d+', text_lower) or re.search(r'ad\d+of\d+', text_clean):
                matched.append(('ad X of Y', text))

        # Check if this appears to be terminal content
        is_terminal = self.is_terminal_content(all_texts)

        return len(matched) > 0, matched, all_texts, is_terminal

    def release(self):
        """Release all models."""
        if self.det_rknn:
            self.det_rknn.release()
        if self.rec_rknn:
            self.rec_rknn.release()
        if self.cls_rknn:
            self.cls_rknn.release()
        self.initialized = False
