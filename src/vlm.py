"""
VLM (Vision Language Model) integration for Stream Sentry.

Uses Qwen3-VL-2B-INT4 on Axera LLM 8850 NPU for ad detection.
Model is loaded ONCE at startup and kept running for fast inference.
Each inference takes ~1.3 seconds.
"""

import os
import sys
import time
import logging
import threading
from pathlib import Path

logger = logging.getLogger('StreamSentry.VLM')

# Model paths
QWEN3_MODEL_DIR = Path("/home/radxa/axera_models/Qwen3-VL-2B")
AXMODEL_SUBDIR = "Qwen3-VL-2B-Instruct-AX650-c128_p1152-int4"


class VLMManager:
    """
    Qwen3-VL-2B-INT4 manager for ad detection on Axera LLM 8850.

    The model is loaded once at initialization and kept running.
    Uses pexpect to communicate with the continuous-mode binary.
    Each inference takes ~1.3 seconds.
    """

    # More specific prompt to reduce false positives on regular video content
    AD_PROMPT = "Is this a video advertisement or commercial break? Look for: Skip Ad button, Ad label, Sponsored tag, or product commercials. Answer No for music videos, TV shows, movies, or regular content. Answer only Yes or No."

    def __init__(self):
        """Initialize VLM manager."""
        self.is_ready = False
        self._lock = threading.Lock()
        self.process = None

        # Validate paths
        if not QWEN3_MODEL_DIR.exists():
            logger.error(f"Qwen3-VL-2B not found at: {QWEN3_MODEL_DIR}")
            return

        axmodel_dir = QWEN3_MODEL_DIR / AXMODEL_SUBDIR
        if not axmodel_dir.exists():
            logger.error(f"Model files not found: {axmodel_dir}")
            return

        logger.info(f"VLM using Qwen3-VL-2B-INT4 at: {QWEN3_MODEL_DIR}")

    def load_model(self):
        """Load the model - starts the binary process."""
        if self.process is not None and self.is_ready:
            logger.info("Model already loaded")
            return True

        try:
            import pexpect
        except ImportError:
            logger.info("Installing pexpect...")
            os.system("pip3 install pexpect --break-system-packages")
            import pexpect

        try:
            logger.info("Starting Qwen3-VL-2B-INT4 model (takes ~40s)...")
            start_time = time.time()

            cmd = (
                f"./main_axcl_aarch64_rebuilt "
                f"--template_filename_axmodel '{AXMODEL_SUBDIR}/qwen3_vl_text_p128_l%d_together.axmodel' "
                f"--axmodel_num 28 "
                f"--filename_image_encoder_axmodedl '{AXMODEL_SUBDIR}/Qwen3-VL-2B-Instruct_vision.axmodel' "
                f"--use_mmap_load_embed 1 "
                f"--filename_tokenizer_model 'qwen3_tokenizer.txt' "
                f"--filename_post_axmodel '{AXMODEL_SUBDIR}/qwen3_vl_text_post.axmodel' "
                f"--filename_tokens_embed '{AXMODEL_SUBDIR}/model.embed_tokens.weight.bfloat16.bin' "
                f"--tokens_embed_num 151936 "
                f"--tokens_embed_size 2048 "
                f"--patch_size 16 "
                f"--live_print 1 "
                f"--video 0 "
                f"--img_width 384 "
                f"--img_height 384 "
                f"--vision_start_token_id 151652 "
                f"--post_config_path post_config.json "
                f"--devices 0"
            )

            self.process = pexpect.spawn(
                '/bin/bash', ['-c', f'cd {QWEN3_MODEL_DIR} && {cmd}'],
                timeout=180,
                encoding='utf-8'
            )

            # Wait for model to load
            try:
                self.process.expect(['prompt >>', 'LLM init ok'], timeout=120)
                load_time = time.time() - start_time

                # Wait for prompt to appear
                if 'LLM init ok' in self.process.after:
                    self.process.expect('prompt >>', timeout=30)

                logger.info(f"Qwen3-VL-2B-INT4 loaded in {load_time:.1f}s")
                self.is_ready = True
                return True

            except pexpect.TIMEOUT:
                logger.error("Timeout waiting for model to load")
                return False
            except pexpect.EOF:
                logger.error(f"Process ended unexpectedly: {self.process.before}")
                return False

        except Exception as e:
            logger.error(f"Failed to load Qwen3-VL-2B: {e}")
            import traceback
            traceback.print_exc()
            return False

    def detect_ad(self, image_path):
        """
        Run ad detection on an image.

        Args:
            image_path: Path to image file (JPEG/PNG)

        Returns:
            tuple: (is_ad, response_text, elapsed_time)
        """
        if not self.is_ready or not self.process:
            return False, "VLM not ready", 0

        if not os.path.exists(image_path):
            return False, f"Image not found: {image_path}", 0

        # Import pexpect here to avoid issues if not installed
        import pexpect

        with self._lock:
            try:
                start_time = time.time()

                # Send prompt
                self.process.sendline(self.AD_PROMPT)

                # Wait for image prompt
                try:
                    self.process.expect(['image >>', 'img >>'], timeout=5)
                    self.process.sendline(str(image_path))
                except pexpect.TIMEOUT:
                    # Model might expect image path right after prompt
                    self.process.sendline(str(image_path))

                # Wait for response and next prompt
                self.process.expect('prompt >>', timeout=60)

                # Extract response from output
                output = self.process.before
                response = self._parse_response(output)

                elapsed = time.time() - start_time
                is_ad = self._is_ad_response(response)

                return is_ad, response, elapsed

            except pexpect.TIMEOUT:
                logger.error(f"VLM timeout on {image_path}")
                return False, "TIMEOUT", time.time() - start_time
            except pexpect.EOF:
                logger.error("VLM process ended unexpectedly")
                self.is_ready = False
                return False, "EOF", 0
            except Exception as e:
                logger.error(f"VLM inference error: {e}")
                return False, str(e), 0

    def _parse_response(self, output):
        """Parse the model output to extract Yes/No response."""
        lines = output.strip().split('\n')
        for line in lines:
            line = line.strip()
            # Skip lines with image path or prompt text
            if line.startswith('/') or 'advertisement' in line.lower():
                continue
            if line.lower().startswith('yes'):
                return line
            elif line.lower().startswith('no'):
                return line
        return output.strip()[-50:] if output.strip() else ""

    def _is_ad_response(self, response):
        """Check if VLM response indicates an ad."""
        r = response.lower().strip()
        if r.startswith('yes') or r == 'y':
            return True
        if r.startswith('no') or r == 'n':
            return False
        # Check for positive indicators
        if 'yes' in r and 'no' not in r:
            return True
        return False

    def release(self):
        """Release resources - stop the model process."""
        if self.process:
            try:
                self.process.sendline('q')
                self.process.close()
            except:
                pass
            self.process = None
        self.is_ready = False
        logger.info("VLM manager released")

    def start_tokenizer_service(self):
        """Compatibility method - Qwen3-VL uses local tokenizer."""
        return self.load_model()
