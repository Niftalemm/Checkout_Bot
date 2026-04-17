from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import get_db
from app.integrations.playwright.form_filler import MicrosoftFormFiller
from app.schemas.session import FormDraft, SessionCreate, SessionResponse, SessionSummary
from app.services.checkout_service import CheckoutService
from app.services.form_draft import FormDraftService
from app.services.pricing import PricingEngine
from app.storage.image_store import LocalImageStore

router = APIRouter(prefix="/api", tags=["checkout"])


def build_service(db: Session) -> CheckoutService:
    pricing = PricingEngine(settings.pricing_sheet_path)
    store = LocalImageStore(settings.uploads_dir)
    return CheckoutService(db=db, pricing=pricing, image_store=store)


@router.post("/sessions", response_model=SessionResponse)
def create_session(payload: SessionCreate, db: Session = Depends(get_db)):
    service = build_service(db)
    return service.create_session(payload)


@router.post("/sessions/{session_id}/damages")
def add_damage(
    session_id: int,
    raw_note: str = Form(...),
    image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    service = build_service(db)
    try:
        damage = service.add_damage(session_id=session_id, raw_note=raw_note, image_file=image)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "message": "Damage added",
        "cleaned_description": damage.cleaned_description,
        "estimated_cost": damage.estimated_cost,
        "category": damage.category,
        "form_section": damage.form_section,
    }


@router.get("/sessions/{session_id}/summary", response_model=SessionSummary)
def session_summary(session_id: int, db: Session = Depends(get_db)):
    service = build_service(db)
    try:
        return service.summarize_session(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/form-draft", response_model=FormDraft)
def prepare_form_draft(session_id: int, db: Session = Depends(get_db)):
    service = build_service(db)
    draft_service = FormDraftService(service)
    try:
        return draft_service.build_draft(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/form-draft/fill")
def fill_form_draft(session_id: int, db: Session = Depends(get_db)):
    service = build_service(db)
    draft_service = FormDraftService(service)
    try:
        draft = draft_service.build_draft(session_id)
        filler = MicrosoftFormFiller(settings.microsoft_form_url)
        message = filler.fill_draft(draft)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"message": message}
