from pathlib import Path

from playwright.sync_api import sync_playwright

from app.core.config import settings


def capture_storage_state() -> str:
    storage_state_path = Path(settings.playwright_storage_state_path)
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(settings.microsoft_form_url or "https://forms.office.com", wait_until="domcontentloaded")
        input(
            "Sign in to Microsoft and open the live form in the browser window. "
            "Press Enter here when the session is ready to save."
        )
        context.storage_state(path=str(storage_state_path))
        browser.close()

    return str(storage_state_path)


if __name__ == "__main__":
    target = capture_storage_state()
    print(f"Saved Playwright storage state to {target}")
