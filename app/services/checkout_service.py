from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import CheckoutSession, DamageItem
from app.schemas.session import SessionCreate, SessionSummary
from app.services.pricing import PricingEngine
from app.storage.image_store import LocalImageStore


class CheckoutService:
    def __init__(self, db: Session, pricing: PricingEngine, image_store: LocalImageStore):
        self.db = db
        self.pricing = pricing
        self.image_store = image_store

    def create_session(self, payload: SessionCreate) -> CheckoutSession:
        session = CheckoutSession(**payload.model_dump())
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        return session

    def get_session(self, session_id: int) -> CheckoutSession | None:
        return self.db.get(CheckoutSession, session_id)

    def add_damage(self, session_id: int, raw_note: str, image_file=None) -> DamageItem:
        session = self.get_session(session_id)
        if not session:
            raise ValueError("Session not found")

        match = self.pricing.match(raw_note)
        image_path = None
        if image_file is not None:
            image_path = self.image_store.save_damage_image(image_file, session_id)

        damage = DamageItem(
            session_id=session_id,
            raw_note=raw_note,
            cleaned_description=match.cleaned_description,
            estimated_cost=match.estimated_cost,
            image_path=image_path,
            category=match.category,
            form_section=match.form_section,
        )
        self.db.add(damage)
        self.db.commit()
        self.db.refresh(damage)
        return damage

    def summarize_session(self, session_id: int) -> SessionSummary:
        session = self.get_session(session_id)
        if not session:
            raise ValueError("Session not found")

        items = (
            self.db.execute(select(DamageItem).where(DamageItem.session_id == session_id))
            .scalars()
            .all()
        )
        total = sum(item.estimated_cost for item in items)

        return SessionSummary(
            session_id=session.id,
            resident_name=session.resident_name,
            room_number=session.room_number,
            hall=session.hall,
            total_estimated_cost=total,
            item_count=len(items),
            items=items,
        )
