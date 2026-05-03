import json
import re
from dataclasses import dataclass

import httpx


@dataclass
class DamageAIResult:
    cleaned_description: str
    item: str | None
    damage_type: str | None
    quantity: int
    confidence: float
    chargeable: bool
    provider: str | None = None
    model: str | None = None


class DamageAIService:
    SUPPORTED_AUDIO_EXTENSIONS = {".flac", ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".ogg", ".wav", ".webm"}
    NO_CHARGE_PATTERNS = [
        r"\bno charge\b",
        r"\bno cost\b",
        r"\bnot charging\b",
        r"\bdon'?t charge\b",
        r"\bdo not charge\b",
    ]
    DAMAGE_TERMS = [
        "broken",
        "cracked",
        "chipped",
        "damaged",
        "missing",
        "stained",
        "scratched",
        "bent",
        "loose",
        "torn",
        "burned",
        "burnt",
        "hole",
        "holes",
    ]
    STOP_TOKENS = {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "with",
        "and",
        "to",
        "for",
        "from",
        "this",
        "that",
        "it",
        "there",
    }
    LOCATION_TOKENS = {
        "by",
        "near",
        "next",
        "under",
        "behind",
        "beside",
        "around",
        "against",
        "inside",
        "outside",
        "around",
        "at",
        "on",
        "in",
    }

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: int = 20,
        transcription_model: str = "whisper-large-v3-turbo",
    ):
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model.strip()
        self.timeout_seconds = timeout_seconds
        self.transcription_model = transcription_model.strip()

    def is_enabled(self) -> bool:
        return bool(self.api_key and self.model)

    def can_transcribe_audio(self) -> bool:
        return bool(self.api_key and self.transcription_model)

    def transcribe_audio(self, file_name: str, payload: bytes, content_type: str | None = None) -> str:
        if not self.can_transcribe_audio():
            raise ValueError("Voice note transcription is not configured yet.")
        if not payload:
            raise ValueError("The voice note was empty.")

        normalized_name = file_name or "voice-note.m4a"
        extension = normalized_name.rsplit(".", 1)[-1].lower() if "." in normalized_name else ""
        if f".{extension}" not in self.SUPPORTED_AUDIO_EXTENSIONS:
            raise ValueError("Voice note must be an audio file such as OGG, M4A, WAV, MP3, or WEBM.")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
        }
        files = {
            "file": (normalized_name, payload, content_type or "application/octet-stream"),
        }
        data = {
            "model": self.transcription_model,
            "response_format": "json",
            "language": "en",
            "temperature": "0",
        }

        try:
            with httpx.Client(timeout=max(self.timeout_seconds, 60)) as client:
                response = client.post(
                    f"{self.base_url}/audio/transcriptions",
                    headers=headers,
                    data=data,
                    files=files,
                )
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPStatusError as exc:
            raise ValueError("I could not transcribe that voice note right now.") from exc
        except Exception as exc:
            raise ValueError("I could not transcribe that voice note right now.") from exc

        transcript = str(body.get("text") or "").strip()
        if not transcript:
            raise ValueError("I could not hear a usable description in that voice note.")
        return transcript

    def analyze_damage(self, raw_note: str) -> DamageAIResult:
        fallback = self.fallback_analyze(raw_note)
        if not self.is_enabled():
            return fallback

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You extract structured housing damage details from short notes. "
                        "Return strict JSON only with keys cleaned_description, item, damage_type, quantity, confidence, and chargeable. "
                        "item must be the primary damaged object only. "
                        "damage_type should be a short phrase like broken, cracked, stained, missing, or scratched. "
                        "quantity must be an integer of at least 1. "
                        "confidence must be a number between 0 and 1. "
                        "chargeable must be false when the note says no charge, no cost, not charging, do not charge, or don't charge. "
                        "Keep cleaned_description short and faithful to the user's wording."
                    ),
                },
                {"role": "user", "content": f"Damage note: {raw_note.strip()}"},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
            content = data["choices"][0]["message"]["content"]
            parsed = self._parse_response(content)
        except Exception:
            return fallback

        return DamageAIResult(
            cleaned_description=parsed["cleaned_description"],
            item=parsed["item"],
            damage_type=parsed["damage_type"],
            quantity=parsed["quantity"],
            confidence=parsed["confidence"],
            chargeable=parsed["chargeable"],
            provider="groq",
            model=self.model,
        )

    @classmethod
    def fallback_analyze(cls, raw_note: str) -> DamageAIResult:
        normalized = cls._normalize_text(raw_note)
        quantity = cls._extract_quantity(normalized)
        chargeable = not any(re.search(pattern, normalized) for pattern in cls.NO_CHARGE_PATTERNS)
        damage_type = cls._extract_damage_type(normalized)
        item = cls._extract_item(normalized, damage_type)
        return DamageAIResult(
            cleaned_description=cls._clean_description(raw_note),
            item=item,
            damage_type=damage_type,
            quantity=quantity,
            confidence=0.35 if item else 0.0,
            chargeable=chargeable,
        )

    @classmethod
    def _parse_response(cls, content: str) -> dict:
        match = re.search(r"\{.*\}", content, flags=re.DOTALL)
        if not match:
            raise ValueError("AI response did not contain JSON.")

        payload = json.loads(match.group(0))
        cleaned_description = cls._clean_description(payload.get("cleaned_description"))
        item = cls._normalize_item(payload.get("item"))
        damage_type = cls._normalize_damage_type(payload.get("damage_type"))
        quantity = cls._normalize_quantity(payload.get("quantity"))
        confidence = cls._normalize_confidence(payload.get("confidence"))
        chargeable = cls._normalize_chargeable(payload.get("chargeable"), cleaned_description)
        return {
            "cleaned_description": cleaned_description,
            "item": item,
            "damage_type": damage_type,
            "quantity": quantity,
            "confidence": confidence,
            "chargeable": chargeable,
        }

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip().lower())

    @classmethod
    def _clean_description(cls, value: object) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        if not text:
            return "Damage reported."
        if text[-1] not in ".!?":
            text = f"{text}."
        return text[0].upper() + text[1:240]

    @staticmethod
    def _normalize_quantity(value: object) -> int:
        try:
            quantity = int(value)
        except (TypeError, ValueError):
            quantity = 1
        return min(max(quantity, 1), 50)

    @staticmethod
    def _normalize_confidence(value: object) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            confidence = 0.0
        return round(min(max(confidence, 0.0), 1.0), 2)

    @classmethod
    def _normalize_chargeable(cls, value: object, cleaned_description: str) -> bool:
        if isinstance(value, bool):
            return value
        normalized = cls._normalize_text(cleaned_description)
        return not any(re.search(pattern, normalized) for pattern in cls.NO_CHARGE_PATTERNS)

    @classmethod
    def _normalize_item(cls, value: object) -> str | None:
        text = cls._normalize_text(str(value or ""))
        if not text:
            return None
        text = re.sub(r"\b\d+\b", " ", text)
        text = re.sub(r"\s+", " ", text).strip(" .,;:-")
        return cls._singularize(text) if text else None

    @classmethod
    def _normalize_damage_type(cls, value: object) -> str | None:
        text = cls._normalize_text(str(value or ""))
        if not text:
            return None
        for candidate in cls.DAMAGE_TERMS:
            if candidate in text:
                return candidate
        return text[:80]

    @classmethod
    def _extract_quantity(cls, normalized_text: str) -> int:
        quantity_patterns = [
            r"\b(\d+)\s*x(?!\d)\b",
            r"\bx\s*(\d+)\b",
            r"\bqty\.?\s*(\d+)\b",
            r"\b(\d+)\s+(?:tiles?|chairs?|desks?|blinds?|windows?|doors?|holes?|screens?|panels?|"
            r"walls?|patches?|scratches?|chips?|cracks?|marks?|slats?|drawers?|shelves?|rods?|"
            r"hinges?|locks?|outlets?|covers?|fixtures?|boards?)\b",
        ]
        for pattern in quantity_patterns:
            match = re.search(pattern, normalized_text)
            if match:
                value = int(match.group(1))
                if 1 < value <= 50:
                    return value

        fallback_numbers = [
            int(value)
            for value in re.findall(r"\b(\d+)\b", normalized_text)
            if 1 < int(value) <= 50
        ]
        if len(fallback_numbers) == 1:
            return fallback_numbers[0]
        return 1

    @classmethod
    def _extract_damage_type(cls, normalized_text: str) -> str | None:
        for candidate in cls.DAMAGE_TERMS:
            if re.search(rf"\b{re.escape(candidate)}\b", normalized_text):
                return candidate
        return None

    @classmethod
    def _extract_item(cls, normalized_text: str, damage_type: str | None) -> str | None:
        text = normalized_text
        for pattern in cls.NO_CHARGE_PATTERNS:
            text = re.sub(pattern, " ", text)
        text = re.sub(r"\bqty\.?\s*\d+\b", " ", text)
        text = re.sub(r"\bx\s*\d+\b", " ", text)
        text = re.sub(r"\b\d+\s*x\b", " ", text)

        tokens = re.findall(r"[a-z0-9]+", text)
        if not tokens:
            return None

        if damage_type and damage_type in tokens:
            damage_index = tokens.index(damage_type)
            before = cls._filter_item_tokens(tokens[:damage_index])
            if before:
                return cls._singularize(" ".join(before))
            after = cls._filter_item_tokens(tokens[damage_index + 1 :])
            if after:
                return cls._singularize(" ".join(after))

        filtered = cls._filter_item_tokens(tokens)
        if not filtered:
            return None
        return cls._singularize(" ".join(filtered))

    @classmethod
    def _filter_item_tokens(cls, tokens: list[str]) -> list[str]:
        filtered: list[str] = []
        for token in tokens:
            if token.isdigit():
                continue
            if token in cls.LOCATION_TOKENS:
                break
            if token in cls.STOP_TOKENS:
                continue
            if token in cls.DAMAGE_TERMS:
                break
            filtered.append(token)
        return filtered

    @staticmethod
    def _singularize(text: str) -> str:
        tokens = text.split()
        singular_tokens: list[str] = []
        for token in tokens:
            if token.endswith("ies") and len(token) > 3:
                singular_tokens.append(f"{token[:-3]}y")
            elif token.endswith("ses") and len(token) > 3:
                singular_tokens.append(token[:-2])
            elif token.endswith("s") and len(token) > 3 and not token.endswith("ss"):
                singular_tokens.append(token[:-1])
            else:
                singular_tokens.append(token)
        return " ".join(singular_tokens)
