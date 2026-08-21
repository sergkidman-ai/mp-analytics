# поток: ev — API дневника событий для дашборда
"""Роутер дневника: чтение для главной страницы и форма добавления.

Отдельным файлом, а не строками в `web/app.py`: тот файл на 4300 строк и его правит поток fin.
Здесь только дневник — конфликтов при мерже нет, включается одной строкой `include_router`.
"""
import sys
import pathlib
from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from ops import biz_diary  # noqa: E402

router = APIRouter(prefix="/api/diary", tags=["Дневник событий"])


class NewEvent(BaseModel):
    event_date: date | None = None
    kind: str = "own"
    platform: str | None = None
    account: str | None = None
    title: str
    details: str | None = None
    expect: str | None = None
    date_to: date | None = None
    review_at: date | None = None
    author: str | None = None


@router.get("")
def list_events(limit: int = 20, kind: str | None = None,
                platform: str | None = None, days: int | None = None):
    """Последние записи дневника — блок «Что мы меняли» на главной."""
    return {"events": biz_diary.recent(limit=limit, kind=kind,
                                       platform=platform, days=days)}


@router.get("/weeks")
def weeks(weeks: int = 8):
    """События по неделям — маркеры 📌 в таблице недельной динамики."""
    return {"weeks": biz_diary.by_week(weeks)}


@router.post("")
def create(ev: NewEvent):
    try:
        new_id = biz_diary.add(
            event_date=ev.event_date or date.today(), title=ev.title, kind=ev.kind,
            platform=ev.platform, account=ev.account, details=ev.details,
            expect=ev.expect, date_to=ev.date_to, review_at=ev.review_at,
            author=ev.author, source="web")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"id": new_id, "ok": True}


@router.post("/parse")
def parse(payload: dict):
    """Свободная строка -> черновик события (для бота и для формы «вставил и проверил»)."""
    text = (payload or {}).get("text", "")
    if not text.strip():
        raise HTTPException(status_code=400, detail="пустой текст")
    draft = biz_diary.parse_free(text)
    draft["event_date"] = draft["event_date"].isoformat() if draft["event_date"] else None
    return draft


@router.delete("/{event_id}")
def remove(event_id: int):
    rows = biz_diary.delete(event_id)
    if not rows:
        raise HTTPException(status_code=404, detail="нет такой записи")
    return {"ok": True, "deleted": event_id}
