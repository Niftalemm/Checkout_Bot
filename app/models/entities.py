from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class CheckoutSession(Base):
    __tablename__ = "checkout_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    resident_name: Mapped[str] = mapped_column(String(120))
    room_number: Mapped[str] = mapped_column(String(20))
    tech_id: Mapped[str] = mapped_column(String(40))
    hall: Mapped[str] = mapped_column(String(80))
    staff_name: Mapped[str] = mapped_column(String(120))
    room_side: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(40), default="active", index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    channel_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(40), default="api")
    form_fill_status: Mapped[str] = mapped_column(String(40), default="not_requested")
    form_fill_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    form_fill_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_saved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    damage_items: Mapped[list["DamageItem"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    pending_damage_captures: Mapped[list["PendingDamageCapture"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class DamageItem(Base):
    __tablename__ = "damage_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("checkout_sessions.id"), index=True)
    raw_note: Mapped[str] = mapped_column(Text)
    cleaned_description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    total_cost: Mapped[float] = mapped_column(Float, default=0.0)
    chargeable: Mapped[bool] = mapped_column(Boolean, default=True)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0)
    image_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    category: Mapped[str] = mapped_column(String(120))
    form_section: Mapped[str] = mapped_column(String(120))
    confirmation_status: Mapped[str] = mapped_column(String(40), default="confirmed")
    guessed_category: Mapped[str | None] = mapped_column(String(120), nullable=True)
    guessed_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    pricing_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    ai_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ai_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    session: Mapped[CheckoutSession] = relationship(back_populates="damage_items")
    images: Mapped[list["DamageImage"]] = relationship(
        back_populates="damage_item", cascade="all, delete-orphan"
    )


class PendingDamageCapture(Base):
    __tablename__ = "pending_damage_captures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("checkout_sessions.id"), index=True)
    raw_note: Mapped[str] = mapped_column(Text)
    cleaned_description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    total_cost: Mapped[float] = mapped_column(Float, default=0.0)
    chargeable: Mapped[bool] = mapped_column(Boolean, default=True)
    parsed_item: Mapped[str | None] = mapped_column(String(160), nullable=True)
    parsed_damage_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    parsed_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    image_temp_path: Mapped[str] = mapped_column(String(255), default="")
    suggested_category: Mapped[str] = mapped_column(String(120))
    suggested_section: Mapped[str] = mapped_column(String(120))
    suggested_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    suggested_cost: Mapped[float] = mapped_column(Float, default=0.0)
    pricing_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    ai_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ai_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    suggestion_options_json: Mapped[str] = mapped_column(Text, default="[]")
    image_name_hint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    confirmed_category: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    session: Mapped[CheckoutSession] = relationship(back_populates="pending_damage_captures")
    images: Mapped[list["PendingDamageImage"]] = relationship(
        back_populates="pending_capture", cascade="all, delete-orphan"
    )


class PendingDamageImage(Base):
    __tablename__ = "pending_damage_images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    pending_capture_id: Mapped[int] = mapped_column(ForeignKey("pending_damage_captures.id"), index=True)
    file_path: Mapped[str] = mapped_column(String(255))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    pending_capture: Mapped[PendingDamageCapture] = relationship(back_populates="images")


class DamageImage(Base):
    __tablename__ = "damage_images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    damage_item_id: Mapped[int] = mapped_column(ForeignKey("damage_items.id"), index=True)
    file_path: Mapped[str] = mapped_column(String(255))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    damage_item: Mapped[DamageItem] = relationship(back_populates="images")
