"""
VLM (Vision Language Model) integration for Minus.

Uses FastVLM-0.5B on Axera LLM 8850 NPU for ad detection.
Model is loaded ONCE at startup and kept running for fast inference.
Each inference takes ~0.62 seconds (2x faster than Qwen3-VL-2B).
"""

import os
import sys

# CRITICAL: Import torch early before any logging configuration
# This avoids "Unknown level: 'WARNING'" errors in torch.fx.passes
os.environ['PYTORCH_MATCHER_LOGLEVEL'] = 'WARNING'
os.environ['TORCH_LOGS'] = '-all'
os.environ['TRANSFORMERS_VERBOSITY'] = 'error'
try:
    import torch  # Import torch first to avoid logging conflicts
except ImportError:
    pass  # torch might not be installed, will fail later with clear message

import time
import logging
import threading
from pathlib import Path

import numpy as np
from PIL import Image

logger = logging.getLogger('Minus.VLM')

# Model paths - FastVLM-0.5B
FASTVLM_MODEL_DIR = Path("/home/radxa/axera_models/FastVLM-0.5B")
LLM_MODEL_PATH = FASTVLM_MODEL_DIR / "fastvlm_C128_CTX1024_P640_ax650"
TOKENIZER_PATH = FASTVLM_MODEL_DIR / "fastvlm_tokenizer"
VISION_MODEL_PATH = LLM_MODEL_PATH / "image_encoder_512x512_0.5b_ax650.axmodel"
EMBEDS_PATH = FASTVLM_MODEL_DIR / "embeds" / "model.embed_tokens.weight.npy"

# Add utils path for LlavaConfig and InferManager
UTILS_PATH = FASTVLM_MODEL_DIR / "utils"


class VLMManager:
    """
    FastVLM-0.5B manager for ad detection on Axera LLM 8850.

    The model is loaded once at initialization and kept running.
    Uses Python axengine for inference.
    Each inference takes ~0.62 seconds (2x faster than Qwen3-VL-2B).
    """

    # Simple prompt per original benchmark (94.7% accuracy)
    # False positive reduction done via thresholds and OCR cross-validation
    AD_PROMPT = "Is this an advertisement? Answer Yes or No."
    INPUT_SIZE = 512  # Vision encoder input size
    TOKEN_LENGTH = 64  # Number of image tokens for 512x512 input

    def __init__(self):
        """Initialize VLM manager."""
        self.is_ready = False
        self._lock = threading.Lock()

        # Model components
        self.config = None
        self.tokenizer = None
        self.imer = None
        self.vision_session = None
        self.embeds = None
        self.image_processor = None

        # Validate paths
        if not FASTVLM_MODEL_DIR.exists():
            logger.error(f"FastVLM-0.5B not found at: {FASTVLM_MODEL_DIR}")
            return

        if not LLM_MODEL_PATH.exists():
            logger.error(f"Model files not found: {LLM_MODEL_PATH}")
            return

        if not VISION_MODEL_PATH.exists():
            logger.error(f"Vision encoder not found: {VISION_MODEL_PATH}")
            return

        if not EMBEDS_PATH.exists():
            logger.error(f"Embeddings not found: {EMBEDS_PATH}")
            return

        logger.info(f"VLM using FastVLM-0.5B at: {FASTVLM_MODEL_DIR}")

    def load_model(self):
        """Load the model - initializes all components."""
        if self.is_ready:
            logger.info("Model already loaded")
            return True

        try:
            logger.info("Starting FastVLM-0.5B model (takes ~13s)...")
            start_time = time.time()

            # Add utils path to sys.path for imports
            if str(UTILS_PATH) not in sys.path:
                sys.path.insert(0, str(UTILS_PATH))

            # Import dependencies
            try:
                from ml_dtypes import bfloat16
                import axengine as ax
                # Suppress torch/transformers logging issues before import
                os.environ['TRANSFORMERS_VERBOSITY'] = 'error'
                os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
                os.environ['TORCH_LOGS'] = '-all'
                # Temporarily store and reset root logger to avoid conflicts
                import logging as _logging
                _root = _logging.getLogger()
                _handlers = _root.handlers[:]
                _level = _root.level
                for h in _handlers:
                    _root.removeHandler(h)
                _root.setLevel(_logging.WARNING)
                try:
                    import transformers
                    transformers.logging.set_verbosity_error()
                    from transformers import AutoTokenizer, CLIPImageProcessor
                finally:
                    # Restore original logging configuration
                    _root.setLevel(_level)
                    for h in _handlers:
                        _root.addHandler(h)
                from llava_qwen import LlavaConfig, expand2square
                from infer_func import InferManager
            except ImportError as e:
                logger.error(f"Missing dependency: {e}")
                logger.error("Make sure axengine, transformers, ml_dtypes are installed")
                return False

            # Load config and tokenizer
            logger.info("  Loading config and tokenizer...")
            self.config = LlavaConfig.from_pretrained(str(TOKENIZER_PATH))
            self.tokenizer = AutoTokenizer.from_pretrained(
                str(TOKENIZER_PATH),
                trust_remote_code=True
            )

            # Add special tokens if needed
            mm_use_im_start_end = getattr(self.config, "mm_use_im_start_end", False)
            mm_use_im_patch_token = getattr(self.config, "mm_use_im_patch_token", True)
            if mm_use_im_patch_token:
                self.tokenizer.add_tokens(["<im_patch>"], special_tokens=True)
            if mm_use_im_start_end:
                self.tokenizer.add_tokens(["<im_start>", "<im_end>"], special_tokens=True)

            # Load LLM decoder layers
            logger.info("  Loading LLM decoder layers...")
            self.imer = InferManager(
                self.config,
                str(LLM_MODEL_PATH),
                max_seq_len=1024
            )

            # Load vision encoder
            logger.info("  Loading vision encoder...")
            self.vision_session = ax.InferenceSession(str(VISION_MODEL_PATH))

            # Load embeddings - KEEP AS FLOAT32 per IMPLEMENTATION_GUIDE.md
            logger.info("  Loading embeddings...")
            self.embeds = np.load(str(EMBEDS_PATH))
            logger.info(f"    Loaded embeddings: {self.embeds.shape}, dtype: {self.embeds.dtype}")

            # Initialize image processor
            self.image_processor = CLIPImageProcessor(
                size={"shortest_edge": self.INPUT_SIZE},
                crop_size={"height": self.INPUT_SIZE, "width": self.INPUT_SIZE},
                image_mean=[0, 0, 0],
                image_std=[1/255, 1/255, 1/255]
            )

            load_time = time.time() - start_time
            logger.info(f"FastVLM-0.5B loaded in {load_time:.1f}s")
            self.is_ready = True
            return True

        except Exception as e:
            logger.error(f"Failed to load FastVLM-0.5B: {e}")
            import traceback
            tb_str = traceback.format_exc()
            logger.error(f"Traceback:\n{tb_str}")
            return False

    def _reset_kv_cache(self):
        """Reset KV cache between inferences - CRITICAL for accuracy."""
        for i in range(self.config.num_hidden_layers):
            self.imer.k_caches[i].fill(0)
            self.imer.v_caches[i].fill(0)

    def _encode_image(self, image_path):
        """Encode image using vision encoder."""
        # Import here to avoid issues at module load time
        if str(UTILS_PATH) not in sys.path:
            sys.path.insert(0, str(UTILS_PATH))
        from llava_qwen import expand2square

        image = Image.open(image_path).convert('RGB')
        # Expand to square with black background
        image = expand2square(image, tuple(int(x*255) for x in self.image_processor.image_mean))

        # Preprocess image
        input_image = self.image_processor.preprocess(image, return_tensors='pt')['pixel_values'][0]
        input_image = input_image.unsqueeze(0)
        input_image = input_image.numpy().astype(np.uint8).transpose((0, 2, 3, 1))

        # Run vision encoder
        vit_output = self.vision_session.run(None, {"images": input_image})[0]
        return vit_output

    def detect_ad(self, image_path):
        """
        Run ad detection on an image.

        Args:
            image_path: Path to image file (JPEG/PNG)

        Returns:
            tuple: (is_ad, response_text, elapsed_time)
        """
        if not self.is_ready:
            return False, "VLM not ready", 0

        if not os.path.exists(image_path):
            return False, f"Image not found: {image_path}", 0

        # Import bfloat16 here
        from ml_dtypes import bfloat16

        with self._lock:
            try:
                start_time = time.time()

                # Reset KV cache for fresh inference
                self._reset_kv_cache()

                # Encode image
                image_features = self._encode_image(image_path)

                # Build prompt - IMAGE FIRST, then question (per IMPLEMENTATION_GUIDE.md)
                full_prompt = "<|im_start|>system\nYou are a helpful assistant that answers questions accurately and concisely.<|im_end|>\n"
                full_prompt += "<|im_start|>user\n" + "<image>" * self.TOKEN_LENGTH + "\n"
                full_prompt += self.AD_PROMPT + "<|im_end|>\n<|im_start|>assistant\n"

                token_ids = self.tokenizer.encode(full_prompt)

                # Prepare prefill data - use astype() NOT view() per IMPLEMENTATION_GUIDE.md
                prefill_data = np.take(self.embeds, token_ids, axis=0)
                prefill_data = prefill_data.astype(bfloat16)

                # Insert image features (convert to bfloat16 to match prefill_data)
                # Image token ID is 151646
                image_token_indices = np.where(np.array(token_ids) == 151646)[0]
                if len(image_token_indices) > 0:
                    image_start_index = image_token_indices[0]
                    image_insert_index = image_start_index + 1
                    prefill_data[image_insert_index:image_insert_index + self.TOKEN_LENGTH] = \
                        image_features[0, :, :].astype(bfloat16)

                # Get EOS token(s)
                eos_token_id = None
                if isinstance(self.config.eos_token_id, list) and len(self.config.eos_token_id) > 1:
                    eos_token_id = self.config.eos_token_id

                # Run inference
                slice_len = 128
                token_ids = self.imer.prefill(
                    self.tokenizer,
                    token_ids,
                    prefill_data,
                    slice_len=slice_len
                )
                response = self.imer.decode(
                    self.tokenizer,
                    token_ids,
                    self.embeds,
                    slice_len=slice_len,
                    eos_token_id=eos_token_id,
                    stream=False
                )

                elapsed = time.time() - start_time
                is_ad = self._is_ad_response(response)

                return is_ad, response, elapsed

            except Exception as e:
                logger.error(f"VLM inference error: {e}")
                import traceback
                traceback.print_exc()
                return False, str(e), time.time() - start_time

    def _is_ad_response(self, response):
        """Check if VLM response indicates an ad - STRICT parsing to reduce false positives."""
        r = response.lower().strip()

        # Check for explicit No first (bias toward not blocking)
        if r.startswith('no') or r == 'n':
            return False

        # Check for explicit Yes at start only
        if r.startswith('yes') or r == 'y':
            return True

        # Check first word only (stricter than first 3 words)
        first_word = r.split()[0] if r.split() else ''
        if first_word == 'no' or first_word == 'no,' or first_word == 'no.':
            return False
        if first_word == 'yes' or first_word == 'yes,' or first_word == 'yes.':
            return True

        # Check for explicit negation phrases (these indicate NOT an ad)
        non_ad_phrases = [
            'not a commercial', 'not a tv commercial', 'not an ad',
            'not an advertisement', 'not a video ad', 'no ad',
            'this is not', 'this is a menu', 'this is a home screen',
            'this appears to be a menu', 'this appears to be a home',
            'interface', 'home screen', 'menu screen', 'app interface'
        ]
        for phrase in non_ad_phrases:
            if phrase in r:
                return False

        # Only mark as ad if explicitly stated as commercial/tv ad
        ad_phrases = ['tv commercial', 'commercial break', 'video advertisement', 'this is a commercial']
        for phrase in ad_phrases:
            if phrase in r:
                return True

        # Default to NOT an ad if uncertain (conservative - avoid false positives)
        return False

    def release(self):
        """Release resources - clean up model components."""
        self.config = None
        self.tokenizer = None
        self.imer = None
        self.vision_session = None
        self.embeds = None
        self.image_processor = None
        self.is_ready = False
        logger.info("VLM manager released")

    def start_tokenizer_service(self):
        """Compatibility method - FastVLM uses transformers tokenizer."""
        return self.load_model()
