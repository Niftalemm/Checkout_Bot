from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    damage_items: Mapped[list["DamageItem"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class DamageItem(Base):
    __tablename__ = "damage_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("checkout_sessions.id"), index=True)
    raw_note: Mapped[str] = mapped_column(Text)
    cleaned_description: Mapped[str] = mapped_column(Text)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0)
    image_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    category: Mapped[str] = mapped_column(String(120))
    form_section: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    session: Mapped[CheckoutSession] = relationship(back_populates="damage_items")
