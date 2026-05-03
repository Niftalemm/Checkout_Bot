import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.entities import (
    CheckoutSession,
    DamageImage,
    DamageItem,
    PendingDamageCapture,
    PendingDamageImage,
)
from app.schemas.session import (
    DamageSuggestion,
    DiscordSessionStart,
    PendingCaptureState,
    PendingDamageCaptureResponse,
    ReviewSection,
    ReviewSummary,
    SessionCreate,
    SessionDetailsUpdate,
    SessionSummary,
)
from app.services.damage_ai import DamageAIService, DamageAIResult
from app.services.form_mapping import get_damage_section, get_damage_sections
from app.services.pricing import PricingEngine, PricingSuggestion
from app.storage.image_store import LocalImageStore

STATUS_PENDING_DETAILS = "pending_details"
STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"
STATUS_COMPLETE_FAILED = "complete_failed"

FORM_FILL_NOT_REQUESTED = "not_requested"
FORM_FILL_AWAITING_APPROVAL = "awaiting_approval"
FORM_FILL_PENDING = "pending"
FORM_FILL_SUCCESS = "success"
FORM_FILL_PARTIAL_FAILURE = "partial_failure"
FORM_FILL_FAILED = "failed"
FORM_FILL_SKIPPED = "skipped"

CAPTURE_STATUS_AWAITING_DESCRIPTION = "awaiting_description"
CAPTURE_STATUS_PENDING = "pending"


class ServiceError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class CheckoutService:
    def __init__(
        self,
        db: Session,
        pricing: PricingEngine,
        image_store: LocalImageStore,
        ai_service: DamageAIService | None = None,
    ):
        self.db = db
        self.pricing = pricing
        self.image_store = image_store
        self.ai_service = ai_service

    @staticmethod
    def _dump_suggestions(suggestions: list[DamageSuggestion]) -> str:
        return json.dumps([suggestion.model_dump() for suggestion in suggestions])

    @staticmethod
    def _load_suggestions(payload: str) -> list[DamageSuggestion]:
        suggestions: list[DamageSuggestion] = []
        for item in json.loads(payload or "[]"):
            estimated_cost = float(item.get("estimated_cost", item.get("total_cost", 0.0)) or 0.0)
            suggestions.append(
                DamageSuggestion(
                    category_key=item["category_key"],
                    category_name=item["category_name"],
                    pricing_name=item.get("pricing_name"),
                    confidence=float(item.get("confidence", 0.0) or 0.0),
                    quantity=int(item.get("quantity", 1) or 1),
                    unit_cost=float(item.get("unit_cost", estimated_cost) or 0.0),
                    total_cost=float(item.get("total_cost", estimated_cost) or 0.0),
                    estimated_cost=estimated_cost,
                    chargeable=bool(item.get("chargeable", True)),
                )
            )
        return suggestions

    def _list_damage_query(self, session_id: int):
        return (
            select(DamageItem)
            .options(joinedload(DamageItem.images))
            .where(DamageItem.session_id == session_id)
            .order_by(DamageItem.created_at.asc())
        )

    def _get_damage_item(self, session_id: int, item_id: int) -> DamageItem:
        item = (
            self.db.execute(
                select(DamageItem)
                .options(joinedload(DamageItem.images))
                .where(DamageItem.id == item_id, DamageItem.session_id == session_id)
            )
            .unique()
            .scalars()
            .first()
        )
        if not item:
            raise ServiceError("Damage item not found for this session.", status_code=404)
        return item

    def _get_pending_capture_for_update(
        self,
        session_id: int,
        capture_id: int,
        allowed_statuses: tuple[str, ...] = (CAPTURE_STATUS_PENDING,),
    ) -> PendingDamageCapture:
        capture = (
            self.db.execute(
                select(PendingDamageCapture)
                .options(joinedload(PendingDamageCapture.images))
                .where(
                    PendingDamageCapture.id == capture_id,
                    PendingDamageCapture.session_id == session_id,
                    PendingDamageCapture.status.in_(allowed_statuses),
                )
            )
            .unique()
            .scalars()
            .first()
        )
        if not capture:
            raise ServiceError("Pending damage capture not found.", status_code=404)
        return capture

    def _require_active_session(self, session_id: int) -> CheckoutSession:
        session = self.get_session(session_id)
        if not session:
            raise ServiceError("Session not found.", status_code=404)
        if session.status != STATUS_ACTIVE:
            raise ServiceError("This checkout is no longer editable.", status_code=409)
        return session

    def _analyze_damage(self, raw_note: str) -> DamageAIResult:
        if self.ai_service is None:
            return DamageAIService.fallback_analyze(raw_note)
        return self.ai_service.analyze_damage(raw_note)

    @staticmethod
    def _primary_image(item: DamageItem) -> DamageImage | None:
        if not item.images:
            return None
        ordered = sorted(item.images, key=lambda image: (not image.is_primary, image.sort_order, image.id))
        return ordered[0]

    @staticmethod
    def _primary_pending_image(capture: PendingDamageCapture) -> PendingDamageImage | None:
        if not capture.images:
            return None
        return sorted(capture.images, key=lambda image: (image.sort_order, image.id))[0]

    def _sync_primary_image_path(self, item: DamageItem) -> None:
        primary = self._primary_image(item)
        item.image_path = primary.file_path if primary else None

    def _sync_pending_image_path(self, capture: PendingDamageCapture) -> None:
        primary = self._primary_pending_image(capture)
        capture.image_temp_path = primary.file_path if primary else ""

    @staticmethod
    def _serialize_damage_suggestions(suggestions: list[PricingSuggestion]) -> list[DamageSuggestion]:
        return [
            DamageSuggestion(
                category_key=suggestion.category_key,
                category_name=suggestion.category_name,
                pricing_name=suggestion.pricing_name,
                confidence=suggestion.confidence,
                quantity=suggestion.quantity,
                unit_cost=suggestion.unit_cost,
                total_cost=suggestion.total_cost,
                estimated_cost=suggestion.estimated_cost,
                chargeable=suggestion.chargeable,
            )
            for suggestion in suggestions
        ]

    def _pending_capture_state(self, capture: PendingDamageCapture) -> PendingCaptureState:
        suggestions = self._load_suggestions(capture.suggestion_options_json)
        return PendingCaptureState(
            status=capture.status,
            awaiting_description=capture.status == CAPTURE_STATUS_AWAITING_DESCRIPTION,
            capture_id=capture.id,
            original_description=capture.raw_note or None,
            cleaned_description=capture.cleaned_description or None,
            quantity=None if capture.status == CAPTURE_STATUS_AWAITING_DESCRIPTION else capture.quantity,
            unit_cost=None if capture.status == CAPTURE_STATUS_AWAITING_DESCRIPTION else capture.unit_cost,
            total_cost=None if capture.status == CAPTURE_STATUS_AWAITING_DESCRIPTION else capture.total_cost,
            chargeable=None if capture.status == CAPTURE_STATUS_AWAITING_DESCRIPTION else capture.chargeable,
            guessed_category_key=capture.suggested_section or None,
            guessed_category_name=capture.suggested_category or None,
            guessed_confidence=(
                None if capture.status == CAPTURE_STATUS_AWAITING_DESCRIPTION else capture.suggested_confidence
            ),
            suggestions=suggestions,
            image_count=len(capture.images),
        )

    def _capture_response(
        self,
        capture: PendingDamageCapture,
        suggestions: list[DamageSuggestion] | None = None,
        requires_explicit_choice: bool = False,
        prompt: str = "",
    ) -> PendingDamageCaptureResponse:
        awaiting_description = capture.status == CAPTURE_STATUS_AWAITING_DESCRIPTION
        return PendingDamageCaptureResponse(
            status=capture.status,
            awaiting_description=awaiting_description,
            capture_id=capture.id,
            original_description=capture.raw_note or None,
            cleaned_description=capture.cleaned_description or None,
            quantity=None if awaiting_description else capture.quantity,
            unit_cost=None if awaiting_description else capture.unit_cost,
            total_cost=None if awaiting_description else capture.total_cost,
            chargeable=None if awaiting_description else capture.chargeable,
            guessed_category_key=capture.suggested_section or None,
            guessed_category_name=capture.suggested_category or None,
            guessed_confidence=None if awaiting_description else capture.suggested_confidence,
            estimated_cost=None if awaiting_description else capture.total_cost,
            suggestions=suggestions or [],
            requires_explicit_choice=requires_explicit_choice,
            prompt=prompt,
            image_count=len(capture.images),
        )

    @staticmethod
    def _prompt_for_suggestions(primary: PricingSuggestion) -> str:
        if primary.confidence < 0.6:
            return "I am not very confident about this one. Pick the best match from the list below."
        return "The top match is listed first below."

    def create_session(self, payload: SessionCreate) -> CheckoutSession:
        session = CheckoutSession(
            **payload.model_dump(),
            status=STATUS_ACTIVE,
            source="api",
            draft_saved=False,
            form_fill_status=FORM_FILL_NOT_REQUESTED,
        )
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        return session

    def start_discord_session(self, payload: DiscordSessionStart) -> CheckoutSession:
        existing = self.get_channel_session(payload.channel_id)
        if existing:
            raise ServiceError(
                "A checkout is already active in this channel. Complete it before starting a new one.",
                status_code=409,
            )

        session = CheckoutSession(
            resident_name="",
            room_number="",
            tech_id="",
            hall="",
            staff_name="Nift",
            room_side="",
            status=STATUS_PENDING_DETAILS,
            started_by=str(payload.started_by),
            channel_id=str(payload.channel_id),
            source=payload.source,
            draft_saved=False,
            form_fill_status=FORM_FILL_NOT_REQUESTED,
        )
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        return session

    def get_session(self, session_id: int) -> CheckoutSession | None:
        return self.db.get(CheckoutSession, session_id)

    def get_channel_session(self, channel_id: int) -> CheckoutSession | None:
        return (
            self.db.execute(
                select(CheckoutSession)
                .where(
                    CheckoutSession.channel_id == str(channel_id),
                    CheckoutSession.status.in_([STATUS_PENDING_DETAILS, STATUS_ACTIVE]),
                )
                .order_by(CheckoutSession.created_at.desc())
            )
            .scalars()
            .first()
        )

    def update_session_details(self, session_id: int, payload: SessionDetailsUpdate) -> CheckoutSession:
        session = self.get_session(session_id)
        if not session:
            raise ServiceError("Session not found.", status_code=404)
        if session.status not in {STATUS_PENDING_DETAILS, STATUS_ACTIVE}:
            raise ServiceError("This checkout can no longer be updated.", status_code=409)

        for key, value in payload.model_dump().items():
            setattr(session, key, value)

        session.status = STATUS_ACTIVE
        session.form_fill_status = FORM_FILL_NOT_REQUESTED
        session.form_fill_error = None
        session.form_fill_result = None
        self.db.commit()
        self.db.refresh(session)
        return session

    def get_pending_capture(self, session_id: int) -> PendingDamageCapture | None:
        return (
            self.db.execute(
                select(PendingDamageCapture)
                .options(joinedload(PendingDamageCapture.images))
                .where(
                    PendingDamageCapture.session_id == session_id,
                    PendingDamageCapture.status.in_(
                        [CAPTURE_STATUS_PENDING, CAPTURE_STATUS_AWAITING_DESCRIPTION]
                    ),
                )
                .order_by(PendingDamageCapture.created_at.desc())
            )
            .unique()
            .scalars()
            .first()
        )

    def capture_damage(
        self,
        session_id: int,
        raw_note: str,
        image_files: list | None = None,
        image_name_hints: list[str] | None = None,
    ) -> PendingDamageCaptureResponse:
        session = self._require_active_session(session_id)
        if self.get_pending_capture(session_id):
            raise ServiceError(
                "Please finish the current pending damage before creating another one.",
                status_code=409,
            )

        note = raw_note.strip()
        uploads = list(image_files or [])
        primary_image_name = (image_name_hints or [None])[0]
        if not note and not uploads:
            raise ServiceError("Send at least one image or a short description.", status_code=400)

        if not note:
            capture = PendingDamageCapture(
                session_id=session_id,
                raw_note="",
                cleaned_description="",
                quantity=1,
                unit_cost=0.0,
                total_cost=0.0,
                chargeable=True,
                parsed_item=None,
                parsed_damage_type=None,
                parsed_confidence=None,
                image_temp_path="",
                suggested_category="",
                suggested_section="",
                suggested_confidence=0.0,
                suggested_cost=0.0,
                pricing_name=None,
                ai_provider=None,
                ai_model=None,
                suggestion_options_json="[]",
                image_name_hint=primary_image_name,
                status=CAPTURE_STATUS_AWAITING_DESCRIPTION,
            )
            session.form_fill_status = FORM_FILL_NOT_REQUESTED
            session.form_fill_error = None
            session.form_fill_result = None
            self.db.add(capture)
            self.db.flush()

            for index, image_file in enumerate(uploads):
                try:
                    image_path, stored_name = self.image_store.save_pending_image(
                        image_file,
                        session_id,
                        "unclassified",
                    )
                except ValueError as exc:
                    raise ServiceError(str(exc), status_code=400) from exc
                capture.images.append(
                    PendingDamageImage(
                        pending_capture_id=capture.id,
                        file_path=image_path,
                        sort_order=index,
                    )
                )
                if index == 0:
                    capture.image_temp_path = image_path
                    capture.image_name_hint = stored_name

            self._sync_pending_image_path(capture)
            self.db.commit()
            self.db.refresh(capture)
            return self._capture_response(capture, prompt="Awaiting description.")

        analysis = self._analyze_damage(note)
        suggestions = self.pricing.suggest(note, image_name_hint=primary_image_name, analysis=analysis, limit=3)
        primary = suggestions[0]

        capture = PendingDamageCapture(
            session_id=session_id,
            raw_note=note,
            cleaned_description=analysis.cleaned_description,
            quantity=primary.quantity,
            unit_cost=primary.unit_cost,
            total_cost=primary.total_cost,
            chargeable=primary.chargeable,
            parsed_item=analysis.item,
            parsed_damage_type=analysis.damage_type,
            parsed_confidence=analysis.confidence,
            image_temp_path="",
            suggested_category=primary.category_name,
            suggested_section=primary.category_key,
            suggested_confidence=primary.confidence,
            suggested_cost=primary.total_cost,
            pricing_name=primary.pricing_name,
            ai_provider=analysis.provider,
            ai_model=analysis.model,
            suggestion_options_json=self._dump_suggestions(self._serialize_damage_suggestions(suggestions)),
            image_name_hint=primary_image_name,
            status=CAPTURE_STATUS_PENDING,
        )
        session.form_fill_status = FORM_FILL_NOT_REQUESTED
        session.form_fill_error = None
        session.form_fill_result = None
        self.db.add(capture)
        self.db.flush()

        for index, image_file in enumerate(uploads):
            try:
                image_path, stored_name = self.image_store.save_pending_image(
                    image_file, session_id, primary.category_key
                )
            except ValueError as exc:
                raise ServiceError(str(exc), status_code=400) from exc
            capture.images.append(
                PendingDamageImage(
                    pending_capture_id=capture.id,
                    file_path=image_path,
                    sort_order=index,
                )
            )
            if index == 0:
                capture.image_temp_path = image_path
                capture.image_name_hint = stored_name

        self._sync_pending_image_path(capture)
        self.db.commit()
        self.db.refresh(capture)

        return self._capture_response(
            capture,
            suggestions=self._serialize_damage_suggestions(suggestions),
            requires_explicit_choice=primary.confidence < 0.6,
            prompt=self._prompt_for_suggestions(primary),
        )

    def add_pending_capture_image(self, session_id: int, capture_id: int, image_file) -> PendingCaptureState:
        self._require_active_session(session_id)
        capture = self._get_pending_capture_for_update(
            session_id,
            capture_id,
            allowed_statuses=(CAPTURE_STATUS_PENDING, CAPTURE_STATUS_AWAITING_DESCRIPTION),
        )
        primary_category_key = capture.suggested_section or "unclassified"

        try:
            image_path, stored_name = self.image_store.save_pending_image(image_file, session_id, primary_category_key)
        except ValueError as exc:
            raise ServiceError(str(exc), status_code=400) from exc

        capture.images.append(
            PendingDamageImage(
                pending_capture_id=capture.id,
                file_path=image_path,
                sort_order=len(capture.images),
            )
        )
        if not capture.image_name_hint:
            capture.image_name_hint = stored_name
        self._sync_pending_image_path(capture)
        self.db.commit()
        self.db.refresh(capture)
        return self._pending_capture_state(capture)

    def describe_pending_capture(
        self,
        session_id: int,
        capture_id: int,
        raw_note: str,
        audio_file=None,
    ) -> PendingDamageCaptureResponse:
        self._require_active_session(session_id)
        capture = self._get_pending_capture_for_update(
            session_id,
            capture_id,
            allowed_statuses=(CAPTURE_STATUS_AWAITING_DESCRIPTION,),
        )

        note = raw_note.strip()
        if not note and audio_file is not None:
            if self.ai_service is None:
                raise ServiceError(
                    "Voice note transcription is not configured yet. Send a text description instead.",
                    status_code=400,
                )
            payload = audio_file.file.read()
            try:
                note = self.ai_service.transcribe_audio(
                    audio_file.filename or "voice-note.m4a",
                    payload,
                    audio_file.content_type,
                )
            except ValueError as exc:
                raise ServiceError(str(exc), status_code=400) from exc

        note = note.strip()
        if not note:
            raise ServiceError(
                "Send a short text description or a voice note so I can match this damage.",
                status_code=400,
            )

        analysis = self._analyze_damage(note)
        primary_image_name = capture.image_name_hint
        suggestions = self.pricing.suggest(note, image_name_hint=primary_image_name, analysis=analysis, limit=3)
        primary = suggestions[0]

        capture.raw_note = note
        capture.cleaned_description = analysis.cleaned_description
        capture.quantity = primary.quantity
        capture.unit_cost = primary.unit_cost
        capture.total_cost = primary.total_cost
        capture.chargeable = primary.chargeable
        capture.parsed_item = analysis.item
        capture.parsed_damage_type = analysis.damage_type
        capture.parsed_confidence = analysis.confidence
        capture.suggested_category = primary.category_name
        capture.suggested_section = primary.category_key
        capture.suggested_confidence = primary.confidence
        capture.suggested_cost = primary.total_cost
        capture.pricing_name = primary.pricing_name
        capture.ai_provider = analysis.provider
        capture.ai_model = analysis.model
        capture.suggestion_options_json = self._dump_suggestions(
            self._serialize_damage_suggestions(suggestions)
        )
        capture.status = CAPTURE_STATUS_PENDING

        ordered_images = sorted(capture.images, key=lambda image: (image.sort_order, image.id))
        if ordered_images and not capture.image_name_hint:
            capture.image_name_hint = ordered_images[0].file_path.replace("\\", "/").rsplit("/", 1)[-1]

        self.db.commit()
        self.db.refresh(capture)
        return self._capture_response(
            capture,
            suggestions=self._serialize_damage_suggestions(suggestions),
            requires_explicit_choice=primary.confidence < 0.6,
            prompt=self._prompt_for_suggestions(primary),
        )

    def cancel_pending_capture(self, session_id: int, capture_id: int) -> None:
        self._require_active_session(session_id)
        capture = self._get_pending_capture_for_update(
            session_id,
            capture_id,
            allowed_statuses=(CAPTURE_STATUS_PENDING, CAPTURE_STATUS_AWAITING_DESCRIPTION),
        )
        for image in capture.images:
            self.image_store.delete_image_file(image.file_path)
        self.db.delete(capture)
        self.db.commit()

    def confirm_damage_capture(
        self,
        session_id: int,
        capture_id: int,
        selection_index: int | None = None,
        category_key: str | None = None,
    ) -> DamageItem:
        self._require_active_session(session_id)
        capture = self._get_pending_capture_for_update(session_id, capture_id)
        analysis = DamageAIResult(
            cleaned_description=capture.cleaned_description,
            item=capture.parsed_item,
            damage_type=capture.parsed_damage_type,
            quantity=capture.quantity,
            confidence=capture.parsed_confidence or 0.0,
            chargeable=capture.chargeable,
            provider=capture.ai_provider,
            model=capture.ai_model,
        )
        suggestions = self._load_suggestions(capture.suggestion_options_json)

        if selection_index is not None:
            if selection_index < 0 or selection_index >= len(suggestions):
                raise ServiceError("That suggestion number was not valid.", status_code=400)
            selected = suggestions[selection_index]
        else:
            target_category_key = category_key or capture.suggested_section
            try:
                get_damage_section(target_category_key)
            except KeyError as exc:
                raise ServiceError("Unknown damage category.", status_code=400) from exc
            selected = self._serialize_damage_suggestions(
                [
                    self.pricing.choose_category(
                        capture.raw_note,
                        target_category_key,
                        image_name_hint=capture.image_name_hint,
                        analysis=analysis,
                    )
                ]
            )[0]

        section = get_damage_section(selected.category_key)
        confirmation_status = "confirmed" if selected.category_key == capture.suggested_section else "corrected"
        damage = DamageItem(
            session_id=session_id,
            raw_note=capture.raw_note,
            cleaned_description=capture.cleaned_description,
            quantity=selected.quantity,
            unit_cost=selected.unit_cost,
            total_cost=selected.total_cost,
            chargeable=selected.chargeable,
            estimated_cost=selected.total_cost,
            image_path=None,
            category=section["name"],
            form_section=section["key"],
            confirmation_status=confirmation_status,
            guessed_category=capture.suggested_category,
            guessed_confidence=capture.suggested_confidence,
            pricing_name=selected.pricing_name,
            ai_provider=capture.ai_provider,
            ai_model=capture.ai_model,
        )
        self.db.add(damage)
        self.db.flush()

        ordered_images = sorted(capture.images, key=lambda image: (image.sort_order, image.id))
        for index, pending_image in enumerate(ordered_images):
            try:
                confirmed_path = self.image_store.confirm_damage_image(
                    pending_image.file_path,
                    session_id,
                    section["key"],
                    item_id=damage.id,
                )
            except ValueError as exc:
                raise ServiceError(str(exc), status_code=400) from exc
            self.db.add(
                DamageImage(
                    damage_item_id=damage.id,
                    file_path=confirmed_path,
                    sort_order=index,
                    is_primary=index == 0,
                )
            )
            if index == 0:
                damage.image_path = confirmed_path

        capture.status = confirmation_status
        capture.confirmed_category = section["name"]
        capture.resolved_at = datetime.utcnow()
        self.db.commit()
        return self._get_damage_item(session_id, damage.id)

    def list_damage_items(self, session_id: int) -> list[DamageItem]:
        self._require_active_session(session_id)
        return self.db.execute(self._list_damage_query(session_id)).unique().scalars().all()

    def update_damage_description(self, session_id: int, item_id: int, raw_note: str) -> DamageItem:
        self._require_active_session(session_id)
        item = self._get_damage_item(session_id, item_id)
        note = raw_note.strip()
        if not note:
            raise ServiceError("Description cannot be empty.", status_code=400)

        analysis = self._analyze_damage(note)
        selection = self.pricing.choose_category(
            note,
            item.form_section,
            image_name_hint=item.image_path,
            analysis=analysis,
        )
        item.raw_note = note
        item.cleaned_description = analysis.cleaned_description
        item.quantity = selection.quantity
        item.unit_cost = selection.unit_cost
        item.total_cost = selection.total_cost
        item.chargeable = selection.chargeable
        item.estimated_cost = selection.total_cost
        item.pricing_name = selection.pricing_name
        item.ai_provider = analysis.provider
        item.ai_model = analysis.model
        self.db.commit()
        return self._get_damage_item(session_id, item_id)

    def update_damage_category(self, session_id: int, item_id: int, category_key: str) -> DamageItem:
        self._require_active_session(session_id)
        item = self._get_damage_item(session_id, item_id)
        try:
            section = get_damage_section(category_key)
        except KeyError as exc:
            raise ServiceError("Unknown damage category.", status_code=400) from exc

        analysis = self._analyze_damage(item.raw_note)
        selection = self.pricing.choose_category(
            item.raw_note,
            category_key,
            image_name_hint=item.image_path,
            analysis=analysis,
        )
        item.category = section["name"]
        item.form_section = section["key"]
        item.cleaned_description = analysis.cleaned_description
        item.quantity = selection.quantity
        item.unit_cost = selection.unit_cost
        item.total_cost = selection.total_cost
        item.chargeable = selection.chargeable
        item.estimated_cost = selection.total_cost
        item.pricing_name = selection.pricing_name
        item.confirmation_status = "corrected"
        item.ai_provider = analysis.provider
        item.ai_model = analysis.model

        for image in item.images:
            new_path = self.image_store.relocate_confirmed_image(image.file_path, session_id, category_key)
            image.file_path = new_path

        self._sync_primary_image_path(item)
        self.db.commit()
        return self._get_damage_item(session_id, item_id)

    def delete_damage_item(self, session_id: int, item_id: int) -> None:
        self._require_active_session(session_id)
        item = self._get_damage_item(session_id, item_id)
        for image in list(item.images):
            self.image_store.delete_image_file(image.file_path)
        self.db.delete(item)
        self.db.commit()

    def add_damage_item_image(self, session_id: int, item_id: int, image_file) -> DamageItem:
        self._require_active_session(session_id)
        item = self._get_damage_item(session_id, item_id)
        try:
            image_path = self.image_store.save_confirmed_image(
                image_file,
                session_id,
                item.form_section,
                item_id=item.id,
            )
        except ValueError as exc:
            raise ServiceError(str(exc), status_code=400) from exc

        image = DamageImage(
            file_path=image_path,
            sort_order=len(item.images),
            is_primary=not item.images,
        )
        item.images.append(image)
        self.db.add(image)
        self.db.flush()
        self._sync_primary_image_path(item)
        self.db.commit()
        return self._get_damage_item(session_id, item_id)

    def remove_damage_item_image(self, session_id: int, item_id: int, image_id: int) -> DamageItem:
        self._require_active_session(session_id)
        item = self._get_damage_item(session_id, item_id)
        image = next((candidate for candidate in item.images if candidate.id == image_id), None)
        if not image:
            raise ServiceError("Damage image not found for this item.", status_code=404)

        self.image_store.delete_image_file(image.file_path)
        self.db.delete(image)
        self.db.flush()
        remaining = sorted(
            [candidate for candidate in item.images if candidate.id != image_id],
            key=lambda candidate: (candidate.sort_order, candidate.id),
        )
        for index, candidate in enumerate(remaining):
            candidate.sort_order = index
            candidate.is_primary = index == 0
        item.images = remaining
        self._sync_primary_image_path(item)
        self.db.commit()
        return self._get_damage_item(session_id, item_id)

    def summarize_session(self, session_id: int) -> SessionSummary:
        session = self.get_session(session_id)
        if not session:
            raise ServiceError("Session not found.", status_code=404)

        items = self.db.execute(self._list_damage_query(session_id)).unique().scalars().all()
        for item in items:
            self._sync_primary_image_path(item)
        total = sum(item.total_cost for item in items)
        return SessionSummary(
            session_id=session.id,
            resident_name=session.resident_name,
            room_number=session.room_number,
            tech_id=session.tech_id,
            hall=session.hall,
            staff_name=session.staff_name,
            room_side=session.room_side,
            status=session.status,
            total_estimated_cost=total,
            item_count=len(items),
            items=items,
        )

    def build_review_summary(self, session_id: int) -> ReviewSummary:
        session = self.get_session(session_id)
        if not session:
            raise ServiceError("Session not found.", status_code=404)
        if self.get_pending_capture(session_id):
            raise ServiceError(
                "Please confirm or cancel the current pending damage before reviewing the checkout.",
                status_code=409,
            )

        summary = self.summarize_session(session_id)
        items_by_section: dict[str, list[DamageItem]] = {}
        for item in summary.items:
            items_by_section.setdefault(item.form_section, []).append(item)

        sections = []
        for section in get_damage_sections():
            section_items = items_by_section.get(section["key"], [])
            sections.append(
                ReviewSection(
                    category_key=section["key"],
                    category_name=section["name"],
                    question=section["yes_no_question"],
                    guessed_confidence=max(
                        (item.guessed_confidence for item in section_items if item.guessed_confidence is not None),
                        default=None,
                    ),
                    has_damage=bool(section_items),
                    description="; ".join(item.cleaned_description for item in section_items),
                    estimated_cost=round(sum(item.total_cost for item in section_items), 2),
                    has_image=any(bool(item.image_path or item.images) for item in section_items),
                    damage_count=len(section_items),
                )
            )

        return ReviewSummary(
            session_id=session.id,
            resident_name=session.resident_name,
            room_number=session.room_number,
            tech_id=session.tech_id,
            hall=session.hall,
            staff_name=session.staff_name,
            room_side=session.room_side,
            has_damages=summary.item_count > 0,
            total_estimated_cost=summary.total_estimated_cost,
            item_count=summary.item_count,
            sections=sections,
        )

    def request_review(self, session_id: int) -> CheckoutSession:
        session = self.get_session(session_id)
        if not session:
            raise ServiceError("Session not found.", status_code=404)
        self.build_review_summary(session_id)
        session.draft_saved = True
        session.form_fill_status = FORM_FILL_AWAITING_APPROVAL
        session.form_fill_error = None
        session.form_fill_result = None
        self.db.commit()
        self.db.refresh(session)
        return session

    def cancel_review(self, session_id: int) -> CheckoutSession:
        session = self.get_session(session_id)
        if not session:
            raise ServiceError("Session not found.", status_code=404)
        session.form_fill_status = FORM_FILL_NOT_REQUESTED
        session.form_fill_error = None
        session.form_fill_result = None
        self.db.commit()
        self.db.refresh(session)
        return session

    def cancel_session(self, session_id: int) -> None:
        session = self.get_session(session_id)
        if not session:
            raise ServiceError("Session not found.", status_code=404)
        if session.status not in {STATUS_PENDING_DETAILS, STATUS_ACTIVE}:
            raise ServiceError("Only an active checkout can be canceled.", status_code=409)

        self.image_store.delete_session_images(session_id)
        self.db.delete(session)
        self.db.commit()

    def mark_form_fill_pending(self, session_id: int) -> CheckoutSession:
        session = self.get_session(session_id)
        if not session:
            raise ServiceError("Session not found.", status_code=404)
        session.draft_saved = True
        session.form_fill_status = FORM_FILL_PENDING
        session.form_fill_error = None
        self.db.commit()
        self.db.refresh(session)
        return session

    def mark_form_fill_success(self, session_id: int, result: dict, mark_completed: bool) -> CheckoutSession:
        session = self.get_session(session_id)
        if not session:
            raise ServiceError("Session not found.", status_code=404)
        session.draft_saved = True
        session.form_fill_status = FORM_FILL_SUCCESS
        session.form_fill_error = None
        session.form_fill_result = json.dumps(result)
        if mark_completed:
            session.status = STATUS_COMPLETED
            session.completed_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(session)
        return session

    def mark_form_fill_partial_failure(self, session_id: int, result: dict, error_message: str) -> CheckoutSession:
        session = self.get_session(session_id)
        if not session:
            raise ServiceError("Session not found.", status_code=404)
        session.draft_saved = True
        session.status = STATUS_COMPLETE_FAILED
        session.form_fill_status = FORM_FILL_PARTIAL_FAILURE
        session.form_fill_error = error_message
        session.form_fill_result = json.dumps(result)
        session.completed_at = None
        self.db.commit()
        self.db.refresh(session)
        return session

    def mark_form_fill_failure(
        self, session_id: int, error_message: str, result: dict | None = None
    ) -> CheckoutSession:
        session = self.get_session(session_id)
        if not session:
            raise ServiceError("Session not found.", status_code=404)
        session.draft_saved = True
        session.status = STATUS_COMPLETE_FAILED
        session.form_fill_status = FORM_FILL_FAILED
        session.form_fill_error = error_message
        session.form_fill_result = json.dumps(result) if result is not None else None
        session.completed_at = None
        self.db.commit()
        self.db.refresh(session)
        return session

    def mark_completed_without_fill(self, session_id: int) -> CheckoutSession:
        session = self.get_session(session_id)
        if not session:
            raise ServiceError("Session not found.", status_code=404)
        session.draft_saved = True
        session.status = STATUS_COMPLETED
        session.completed_at = datetime.utcnow()
        session.form_fill_status = FORM_FILL_SKIPPED
        session.form_fill_error = None
        self.db.commit()
        self.db.refresh(session)
        return session
