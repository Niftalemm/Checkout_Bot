from pathlib import Path
from time import monotonic, sleep

from playwright.sync_api import Error, Page, TimeoutError, sync_playwright

from app.core.config import settings
from app.schemas.session import FormDraft
from app.services.form_mapping import (
    get_checkout_fields,
    get_damage_section,
    get_hall_options,
    get_room_side_options,
)
from app.services.checkout_service import FORM_FILL_PARTIAL_FAILURE, FORM_FILL_SUCCESS


class MicrosoftFormFiller:
    def __init__(self, form_url: str):
        self.form_url = form_url
        self.checkout_fields = get_checkout_fields()
        self.hall_options = get_hall_options()
        self.room_side_options = get_room_side_options()

    def fill_draft(self, draft: FormDraft) -> dict:
        if not self.form_url:
            raise ValueError("Microsoft Form URL is not configured.")

        storage_state_path = Path(settings.playwright_storage_state_path)
        if not storage_state_path.exists():
            raise ValueError(
                "Playwright storage state was not found. Authenticate first and save a Microsoft session."
            )

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=settings.playwright_headless)
            context = browser.new_context(storage_state=str(storage_state_path))
            page = context.new_page()
            page.goto(self.form_url, wait_until="domcontentloaded", timeout=120000)

            self._fill_text(page, [self.checkout_fields["resident_name"]], draft.resident_fields["resident_name"])
            self._fill_text(page, [self.checkout_fields["room_number"]], draft.resident_fields["room_number"])
            self._fill_text(page, [self.checkout_fields["tech_id"]], draft.resident_fields["tech_id"])
            self._select_choice(
                page,
                self.checkout_fields["hall"],
                self.hall_options[draft.resident_fields["hall"]],
            )
            self._fill_text(page, [self.checkout_fields["staff_name"]], "Nift")
            self._select_choice(
                page,
                self.checkout_fields["room_side"],
                self.room_side_options[draft.resident_fields["room_side"]],
            )
            self._select_choice(
                page,
                self.checkout_fields["has_damages"],
                "Yes" if any(section.answer_yes_no == "Yes" for section in draft.sections) else "No",
            )

            results = []
            has_partial_failure = False
            for section in draft.sections:
                result = {
                    "category_name": section.category_name,
                    "guessed_category_confidence": section.guessed_confidence,
                    "confirmed_category": section.category_name,
                    "answered_yes_no": section.answer_yes_no,
                    "conditional_fields_appeared": False,
                    "description_filled": False,
                    "cost_filled": False,
                    "image_upload_succeeded": False,
                    "skipped": section.answer_yes_no == "No",
                    "partial_failure": False,
                }
                section_mapping = get_damage_section(section.category_key)
                self._select_choice(page, section.question, section.answer_yes_no)

                if section.answer_yes_no == "No":
                    results.append(result)
                    continue

                conditional_appeared = self._wait_for_any_input(
                    page,
                    section_mapping["description_labels"]
                    + section_mapping["cost_labels"]
                    + section_mapping["image_labels"],
                )
                result["conditional_fields_appeared"] = conditional_appeared

                if self._fill_text(page, section_mapping["description_labels"], section.description):
                    result["description_filled"] = True
                if self._fill_text(
                    page, section_mapping["cost_labels"], f"{section.estimated_cost:.2f}"
                ):
                    result["cost_filled"] = True
                image_inputs = section.image_paths or ([section.image_path] if section.image_path else [])
                if image_inputs and self._upload(page, section_mapping["image_labels"], image_inputs):
                    result["image_upload_succeeded"] = True

                if not all(
                    [
                        result["conditional_fields_appeared"],
                        result["description_filled"],
                        result["cost_filled"],
                        result["image_upload_succeeded"],
                    ]
                ):
                    result["partial_failure"] = True
                    has_partial_failure = True

                results.append(result)

            self._select_choice(page, self.checkout_fields["has_bathroom"], draft.room_has_bathroom)
            context.storage_state(path=str(storage_state_path))

            message = "Form opened and filled. Review it in the browser and submit manually."
            if has_partial_failure:
                message = (
                    "Form opened for manual review, but some conditional damage fields need attention before submit."
                )

            if not settings.playwright_headless:
                self._hold_browser_for_manual_review(page)

            browser.close()
            return {
                "status": FORM_FILL_PARTIAL_FAILURE if has_partial_failure else FORM_FILL_SUCCESS,
                "message": message,
                "sections": results,
            }

    @staticmethod
    def _escape_label(label: str) -> str:
        return label.replace("'", "\\'")

    def _fill_text(self, page: Page, labels: list[str], value: str) -> bool:
        for label in labels:
            escaped = self._escape_label(label)
            locator = page.locator(
                f"input[aria-label*='{escaped}'], textarea[aria-label*='{escaped}']"
            ).first
            if locator.count():
                locator.wait_for(state="visible", timeout=5000)
                locator.fill(value)
                return True
        return False

    def _select_choice(self, page: Page, question_text: str, choice_text: str) -> bool:
        container = self._get_question_container(page, question_text)
        choice_locators = [
            container.locator(
                f"[role='radio'][name='{choice_text}'], [role='option'][name='{choice_text}']"
            ).first,
            container.get_by_text(choice_text, exact=True).first,
            page.locator(
                f"[role='radio'][name='{choice_text}'], [role='option'][name='{choice_text}']"
            ).first,
        ]
        for locator in choice_locators:
            try:
                locator.wait_for(state="visible", timeout=10000)
                locator.click()
                return True
            except (TimeoutError, Error):
                continue
        raise ValueError(f"Could not answer '{question_text}' with '{choice_text}'.")

    def _upload(self, page: Page, labels: list[str], image_paths: list[str]) -> bool:
        for label in labels:
            escaped = self._escape_label(label)
            locator = page.locator(f"input[type='file'][aria-label*='{escaped}']").first
            if locator.count():
                locator.wait_for(state="attached", timeout=5000)
                if locator.evaluate("element => element.hasAttribute('multiple')"):
                    locator.set_input_files(image_paths)
                else:
                    locator.set_input_files(image_paths[0])
                return True
        return False

    def _wait_for_any_input(self, page: Page, labels: list[str]) -> bool:
        deadline = monotonic() + 6
        while monotonic() < deadline:
            for label in labels:
                escaped = self._escape_label(label)
                if page.locator(
                    f"input[aria-label*='{escaped}'], textarea[aria-label*='{escaped}'], "
                    f"input[type='file'][aria-label*='{escaped}']"
                ).count():
                    return True
            sleep(0.25)
        return False

    def _hold_browser_for_manual_review(self, page: Page) -> None:
        deadline = monotonic() + settings.playwright_manual_review_timeout_seconds
        while monotonic() < deadline:
            try:
                if page.is_closed():
                    return
                sleep(1)
            except Error:
                return

    def _get_question_container(self, page: Page, question_text: str):
        question = page.get_by_text(question_text, exact=True).first
        question.wait_for(state="visible", timeout=15000)
        for xpath in [
            "xpath=ancestor::*[@role='listitem'][1]",
            "xpath=ancestor::*[@data-automation-id='questionItem'][1]",
            "xpath=ancestor::div[1]",
        ]:
            container = question.locator(xpath).first
            if container.count():
                return container
        return page
