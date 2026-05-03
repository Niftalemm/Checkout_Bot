from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


VALID_HALLS = {"A", "B", "C", "D"}
VALID_ROOM_SIDES = {"left", "right", "single"}


class SessionDetailsBase(BaseModel):
    resident_name: str = Field(..., min_length=1, max_length=120)
    room_number: str = Field(..., min_length=1, max_length=20)
    tech_id: str = Field(..., min_length=1, max_length=40)
    hall: str
    staff_name: str = "Nift"
    room_side: str

    @field_validator("resident_name", "room_number", "tech_id", mode="before")
    @classmethod
    def _strip_required_text(cls, value: str) -> str:
        if value is None:
            raise ValueError("This field is required.")
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("This field is required.")
        return normalized

    @field_validator("hall", mode="before")
    @classmethod
    def _normalize_hall(cls, value: str) -> str:
        normalized = str(value or "").strip().upper()
        if normalized not in VALID_HALLS:
            raise ValueError("Hall must be one of: A, B, C, D.")
        return normalized

    @field_validator("room_side", mode="before")
    @classmethod
    def _normalize_room_side(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in VALID_ROOM_SIDES:
            raise ValueError("Side must be one of: left, right, single.")
        return normalized

    @field_validator("staff_name", mode="before")
    @classmethod
    def _default_staff_name(cls, value: str | None) -> str:
        normalized = str(value or "").strip()
        return normalized or "Nift"


class SessionCreate(SessionDetailsBase):
    pass


class SessionDetailsUpdate(SessionDetailsBase):
    pass


class DiscordSessionStart(BaseModel):
    started_by: int
    channel_id: int
    source: Literal["discord"] = "discord"


class SessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    resident_name: str
    room_number: str
    tech_id: str
    hall: str
    staff_name: str
    room_side: str
    status: str
    completed_at: datetime | None
    started_by: str | None
    channel_id: str | None
    source: str
    form_fill_status: str
    form_fill_error: str | None
    form_fill_result: str | None
    draft_saved: bool
    scheduled_checkout_id: str | None
    created_at: datetime


class DamageSuggestion(BaseModel):
    category_key: str
    category_name: str
    pricing_name: str | None
    confidence: float
    quantity: int = 1
    unit_cost: float = 0.0
    total_cost: float = 0.0
    estimated_cost: float
    chargeable: bool = True


class PendingDamageCaptureResponse(BaseModel):
    status: str = "pending"
    awaiting_description: bool = False
    capture_id: int
    original_description: str | None = None
    cleaned_description: str | None = None
    quantity: int | None = None
    unit_cost: float | None = None
    total_cost: float | None = None
    chargeable: bool | None = None
    guessed_category_key: str | None = None
    guessed_category_name: str | None = None
    guessed_confidence: float | None = None
    estimated_cost: float | None = None
    suggestions: list[DamageSuggestion] = Field(default_factory=list)
    requires_explicit_choice: bool = False
    prompt: str = ""
    image_count: int = 0


class DamageConfirmRequest(BaseModel):
    selection_index: int | None = None
    category_key: str | None = None


class PendingCaptureState(BaseModel):
    status: str = "pending"
    awaiting_description: bool = False
    capture_id: int
    original_description: str | None = None
    cleaned_description: str | None = None
    quantity: int | None = None
    unit_cost: float | None = None
    total_cost: float | None = None
    chargeable: bool | None = None
    guessed_category_key: str | None = None
    guessed_category_name: str | None = None
    guessed_confidence: float | None = None
    suggestions: list[DamageSuggestion] = Field(default_factory=list)
    image_count: int = 0


class DamageImageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    file_path: str
    sort_order: int
    is_primary: bool


class DamageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: int
    raw_note: str
    quantity: int
    unit_cost: float
    total_cost: float
    chargeable: bool
    cleaned_description: str
    estimated_cost: float
    image_path: str | None
    category: str
    form_section: str
    confirmation_status: str
    guessed_category: str | None
    guessed_confidence: float | None
    pricing_name: str | None
    ai_provider: str | None = None
    ai_model: str | None = None
    images: list[DamageImageResponse] = Field(default_factory=list)


class SessionSummary(BaseModel):
    session_id: int
    resident_name: str
    room_number: str
    tech_id: str
    hall: str
    staff_name: str
    room_side: str
    status: str
    total_estimated_cost: float
    item_count: int
    items: list[DamageResponse]


class ReviewSection(BaseModel):
    category_key: str
    category_name: str
    question: str
    guessed_confidence: float | None
    has_damage: bool
    description: str
    estimated_cost: float
    has_image: bool
    damage_count: int


class ReviewSummary(BaseModel):
    session_id: int
    resident_name: str
    room_number: str
    tech_id: str
    hall: str
    staff_name: str
    room_side: str
    has_damages: bool
    total_estimated_cost: float
    item_count: int
    sections: list[ReviewSection]


class FormDraftSection(BaseModel):
    category_key: str
    category_name: str
    question: str
    guessed_confidence: float | None
    answer_yes_no: str
    description: str
    estimated_cost: float
    image_path: str | None
    image_paths: list[str] = Field(default_factory=list)


class FormDraft(BaseModel):
    session_id: int
    resident_fields: dict[str, str]
    room_has_bathroom: str
    sections: list[FormDraftSection]
    total_estimated_cost: float


class CompleteSessionResponse(BaseModel):
    session_id: int
    status: str
    form_fill_status: str
    form_fill_error: str | None
    draft_saved: bool
    total_estimated_cost: float
    item_count: int
    message: str
    form_fill_result: dict[str, Any] | None = None


class ScheduledCheckoutBase(BaseModel):
    resident_name: str = Field(..., min_length=1, max_length=120)
    room_number: str = Field(..., min_length=1, max_length=20)
    tech_id: str = Field(..., min_length=1, max_length=40)
    hall: str
    room_side: str
    checkout_date: str = Field(..., min_length=10, max_length=10)
    checkout_time: str = Field(..., min_length=4, max_length=5)

    @field_validator("resident_name", "room_number", "tech_id", "checkout_date", "checkout_time", mode="before")
    @classmethod
    def _strip_schedule_text(cls, value: str) -> str:
        if value is None:
            raise ValueError("This field is required.")
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("This field is required.")
        return normalized

    @field_validator("hall", mode="before")
    @classmethod
    def _schedule_hall(cls, value: str) -> str:
        return SessionDetailsBase._normalize_hall(value)

    @field_validator("room_side", mode="before")
    @classmethod
    def _schedule_room_side(cls, value: str) -> str:
        return SessionDetailsBase._normalize_room_side(value)


class ScheduledCheckoutCreateRequest(ScheduledCheckoutBase):
    creator_discord_user_id: str = Field(..., min_length=1, max_length=80)
    creator_display_name: str = Field(..., min_length=1, max_length=120)
    discord_channel_id: str = Field(..., min_length=1, max_length=80)


class ScheduledCheckoutUpdateRequest(BaseModel):
    resident_name: str | None = Field(None, min_length=1, max_length=120)
    room_number: str | None = Field(None, min_length=1, max_length=20)
    tech_id: str | None = Field(None, min_length=1, max_length=40)
    hall: str | None = None
    room_side: str | None = None
    checkout_date: str | None = Field(None, min_length=10, max_length=10)
    checkout_time: str | None = Field(None, min_length=4, max_length=5)
    creator_discord_user_id: str = Field(..., min_length=1, max_length=80)

    @field_validator("resident_name", "room_number", "tech_id", "checkout_date", "checkout_time", mode="before")
    @classmethod
    def _strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("This field cannot be empty.")
        return normalized

    @field_validator("hall", mode="before")
    @classmethod
    def _optional_hall(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return SessionDetailsBase._normalize_hall(value)

    @field_validator("room_side", mode="before")
    @classmethod
    def _optional_room_side(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return SessionDetailsBase._normalize_room_side(value)


class ScheduledCheckoutStartRequest(BaseModel):
    creator_discord_user_id: str = Field(..., min_length=1, max_length=80)
    creator_display_name: str = Field(..., min_length=1, max_length=120)
    discord_channel_id: str = Field(..., min_length=1, max_length=80)


class ScheduledCheckoutCancelRequest(BaseModel):
    creator_discord_user_id: str = Field(..., min_length=1, max_length=80)


class ScheduledCheckoutResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    resident_name: str
    tech_id: str
    room_number: str
    hall: str
    room_side: str
    checkout_time: datetime
    timezone: str
    creator_discord_user_id: str
    creator_display_name: str
    discord_channel_id: str
    status: str
    reminder_30_sent: bool
    reminder_10_sent: bool
    reminder_at_time_sent: bool
    ready_to_start_notified: bool
    started_session_id: int | None
    created_at: datetime
    updated_at: datetime


class ScheduledCheckoutStartResponse(BaseModel):
    status: str
    message: str
    schedule: ScheduledCheckoutResponse
    session: SessionResponse | None = None


class DamageItemUpdateDescriptionRequest(BaseModel):
    raw_note: str = Field(..., min_length=1)


class DamageItemUpdateCategoryRequest(BaseModel):
    category_key: str = Field(..., min_length=1)
