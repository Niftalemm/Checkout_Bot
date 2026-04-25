import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile


class LocalImageStore:
    allowed_extensions = {".jpg", ".jpeg", ".png", ".webp"}
    max_size_bytes = 10 * 1024 * 1024

    def __init__(self, uploads_dir: str):
        self.root = Path(uploads_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _slug(value: str) -> str:
        return "".join(character.lower() if character.isalnum() else "_" for character in value).strip("_")

    def _validate_image(self, file: UploadFile) -> tuple[bytes, str]:
        extension = (Path(file.filename or "image.jpg").suffix or ".jpg").lower()
        if extension not in self.allowed_extensions:
            raise ValueError("Image must be a JPG, PNG, or WEBP file.")
        if file.content_type and not file.content_type.lower().startswith("image/"):
            raise ValueError("Uploaded file must be an image.")

        payload = file.file.read()
        if not payload:
            raise ValueError("Uploaded image was empty.")
        if len(payload) > self.max_size_bytes:
            raise ValueError("Image must be 10 MB or smaller.")
        return payload, extension

    def save_pending_image(self, file: UploadFile, session_id: int, suggested_category_key: str) -> tuple[str, str]:
        payload, extension = self._validate_image(file)
        target_dir = (
            self.root / f"session_{session_id}" / "pending" / self._slug(suggested_category_key or "unclassified")
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{uuid4().hex}{extension}"
        with target_path.open("wb") as handle:
            handle.write(payload)
        return str(target_path), (file.filename or target_path.name)

    def confirm_damage_image(
        self,
        pending_path: str,
        session_id: int,
        category_key: str,
        item_id: int | None = None,
    ) -> str:
        source_path = Path(pending_path)
        if not source_path.exists():
            raise ValueError("Pending image could not be found.")

        target_dir = self.root / f"session_{session_id}" / self._slug(category_key)
        if item_id is not None:
            target_dir = target_dir / f"item_{item_id}"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = self._next_available_path(target_dir, source_path.name)
        shutil.move(str(source_path), str(target_path))
        return str(target_path)

    def save_confirmed_image(
        self,
        file: UploadFile,
        session_id: int,
        category_key: str,
        item_id: int | None = None,
    ) -> str:
        payload, extension = self._validate_image(file)
        target_dir = self.root / f"session_{session_id}" / self._slug(category_key)
        if item_id is not None:
            target_dir = target_dir / f"item_{item_id}"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = self._next_available_path(target_dir, f"{uuid4().hex}{extension}")
        with target_path.open("wb") as handle:
            handle.write(payload)
        return str(target_path)

    def relocate_confirmed_image(self, file_path: str, session_id: int, category_key: str) -> str:
        source_path = Path(file_path)
        if not source_path.exists():
            return file_path

        item_dir = source_path.parent.name if source_path.parent.name.startswith("item_") else None
        target_dir = self.root / f"session_{session_id}" / self._slug(category_key)
        if item_dir:
            target_dir = target_dir / item_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = self._next_available_path(target_dir, source_path.name)
        shutil.move(str(source_path), str(target_path))
        return str(target_path)

    @staticmethod
    def _next_available_path(target_dir: Path, filename: str) -> Path:
        target_path = target_dir / filename
        counter = 1
        while target_path.exists():
            target_path = target_dir / f"{target_path.stem}_{counter}{target_path.suffix}"
            counter += 1
        return target_path

    def delete_image_file(self, file_path: str) -> None:
        target_path = Path(file_path)
        if target_path.exists():
            target_path.unlink(missing_ok=True)

    def delete_session_images(self, session_id: int) -> None:
        target_dir = self.root / f"session_{session_id}"
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
