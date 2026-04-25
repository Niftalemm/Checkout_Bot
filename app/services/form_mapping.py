import json
from functools import lru_cache
from pathlib import Path

from app.core.config import settings


@lru_cache(maxsize=1)
def load_form_mapping() -> dict:
    path = Path(settings.form_mapping_path)
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def get_checkout_fields() -> dict[str, str]:
    return load_form_mapping()["checkout_fields"]


def get_hall_options() -> dict[str, str]:
    return load_form_mapping()["hall_options"]


def get_room_side_options() -> dict[str, str]:
    return load_form_mapping()["room_side_options"]


def get_damage_sections() -> list[dict]:
    return load_form_mapping()["damage_sections"]


@lru_cache(maxsize=1)
def get_damage_section_map() -> dict[str, dict]:
    return {section["key"]: section for section in get_damage_sections()}


def get_damage_section(key: str) -> dict:
    section = get_damage_section_map().get(key)
    if not section:
        raise KeyError(f"Unknown damage section: {key}")
    return section


def get_damage_category_names() -> list[str]:
    return [section["name"] for section in get_damage_sections()]
