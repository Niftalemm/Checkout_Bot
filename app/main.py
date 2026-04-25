from fastapi import FastAPI

from app.api.routes import router
from app.core.config import settings
from app.db.base import bootstrap_database
from app.models import entities  # noqa: F401

app = FastAPI(title=settings.app_name)
app.include_router(router)


@app.on_event("startup")
def startup():
    bootstrap_database()


@app.get("/health")
def health():
    return {"status": "ok", "service": settings.app_name}
