from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import Error, Locator, Page, TimeoutError, sync_playwright

from app.core.config import settings
from app.schemas.session import FormDraft
from app.services.checkout_service import FORM_FILL_PARTIAL_FAILURE, FORM_FILL_SUCCESS
from app.services.form_mapping import (
    get_checkout_fields,
    get_damage_section,
    get_hall_options,
    get_room_side_options,
)


logger = logging.getLogger(__name__)

FIELD_TIMEOUT_MS = 3500
QUESTION_LOOKUP_TIMEOUT_MS = 800
CHOICE_TIMEOUT_MS = 1800
CONDITIONAL_TIMEOUT_MS = 5000
NAVIGATION_TIMEOUT_MS = 45000
PAGE_TRANSITION_TIMEOUT_MS = 6000
ALREADY_VISIBLE_TIMEOUT_MS = 500
SCROLL_TIMEOUT_MS = 1200
SUBMIT_TIMEOUT_MS = 8000
SUBMIT_CONFIRMATION_TIMEOUT_MS = 12000

FORM_STORAGE_HOSTS = (
    "forms.office.com",
    "forms.cloud.microsoft",
    "forms.microsoft.com",
)


class MicrosoftFormFiller:
    def __init__(self, form_url: str):
        self.form_url = form_url
        self.checkout_fields = get_checkout_fields()
        self.hall_options = get_hall_options()
        self.room_side_options = get_room_side_options()
        self.debug_enabled = settings.playwright_debug
        self.debug_events: list[dict[str, Any]] = []
        self.console_messages: list[dict[str, str]] = []
        self._active_session_id: int | None = None

    def fill_draft(self, draft: FormDraft) -> dict:
        if not self.form_url:
            raise ValueError("Microsoft Form URL is not configured.")

        storage_state_path = Path(settings.playwright_storage_state_path)
        if not storage_state_path.exists():
            raise ValueError(
                "Playwright storage state was not found. Authenticate first and save a Microsoft session."
            )

        self._active_session_id = draft.session_id
        self.debug_events = []
        self.console_messages = []
        timings: dict[str, float] = {}

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=settings.playwright_headless,
                args=self._launch_args(),
            )
            context = browser.new_context(
                storage_state=self._isolated_storage_state(storage_state_path),
                viewport={"width": 1280, "height": 900},
                locale="en-US",
                reduced_motion="reduce",
                permissions=[],
            )
            context.set_default_timeout(FIELD_TIMEOUT_MS)
            context.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
            if settings.playwright_headless and not self.debug_enabled:
                context.route("**/*", self._route_request)

            page = context.new_page()
            page.on("console", self._capture_console_message)
            try:
                with self._timed(timings, "total"):
                    return self._fill_draft_in_page(page, draft, timings)
            except Exception as exc:
                screenshot_path = self._save_debug_screenshot(page, "fatal_fill_error", force=True)
                raise RuntimeError(
                    f"Microsoft Form fill failed at {page.url}. "
                    f"Screenshot: {screenshot_path or 'not captured'}. Error: {exc}"
                ) from exc
            finally:
                try:
                    browser.close()
                except Error:
                    pass

    def _fill_draft_in_page(self, page: Page, draft: FormDraft, timings: dict[str, float]) -> dict:
        with self._timed(timings, "open_form"):
            self._open_clean_form(page, draft.session_id)

        with self._timed(timings, "top_level"):
            top_level_results = self._fill_top_level_fields(page, draft)

        has_damage_sections = any(section.answer_yes_no == "Yes" for section in draft.sections)
        if has_damage_sections:
            expected_page_texts = self._expected_after_top_level_texts(draft, has_damage_sections)
            next_result = self._record_next_page(page, expected_page_texts)
            top_level_results.append(next_result)

            results = []
            for section in draft.sections:
                with self._timed(timings, f"section.{section.category_key}"):
                    results.append(self._fill_section_safely(page, section))

            with self._timed(timings, "bathroom"):
                bathroom_result = self._record_choice(
                    page,
                    "has_bathroom",
                    self.checkout_fields["has_bathroom"],
                    draft.room_has_bathroom,
                )
                top_level_results.append(bathroom_result)
        else:
            expected_page_texts = self._expected_after_top_level_texts(draft, has_damage_sections)
            next_result = self._record_optional_next_page(page, expected_page_texts, "has_damages_no")
            top_level_results.append(next_result)
            results = [self._skipped_section_result(section, "has_damages_no") for section in draft.sections]
            with self._timed(timings, "bathroom"):
                if next_result.get("page_available"):
                    bathroom_result = self._record_optional_choice(
                        page,
                        "has_bathroom",
                        self.checkout_fields["has_bathroom"],
                        draft.room_has_bathroom,
                        "has_damages_no",
                    )
                else:
                    bathroom_result = self._skipped_field_result(
                        "has_bathroom",
                        "select_choice",
                        "has_damages_no",
                        question=self.checkout_fields["has_bathroom"],
                        choice=draft.room_has_bathroom,
                    )
                top_level_results.append(bathroom_result)

        summary = self._summarize_results(top_level_results, results)
        has_partial_failure = bool(summary["fields_failed"])
        submit_result: dict[str, Any] | None = None

        if settings.playwright_headless and settings.playwright_auto_submit_headless and not has_partial_failure:
            with self._timed(timings, "submit"):
                submit_result = self._record_submit(page)
                top_level_results.append(submit_result)
            summary = self._summarize_results(top_level_results, results)
            has_partial_failure = bool(summary["fields_failed"])

        if not settings.playwright_headless:
            message = "Form opened and filled. Review it in the browser and submit manually."
            if has_partial_failure:
                message = "Form opened for manual review, but some fields need attention before submit."
            self._hold_browser_for_manual_review(page)
        else:
            if submit_result and submit_result.get("ok"):
                message = "Headless form fill submitted the Microsoft Form successfully."
            elif submit_result:
                message = "Headless form fill completed, but the Microsoft Form submit step needs attention."
            elif settings.playwright_auto_submit_headless:
                message = "Headless form fill stopped with fields still needing attention before submit."
            else:
                message = (
                    "Headless form fill ran in an isolated browser context and stopped before submit. "
                    "Enable PLAYWRIGHT_AUTO_SUBMIT_HEADLESS to submit automatically."
                )
            if has_partial_failure and not submit_result:
                message = "Headless form fill stopped with fields still needing attention before submit."

        outcome = {
            "success": not has_partial_failure,
            "status": FORM_FILL_PARTIAL_FAILURE if has_partial_failure else FORM_FILL_SUCCESS,
            "message": message,
            "submitted": bool(submit_result and submit_result.get("ok")),
            "submit_result": submit_result,
            "errors": summary["errors"],
            "fields_completed": summary["fields_completed"],
            "fields_failed": summary["fields_failed"],
            "timings": timings,
            "top_level_fields": top_level_results,
            "sections": results,
            "debug": {
                "enabled": self.debug_enabled,
                "events": self.debug_events,
                "console": self.console_messages,
            },
        }
        return outcome

    def _fill_top_level_fields(self, page: Page, draft: FormDraft) -> list[dict[str, Any]]:
        return [
            self._record_text(
                page,
                "resident_name",
                [self.checkout_fields["resident_name"]],
                draft.resident_fields["resident_name"],
            ),
            self._record_text(
                page,
                "room_number",
                [self.checkout_fields["room_number"]],
                draft.resident_fields["room_number"],
            ),
            self._record_text(page, "tech_id", [self.checkout_fields["tech_id"]], draft.resident_fields["tech_id"]),
            self._record_dropdown(
                page,
                "hall",
                self.checkout_fields["hall"],
                self.hall_options[draft.resident_fields["hall"]],
            ),
            self._record_dropdown_or_text(
                page,
                "staff_name",
                self.checkout_fields["staff_name"],
                draft.resident_fields.get("staff_name") or "Nift",
            ),
            self._record_choice(
                page,
                "room_side",
                self.checkout_fields["room_side"],
                self.room_side_options[draft.resident_fields["room_side"]],
            ),
            self._record_choice(
                page,
                "has_damages",
                self.checkout_fields["has_damages"],
                "Yes" if any(section.answer_yes_no == "Yes" for section in draft.sections) else "No",
            ),
        ]

    def _fill_section_safely(self, page: Page, section) -> dict[str, Any]:
        try:
            return self._fill_section(page, section)
        except Exception as exc:  # noqa: BLE001
            error = self._error_payload(
                field=section.category_key,
                operation="section",
                message=str(exc),
                screenshot=self._save_debug_screenshot(page, f"{section.category_key}_section"),
            )
            return {
                "category_key": section.category_key,
                "category_name": section.category_name,
                "guessed_category_confidence": section.guessed_confidence,
                "confirmed_category": section.category_name,
                "answered_yes_no": section.answer_yes_no,
                "yes_no_selected": False,
                "conditional_fields_appeared": False,
                "description_filled": False,
                "cost_filled": False,
                "image_upload_succeeded": False,
                "skipped": False,
                "partial_failure": True,
                "errors": [error],
                "field_results": [
                    {
                        "field": section.category_key,
                        "ok": False,
                        "operation": "section",
                        "error": error,
                    }
                ],
            }

    def _skipped_section_result(self, section, reason: str) -> dict[str, Any]:
        return {
            "category_key": section.category_key,
            "category_name": section.category_name,
            "guessed_category_confidence": section.guessed_confidence,
            "confirmed_category": section.category_name,
            "answered_yes_no": section.answer_yes_no,
            "yes_no_selected": False,
            "conditional_fields_appeared": False,
            "description_filled": False,
            "cost_filled": False,
            "image_upload_succeeded": True,
            "skipped": True,
            "skip_reason": reason,
            "partial_failure": False,
            "errors": [],
            "field_results": [],
        }

    def _fill_section(self, page: Page, section) -> dict[str, Any]:
        result: dict[str, Any] = {
            "category_key": section.category_key,
            "category_name": section.category_name,
            "guessed_category_confidence": section.guessed_confidence,
            "confirmed_category": section.category_name,
            "answered_yes_no": section.answer_yes_no,
            "yes_no_selected": False,
            "conditional_fields_appeared": False,
            "description_filled": False,
            "cost_filled": False,
            "image_upload_succeeded": False,
            "skipped": section.answer_yes_no == "No",
            "partial_failure": False,
            "errors": [],
            "field_results": [],
        }
        section_mapping = get_damage_section(section.category_key)
        if section_mapping.get("live_form_enabled") is False:
            result["skipped"] = True
            result["image_upload_succeeded"] = True
            return result

        question_texts = self._question_texts(section_mapping, section.question)
        choice_result = self._record_choice(page, section.category_key, question_texts, section.answer_yes_no)
        result["field_results"].append(choice_result)
        result["yes_no_selected"] = choice_result["ok"]
        if not choice_result["ok"]:
            result["errors"].append(choice_result["error"])

        if section.answer_yes_no == "No":
            result["partial_failure"] = not result["yes_no_selected"]
            return result

        conditional_labels = (
            section_mapping["description_labels"] + section_mapping["cost_labels"] + section_mapping["image_labels"]
        )
        result["conditional_fields_appeared"] = self._wait_for_conditional_fields(
            page,
            conditional_labels,
            question_texts,
            section.answer_yes_no,
        )
        if not result["conditional_fields_appeared"]:
            error = self._error_payload(
                field=section.category_key,
                operation="conditional_render",
                message=f"Conditional fields did not appear after selecting {section.answer_yes_no}.",
                labels=conditional_labels,
                screenshot=self._save_debug_screenshot(page, f"{section.category_key}_conditional"),
            )
            result["errors"].append(error)
            result["partial_failure"] = True
            return result

        description_result = self._record_text(
            page,
            f"{section.category_key}_description",
            section_mapping["description_labels"],
            section.description,
        )
        result["field_results"].append(description_result)
        result["description_filled"] = description_result["ok"]
        if not description_result["ok"]:
            result["errors"].append(description_result["error"])

        cost_result = self._record_text(
            page,
            f"{section.category_key}_cost",
            section_mapping["cost_labels"],
            f"{section.estimated_cost:.2f}",
        )
        result["field_results"].append(cost_result)
        result["cost_filled"] = cost_result["ok"]
        if not cost_result["ok"]:
            result["errors"].append(cost_result["error"])

        image_inputs = section.image_paths or ([section.image_path] if section.image_path else [])
        if image_inputs:
            upload_result = self._record_upload(
                page,
                f"{section.category_key}_images",
                section_mapping["image_labels"],
                image_inputs,
            )
            result["field_results"].append(upload_result)
            result["image_upload_succeeded"] = upload_result["ok"]
            if not upload_result["ok"]:
                result["errors"].append(upload_result["error"])
        else:
            result["image_upload_succeeded"] = True

        result["partial_failure"] = not all(
            [
                result["yes_no_selected"],
                result["conditional_fields_appeared"],
                result["description_filled"],
                result["cost_filled"],
                result["image_upload_succeeded"],
            ]
        )
        return result

    def _open_clean_form(self, page: Page, session_id: int) -> None:
        page.add_init_script(
            """
            () => {
                try { window.sessionStorage.clear(); } catch (error) {}
                try { window.history.scrollRestoration = "manual"; } catch (error) {}
                const disableAutocomplete = () => {
                    for (const field of document.querySelectorAll("input, textarea")) {
                        field.setAttribute("autocomplete", "off");
                    }
                };
                if (document.readyState === "loading") {
                    document.addEventListener("DOMContentLoaded", disableAutocomplete, { once: true });
                } else {
                    disableAutocomplete();
                }
                new MutationObserver(disableAutocomplete).observe(document.documentElement, {
                    childList: true,
                    subtree: true
                });
            }
            """
        )
        url = self._fresh_form_url(session_id)
        self._debug("navigate", url=url)
        page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        self._wait_for_form_ready(page)
        self._scroll_to_top(page)

    def _record_text(self, page: Page, field: str, labels: list[str], value: str) -> dict[str, Any]:
        started = monotonic()
        try:
            ok, strategy = self._fill_text(page, labels, value)
            error = None
            if not ok:
                error = self._error_payload(
                    field=field,
                    operation="fill_text",
                    message=f"Could not fill text field for labels: {', '.join(labels)}",
                    labels=labels,
                    screenshot=self._save_debug_screenshot(page, field),
                )
            return self._field_result(
                field=field,
                ok=ok,
                operation="fill_text",
                error=error,
                labels=labels,
                selector_strategy=strategy,
                duration_ms=self._elapsed_ms(started),
            )
        except Exception as exc:  # noqa: BLE001
            return self._field_result(
                field=field,
                ok=False,
                operation="fill_text",
                labels=labels,
                error=self._error_payload(
                    field=field,
                    operation="fill_text",
                    message=str(exc),
                    labels=labels,
                    screenshot=self._save_debug_screenshot(page, field),
                ),
                duration_ms=self._elapsed_ms(started),
            )

    def _record_dropdown(self, page: Page, field: str, question_text: str, choice_text: str) -> dict[str, Any]:
        started = monotonic()
        try:
            ok, strategy = self._select_dropdown(page, question_text, choice_text)
            error = None
            if not ok:
                error = self._error_payload(
                    field=field,
                    operation="select_dropdown",
                    message=f"Could not select dropdown value '{choice_text}' for '{question_text}'.",
                    question=question_text,
                    choice=choice_text,
                    screenshot=self._save_debug_screenshot(page, field),
                )
            return self._field_result(
                field=field,
                ok=ok,
                operation="select_dropdown",
                question=question_text,
                choice=choice_text,
                error=error,
                selector_strategy=strategy,
                duration_ms=self._elapsed_ms(started),
            )
        except Exception as exc:  # noqa: BLE001
            return self._field_result(
                field=field,
                ok=False,
                operation="select_dropdown",
                question=question_text,
                choice=choice_text,
                error=self._error_payload(
                    field=field,
                    operation="select_dropdown",
                    message=str(exc),
                    question=question_text,
                    choice=choice_text,
                    screenshot=self._save_debug_screenshot(page, field),
                ),
                duration_ms=self._elapsed_ms(started),
            )

    def _record_dropdown_or_text(self, page: Page, field: str, question_text: str, value: str) -> dict[str, Any]:
        result = self._record_dropdown(page, field, question_text, value)
        if result["ok"]:
            return result

        fallback = self._record_text(page, field, [question_text], value)
        if fallback["ok"]:
            fallback["operation"] = "select_dropdown_or_fill_text"
            fallback["dropdown_error"] = result.get("error")
        return fallback

    def _record_next_page(self, page: Page, expected_texts: list[str]) -> dict[str, Any]:
        started = monotonic()
        ok, strategy = self._go_to_next_page(page, expected_texts)
        error = None
        if not ok:
            error = self._error_payload(
                field="next_page",
                operation="navigate_next",
                message="Could not move to the damage questions page.",
                labels=expected_texts,
                screenshot=self._save_debug_screenshot(page, "next_page"),
            )
        return self._field_result(
            field="next_page",
            ok=ok,
            operation="navigate_next",
            error=error,
            selector_strategy=strategy,
            duration_ms=self._elapsed_ms(started),
        )

    def _record_optional_next_page(self, page: Page, expected_texts: list[str], reason: str) -> dict[str, Any]:
        started = monotonic()
        ok, strategy = self._go_to_optional_next_page(page, expected_texts)
        if ok:
            result = self._field_result(
                field="next_page",
                ok=True,
                operation="navigate_next",
                error=None,
                selector_strategy=strategy,
                duration_ms=self._elapsed_ms(started),
            )
            result["page_available"] = True
            return result

        result = self._skipped_field_result(
            "next_page",
            "navigate_next",
            reason,
            labels=expected_texts,
            selector_strategy="skipped_optional_branch",
            duration_ms=self._elapsed_ms(started),
        )
        result["page_available"] = False
        return result

    def _record_optional_choice(
        self,
        page: Page,
        field: str,
        question_text: str | list[str],
        choice_text: str,
        reason: str,
    ) -> dict[str, Any]:
        result = self._record_choice(page, field, question_text, choice_text)
        if result["ok"]:
            return result

        return self._skipped_field_result(
            field,
            "select_choice",
            reason,
            question=self._format_question_text(question_text),
            choice=choice_text,
            selector_strategy="skipped_optional_branch",
            duration_ms=result.get("duration_ms"),
        )

    def _record_submit(self, page: Page) -> dict[str, Any]:
        started = monotonic()
        clicked, strategy = self._click_submit(page)
        if clicked and self._wait_for_submit_confirmation(page):
            return self._field_result(
                field="submit",
                ok=True,
                operation="submit_form",
                error=None,
                selector_strategy=strategy,
                duration_ms=self._elapsed_ms(started),
            )

        message = "Could not find the Microsoft Forms Submit button."
        if clicked:
            message = "Clicked Submit, but Microsoft Forms did not show a submission confirmation."
        error = self._error_payload(
            field="submit",
            operation="submit_form",
            message=message,
            screenshot=self._save_debug_screenshot(page, "submit"),
        )
        return self._field_result(
            field="submit",
            ok=False,
            operation="submit_form",
            error=error,
            selector_strategy=strategy,
            duration_ms=self._elapsed_ms(started),
        )

    def _record_choice(self, page: Page, field: str, question_text: str | list[str], choice_text: str) -> dict[str, Any]:
        started = monotonic()
        try:
            ok, strategy = self._select_choice(page, question_text, choice_text)
            error = None
            if not ok:
                error = self._error_payload(
                    field=field,
                    operation="select_choice",
                    message=f"Could not select '{choice_text}' for '{self._format_question_text(question_text)}'.",
                    question=self._format_question_text(question_text),
                    choice=choice_text,
                    screenshot=self._save_debug_screenshot(page, field),
                )
            return self._field_result(
                field=field,
                ok=ok,
                operation="select_choice",
                question=self._format_question_text(question_text),
                choice=choice_text,
                error=error,
                selector_strategy=strategy,
                duration_ms=self._elapsed_ms(started),
            )
        except Exception as exc:  # noqa: BLE001
            return self._field_result(
                field=field,
                ok=False,
                operation="select_choice",
                question=self._format_question_text(question_text),
                choice=choice_text,
                error=self._error_payload(
                    field=field,
                    operation="select_choice",
                    message=str(exc),
                    question=self._format_question_text(question_text),
                    choice=choice_text,
                    screenshot=self._save_debug_screenshot(page, field),
                ),
                duration_ms=self._elapsed_ms(started),
            )

    def _record_upload(self, page: Page, field: str, labels: list[str], image_paths: list[str]) -> dict[str, Any]:
        started = monotonic()
        ok, strategy = self._upload(page, labels, image_paths)
        error = None
        if not ok:
            error = self._error_payload(
                field=field,
                operation="upload",
                message=f"Could not upload images for labels: {', '.join(labels)}",
                labels=labels,
                screenshot=self._save_debug_screenshot(page, field),
            )
        return self._field_result(
            field=field,
            ok=ok,
            operation="upload",
            labels=labels,
            error=error,
            selector_strategy=strategy,
            duration_ms=self._elapsed_ms(started),
        )

    def _fill_text(self, page: Page, labels: list[str], value: str) -> tuple[bool, str | None]:
        for label in labels:
            for strategy, locator in self._text_locator_candidates(page, label):
                if self._fill_locator(locator, value):
                    self._debug("text_filled", label=label, strategy=strategy)
                    return True, strategy
        return False, None

    def _text_locator_candidates(self, page: Page, label: str) -> list[tuple[str, Locator]]:
        css_label = self._css_string(label)
        candidates: list[tuple[str, Locator]] = [
            ("label", page.get_by_label(label, exact=False).first),
            (
                "aria",
                page.locator(
                    f"input[aria-label*={css_label}], textarea[aria-label*={css_label}], "
                    f"[role='textbox'][aria-label*={css_label}], [contenteditable='true'][aria-label*={css_label}]"
                ).first,
            ),
            (
                "placeholder",
                page.locator(f"input[placeholder*={css_label}], textarea[placeholder*={css_label}]").first,
            ),
        ]
        container = self._get_question_container_or_none(page, label)
        if container is not None:
            candidates.extend(
                [
                    ("question_textarea", container.locator("textarea").first),
                    (
                        "question_textbox",
                        container.locator(
                            "input:not([type='hidden']):not([type='file']):not([type='radio']):not([type='checkbox']), "
                            "[role='textbox'], [contenteditable='true']"
                        ).first,
                    ),
                ]
            )
        return candidates

    def _fill_locator(self, locator: Locator, value: str) -> bool:
        try:
            locator.wait_for(state="visible", timeout=FIELD_TIMEOUT_MS)
            locator.scroll_into_view_if_needed(timeout=SCROLL_TIMEOUT_MS)
            locator.fill(value, timeout=FIELD_TIMEOUT_MS)
            return True
        except (TimeoutError, Error):
            return False

    def _select_dropdown(self, page: Page, question_text: str, choice_text: str) -> tuple[bool, str | None]:
        container = self._get_question_container(page, question_text)
        triggers = [
            ("native_select", container.locator("select").first),
            ("role_combobox", container.get_by_role("combobox").first),
            ("aria_combobox", container.locator("[role='combobox']").first),
            ("listbox_button", container.locator("[aria-haspopup='listbox']").first),
            ("question_button", container.get_by_role("button").first),
        ]
        for strategy, trigger in triggers:
            try:
                trigger.wait_for(state="visible", timeout=QUESTION_LOOKUP_TIMEOUT_MS)
                trigger.scroll_into_view_if_needed(timeout=SCROLL_TIMEOUT_MS)
                tag_name = trigger.evaluate("element => element.tagName.toLowerCase()")
                if tag_name == "select":
                    trigger.select_option(label=choice_text, timeout=FIELD_TIMEOUT_MS)
                    self._debug("dropdown_selected", question=question_text, choice=choice_text, strategy=strategy)
                    return True, strategy
                trigger.click(timeout=FIELD_TIMEOUT_MS)
                option_strategy = self._click_dropdown_option(page, choice_text)
                if option_strategy:
                    return True, f"{strategy}->{option_strategy}"
            except (TimeoutError, Error):
                continue
        return False, None

    def _click_dropdown_option(self, page: Page, choice_text: str) -> str | None:
        option_locators = [
            ("role_option_exact", page.get_by_role("option", name=choice_text, exact=True).first),
            ("role_option_fuzzy", page.get_by_role("option", name=choice_text, exact=False).first),
            ("listbox_text", page.locator("[role='listbox'] [role='option']").filter(has_text=choice_text).first),
            ("text_exact", page.get_by_text(choice_text, exact=True).first),
        ]
        if len(choice_text) == 1:
            option_locators.append(
                (
                    "single_letter_option",
                    page.locator(f"[role='option'] >> text=/^{self._escape_regex(choice_text)}\\b/i").first,
                )
            )
        for strategy, locator in option_locators:
            try:
                locator.wait_for(state="visible", timeout=CHOICE_TIMEOUT_MS)
                locator.click(timeout=FIELD_TIMEOUT_MS)
                self._debug("dropdown_option_clicked", choice=choice_text, strategy=strategy)
                return strategy
            except (TimeoutError, Error):
                continue
        return None

    def _select_choice(self, page: Page, question_text: str | list[str], choice_text: str) -> tuple[bool, str | None]:
        container = self._get_question_container(page, question_text)
        dom_strategy = self._click_choice_by_dom(page, container, choice_text)
        if dom_strategy:
            self._debug(
                "choice_selected",
                question=self._format_question_text(question_text),
                choice=choice_text,
                strategy=dom_strategy,
            )
            return True, dom_strategy

        choice_locators = [
            ("role_radio_exact", container.get_by_role("radio", name=choice_text, exact=True).first),
            ("label_exact", container.get_by_label(choice_text, exact=True).first),
            ("role_option_exact", container.get_by_role("option", name=choice_text, exact=True).first),
            (
                "choice_item",
                container.locator("[data-automation-id='choiceItem'], [role='radio'], [role='option']")
                .filter(has_text=choice_text)
                .first,
            ),
            ("text_exact", container.get_by_text(choice_text, exact=True).first),
            ("role_radio_fuzzy", container.get_by_role("radio", name=choice_text, exact=False).first),
            ("role_option_fuzzy", container.get_by_role("option", name=choice_text, exact=False).first),
        ]
        for strategy, locator in choice_locators:
            try:
                locator.wait_for(state="visible", timeout=CHOICE_TIMEOUT_MS)
                locator.scroll_into_view_if_needed(timeout=SCROLL_TIMEOUT_MS)
                locator.click(timeout=FIELD_TIMEOUT_MS)
                self._debug(
                    "choice_selected",
                    question=self._format_question_text(question_text),
                    choice=choice_text,
                    strategy=strategy,
                )
                return True, strategy
            except (TimeoutError, Error):
                continue
        return False, None

    def _click_choice_by_dom(self, page: Page, container: Locator, choice_text: str) -> str | None:
        marker = f"clawbot-choice-{int(monotonic() * 1000000)}"
        try:
            matched = container.evaluate(
                """
                (container, { choiceText, marker }) => {
                    const normalize = value => (value || "")
                        .toLowerCase()
                        .replace(/[^a-z0-9]+/g, " ")
                        .trim();
                    const wanted = normalize(choiceText);
                    const clickable = element => element.closest(
                        "label, [role='radio'], [role='option'], button, "
                        "[data-automation-id='choiceItem'], [data-automation-id='choice']"
                    ) || element;
                    const textFor = element => normalize([
                        element.getAttribute("aria-label"),
                        element.getAttribute("data-automation-value"),
                        element.innerText,
                        element.textContent
                    ].join(" "));
                    const candidates = Array.from(container.querySelectorAll(
                        "label, [role='radio'], [role='option'], button, "
                        "[data-automation-id='choiceItem'], [data-automation-id='choice'], span, div"
                    ));
                    const exact = candidates.find(element => textFor(element) === wanted);
                    const aria = candidates.find(element => normalize(element.getAttribute("aria-label")) === wanted);
                    const matched = exact || aria;
                    if (!matched) return false;
                    const target = clickable(matched);
                    target.setAttribute("data-clawbot-choice-match", marker);
                    target.scrollIntoView({ block: "center", inline: "nearest" });
                    return true;
                }
                """,
                {"choiceText": choice_text, "marker": marker},
            )
            if not matched:
                return None
            locator = page.locator(f"[data-clawbot-choice-match={self._css_string(marker)}]").first
            locator.wait_for(state="visible", timeout=QUESTION_LOOKUP_TIMEOUT_MS)
            locator.click(timeout=FIELD_TIMEOUT_MS)
            return "dom_choice"
        except (TimeoutError, Error):
            return None

    def _upload(self, page: Page, labels: list[str], image_paths: list[str]) -> tuple[bool, str | None]:
        for label in labels:
            css_label = self._css_string(label)
            locators = [
                ("aria_file_input", page.locator(f"input[type='file'][aria-label*={css_label}]").first),
            ]
            container = self._get_question_container_or_none(page, label)
            if container is not None:
                locators.append(("question_file_input", container.locator("input[type='file']").first))
            locators.append(("fallback_file_input", page.locator("input[type='file']").first))

            for strategy, locator in locators:
                try:
                    locator.wait_for(state="attached", timeout=FIELD_TIMEOUT_MS)
                    if locator.evaluate("element => element.hasAttribute('multiple')"):
                        locator.set_input_files(image_paths, timeout=FIELD_TIMEOUT_MS)
                    else:
                        locator.set_input_files(image_paths[0], timeout=FIELD_TIMEOUT_MS)
                    self._debug("files_uploaded", label=label, count=len(image_paths), strategy=strategy)
                    return True, strategy
                except (TimeoutError, Error):
                    continue
        return False, None

    def _wait_for_conditional_fields(
        self,
        page: Page,
        labels: list[str],
        question_texts: list[str],
        choice_text: str,
    ) -> bool:
        if self._wait_for_any_input(page, labels, timeout_ms=CONDITIONAL_TIMEOUT_MS):
            return True

        self._debug("conditional_retry", question=self._format_question_text(question_texts), choice=choice_text)
        try:
            self._select_choice(page, question_texts, choice_text)
        except Exception as exc:  # noqa: BLE001
            self._debug("conditional_retry_failed", error=str(exc))

        return self._wait_for_any_input(page, labels, timeout_ms=CONDITIONAL_TIMEOUT_MS // 2)

    def _wait_for_any_input(self, page: Page, labels: list[str], timeout_ms: int) -> bool:
        if not labels:
            return True
        try:
            page.wait_for_function(
                """
                labels => {
                    const normalize = value => (value || "")
                        .toLowerCase()
                        .replace(/[^a-z0-9]+/g, " ")
                        .trim();
                    const wanted = labels.map(normalize).filter(Boolean);
                    const visible = element => {
                        const style = window.getComputedStyle(element);
                        const rect = element.getBoundingClientRect();
                        return style.visibility !== "hidden"
                            && style.display !== "none"
                            && rect.width > 0
                            && rect.height > 0;
                    };
                    const visibleField = element => {
                        const question = element.closest("[data-automation-id='questionItem'], [role='listitem']");
                        return visible(element) || (question && visible(question));
                    };
                    const fieldText = element => normalize([
                        element.getAttribute("aria-label"),
                        element.getAttribute("placeholder"),
                        element.id ? document.querySelector(`label[for="${CSS.escape(element.id)}"]`)?.innerText : "",
                        element.closest("[data-automation-id='questionItem'], [role='listitem']")?.innerText
                    ].join(" "));
                    const fields = Array.from(document.querySelectorAll(
                        "input:not([type='hidden']), textarea, [role='textbox'], input[type='file']"
                    ));
                    if (fields.some(field => visibleField(field)
                        && wanted.some(label => fieldText(field).includes(label)))) {
                        return true;
                    }
                    const questions = Array.from(document.querySelectorAll(
                        "[data-automation-id='questionItem'], [role='listitem']"
                    ));
                    return questions.some(question => visible(question)
                        && wanted.some(label => normalize(question.innerText).includes(label)));
                }
                """,
                arg=labels,
                timeout=timeout_ms,
            )
            return True
        except (TimeoutError, Error):
            return False

    def _get_question_container(self, page: Page, question_text: str | list[str]) -> Locator:
        container = self._get_question_container_or_none(page, question_text)
        if container is None:
            raise ValueError(f"Could not find question: {self._format_question_text(question_text)}")
        return container

    def _get_question_container_or_none(self, page: Page, question_text: str | list[str]) -> Locator | None:
        fuzzy = self._find_fuzzy_question_container(page, self._as_texts(question_text))
        if fuzzy is not None:
            return fuzzy

        for candidate in self._as_texts(question_text):
            for strategy, locator in self._question_container_candidates(page, candidate):
                try:
                    locator.wait_for(state="visible", timeout=QUESTION_LOOKUP_TIMEOUT_MS)
                    locator.scroll_into_view_if_needed(timeout=SCROLL_TIMEOUT_MS)
                    self._debug("question_container_found", text=candidate, strategy=strategy)
                    return locator
                except (TimeoutError, Error):
                    continue

        return None

    def _question_container_candidates(self, page: Page, text: str) -> list[tuple[str, Locator]]:
        question = page.get_by_text(text, exact=False).first
        return [
            (
                "question_item_has_text",
                page.locator("[data-automation-id='questionItem']").filter(has_text=text).first,
            ),
            ("listitem_has_text", page.locator("[role='listitem']").filter(has_text=text).first),
            ("text_to_question_item", question.locator("xpath=ancestor::*[@data-automation-id='questionItem'][1]").first),
            ("text_to_listitem", question.locator("xpath=ancestor::*[@role='listitem'][1]").first),
            ("text_to_question_class", question.locator("xpath=ancestor::*[contains(@class, 'question')][1]").first),
            ("text_to_parent", question.locator("xpath=ancestor::div[1]").first),
        ]

    def _find_fuzzy_question_container(self, page: Page, texts: list[str]) -> Locator | None:
        marker = f"clawbot-match-{int(monotonic() * 1000000)}"
        try:
            matched = page.evaluate(
                """
                ({ texts, marker }) => {
                    const normalize = value => (value || "")
                        .toLowerCase()
                        .replace(/[^a-z0-9]+/g, " ")
                        .trim();
                    const tokenSet = value => new Set(normalize(value).split(" ").filter(token => token.length > 2));
                    const wanted = texts.map(text => ({
                        raw: text,
                        normalized: normalize(text),
                        tokens: tokenSet(text)
                    })).filter(item => item.normalized);
                    const nodes = Array.from(document.querySelectorAll(
                        "[data-automation-id='questionItem'], [role='listitem']"
                    ));
                    let best = null;
                    for (const node of nodes) {
                        const nodeText = normalize(node.innerText);
                        if (!nodeText) continue;
                        for (const target of wanted) {
                            let score = 0;
                            if (nodeText === target.normalized) {
                                score = 1.1;
                            } else if (nodeText.includes(target.normalized)) {
                                score = 0.95;
                            } else if (target.normalized.includes(nodeText)) {
                                score = 0.9;
                            } else {
                                const overlap = [...target.tokens].filter(token => nodeText.includes(token)).length;
                                score = target.tokens.size ? overlap / target.tokens.size : 0;
                            }
                            if (!best || score > best.score) {
                                best = { node, score };
                            }
                        }
                    }
                    if (!best || best.score < 0.55) return false;
                    best.node.setAttribute("data-clawbot-question-match", marker);
                    best.node.scrollIntoView({ block: "center" });
                    return true;
                }
                """,
                {"texts": texts, "marker": marker},
            )
            if matched:
                locator = page.locator(f"[data-clawbot-question-match={self._css_string(marker)}]").first
                locator.wait_for(state="visible", timeout=QUESTION_LOOKUP_TIMEOUT_MS)
                self._debug("question_container_found", text=self._format_question_text(texts), strategy="fuzzy_js")
                return locator
        except (TimeoutError, Error):
                return None
        return None

    def _go_to_optional_next_page(self, page: Page, expected_texts: list[str]) -> tuple[bool, str | None]:
        if expected_texts and self._wait_for_any_visible_text(page, expected_texts, ALREADY_VISIBLE_TIMEOUT_MS):
            self._debug("next_page_already_visible", expected_texts=expected_texts[:3])
            return True, "already_visible"

        before_text = self._visible_page_text(page)
        try:
            clicked = page.evaluate(
                """
                () => {
                    const visible = element => {
                        const style = window.getComputedStyle(element);
                        const rect = element.getBoundingClientRect();
                        return style.visibility !== "hidden"
                            && style.display !== "none"
                            && rect.width > 0
                            && rect.height > 0;
                    };
                    const label = element => ([
                        element.getAttribute("aria-label"),
                        element.innerText,
                        element.textContent
                    ].join(" ") || "").trim();
                    const enabled = element => !element.disabled
                        && element.getAttribute("aria-disabled") !== "true"
                        && !element.closest("[aria-disabled='true']");
                    const target = Array.from(document.querySelectorAll("button, [role='button']"))
                        .find(element => visible(element) && enabled(element) && /^next$/i.test(label(element)));
                    if (!target) return false;
                    target.scrollIntoView({ block: "center", inline: "nearest" });
                    target.click();
                    return true;
                }
                """
            )
        except Error:
            clicked = False

        if not clicked:
            return False, None
        if self._wait_for_page_transition(page, before_text, expected_texts):
            self._scroll_to_top(page)
            self._debug("next_page_clicked", strategy="dom_optional_next")
            return True, "dom_optional_next"
        return False, "dom_optional_next_no_transition"

    def _go_to_next_page(self, page: Page, expected_texts: list[str]) -> tuple[bool, str | None]:
        if expected_texts and self._wait_for_any_visible_text(page, expected_texts, ALREADY_VISIBLE_TIMEOUT_MS):
            self._debug("next_page_already_visible", expected_texts=expected_texts[:3])
            return True, "already_visible"

        before_text = self._visible_page_text(page)
        for strategy, button in self._next_button_candidates(page):
            try:
                button.wait_for(state="visible", timeout=QUESTION_LOOKUP_TIMEOUT_MS)
                self._wait_for_enabled(page, button)
                button.scroll_into_view_if_needed(timeout=SCROLL_TIMEOUT_MS)
                button.click(timeout=FIELD_TIMEOUT_MS)
                if self._wait_for_page_transition(page, before_text, expected_texts):
                    self._scroll_to_top(page)
                    self._debug("next_page_clicked", strategy=strategy)
                    return True, strategy
            except (TimeoutError, Error):
                continue
        return False, None

    def _click_submit(self, page: Page) -> tuple[bool, str | None]:
        dom_strategy = self._click_submit_by_dom(page)
        if dom_strategy:
            self._debug("submit_clicked", strategy=dom_strategy)
            return True, dom_strategy

        for strategy, button in self._submit_button_candidates(page):
            try:
                button.wait_for(state="visible", timeout=QUESTION_LOOKUP_TIMEOUT_MS)
                self._wait_for_enabled(page, button)
                button.scroll_into_view_if_needed(timeout=SCROLL_TIMEOUT_MS)
                button.click(timeout=FIELD_TIMEOUT_MS)
                self._debug("submit_clicked", strategy=strategy)
                return True, strategy
            except (TimeoutError, Error):
                continue
        return False, None

    def _click_submit_by_dom(self, page: Page) -> str | None:
        marker = f"clawbot-submit-{int(monotonic() * 1000000)}"
        try:
            matched = page.evaluate(
                """
                marker => {
                    const visible = element => {
                        const style = window.getComputedStyle(element);
                        const rect = element.getBoundingClientRect();
                        return style.visibility !== "hidden"
                            && style.display !== "none"
                            && rect.width > 0
                            && rect.height > 0;
                    };
                    const enabled = element => !element.disabled
                        && element.getAttribute("aria-disabled") !== "true"
                        && !element.closest("[aria-disabled='true']");
                    const label = element => ([
                        element.getAttribute("aria-label"),
                        element.innerText,
                        element.textContent
                    ].join(" ") || "").replace(/\\s+/g, " ").trim();
                    const target = Array.from(document.querySelectorAll("button, [role='button']"))
                        .find(element => visible(element)
                            && enabled(element)
                            && /^submit$/i.test(label(element)));
                    if (!target) return false;
                    target.setAttribute("data-clawbot-submit-match", marker);
                    target.scrollIntoView({ block: "center", inline: "nearest" });
                    return true;
                }
                """,
                marker,
            )
            if not matched:
                return None
            locator = page.locator(f"[data-clawbot-submit-match={self._css_string(marker)}]").first
            locator.wait_for(state="visible", timeout=QUESTION_LOOKUP_TIMEOUT_MS)
            locator.click(timeout=FIELD_TIMEOUT_MS)
            return "dom_submit"
        except (TimeoutError, Error):
            return None

    def _submit_button_candidates(self, page: Page) -> list[tuple[str, Locator]]:
        submit_name = re.compile(r"^\s*Submit\s*$", re.I)
        return [
            ("automation_id", page.locator("[data-automation-id='submitButton']").first),
            ("role_exact", page.get_by_role("button", name=submit_name).first),
            ("aria_label", page.locator("button[aria-label='Submit'], [role='button'][aria-label='Submit']").first),
            ("button_text", page.locator("button").filter(has_text=submit_name).first),
            ("role_text", page.locator("[role='button']").filter(has_text=submit_name).first),
        ]

    def _wait_for_submit_confirmation(self, page: Page) -> bool:
        confirmation_texts = [
            "response was submitted",
            "response has been submitted",
            "your response has been recorded",
            "thanks",
            "thank you",
            "submitted",
            "submit another response",
        ]
        try:
            page.wait_for_load_state("domcontentloaded", timeout=FIELD_TIMEOUT_MS)
        except TimeoutError:
            pass
        return self._wait_for_any_visible_text(page, confirmation_texts, SUBMIT_CONFIRMATION_TIMEOUT_MS)

    def _next_button_candidates(self, page: Page) -> list[tuple[str, Locator]]:
        return [
            ("automation_id", page.locator("[data-automation-id='nextButton']").first),
            ("role_exact", page.get_by_role("button", name=re.compile(r"^\s*Next\s*$", re.I)).first),
            ("aria_label", page.locator("button[aria-label*='Next'], [role='button'][aria-label*='Next']").first),
            ("button_text", page.locator("button").filter(has_text=re.compile(r"^\s*Next\s*$", re.I)).first),
            ("role_text", page.locator("[role='button']").filter(has_text=re.compile(r"^\s*Next\s*$", re.I)).first),
        ]

    def _wait_for_enabled(self, page: Page, locator: Locator) -> None:
        handle = locator.element_handle(timeout=FIELD_TIMEOUT_MS)
        if handle is None:
            raise TimeoutError("Button was not attached.")
        page.wait_for_function(
            """
            element => !element.disabled
                && element.getAttribute("aria-disabled") !== "true"
                && !element.closest("[aria-disabled='true']")
            """,
            arg=handle,
            timeout=FIELD_TIMEOUT_MS,
        )

    def _wait_for_page_transition(self, page: Page, before_text: str, expected_texts: list[str]) -> bool:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=FIELD_TIMEOUT_MS)
        except TimeoutError:
            pass

        before_normalized = self._normalize_text(before_text)
        new_expected_texts = [
            text for text in expected_texts if self._normalize_text(text) not in before_normalized
        ]
        if new_expected_texts and self._wait_for_any_visible_text(page, new_expected_texts, PAGE_TRANSITION_TIMEOUT_MS):
            return True

        try:
            page.wait_for_function(
                """
                before => document.body
                    && document.body.innerText
                    && document.body.innerText.trim() !== before.trim()
                """,
                arg=before_text,
                timeout=PAGE_TRANSITION_TIMEOUT_MS,
            )
            return True
        except (TimeoutError, Error):
            return False

    def _wait_for_any_visible_text(self, page: Page, texts: list[str], timeout_ms: int) -> bool:
        try:
            page.wait_for_function(
                """
                texts => {
                    const normalize = value => (value || "")
                        .toLowerCase()
                        .replace(/[^a-z0-9]+/g, " ")
                        .trim();
                    const body = normalize(document.body?.innerText || "");
                    return texts.some(text => body.includes(normalize(text)));
                }
                """,
                arg=texts,
                timeout=timeout_ms,
            )
            return True
        except (TimeoutError, Error):
            return False

    def _wait_for_form_ready(self, page: Page) -> None:
        expected = [
            self.checkout_fields["resident_name"],
            self.checkout_fields["room_number"],
            self.checkout_fields["tech_id"],
        ]
        if self._wait_for_any_visible_text(page, expected, PAGE_TRANSITION_TIMEOUT_MS):
            return
        page.wait_for_selector(
            "form, [data-automation-id='questionItem'], [role='listitem']",
            timeout=PAGE_TRANSITION_TIMEOUT_MS,
        )

    def _expected_after_top_level_texts(self, draft: FormDraft, has_damage_sections: bool) -> list[str]:
        if not has_damage_sections:
            return [self.checkout_fields["has_bathroom"]]

        texts: list[str] = []
        for section in draft.sections:
            section_mapping = get_damage_section(section.category_key)
            if section_mapping.get("live_form_enabled") is False:
                continue
            texts.extend(self._question_texts(section_mapping, section.question))
            if len(texts) >= 3:
                break
        texts.append(self.checkout_fields["has_bathroom"])
        return list(dict.fromkeys(text for text in texts if text))

    def _summarize_results(
        self,
        top_level_results: list[dict[str, Any]],
        sections: list[dict[str, Any]],
    ) -> dict[str, list[Any]]:
        fields_completed: list[str] = []
        fields_failed: list[str] = []
        errors: list[dict[str, Any]] = []

        for result in top_level_results:
            self._collect_field_result(result, fields_completed, fields_failed, errors)

        for section in sections:
            for result in section.get("field_results", []):
                self._collect_field_result(result, fields_completed, fields_failed, errors)
            for error in section.get("errors", []):
                if not isinstance(error, dict):
                    continue
                if error not in errors:
                    errors.append(error)
                field = error.get("field")
                if field and field not in fields_failed and field not in fields_completed:
                    fields_failed.append(field)

        return {
            "fields_completed": fields_completed,
            "fields_failed": fields_failed,
            "errors": errors,
        }

    @staticmethod
    def _collect_field_result(
        result: dict[str, Any],
        fields_completed: list[str],
        fields_failed: list[str],
        errors: list[dict[str, Any]],
    ) -> None:
        field = result.get("field")
        if not field:
            return
        if result.get("ok"):
            fields_completed.append(field)
            return
        fields_failed.append(field)
        error = result.get("error")
        if isinstance(error, dict):
            errors.append(error)
        elif error:
            errors.append({"field": field, "message": str(error)})

    def _field_result(
        self,
        *,
        field: str,
        ok: bool,
        operation: str,
        error: dict[str, Any] | None,
        labels: list[str] | None = None,
        question: str | None = None,
        choice: str | None = None,
        selector_strategy: str | None = None,
        duration_ms: int | None = None,
    ) -> dict[str, Any]:
        return {
            "field": field,
            "ok": ok,
            "operation": operation,
            "labels": labels,
            "question": question,
            "choice": choice,
            "selector_strategy": selector_strategy,
            "duration_ms": duration_ms,
            "error": error,
        }

    def _skipped_field_result(
        self,
        field: str,
        operation: str,
        reason: str,
        *,
        labels: list[str] | None = None,
        question: str | None = None,
        choice: str | None = None,
        selector_strategy: str | None = None,
        duration_ms: int | None = None,
    ) -> dict[str, Any]:
        result = self._field_result(
            field=field,
            ok=True,
            operation=operation,
            labels=labels,
            question=question,
            choice=choice,
            error=None,
            selector_strategy=selector_strategy,
            duration_ms=duration_ms,
        )
        result["skipped"] = True
        result["skip_reason"] = reason
        return result

    @staticmethod
    def _error_payload(
        *,
        field: str,
        operation: str,
        message: str,
        labels: list[str] | None = None,
        question: str | None = None,
        choice: str | None = None,
        screenshot: str | None = None,
    ) -> dict[str, Any]:
        return {
            "field": field,
            "operation": operation,
            "message": message,
            "labels": labels,
            "question": question,
            "choice": choice,
            "screenshot": screenshot,
        }

    def _save_debug_screenshot(self, page: Page, name: str, *, force: bool = False) -> str | None:
        if not force and not self.debug_enabled:
            return None
        try:
            debug_dir = Path(settings.playwright_debug_dir)
            if self._active_session_id is not None:
                debug_dir = debug_dir / f"session_{self._active_session_id}"
            debug_dir.mkdir(parents=True, exist_ok=True)
            safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", name).strip("_") or "failure"
            path = debug_dir / f"{safe_name}_{int(monotonic() * 1000)}.png"
            page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception:
            return None

    def _hold_browser_for_manual_review(self, page: Page) -> None:
        try:
            page.wait_for_event("close", timeout=settings.playwright_manual_review_timeout_seconds * 1000)
        except TimeoutError:
            return
        except Error:
            return

    def _isolated_storage_state(self, storage_state_path: Path) -> dict[str, Any]:
        with storage_state_path.open("r", encoding="utf-8") as state_file:
            state = json.load(state_file)

        isolated = dict(state)
        isolated["origins"] = [
            origin
            for origin in state.get("origins", [])
            if not self._is_form_storage_origin(origin.get("origin", ""))
        ]
        return isolated

    @staticmethod
    def _is_form_storage_origin(origin: str) -> bool:
        normalized = origin.lower()
        return any(host in normalized for host in FORM_STORAGE_HOSTS)

    def _fresh_form_url(self, session_id: int) -> str:
        parts = urlsplit(self.form_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["_clawbot_session"] = str(session_id)
        query["_clawbot_run"] = str(int(monotonic() * 1000))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    @staticmethod
    def _route_request(route) -> None:
        if route.request.resource_type in {"image", "media", "font"}:
            route.abort()
            return
        route.continue_()

    @staticmethod
    def _launch_args() -> list[str]:
        return [
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-breakpad",
            "--disable-client-side-phishing-detection",
            "--disable-component-extensions-with-background-pages",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--disable-features=AutofillServerCommunication,Translate",
            "--disable-gpu",
            "--disable-renderer-backgrounding",
            "--disable-sync",
            "--metrics-recording-only",
            "--no-first-run",
            "--no-sandbox",
        ]

    def _capture_console_message(self, message) -> None:
        if not self.debug_enabled:
            return
        try:
            self.console_messages.append(
                {
                    "type": message.type,
                    "text": message.text[:1000],
                    "location": json.dumps(message.location),
                }
            )
        except Error:
            return

    def _debug(self, event: str, **details: Any) -> None:
        if not self.debug_enabled:
            return
        payload = {"event": event, **details}
        self.debug_events.append(payload)
        logger.info("playwright.%s %s", event, details)

    @staticmethod
    @contextmanager
    def _timed(timings: dict[str, float], name: str):
        started = monotonic()
        try:
            yield
        finally:
            timings[name] = round(monotonic() - started, 3)

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((monotonic() - started) * 1000)

    @staticmethod
    def _as_texts(text_or_texts: str | list[str]) -> list[str]:
        if isinstance(text_or_texts, str):
            return [text_or_texts]
        return [text for text in text_or_texts if text]

    @staticmethod
    def _format_question_text(text_or_texts: str | list[str]) -> str:
        texts = MicrosoftFormFiller._as_texts(text_or_texts)
        return texts[0] if texts else ""

    @staticmethod
    def _question_texts(section_mapping: dict[str, Any], draft_question: str) -> list[str]:
        texts = [section_mapping.get("yes_no_question", ""), draft_question]
        texts.extend(section_mapping.get("question_aliases", []))
        return list(dict.fromkeys(text for text in texts if text))

    @staticmethod
    def _escape_regex(value: str) -> str:
        return re.escape(value)

    @staticmethod
    def _css_string(value: str) -> str:
        return json.dumps(value)

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

    @staticmethod
    def _visible_page_text(page: Page) -> str:
        try:
            return page.evaluate("document.body ? document.body.innerText : ''")
        except Error:
            return ""

    @staticmethod
    def _scroll_to_top(page: Page) -> None:
        try:
            page.evaluate("window.scrollTo(0, 0)")
        except Error:
            return
