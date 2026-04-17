import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PricingMatch:
    category: str
    form_section: str
    cleaned_description: str
    estimated_cost: float


class PricingEngine:
    def __init__(self, pricing_file: str):
        self.pricing_file = Path(pricing_file)
        self._items = self._load_sheet()

    def _load_sheet(self) -> list[dict]:
        with self.pricing_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # v2 schema: {"items": [{name, form_section, default_cost, keywords}]}
        if "items" in data:
            return data["items"]

        # backward compatible with original v1 schema.
        items = []
        for row in data.get("categories", []):
            items.append(
                {
                    "name": row["label"],
                    "form_section": row["form_section"],
                    "default_cost": row["default_cost"],
                    "keywords": row.get("keywords", []),
                }
            )
        return items

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", " ", text.lower().strip())

    def match(self, raw_note: str) -> PricingMatch:
        normalized = self._normalize(raw_note)
        words = set(re.findall(r"[a-z0-9]+", normalized))
        best = None
        best_score = -1

        for row in self._items:
            keywords = [self._normalize(k) for k in row.get("keywords", [])]
            score = 0
            for keyword in keywords:
                if not keyword:
                    continue
                if keyword in normalized:
                    score += 3
                    continue
                key_words = set(re.findall(r"[a-z0-9]+", keyword))
                overlap = len(words.intersection(key_words))
                if overlap:
                    score += overlap
            if score > best_score:
                best = row
                best_score = score

        if not best or best_score <= 0:
            return PricingMatch(
                category="General Damage",
                form_section="Other",
                cleaned_description=raw_note.strip().capitalize(),
                estimated_cost=0.0,
            )

        cleaned = f"{best['name']}: {raw_note.strip()}"
        return PricingMatch(
            category=best["name"],
            form_section=best["form_section"],
            cleaned_description=cleaned,
            estimated_cost=float(best["default_cost"]),
        )
