from datetime import datetime

from pydantic import BaseModel


class SessionCreate(BaseModel):
    resident_name: str
    room_number: str
    tech_id: str
    hall: str
    staff_name: str
    room_side: str


class SessionResponse(SessionCreate):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class DamageCreate(BaseModel):
    raw_note: str


class DamageResponse(BaseModel):
    id: int
    session_id: int
    raw_note: str
    cleaned_description: str
    estimated_cost: float
    image_path: str | None
    category: str
    form_section: str

    class Config:
        from_attributes = True


class SessionSummary(BaseModel):
    session_id: int
    resident_name: str
    room_number: str
    hall: str
    total_estimated_cost: float
    item_count: int
    items: list[DamageResponse]


class FormDraft(BaseModel):
    session_id: int
    resident_fields: dict[str, str]
    yes_no_flags: dict[str, str]
    damages: list[dict[str, str | float | None]]
    total_estimated_cost: float
