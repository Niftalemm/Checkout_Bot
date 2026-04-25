from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _apply_column_migrations(table_name: str, migrations: dict[str, str]) -> None:
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return

    existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
    statements = [sql for column_name, sql in migrations.items() if column_name not in existing_columns]
    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def bootstrap_database() -> None:
    Base.metadata.create_all(bind=engine)

    _apply_column_migrations(
        "checkout_sessions",
        {
            "status": (
                "ALTER TABLE checkout_sessions ADD COLUMN status "
                "VARCHAR(40) NOT NULL DEFAULT 'active'"
            ),
            "completed_at": "ALTER TABLE checkout_sessions ADD COLUMN completed_at DATETIME",
            "started_by": "ALTER TABLE checkout_sessions ADD COLUMN started_by VARCHAR(80)",
            "channel_id": "ALTER TABLE checkout_sessions ADD COLUMN channel_id VARCHAR(80)",
            "source": (
                "ALTER TABLE checkout_sessions ADD COLUMN source "
                "VARCHAR(40) NOT NULL DEFAULT 'api'"
            ),
            "form_fill_status": (
                "ALTER TABLE checkout_sessions ADD COLUMN form_fill_status "
                "VARCHAR(40) NOT NULL DEFAULT 'not_requested'"
            ),
            "form_fill_error": "ALTER TABLE checkout_sessions ADD COLUMN form_fill_error TEXT",
            "form_fill_result": "ALTER TABLE checkout_sessions ADD COLUMN form_fill_result TEXT",
            "draft_saved": (
                "ALTER TABLE checkout_sessions ADD COLUMN draft_saved "
                "BOOLEAN NOT NULL DEFAULT 0"
            ),
        },
    )
    _apply_column_migrations(
        "damage_items",
        {
            "quantity": "ALTER TABLE damage_items ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1",
            "unit_cost": "ALTER TABLE damage_items ADD COLUMN unit_cost FLOAT NOT NULL DEFAULT 0",
            "total_cost": "ALTER TABLE damage_items ADD COLUMN total_cost FLOAT NOT NULL DEFAULT 0",
            "chargeable": "ALTER TABLE damage_items ADD COLUMN chargeable BOOLEAN NOT NULL DEFAULT 1",
            "confirmation_status": (
                "ALTER TABLE damage_items ADD COLUMN confirmation_status "
                "VARCHAR(40) NOT NULL DEFAULT 'confirmed'"
            ),
            "guessed_category": "ALTER TABLE damage_items ADD COLUMN guessed_category VARCHAR(120)",
            "guessed_confidence": "ALTER TABLE damage_items ADD COLUMN guessed_confidence FLOAT",
            "pricing_name": "ALTER TABLE damage_items ADD COLUMN pricing_name VARCHAR(160)",
            "ai_provider": "ALTER TABLE damage_items ADD COLUMN ai_provider VARCHAR(40)",
            "ai_model": "ALTER TABLE damage_items ADD COLUMN ai_model VARCHAR(120)",
        },
    )
    _apply_column_migrations(
        "pending_damage_captures",
        {
            "quantity": (
                "ALTER TABLE pending_damage_captures ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1"
            ),
            "unit_cost": (
                "ALTER TABLE pending_damage_captures ADD COLUMN unit_cost FLOAT NOT NULL DEFAULT 0"
            ),
            "total_cost": (
                "ALTER TABLE pending_damage_captures ADD COLUMN total_cost FLOAT NOT NULL DEFAULT 0"
            ),
            "chargeable": (
                "ALTER TABLE pending_damage_captures ADD COLUMN chargeable BOOLEAN NOT NULL DEFAULT 1"
            ),
            "parsed_item": "ALTER TABLE pending_damage_captures ADD COLUMN parsed_item VARCHAR(160)",
            "parsed_damage_type": (
                "ALTER TABLE pending_damage_captures ADD COLUMN parsed_damage_type VARCHAR(80)"
            ),
            "parsed_confidence": (
                "ALTER TABLE pending_damage_captures ADD COLUMN parsed_confidence FLOAT"
            ),
            "ai_provider": "ALTER TABLE pending_damage_captures ADD COLUMN ai_provider VARCHAR(40)",
            "ai_model": "ALTER TABLE pending_damage_captures ADD COLUMN ai_model VARCHAR(120)",
        },
    )
    _backfill_damage_images()
    _backfill_damage_costs()
    _backfill_pending_damage_costs()
    _backfill_pending_damage_images()


def _backfill_damage_images() -> None:
    from app.models.entities import DamageImage

    with SessionLocal() as db:
        rows = db.execute(
            text(
                "SELECT id, image_path FROM damage_items "
                "WHERE image_path IS NOT NULL AND image_path != '' "
                "AND id NOT IN (SELECT damage_item_id FROM damage_images)"
            )
        ).all()
        if not rows:
            return

        for damage_item_id, image_path in rows:
            db.add(
                DamageImage(
                    damage_item_id=damage_item_id,
                    file_path=image_path,
                    sort_order=0,
                    is_primary=True,
                )
            )
        db.commit()


def _backfill_damage_costs() -> None:
    with SessionLocal() as db:
        db.execute(
            text(
                "UPDATE damage_items "
                "SET quantity = CASE WHEN quantity IS NULL OR quantity < 1 THEN 1 ELSE quantity END, "
                "unit_cost = CASE "
                "    WHEN (unit_cost IS NULL OR unit_cost = 0) AND (quantity IS NOT NULL AND quantity > 1) "
                "        THEN COALESCE(estimated_cost, 0) / quantity "
                "    WHEN unit_cost IS NULL THEN COALESCE(estimated_cost, 0) "
                "    ELSE unit_cost "
                "END, "
                "total_cost = CASE WHEN total_cost IS NULL OR total_cost = 0 THEN COALESCE(estimated_cost, 0) ELSE total_cost END, "
                "chargeable = CASE WHEN chargeable IS NULL THEN 1 ELSE chargeable END "
                "WHERE quantity IS NULL OR unit_cost IS NULL OR total_cost IS NULL OR chargeable IS NULL"
            )
        )
        db.execute(
            text(
                "UPDATE damage_items "
                "SET estimated_cost = total_cost "
                "WHERE total_cost IS NOT NULL AND estimated_cost != total_cost"
            )
        )
        db.commit()


def _backfill_pending_damage_costs() -> None:
    with SessionLocal() as db:
        db.execute(
            text(
                "UPDATE pending_damage_captures "
                "SET quantity = CASE WHEN quantity IS NULL OR quantity < 1 THEN 1 ELSE quantity END, "
                "unit_cost = CASE "
                "    WHEN (unit_cost IS NULL OR unit_cost = 0) AND (quantity IS NOT NULL AND quantity > 1) "
                "        THEN COALESCE(suggested_cost, 0) / quantity "
                "    WHEN unit_cost IS NULL THEN COALESCE(suggested_cost, 0) "
                "    ELSE unit_cost "
                "END, "
                "total_cost = CASE WHEN total_cost IS NULL OR total_cost = 0 THEN COALESCE(suggested_cost, 0) ELSE total_cost END, "
                "chargeable = CASE WHEN chargeable IS NULL THEN 1 ELSE chargeable END "
                "WHERE quantity IS NULL OR unit_cost IS NULL OR total_cost IS NULL OR chargeable IS NULL"
            )
        )
        db.commit()


def _backfill_pending_damage_images() -> None:
    from app.models.entities import PendingDamageImage

    with SessionLocal() as db:
        rows = db.execute(
            text(
                "SELECT id, image_temp_path FROM pending_damage_captures "
                "WHERE image_temp_path IS NOT NULL AND image_temp_path != '' "
                "AND id NOT IN (SELECT pending_capture_id FROM pending_damage_images)"
            )
        ).all()
        if not rows:
            return

        for pending_capture_id, file_path in rows:
            db.add(
                PendingDamageImage(
                    pending_capture_id=pending_capture_id,
                    file_path=file_path,
                    sort_order=0,
                )
            )
        db.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
