from app.schemas.session import FormDraft
from app.services.checkout_service import CheckoutService


class FormDraftService:
    def __init__(self, checkout_service: CheckoutService):
        self.checkout_service = checkout_service

    def build_draft(self, session_id: int) -> FormDraft:
        session = self.checkout_service.get_session(session_id)
        if not session:
            raise ValueError("Session not found")
        summary = self.checkout_service.summarize_session(session_id)

        yes_no = {
            "has_damage": "Yes" if summary.item_count > 0 else "No",
            "has_photos": "Yes" if any(i.image_path for i in summary.items) else "No",
        }
        damages = [
            {
                "form_section": item.form_section,
                "category": item.category,
                "description": item.cleaned_description,
                "estimated_cost": item.estimated_cost,
                "image_path": item.image_path,
            }
            for item in summary.items
        ]
        resident_fields = {
            "resident_name": session.resident_name,
            "room_number": session.room_number,
            "tech_id": session.tech_id,
            "hall": session.hall,
            "staff_name": session.staff_name,
            "room_side": session.room_side,
        }
        return FormDraft(
            session_id=session_id,
            resident_fields=resident_fields,
            yes_no_flags=yes_no,
            damages=damages,
            total_estimated_cost=summary.total_estimated_cost,
        )
