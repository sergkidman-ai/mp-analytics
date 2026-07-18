"""reports/compat_cache.py — КЭШ СОВМЕСТИМОСТИ (пара наш-товар × модель принтера → вердикт).

Главный рычаг экономии веб-поиска: веб по конкретной паре платится один раз, дальше из БД.
Ключ — (platform, item_id, model_norm). Не зависит от провайдера LLM.

  from reports.compat_cache import get as cc_get, put as cc_put
  hit = cc_get('wb', '199574754', 'Xerox Phaser 3330')   # None или dict {verdict,reply,source,...}
  cc_put('wb', '199574754', 'Xerox Phaser 3330', verdict='no', reply='...', source='веб', sources=[...])
"""
import re
import sys
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                          # noqa: E402


def norm_model(m):
    """Нормализация модели принтера в стабильный ключ: только буквы+цифры, нижний регистр,
    кириллица бренда → латиница по частым синонимам (лексмарк→lexmark и т.п.)."""
    s = (m or "").lower().strip()
    syn = {"лексмарк": "lexmark", "кэнон": "canon", "кенон": "canon", "куосера": "kyocera",
           "куасера": "kyocera", "ксерокс": "xerox", "эпсон": "epson", "бразер": "brother",
           "самсунг": "samsung", "рико": "ricoh", "пантум": "pantum", "катюша": "katusha",
           "коника": "konica", "минолта": "minolta", "шарп": "sharp", "панасоник": "panasonic"}
    for ru, en in syn.items():
        s = s.replace(ru, en)
    return re.sub(r"[^0-9a-z]", "", s) or None


def get(platform, item_id, model_raw):
    """Вернуть кэшированный вердикт для пары или None. Инкрементит hits при попадании."""
    mn = norm_model(model_raw)
    if not mn:
        return None
    rows = db.query("""SELECT verdict, reply, source, sources, note FROM compat_cache
        WHERE platform=%s AND item_id=%s AND model_norm=%s""", (platform, str(item_id), mn))
    if not rows:
        return None
    db.execute("""UPDATE compat_cache SET hits=hits+1, updated_at=now()
        WHERE platform=%s AND item_id=%s AND model_norm=%s""", (platform, str(item_id), mn))
    return rows[0]


def put(platform, item_id, model_raw, verdict, reply="", source="", sources=None, note=""):
    """Сохранить/обновить вердикт совместимости пары. unclear НЕ кэшируем (пусть перепроверится)."""
    mn = norm_model(model_raw)
    if not mn or verdict not in ("yes", "no"):
        return
    from psycopg2.extras import Json
    db.execute("""INSERT INTO compat_cache
        (platform,item_id,model_norm,model_raw,verdict,reply,source,sources,note)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (platform,item_id,model_norm) DO UPDATE SET
          verdict=EXCLUDED.verdict, reply=EXCLUDED.reply, source=EXCLUDED.source,
          sources=EXCLUDED.sources, note=EXCLUDED.note, updated_at=now()""",
        (platform, str(item_id), mn, (model_raw or "")[:120], verdict, (reply or "")[:1000],
         (source or "")[:40], Json(sources or []), (note or "")[:300]))


if __name__ == "__main__":
    put("wb", "199574754", "Xerox Phaser 3330", "no", "нет, для 3330 наш артикул 199333468", "веб")
    print("get:", get("wb", "199574754", "xerox phaser-3330"))   # нормализация ловит дефис/регистр
    print("miss:", get("wb", "199574754", "HP LaserJet 1010"))
