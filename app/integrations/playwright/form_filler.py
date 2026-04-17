from playwright.sync_api import sync_playwright

from app.schemas.session import FormDraft


class MicrosoftFormFiller:
    """
    Uses generic text selectors and should be adapted to a specific form layout.
    Intentionally stops before final submit.
    """

    def __init__(self, form_url: str):
        self.form_url = form_url

    def fill_draft(self, draft: FormDraft) -> str:
        if not self.form_url:
            raise ValueError("MICROSOFT_FORM_URL is not configured")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            page = browser.new_page()
            page.goto(self.form_url, wait_until="domcontentloaded")

            for label, value in draft.resident_fields.items():
                self._fill_text(page, label.replace("_", " ").title(), value)

            for label, value in draft.yes_no_flags.items():
                self._click_choice(page, label.replace("_", " ").title(), value)

            for index, item in enumerate(draft.damages, start=1):
                self._fill_text(page, f"Damage {index} Description", str(item["description"]))
                self._fill_text(page, f"Damage {index} Estimated Cost", str(item["estimated_cost"]))
                if item.get("image_path"):
                    self._upload(page, f"Damage {index} Image", str(item["image_path"]))

            page.wait_for_timeout(3000)
            browser.close()

        return "Form draft filled successfully. Stopped before final submit."

    def _fill_text(self, page, question_label: str, value: str) -> None:
        selector = f"input[aria-label*='{question_label}'], textarea[aria-label*='{question_label}']"
        target = page.locator(selector).first
        if target.count():
            target.fill(value)

    def _click_choice(self, page, question_label: str, choice: str) -> None:
        selector = f"div[aria-label*='{question_label}'] span:has-text('{choice}')"
        target = page.locator(selector).first
        if target.count():
            target.click()

    def _upload(self, page, question_label: str, image_path: str) -> None:
        input_selector = f"input[type='file'][aria-label*='{question_label}']"
        uploader = page.locator(input_selector).first
        if uploader.count():
            uploader.set_input_files(image_path)
