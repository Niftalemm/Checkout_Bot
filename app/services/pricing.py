import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.services.damage_ai import DamageAIResult
from app.services.form_mapping import get_damage_sections


@dataclass
class PricingSuggestion:
    category_key: str
    category_name: str
    pricing_name: str | None
    cleaned_description: str
    confidence: float
    quantity: int
    unit_cost: float
    total_cost: float
    estimated_cost: float
    chargeable: bool
    item: str | None = None
    damage_type: str | None = None


class PricingEngine:
    NO_CHARGE_PATTERNS = [
        r"\bno charge\b",
        r"\bno cost\b",
        r"\bnot charging\b",
        r"\bdon'?t charge\b",
        r"\bdo not charge\b",
    ]

    def __init__(self, pricing_file: str):
        self.pricing_file = Path(pricing_file)
        self._items = self._load_sheet()
        self._sections = get_damage_sections()

    def _load_sheet(self) -> list[dict]:
        with self.pricing_file.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if "items" in data:
            return data["items"]

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

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", text.lower()))

    @staticmethod
    def _clean_note(raw_note: str) -> str:
        normalized = re.sub(r"\s+", " ", raw_note.strip())
        normalized = re.sub(r"\s+([,.;:!?])", r"\1", normalized)
        if not normalized:
            return "Damage reported."
        if normalized[-1] not in ".!?":
            normalized = f"{normalized}."
        return normalized[0].upper() + normalized[1:]

    @staticmethod
    def _extract_quantity(normalized_text: str) -> int:
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
    def detect_no_charge(cls, text: str) -> bool:
        normalized = cls._normalize(text)
        return any(re.search(pattern, normalized) for pattern in cls.NO_CHARGE_PATTERNS)

    @staticmethod
    def _contextual_section_bonus(section_key: str, text: str, words: set[str]) -> float:
        phrase_groups = {
            "entry_door": [
                "entry door",
                "door closer",
                "emergency exit sign",
                "exit sign",
                "peephole",
                "lock",
                "hinge",
                "door",
            ],
            "entry_surround": [
                "entry surround",
                "outside entry",
                "entry wall",
                "room number",
                "room number plate",
                "door frame",
                "tackable wall",
                "tack-able wall",
            ],
            "bathroom_door": [
                "bathroom door",
                "bathroom lock",
                "bath door",
            ],
            "wall_surfaces": [
                "wall",
                "walls",
                "drywall",
                "sheetrock",
                "bulletin board",
                "graffiti",
                "paint chip",
            ],
            "floor_surfaces": [
                "floor",
                "tile",
                "tiles",
                "ceramic",
                "vinyl tile",
                "threshold",
                "floor drain",
            ],
            "ceiling": [
                "ceiling",
                "ceiling tile",
            ],
            "loft": [
                "loft",
                "bed",
                "mattress",
                "guard rail",
                "platform",
            ],
            "desk_chair": [
                "desk chair",
                "chair",
                "casters",
                "swivel",
                "tilt",
            ],
            "heating_cooling": [
                "radiator",
                "thermostat",
                "vent cover",
                "heater",
                "air conditioner",
                "ac",
                "a/c",
            ],
            "lighting": [
                "light",
                "fixture",
                "light cover",
                "ceiling light",
            ],
            "fire_safety": [
                "smoke detector",
                "sprinkler",
                "sprinkler head",
            ],
            "sink": [
                "sink",
                "faucet",
                "tap",
                "medicine cabinet",
                "mirror",
            ],
            "shower": [
                "shower",
                "shower curtain",
                "curtain rod",
            ],
            "toilet": [
                "toilet",
            ],
            "towel_rack": [
                "towel rack",
                "towel bar",
                "hand dryer",
                "sanitary napkin receptacle",
            ],
        }
        measurement_words = {"inch", "inches", "ft", "foot", "feet", "sq", "square"}
        score = 0.0

        for phrase in phrase_groups.get(section_key, []):
            phrase_tokens = set(re.findall(r"[a-z0-9]+", phrase))
            if " " in phrase and phrase in text:
                score += 3.5
            elif len(phrase_tokens) == 1 and phrase in words:
                score += 2.0

        if section_key == "wall_surfaces":
            if {"wall", "walls", "drywall", "sheetrock"}.intersection(words):
                score += 3.5
            if {"outside", "entry"}.issubset(words) or "room number" in text or "door frame" in text:
                score -= 4.0
        elif section_key == "entry_surround":
            if "outside entry" in text or "room number" in text or "entry wall" in text:
                score += 5.0
            elif "outside" in words and "door" in words:
                score += 3.0
            elif "wall" in words and not {"outside", "entry"}.intersection(words):
                score -= 3.5
        elif section_key == "bathroom_door":
            if "bathroom" in words and "door" in words:
                score += 5.0
            elif "door" in words and "bathroom" not in words:
                score -= 2.0
        elif section_key == "floor_surfaces" and "wall" in words:
            score -= 2.0
        elif section_key == "entry_door" and "wall" in words and "door" not in words:
            score -= 2.0
            if "bathroom" in words:
                score -= 2.0
        elif section_key == "ceiling" and measurement_words.intersection(words):
            score += 0.5
        elif section_key == "lighting" and "outlet" in words:
            score -= 1.0
        elif section_key == "electrical" and {"light", "lighting", "fixture"}.intersection(words):
            score += 1.0

        return score

    def _direct_section_from_row(self, row: dict) -> dict | None:
        row_text = self._normalize(
            " ".join([row["name"], row.get("form_section", ""), " ".join(row.get("keywords", []))])
        )
        form_section = row.get("form_section", "").lower()
        if "bathroom door" in row_text:
            return next(section for section in self._sections if section["key"] == "bathroom_door")
        if "exit sign" in row_text:
            return next(section for section in self._sections if section["key"] == "entry_door")
        if "desk chair" in row_text or "rolling chair" in row_text:
            return next(section for section in self._sections if section["key"] == "desk_chair")
        if "dresser" in row_text:
            return next(section for section in self._sections if section["key"] == "dresser")
        if "desk" in row_text or "study table" in row_text:
            return next(section for section in self._sections if section["key"] == "desk")
        if "loft" in row_text or "mattress" in row_text:
            return next(section for section in self._sections if section["key"] == "loft")
        if "blind" in row_text or "shade" in row_text:
            return next(section for section in self._sections if section["key"] == "blinds")
        if "window" in row_text or "screen" in row_text:
            return next(section for section in self._sections if section["key"] == "window")
        if form_section == "electrical" and "light" in row_text:
            return next(section for section in self._sections if section["key"] == "lighting")
        if form_section == "safety":
            return next(section for section in self._sections if section["key"] == "fire_safety")
        if form_section == "bathroom":
            if any(term in row_text for term in ["shower", "curtain rod"]):
                return next(section for section in self._sections if section["key"] == "shower")
            if "toilet" in row_text:
                return next(section for section in self._sections if section["key"] == "toilet")
            if any(term in row_text for term in ["towel", "hand dryer", "napkin receptacle"]):
                return next(section for section in self._sections if section["key"] == "towel_rack")
            if any(term in row_text for term in ["sink", "faucet", "mirror", "medicine cabinet"]):
                return next(section for section in self._sections if section["key"] == "sink")
        if row.get("form_section", "").lower() == "paint":
            if "room number" in row_text or "entry" in row_text:
                return next(section for section in self._sections if section["key"] == "entry_surround")
            if "door" in row_text:
                return next(section for section in self._sections if section["key"] == "entry_door")
            if "ceiling" in row_text:
                return next(section for section in self._sections if section["key"] == "ceiling")
            if "wall" in row_text or "room" in row_text:
                return next(section for section in self._sections if section["key"] == "wall_surfaces")
        return None

    def _infer_section(self, row: dict) -> dict:
        direct_section = self._direct_section_from_row(row)
        if direct_section is not None:
            return direct_section

        normalized_form_section = self._normalize(row.get("form_section", ""))
        mapped_section_keys = self._mapped_sections_for_form_section(normalized_form_section)
        if len(mapped_section_keys) == 1:
            return next(section for section in self._sections if section["key"] == mapped_section_keys[0])

        row_text = self._normalize(
            " ".join(
                [
                    row["name"],
                    row.get("form_section", ""),
                    " ".join(row.get("keywords", [])),
                ]
            )
        )
        row_words = self._tokenize(row_text)
        candidate_sections = (
            [section for section in self._sections if section["key"] in mapped_section_keys]
            if mapped_section_keys
            else self._sections
        )
        best_section = candidate_sections[0]
        best_score = -1
        for section in candidate_sections:
            score = self._score_section_aliases(section, row_text, row_words)
            score += len(self._tokenize(section["name"]).intersection(row_words))
            score += self._contextual_section_bonus(section["key"], row_text, row_words)
            if normalized_form_section and normalized_form_section in self._normalize(section["name"]):
                score += 2
            if score > best_score:
                best_section = section
                best_score = score
        return best_section

    @staticmethod
    def _mapped_sections_for_form_section(normalized_form_section: str) -> list[str]:
        if not normalized_form_section:
            return []

        mapping = {
            "doors": ["entry_door", "bathroom_door"],
            "security": ["entry_door", "bathroom_door"],
            "closet": ["closet"],
            "furniture": ["loft", "dresser", "desk", "desk_chair"],
            "windows": ["window", "blinds"],
            "hvac": ["heating_cooling"],
            "electrical": ["electrical", "lighting"],
            "network": ["electrical"],
            "bathroom": ["sink", "shower", "toilet", "towel_rack"],
            "safety": ["fire_safety"],
            "walls": ["wall_surfaces", "entry_surround"],
            "flooring": ["floor_surfaces"],
            "ceiling": ["ceiling"],
            "paint": ["entry_surround", "entry_door", "bathroom_door", "wall_surfaces", "ceiling"],
            "cleaning": ["wall_surfaces", "floor_surfaces"],
            "labor": ["wall_surfaces"],
            "admin": ["entry_surround"],
        }
        return mapping.get(normalized_form_section, [])

    @staticmethod
    def _score_section_aliases(section: dict, text: str, words: set[str]) -> float:
        score = 0.0
        for alias in section.get("aliases", []):
            normalized_alias = re.sub(r"\s+", " ", alias.lower().strip())
            alias_tokens = set(re.findall(r"[a-z0-9]+", normalized_alias))
            if not normalized_alias or not alias_tokens:
                continue
            if len(alias_tokens) > 1 and normalized_alias in text:
                score += 2.5
                continue
            if len(alias_tokens) == 1 and normalized_alias in words:
                score += 0.75
                continue
            overlap = len(words.intersection(alias_tokens))
            if overlap and len(alias_tokens) > 1:
                score += overlap * 0.5
        return score

    @staticmethod
    def _score_terms(haystack: str, words: set[str], phrases: list[str], phrase_weight: float) -> tuple[float, int]:
        score = 0.0
        strong_matches = 0
        for phrase in phrases:
            normalized_phrase = re.sub(r"\s+", " ", phrase.lower().strip())
            phrase_tokens = set(re.findall(r"[a-z0-9]+", normalized_phrase))
            if not normalized_phrase or not phrase_tokens:
                continue

            if normalized_phrase in haystack:
                score += phrase_weight
                strong_matches += 1
                continue

            overlap = len(words.intersection(phrase_tokens))
            if not overlap:
                continue

            if len(phrase_tokens) == 1:
                score += 1.5
                strong_matches += 1
                continue

            score += overlap
            if overlap >= min(2, len(phrase_tokens)):
                strong_matches += 1

        return score, strong_matches

    def _build_search_texts(
        self, raw_note: str, image_name_hint: str | None, analysis: DamageAIResult | None
    ) -> tuple[str, str]:
        normalized_note = self._normalize(raw_note)
        normalized_image = self._normalize(Path(image_name_hint or "").stem.replace("_", " "))
        structured_item = self._normalize((analysis.item if analysis else "") or "")
        primary_text = structured_item or normalized_note
        combined_text = self._normalize(" ".join([primary_text, normalized_note, normalized_image]).strip())
        return primary_text, combined_text

    def _score_row(
        self,
        row: dict,
        primary_text: str,
        combined_text: str,
        analysis: DamageAIResult | None,
    ) -> dict | None:
        primary_words = self._tokenize(primary_text)
        combined_words = self._tokenize(combined_text)
        row_name = self._normalize(row["name"])
        keywords = [self._normalize(keyword) for keyword in row.get("keywords", [])]
        row_terms = [row_name, *keywords]
        row_words = self._tokenize(" ".join(row_terms))
        section = self._infer_section(row)

        item_score, item_matches = self._score_terms(primary_text, primary_words, row_terms, phrase_weight=10.0)
        note_score, note_matches = self._score_terms(combined_text, combined_words, row_terms, phrase_weight=5.0)
        section_score = self._score_section_aliases(section, combined_text, combined_words)
        section_score += self._contextual_section_bonus(section["key"], combined_text, combined_words)
        token_overlap = len(primary_words.intersection(row_words))
        if not primary_text:
            token_overlap = len(combined_words.intersection(row_words))

        damage_bonus = 0.0
        if analysis and analysis.damage_type:
            damage_tokens = self._tokenize(analysis.damage_type)
            if damage_tokens.intersection(row_words):
                damage_bonus += 1.5
            elif any(token in combined_words for token in damage_tokens):
                damage_bonus += 0.5

        score = item_score + note_score + section_score + (token_overlap * 1.5) + damage_bonus
        strong_matches = item_matches + note_matches
        if strong_matches == 0 and token_overlap == 0:
            return None

        return {
            "score": score,
            "strong_matches": strong_matches,
            "section": section,
            "row": row,
        }

    def suggest(
        self,
        raw_note: str,
        image_name_hint: str | None = None,
        analysis: DamageAIResult | None = None,
        limit: int = 3,
    ) -> list[PricingSuggestion]:
        primary_text, combined_text = self._build_search_texts(raw_note, image_name_hint, analysis)
        cleaned_note = (analysis.cleaned_description if analysis else "") or self._clean_note(raw_note)
        quantity = (analysis.quantity if analysis else 0) or self._extract_quantity(combined_text)
        chargeable = analysis.chargeable if analysis is not None else not self.detect_no_charge(raw_note)

        candidates: list[dict] = []
        for row in self._items:
            candidate = self._score_row(row, primary_text, combined_text, analysis)
            if candidate:
                candidates.append(candidate)

        if not candidates:
            fallback = self._sections[0]
            return [
                PricingSuggestion(
                    category_key=fallback["key"],
                    category_name=fallback["name"],
                    pricing_name=None,
                    cleaned_description=cleaned_note,
                    confidence=0.0,
                    quantity=quantity,
                    unit_cost=0.0,
                    total_cost=0.0,
                    estimated_cost=0.0,
                    chargeable=chargeable,
                    item=analysis.item if analysis else None,
                    damage_type=analysis.damage_type if analysis else None,
                )
            ]

        ranked = sorted(
            candidates,
            key=lambda item: (item["strong_matches"], item["score"]),
            reverse=True,
        )
        max_score = ranked[0]["score"] or 1.0
        suggestions: list[PricingSuggestion] = []
        for candidate in ranked[:limit]:
            unit_cost = round(float(candidate["row"].get("default_cost", 0.0)), 2)
            total_cost = round(unit_cost * quantity, 2) if chargeable else 0.0
            suggestions.append(
                PricingSuggestion(
                    category_key=candidate["section"]["key"],
                    category_name=candidate["section"]["name"],
                    pricing_name=candidate["row"]["name"],
                    cleaned_description=cleaned_note,
                    confidence=round(candidate["score"] / max_score, 2),
                    quantity=quantity,
                    unit_cost=unit_cost,
                    total_cost=total_cost,
                    estimated_cost=total_cost,
                    chargeable=chargeable,
                    item=analysis.item if analysis else None,
                    damage_type=analysis.damage_type if analysis else None,
                )
            )
        return suggestions

    def choose_category(
        self,
        raw_note: str,
        category_key: str,
        image_name_hint: str | None = None,
        analysis: DamageAIResult | None = None,
    ) -> PricingSuggestion:
        suggestions = self.suggest(
            raw_note,
            image_name_hint=image_name_hint,
            analysis=analysis,
            limit=10,
        )
        for suggestion in suggestions:
            if suggestion.category_key == category_key:
                return suggestion

        cleaned_note = (analysis.cleaned_description if analysis else "") or self._clean_note(raw_note)
        primary_text, combined_text = self._build_search_texts(raw_note, image_name_hint, analysis)
        section_candidates: list[dict] = []
        for row in self._items:
            if self._infer_section(row)["key"] != category_key:
                continue
            candidate = self._score_row(row, primary_text, combined_text, analysis)
            if candidate:
                section_candidates.append(candidate)

        if section_candidates:
            best_candidate = sorted(
                section_candidates,
                key=lambda item: (item["strong_matches"], item["score"]),
                reverse=True,
            )[0]
            quantity = (analysis.quantity if analysis else 0) or self._extract_quantity(self._normalize(raw_note))
            chargeable = analysis.chargeable if analysis is not None else not self.detect_no_charge(raw_note)
            unit_cost = round(float(best_candidate["row"].get("default_cost", 0.0)), 2)
            total_cost = round(unit_cost * quantity, 2) if chargeable else 0.0
            return PricingSuggestion(
                category_key=best_candidate["section"]["key"],
                category_name=best_candidate["section"]["name"],
                pricing_name=best_candidate["row"]["name"],
                cleaned_description=cleaned_note,
                confidence=round(best_candidate["score"] / (best_candidate["score"] or 1.0), 2),
                quantity=quantity,
                unit_cost=unit_cost,
                total_cost=total_cost,
                estimated_cost=total_cost,
                chargeable=chargeable,
                item=analysis.item if analysis else None,
                damage_type=analysis.damage_type if analysis else None,
            )

        section = next(section for section in self._sections if section["key"] == category_key)
        quantity = (analysis.quantity if analysis else 0) or self._extract_quantity(self._normalize(raw_note))
        chargeable = analysis.chargeable if analysis is not None else not self.detect_no_charge(raw_note)
        return PricingSuggestion(
            category_key=section["key"],
            category_name=section["name"],
            pricing_name=None,
            cleaned_description=cleaned_note,
            confidence=0.0,
            quantity=quantity,
            unit_cost=0.0,
            total_cost=0.0,
            estimated_cost=0.0,
            chargeable=chargeable,
            item=analysis.item if analysis else None,
            damage_type=analysis.damage_type if analysis else None,
        )

    def build_ai_pricing_context(
        self,
        raw_note: str,
        image_name_hint: str | None = None,
        analysis: DamageAIResult | None = None,
        limit: int = 4,
    ) -> list[dict]:
        suggestions = self.suggest(
            raw_note,
            image_name_hint=image_name_hint,
            analysis=analysis,
            limit=limit,
        )
        context: list[dict] = []
        for suggestion in suggestions:
            context.append(
                {
                    "category_key": suggestion.category_key,
                    "category_name": suggestion.category_name,
                    "pricing_name": suggestion.pricing_name,
                    "quantity": suggestion.quantity,
                    "unit_cost": round(suggestion.unit_cost, 2),
                    "estimated_cost": round(suggestion.total_cost, 2),
                    "chargeable": suggestion.chargeable,
                    "confidence": suggestion.confidence,
                }
            )
        return context
