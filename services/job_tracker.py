"""
services/job_tracker.py

מעקב אחר עבודות רקע פעילות בכל המערכת (תמלול שיחה, OCR, הקראת כתב יד,
עיבוד תלמידי מוסד) - עבור מסך "פעילות מערכת" (routes/admin.py -> /admin/maintenance),
שנועד לתת למנהל תמונה בזמן אמת של מה עדיין רץ, כדי לדעת מתי בטוח לדפלוי.

חשוב מאוד: gunicorn רץ כאן עם כמה worker processes (--workers 3, ראה
Procfile/railway.json) - לכל אחד זיכרון (RAM) נפרד לגמרי. מונה "פשוט" מבוסס
משתנה בזיכרון (dict רגיל) היה מראה רק את מה שרץ באותו worker process שמטפל
בבקשת הניטור עצמה עכשיו, ומפספס לגמרי עבודות שרצות באותו רגע אצל שני ה-
workers האחרים - מסוכן במיוחד כשהמטרה המוצהרת היא "תחכה שהמונה יגיע ל-0
לפני שתדפלוי". לכן המעקב כאן מבוסס על טבלת DB משותפת (models.ActiveJob),
לא על זיכרון תהליך.

שימוש: לעטוף את גוף העבודה עצמו (לא רק את קריאת ה-submit) -
    with tracked('call', label=f'שיחה {call_id}'):
        ... כל הלוגיקה הכבדה של העבודה עצמה ...
"""
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

KIND_LABELS = {
    'call': 'תמלול שיחה',
    'ocr': 'OCR כתב יד',
    'dictation': 'הקראת כתב יד',
    'institution_student': 'תמלול תלמיד מוסד',
    'institution_upload': 'העלאת מוסד',
}

# אם עבודה "פעילה" ישנה מזה יותר מהזמן הזה (בדקות) - כנראה שה-worker process
# שהריץ אותה נהרג (דיפלוי, קריסה, OOM וכו') בלי לנקות את השורה אחריו, והיא
# כבר לא באמת רצה - "תקועה" (stale), לא פעילה. הסף גבוה בכוונה (הקלטה
# מקסימלית מוגדרת ל-30 דקות + זמן תמלול בפועל) כדי לא לסמן עבודה כבדה אך
# תקינה כתקועה בטעות.
STALE_MINUTES = 60


@contextmanager
def tracked(kind, label=''):
    """context manager - עוטף את גוף העבודה עצמו (לא רק את ה-submit לתור)."""
    from app import db
    from models import ActiveJob

    job_id = None
    try:
        job = ActiveJob(kind=kind, label=(label or '')[:255])
        db.session.add(job)
        db.session.commit()
        job_id = job.id
    except Exception as e:
        log.error(f"job_tracker: start failed (kind={kind}): {e}")
        try:
            db.session.rollback()
        except Exception:
            pass

    try:
        yield
    finally:
        if job_id is not None:
            try:
                from app import db as _db
                from models import ActiveJob as _ActiveJob
                row = _ActiveJob.query.get(job_id)
                if row:
                    _db.session.delete(row)
                    _db.session.commit()
            except Exception as e:
                log.error(f"job_tracker: end failed (kind={kind}, id={job_id}): {e}")
                try:
                    from app import db as _db2
                    _db2.session.rollback()
                except Exception:
                    pass


def snapshot():
    """מחזיר תמונת מצב נוכחית: {'total', 'by_kind', 'jobs', 'stale_count', 'stale_jobs'}."""
    from models import ActiveJob

    cutoff = datetime.utcnow() - timedelta(minutes=STALE_MINUTES)
    rows = ActiveJob.query.order_by(ActiveJob.started_at.asc()).all()
    live, stale = [], []
    now = datetime.utcnow()
    for row in rows:
        item = {
            'id': row.id,
            'kind': row.kind,
            'kind_label': KIND_LABELS.get(row.kind, row.kind),
            'label': row.label or '',
            'seconds': round((now - row.started_at).total_seconds(), 1),
        }
        (stale if row.started_at < cutoff else live).append(item)

    by_kind = {}
    for item in live:
        by_kind[item['kind']] = by_kind.get(item['kind'], 0) + 1

    return {
        'total': len(live),
        'by_kind': by_kind,
        'jobs': live,
        'stale_count': len(stale),
        'stale_jobs': stale,
    }


def clear_stale():
    """מוחק שורות "תקועות" (ישנות מדי - כנראה שריד מ-worker שנהרג בלי לנקות
    אחריו). מחזיר כמה נמחקו."""
    from app import db
    from models import ActiveJob

    cutoff = datetime.utcnow() - timedelta(minutes=STALE_MINUTES)
    stale_rows = ActiveJob.query.filter(ActiveJob.started_at < cutoff).all()
    n = len(stale_rows)
    for row in stale_rows:
        db.session.delete(row)
    db.session.commit()
    return n
