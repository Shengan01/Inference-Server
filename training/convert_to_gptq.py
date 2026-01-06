#!/usr/bin/env python3
"""
GPTQ Conversion Script - Merge LoRA and Quantize

This script takes the LoRA adapters from QLoRA training and:
1. Loads the base Qwen2-0.5B model
2. Merges the LoRA adapters
3. Quantizes using llm-compressor (vLLM's production tool)
4. Saves for Triton vLLM deployment

Requirements:
    - NVIDIA GPU with 16GB+ VRAM
    - Python packages: transformers, peft, llmcompressor

Output:
    - Quantized model in ../models/finance-qwen/1/

Usage:
    python convert_to_gptq.py
"""

import os
import shutil
import torch
from pathlib import Path

from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel
from llmcompressor import oneshot

# ============================================
# CONFIGURATION
# ============================================

# Input paths
LORA_ADAPTER_PATH = "./output/finance-qwen-lora"
# Qwen2-0.5B - tiny, ~4GB RAM for merging
BASE_MODEL_NAME = "Qwen/Qwen2-0.5B-Instruct"

# Output paths
MERGED_MODEL_PATH = "./output/finance-qwen-merged"
OUTPUT_MODEL_PATH = "../models/finance-qwen/1"

# Quantization recipe (W4A16 = 4-bit weights, 16-bit activations)
QUANTIZATION_RECIPE = """
quant_stage:
    quant_modifiers:
        GPTQModifier:
            ignore: ["lm_head"]
            config_groups:
                group_0:
                    targets: ["Linear"]
                    weights:
                        num_bits: 4
                        type: "int"
                        symmetric: true
                        strategy: "group"
                        group_size: 128
"""


def check_lora_exists():
    """Verify LoRA adapters exist."""
    if not os.path.exists(LORA_ADAPTER_PATH):
        print(f"LoRA adapters not found at: {LORA_ADAPTER_PATH}")
        print("Please run train_qlora.py first.")
        return False
    return True


def merge_lora_to_base():
    """Load base model and merge LoRA adapters on GPU."""
    print("\n[1/4] Loading base model")
    
    base_model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL_NAME,
        torch_dtype=torch.float16,
        device_map="auto",
        trust_remote_code=True,
    )
    
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL_NAME, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    
    print("\n[2/4] Merging LoRA adapters...")
    
    # Load LoRA adapters
    model = PeftModel.from_pretrained(base_model, LORA_ADAPTER_PATH)
    
    # Merge and unload
    model = model.merge_and_unload()
    
    print("Merged model created (fp16 format)")
    
    # Save merged model temporarily
    print(f"Saving merged model to: {MERGED_MODEL_PATH}")
    os.makedirs(MERGED_MODEL_PATH, exist_ok=True)
    model.save_pretrained(MERGED_MODEL_PATH, safe_serialization=True)
    tokenizer.save_pretrained(MERGED_MODEL_PATH)
    
    # Clean up memory
    del model
    del base_model
    torch.cuda.empty_cache()


def quantize_model():
    """Quantize the merged model using llm-compressor."""
    print("\n[3/4] Quantizing model with llm-compressor...")
    print("Method: GPTQ W4A16 (4-bit weights, 16-bit activations)")
    
    # Create output directory
    os.makedirs(OUTPUT_MODEL_PATH, exist_ok=True)
    
    # Save recipe to temp file
    recipe_path = "./output/quantize_recipe.yaml"
    with open(recipe_path, "w") as f:
        f.write(QUANTIZATION_RECIPE)
    
    # Run quantization using wikitext dataset for calibration
    model = oneshot(
        model=MERGED_MODEL_PATH,
        dataset="wikitext",
        dataset_config_name="wikitext-2-raw-v1",
        splits="train",
        recipe=recipe_path,
        output_dir=OUTPUT_MODEL_PATH,
        num_calibration_samples=256,
        max_seq_length=2048,
        save_compressed=True,
    )
    
    print("Quantization complete")
    
    # Cleanup recipe file
    os.remove(recipe_path)
    
    return model


def verify_output():
    """Verify the output model files."""
    print(f"\n[4/4] Verifying output at: {OUTPUT_MODEL_PATH}")
    
    files = os.listdir(OUTPUT_MODEL_PATH) if os.path.exists(OUTPUT_MODEL_PATH) else []
    print(f"Files created: {len(files)}")
    
    for f in sorted(files)[:10]:
        fpath = os.path.join(OUTPUT_MODEL_PATH, f)
        if os.path.isfile(fpath):
            size = os.path.getsize(fpath)
            print(f"   - {f}: {size / 1e6:.1f} MB")
    
    # Total size
    total_size = sum(
        os.path.getsize(os.path.join(OUTPUT_MODEL_PATH, f))
        for f in files
        if os.path.isfile(os.path.join(OUTPUT_MODEL_PATH, f))
    )
    print(f"\nTotal model size: {total_size / 1e9:.2f} GB")


def cleanup():
    """Remove intermediate merged model."""
    if os.path.exists(MERGED_MODEL_PATH):
        print(f"\n   Cleaning up: {MERGED_MODEL_PATH}")
        shutil.rmtree(MERGED_MODEL_PATH)


def main():
    print("=" * 60)
    print("Model Quantization: LoRA → Merged → GPTQ")
    print("Model: Qwen2-0.5B (fits easily on 16GB GPU)")
    print("Using: llm-compressor (vLLM Project)")
    print("=" * 60)
    
    # Check GPU
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required for conversion!")
    
    print(f"\nGPU: {torch.cuda.get_device_name(0)}")
    print(f"VRAM: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")
    
    # Check LoRA exists
    if not check_lora_exists():
        return
    
    try:
        # Step 1-2: Merge LoRA
        merge_lora_to_base()
        
        # Step 3: Quantize
        quantize_model()
        
        # Step 4: Verify
        verify_output()
        
        # Cleanup
        cleanup()
        
        print("\n" + "=" * 60)
        print("CONVERSION COMPLETE!")
        print("=" * 60)
        print(f"\nQuantized model ready at:")
        print(f"{os.path.abspath(OUTPUT_MODEL_PATH)}")
        print("\nNext steps:")
        print("1. cd ..")
        print("2. docker compose up -d")
        print("3. python simulation.py")
        
    except Exception as e:
        print(f"\nError during conversion: {e}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
