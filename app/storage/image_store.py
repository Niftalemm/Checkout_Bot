from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile


class LocalImageStore:
    def __init__(self, uploads_dir: str):
        self.root = Path(uploads_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def save_damage_image(self, file: UploadFile, session_id: int) -> str:
        extension = Path(file.filename or "image.jpg").suffix or ".jpg"
        target_dir = self.root / f"session_{session_id}"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{uuid4().hex}{extension}"
        with target_path.open("wb") as out:
            out.write(file.file.read())
        return str(target_path)
