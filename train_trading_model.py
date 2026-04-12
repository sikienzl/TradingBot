from datetime import datetime
import os
import pandas as pd
import numpy as np
import torch
from sklearn.preprocessing import MinMaxScaler
from datasets import Dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    TrainingArguments,
    BitsAndBytesConfig,
)
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from trl import SFTTrainer
import warnings
warnings.filterwarnings("ignore")

# --- 1. LOAD & PREPARE DATA ---


def load_and_preprocess_data(filepath="training_data.csv"):
    """Loads data produced by data_preparation.py and prepares it for training."""
    try:
        df = pd.read_csv(filepath)

        # Filter for complete records only (with all indicators)
        required_cols = ['timestamp', 'coin', 'close']
        available_cols = [col for col in ['rsi', 'macd', 'macd_signal', 'macd_hist',
                                          'sma_50', 'sma_200', 'bb_upper', 'bb_middle', 'bb_lower'] if col in df.columns]
        required_cols += available_cols

        df = df.dropna(subset=required_cols)

        # Use most common coin if insufficient BTC data
        if 'coin' in df.columns:
            if len(df[df['coin'] == 'BTC']) > 50:
                df = df[df['coin'] == 'BTC'].copy()
            else:
                most_common_coin = df['coin'].mode()[0]
                df = df[df['coin'] == most_common_coin].copy()
                print(f"🔄 Using {most_common_coin} instead of BTC")

        # Normalise available indicators only
        scaler = MinMaxScaler()
        numeric_cols = ['rsi', 'macd', 'macd_signal',
                        'macd_hist']  # Basis-Indikatoren
        numeric_cols = [col for col in numeric_cols if col in df.columns]
        if numeric_cols:
            df[numeric_cols] = scaler.fit_transform(df[numeric_cols])

        # Simple label creation based on available data
        if 'sma_50' in df.columns and 'rsi' in df.columns:
            df["label"] = np.where(
                (df["close"] > df["sma_50"]) & (df["rsi"] < 30), "kaufen",
                np.where(
                    (df["close"] < df["sma_50"]) & (
                        df["rsi"] > 70), "verkaufen",
                    "halten"
                )
            )
        else:
            # Fallback: simple price change
            df["label"] = np.where(df["close"].pct_change() > 0.02, "kaufen",
                                   np.where(df["close"].pct_change() < -0.02, "verkaufen", "halten"))

        df = df.dropna()

        # Timestamp-Konvertierung
        df['timestamp'] = pd.to_datetime(
            df['timestamp']).astype(np.int64) // 10**6

        print(f"✅ Data prepared: {len(df)} records")
        return df

    except Exception as e:
        print(f"❌ Error during data preparation: {str(e)}")
        return None

# --- 2. PROMPT CREATION (ADAPTED FOR YOUR DATA) ---


def create_prompt(row):
    """Creates an optimized text prompt from the data."""
    # Dynamische Indikatorenauswahl
    indicators = []
    for col in ['rsi', 'macd', 'macd_signal', 'macd_hist', 'sma_50', 'sma_200', 'bb_upper', 'bb_middle', 'bb_lower']:
        if col in row and not pd.isna(row[col]):
            if col in ['rsi', 'macd', 'macd_signal', 'macd_hist']:
                indicators.append(f"{col.upper()}: {row[col]:.2f}")
            else:
                indicators.append(f"{col}: {row[col]:.2f}")

    prompt = f"""[INST] <<SYS>>
Du bist ein Krypto-Handelsassistent. Analysiere die folgenden Marktdaten und gib eine klare Handelsempfehlung.
Verwende nur diese Antworten: kaufen, verkaufen oder halten.
<</SYS>>

Aktuelle Marktdaten für {row['coin']}:
- Datum: {pd.to_datetime(row['timestamp'], unit='ms').strftime('%Y-%m-%d %H:%M')}
- Preis: Close={row['close']:.2f}
- Technische Indikatoren:
  {'  '.join(f'- {ind}' for ind in indicators)}

Handelsempfehlung: [/INST]
{row['label']}"""

    return prompt

# --- 3. DATASET FOR FINE-TUNING (ADAPTED) ---


def prepare_dataset(df):
    """Converts the DataFrame into a Hugging Face Dataset."""
    try:
        # Random sampling for better performance
        df = df.sample(frac=1).head(min(1000, len(df)))

        prompts = [create_prompt(row) for _, row in df.iterrows()]
        dataset = Dataset.from_dict({"text": prompts})

        print(f"📚 Dataset created with {len(dataset)} examples")
        return dataset
    except Exception as e:
        print(f"❌ Error during dataset creation: {str(e)}")
        return None

# --- 4. MODELL LADEN (OPTIMIERT) ---


def load_model():
    """Loads Mistral-7B with 4-bit quantization."""
    model_name = "mistralai/Mistral-7B-v0.1"

    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=False,
    )

    print("🤖 Loading model and tokenizer...")
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    model = AutoModelForCausalLM.from_pretrained(
        model_name,
        quantization_config=bnb_config,
        device_map="auto",
        torch_dtype=torch.bfloat16,
    )

    model = prepare_model_for_kbit_training(model)
    return model, tokenizer

# --- 5. LoRA-KONFIGURATION ---


def get_lora_config():
    """Konfiguriert Low-Rank Adaptation."""
    return LoraConfig(
        r=16,
        lora_alpha=32,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
    )

# --- 6. TRAINING (ADAPTED FOR LATEST trl VERSION) ---


def train(model, tokenizer, dataset):
    """Fine-tunes the model with the prepared data."""
    try:
        # Grundlegende Training-Parameter
        training_args = TrainingArguments(
            output_dir="./results",
            per_device_train_batch_size=1,
            gradient_accumulation_steps=4,
            learning_rate=1e-4,
            num_train_epochs=3,
            bf16=torch.cuda.is_available(),
            save_steps=200,
            logging_steps=10,
            optim="paged_adamw_8bit",
            warmup_ratio=0.1,
            max_grad_norm=0.3,
            lr_scheduler_type="cosine",
            report_to="none",
        )

        # Minimal SFTTrainer call for latest version
        trainer = SFTTrainer(
            model=model,
            args=training_args,
            train_dataset=dataset,
            peft_config=get_lora_config(),
            # No additional parameters needed anymore
        )

        print("🚀 Training startet...")
        trainer.train()

        # Modell speichern
        # output_dir = f"./model/trading_model_{datetime.now().strftime('%Y%m%d_%H%M')}"
        output_dir = "./model/fine_tuned_trading_model"
        os.makedirs(output_dir, exist_ok=True)
        trainer.model.save_pretrained(output_dir)
        tokenizer.save_pretrained(output_dir)

        print(f"✅ Training complete! Model saved to '{output_dir}'")
        return True

    except Exception as e:
        print(f"❌ Training failed: {str(e)}")
        return False


# --- 7. HAUPTPROGRAMM ---
if __name__ == "__main__":
    try:
        print("📊 Loading and preparing data...")
        df = load_and_preprocess_data()

        if df is None or len(df) < 50:
            print(
                f"❌ Insufficient data: only {len(df) if df is not None else 0} valid records")
            raise ValueError("Insufficient data volume")

        print("💾 Creating dataset...")
        dataset = prepare_dataset(df)
        if dataset is None:
            raise ValueError("Dataset creation failed")

        print("🤖 Loading model...")
        model, tokenizer = load_model()

        print("🎓 Starting training...")
        success = train(model, tokenizer, dataset)

        if success:
            print("🎉 Training completed successfully!")
        else:
            print("❌ Training ended with errors")

    except Exception as e:
        print(f"\n❌ Fehler: {str(e)}")
        print("\n🔍 Troubleshooting tips:")
        print("1. Check if 'training_data.csv' exists")
        print("2. Make sure enough VRAM is available (min. 24GB recommended)")
        print("3. Versuche: CUDA_VISIBLE_DEVICES=0 python3 train_trading_model.py")
        print("4. For smaller VRAM: reduce per_device_train_batch_size to 1")
        print("5. Check data quality with data_preparation.py")
