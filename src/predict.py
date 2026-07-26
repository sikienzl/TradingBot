import pandas as pd
import numpy as np
from sklearn.preprocessing import MinMaxScaler
from transformers import AutoModelForCausalLM, AutoTokenizer, pipeline, BitsAndBytesConfig
import torch
import warnings

# Konfiguration für saubere Ausgabe
warnings.filterwarnings("ignore", category=UserWarning)
pd.set_option('display.max_colwidth', None)


class TradingModelPredictor:
    def __init__(self, model_path="./model/fine_tuned_trading_model"):
        """Initialisiert das Modell und Tokenizer"""
        self.model_path = model_path
        self.model = None
        self.tokenizer = None
        self.pipe = None
        self._load_model()

    def _load_model(self):
        """Lädt das Modell mit optimalen Einstellungen"""
        # Quantisierungskonfiguration
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=False,
        )

        # Modell laden
        self.model = AutoModelForCausalLM.from_pretrained(
            self.model_path,
            quantization_config=bnb_config,
            device_map="auto",
            trust_remote_code=True
        )

        self.tokenizer = AutoTokenizer.from_pretrained(self.model_path)
        self.tokenizer.pad_token = self.tokenizer.eos_token
        self.tokenizer.padding_side = "left"

        # Pipeline konfigurieren
        self.pipe = pipeline(
            "text-generation",
            model=self.model,
            tokenizer=self.tokenizer,
            truncation=True,
            pad_token_id=self.tokenizer.eos_token_id,
            clean_up_tokenization_spaces=True
        )

    def prepare_data(self, data):
        """Bereitet die Eingabedaten vor"""
        scaler = MinMaxScaler()
        data = data.copy()
        data[["rsi", "macd", "volume"]] = scaler.fit_transform(
            data[["rsi", "macd", "volume"]]
        )
        return data

    def create_prompt(self, data):
        """Erstellt den Optimierten Prompt"""
        template = """Aktuelle Marktdaten für {coin} am {date}:
Preis: Open={open:.2f}, High={high:.2f}, Low={low:.2f}, Close={close:.2f}
Volumen: {volume:.2f}
Technische Indikatoren: RSI={rsi:.2f}, MACD={macd:.2f}

Frage: Sollte ich kaufen, verkaufen oder halten?
Antwort nur mit einem Wort (kaufen/verkaufen/halten):"""

        row = data.iloc[0] if isinstance(data, pd.DataFrame) else data
        return template.format(
            coin=row['coin'],
            date=pd.to_datetime(row['timestamp'], unit='ms').strftime(
                '%Y-%m-%d %H:%M'),
            open=row['open'],
            high=row['high'],
            low=row['low'],
            close=row['close'],
            volume=row['volume'],
            rsi=row['rsi'],
            macd=row['macd']
        )

    def rule_based_decision(self, data, rsi_buy=30, rsi_sell=70):
        row = data.iloc[0] if isinstance(data, pd.DataFrame) else data
        if 'rsi' in row:
            if row['rsi'] < rsi_buy:
                return 'kaufen'
            elif row['rsi'] > rsi_sell:
                return 'verkaufen'
        return 'halten'

    def predict(self, data, n_votes=5, confidence_threshold=0.6):
        """Gibt die Handelsentscheidung und Confidence zurück (Ensemble aus LLM und Regel)."""
        try:
            processed_data = self.prepare_data(data)
            prompt = self.create_prompt(processed_data)
            # LLM-Vorhersagen (Mehrfach, für Confidence)
            responses = self.pipe(
                prompt,
                max_new_tokens=50,
                temperature=0.1,
                do_sample=True,
                top_k=10,
                top_p=0.9,
                num_return_sequences=n_votes,
                eos_token_id=self.tokenizer.eos_token_id
            )
            # Entscheidungen extrahieren – nur den neu generierten Teil auswerten,
            # Groß-/Kleinschreibung und Satzzeichen ignorieren
            valid_decisions = {"kaufen", "verkaufen", "halten"}
            votes = []
            for r in responses:
                # Nur den neu generierten Teil betrachten (nach dem Prompt)
                full_text = r['generated_text']
                generated = full_text[len(prompt):].strip().lower()
                # 1. Versuch: erstes Wort (Satzzeichen entfernen)
                first_word = generated.split()[0].strip(
                    '.,!?:;"\'-') if generated.split() else ''
                if first_word in valid_decisions:
                    votes.append(first_word)
                    continue
                # 2. Versuch: irgendwo im Text suchen (Priorität: spezifischeres zuerst)
                found = None
                for keyword in ("verkaufen", "kaufen", "halten"):
                    if keyword in generated:
                        found = keyword
                        break
                if found:
                    votes.append(found)
            if not votes:
                return {'decision': 'halten', 'confidence': 0.0, 'llm_votes': {}, 'rule': self.rule_based_decision(data)}
            # Mehrheitsentscheidung und Confidence
            from collections import Counter
            vote_counts = Counter(votes)
            decision, count = vote_counts.most_common(1)[0]
            confidence = count / n_votes
            # Regelbasierte Entscheidung
            rule_decision = self.rule_based_decision(data)
            # Ensemble-Logik: Nur handeln, wenn LLM und Regel übereinstimmen und Confidence hoch
            if decision == rule_decision and confidence >= confidence_threshold:
                final_decision = decision
            else:
                final_decision = 'halten'
            return {
                'decision': final_decision,
                'confidence': confidence,
                'llm_votes': dict(vote_counts),
                'rule': rule_decision
            }
        except Exception as e:
            print(f"Vorhersagefehler: {str(e)}")
            return {'decision': 'halten', 'confidence': 0.0, 'llm_votes': {}, 'rule': 'halten'}


# Beispielanwendung
if __name__ == "__main__":
    # Modell initialisieren
    predictor = TradingModelPredictor()

    # Beispiel-Daten
    example_data = pd.DataFrame([{
        'coin': 'BTC/EUR',
        'timestamp': 1672531200000,
        'open': 16500.0,
        'high': 16600.0,
        'low': 16450.0,
        'close': 16550.0,
        'volume': 1234.5,
        'rsi': 55.2,
        'macd': 12.3
    }])

    # Vorhersage durchführen
    print("\n=== Handelsentscheidungs-Assistent (Ensemble) ===")
    result = predictor.predict(example_data)
    print(
        f"\nEmpfohlene Aktion: {result['decision'].upper()} (Confidence: {result['confidence']*100:.0f}%)")
    print(f"LLM-Votes: {result['llm_votes']}")
    print(f"Regelbasierte Empfehlung: {result['rule']}")
