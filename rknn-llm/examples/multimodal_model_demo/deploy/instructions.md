# InternVL3-1B Multimodal Demo - Youyeetoo R1 Instructions

## Prerequisites

- Models already downloaded:
  - `internvl3-1b_w8a8_rk3588.rkllm` (761 MB) - the LLM component
  - `internvl3-1b_vision_fp16_rk3588.rknn` (619 MB) - the vision encoder

## Step 1: Build the Demo

```bash
cd ~/rknn-llm/examples/multimodal_model_demo/deploy
chmod +x build-native.sh
./build-native.sh
```

This creates `install/demo_Linux_aarch64/` containing:
- `demo` - main multimodal executable
- `imgenc` - image encoder test tool
- `lib/` - required libraries

## Step 2: Set Up Directory Structure

```bash
cd ~/rknn-llm/examples/multimodal_model_demo/deploy/install/demo_Linux_aarch64

# Create models directory and copy/link your models
mkdir -p models
cp /path/to/internvl3-1b_w8a8_rk3588.rkllm models/
cp /path/to/internvl3-1b_vision_fp16_rk3588.rknn models/

# Copy a test image
cp ~/rknn-llm/examples/multimodal_model_demo/data/demo.jpg .
```

## Step 3: Run the Demo

```bash
cd ~/rknn-llm/examples/multimodal_model_demo/deploy/install/demo_Linux_aarch64

# Set library path
export LD_LIBRARY_PATH=./lib:$LD_LIBRARY_PATH

# Test the image encoder first
./imgenc models/internvl3-1b_vision_fp16_rk3588.rknn demo.jpg 3

# Run full multimodal demo
# Format: ./demo <image> <vision_model> <llm_model> <max_new_tokens> <max_context_len> <npu_cores> <img_start_token> <img_end_token> <img_pad_token>
./demo demo.jpg models/internvl3-1b_vision_fp16_rk3588.rknn models/internvl3-1b_w8a8_rk3588.rkllm 2048 4096 3 "<|vision_start|>" "<|vision_end|>" "<|image_pad|>"
```

**Note:** The vision tokens (`<|vision_start|>`, `<|vision_end|>`, `<|image_pad|>`) may differ for InternVL3 vs Qwen2-VL. Check InternVL3 documentation if these don't work.

## Troubleshooting

### Driver Version Error
Your device has RKNPU driver v0.8.2, but RKLLM typically requires v0.9.8+. If you see driver errors, you'll need to update the kernel/driver.

### Library Not Found
If you get "librkllmrt.so not found":
```bash
export LD_LIBRARY_PATH=./lib:$LD_LIBRARY_PATH
```

### Permission Denied
```bash
chmod +x demo imgenc
```
