#!/usr/bin/env python3
"""
QLoRA Fine-Tuning Script for Qwen2 on Twitter Financial News Sentiment

This script fine-tunes Qwen2 using QLoRA (Quantized Low-Rank Adaptation)
on the Twitter Financial News Sentiment dataset for sentiment analysis.

Requirements:
    - NVIDIA GPU with 16GB+ VRAM
    - CUDA 12.x
    - Python packages: unsloth, transformers, datasets, trl, peft

Output:
    - LoRA adapter weights saved to ./output/finance-qwen-lora/
    
Usage:
    python train_qlora.py
    
After training, run convert_to_gptq.py to create the final model.
"""

# IMPORTANT: Import unsloth FIRST before any other ML libraries
from unsloth import FastLanguageModel

import torch
from datasets import load_dataset
from transformers import TrainingArguments
from trl import SFTTrainer

# ============================================
# CONFIGURATION
# ============================================

# Model configuration - Qwen2-0.5B (tiny, ~4GB RAM for merge)
MODEL_NAME = "unsloth/Qwen2-0.5B-Instruct-bnb-4bit"
MAX_SEQ_LENGTH = 2048
LOAD_IN_4BIT = True

# LoRA configuration
LORA_R = 16  # Rank
LORA_ALPHA = 16
LORA_DROPOUT = 0.0
TARGET_MODULES = [
    "q_proj", "k_proj", "v_proj", "o_proj",
    "gate_proj", "up_proj", "down_proj"
]

# Training configuration
OUTPUT_DIR = "./output/finance-qwen-lora"
BATCH_SIZE = 4
GRADIENT_ACCUMULATION_STEPS = 4
NUM_EPOCHS = 3
LEARNING_RATE = 2e-4
WARMUP_STEPS = 5
MAX_STEPS = 100  # Set to -1 for full training

# Dataset configuration
DATASET_NAME = "zeroshot/twitter-financial-news-sentiment"
DATASET_SPLIT = "train"

# ============================================
# PROMPT TEMPLATE
# ============================================

FINANCE_PROMPT = """Below is a financial statement. Analyze the sentiment.

### Statement:
{text}

### Sentiment:
{label}"""

# Twitter Financial News Sentiment labels
LABEL_MAP = {
    0: "bearish",
    1: "bullish",
    2: "neutral"
}


def format_example(example):
    """Format a single example for training."""
    return FINANCE_PROMPT.format(
        text=example["text"],
        label=LABEL_MAP[example["label"]]
    )


def main():
    print("=" * 60)
    print("QLoRA Fine-Tuning: Qwen2 for Finance")
    print("=" * 60)
    
    # Check GPU
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required for training!")
    
    print(f"\nGPU: {torch.cuda.get_device_name(0)}")
    print(f"VRAM: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")
    
    # ============================================
    # LOAD MODEL WITH UNSLOTH
    # ============================================
    print("\n[1/5] Loading model with Unsloth...")
    
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=MODEL_NAME,
        max_seq_length=MAX_SEQ_LENGTH,
        dtype=None,  # Auto-detect
        load_in_4bit=LOAD_IN_4BIT,
    )
    
    # Add LoRA adapters
    print("\n[2/5] Adding LoRA adapters...")
    
    model = FastLanguageModel.get_peft_model(
        model,
        r=LORA_R,
        target_modules=TARGET_MODULES,
        lora_alpha=LORA_ALPHA,
        lora_dropout=LORA_DROPOUT,
        bias="none",
        use_gradient_checkpointing="unsloth",
        random_state=42,
        use_rslora=False,
        loftq_config=None,
    )
    
    # ============================================
    # LOAD DATASET
    # ============================================
    print("\n[3/5] Loading Twitter Financial News Sentiment dataset...")
    
    dataset = load_dataset(DATASET_NAME, split=DATASET_SPLIT)
    print(f"Dataset size: {len(dataset)} examples")
    
    # Format dataset
    dataset = dataset.map(
        lambda x: {"text": format_example(x)},
        remove_columns=dataset.column_names
    )
    
    # ============================================
    # TRAINING
    # ============================================
    print("\n[4/5] Starting training...")
    
    training_args = TrainingArguments(
        output_dir=OUTPUT_DIR,
        per_device_train_batch_size=BATCH_SIZE,
        gradient_accumulation_steps=GRADIENT_ACCUMULATION_STEPS,
        warmup_steps=WARMUP_STEPS,
        max_steps=MAX_STEPS,
        num_train_epochs=NUM_EPOCHS if MAX_STEPS == -1 else 1,
        learning_rate=LEARNING_RATE,
        fp16=not torch.cuda.is_bf16_supported(),
        bf16=torch.cuda.is_bf16_supported(),
        logging_steps=10,
        save_steps=50,
        optim="adamw_8bit",
        weight_decay=0.01,
        lr_scheduler_type="linear",
        seed=42,
        report_to="none",
    )
    
    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=dataset,
        dataset_text_field="text",
        max_seq_length=MAX_SEQ_LENGTH,
        args=training_args,
    )
    
    # Train!
    trainer.train()
    
    # ============================================
    # SAVE MODEL
    # ============================================
    print("\n[5/5] Saving LoRA adapters...")
    
    model.save_pretrained(OUTPUT_DIR)
    tokenizer.save_pretrained(OUTPUT_DIR)
    
    print(f"\nTraining complete!")
    print(f"LoRA adapters saved to: {OUTPUT_DIR}")
    print(f"\nNext step: Run convert_to_gptq.py to create the final model.")


if __name__ == "__main__":
    main()
