from app.core.config import settings
from app.schemas.session import FormDraft, FormDraftSection
from app.services.checkout_service import CheckoutService, ServiceError
from app.services.form_mapping import get_damage_sections


class FormDraftService:
    def __init__(self, checkout_service: CheckoutService):
        self.checkout_service = checkout_service

    def build_draft(self, session_id: int) -> FormDraft:
        session = self.checkout_service.get_session(session_id)
        if not session:
            raise ServiceError("Session not found.", status_code=404)
        if not session.resident_name or not session.room_number or not session.tech_id:
            raise ServiceError(
                "This checkout is missing resident details and cannot create a form draft yet.",
                status_code=409,
            )

        review = self.checkout_service.build_review_summary(session_id)
        summary = self.checkout_service.summarize_session(session_id)
        resident_fields = {
            "resident_name": session.resident_name,
            "room_number": session.room_number,
            "tech_id": session.tech_id,
            "hall": session.hall,
            "staff_name": "Nift",
            "room_side": session.room_side,
        }
        sections = []
        sections_by_key = {section.category_key: section for section in review.sections}
        for section_config in get_damage_sections():
            review_section = sections_by_key[section_config["key"]]
            section_items = [item for item in summary.items if item.form_section == review_section.category_key]
            section_images = [
                image.file_path
                for item in section_items
                for image in sorted(item.images, key=lambda image: (not image.is_primary, image.sort_order, image.id))
            ]
            sections.append(
                FormDraftSection(
                    category_key=review_section.category_key,
                    category_name=review_section.category_name,
                    question=review_section.question,
                    guessed_confidence=review_section.guessed_confidence,
                    answer_yes_no="Yes" if review_section.has_damage else "No",
                    description=review_section.description,
                    estimated_cost=review_section.estimated_cost,
                    image_path=section_images[0] if section_images else None,
                    image_paths=section_images,
                )
            )

        return FormDraft(
            session_id=session_id,
            resident_fields=resident_fields,
            room_has_bathroom="Yes" if settings.default_has_bathroom else "No",
            sections=sections,
            total_estimated_cost=review.total_estimated_cost,
        )
