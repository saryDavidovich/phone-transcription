"""
התראה למנהל על הקלטה "תקועה": בטיפול (התקבלה / בתור / מתמללת / נשלחת) הרבה יותר
זמן ממה שסביר - כנראה ה-worker נפל באמצע (דפלוי, קריסה) או ששירות חיצוני לא ענה.

רץ ברקע, בדיקה כל 5 דקות. ההתראה נכנסת ל"הודעות למנהל" (כמו שגיאת תמלול), פעם
אחת לכל הקלטה, בלי לשנות את סטטוס ההקלטה. בכמה workers במקביל רק אחד בודק בכל
פעם (נעילה ב-Postgres).

סף: 30 דקות + פי 3 מאורך ההקלטה (קובץ של שעה - שלוש וחצי שעות). תמלול מקצועי
(אלף בוט, שירות חיצוני) - 12 שעות. ממתינה למצב תחזוקה / לתשלום - לא נחשבת.
"""
import logging
import os
import threading
import time
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

CHECK_EVERY = 300
BASE_MINUTES = 30
ACTIVE = ('recording', 'processing', 'queued', 'transcribing', 'transcribed', 'alefbot_finalizing')
EXTERNAL = ('alefbot_pending',)
EXTERNAL_HOURS = 12
LOOKBACK_DAYS = 3          # הקלטות ישנות יותר כבר טופלו/נבדקו - לא מתריעים עליהן בדיעבד
_LOCK_KEY = 74120931       # מזהה נעילה (pg_try_advisory_lock - לכל משך הבדיקה)
_started = False


def _limit_seconds(rec):
    if rec.status in EXTERNAL:
        return EXTERNAL_HOURS * 3600
    return BASE_MINUTES * 60 + 3 * int(rec.duration_seconds or 0)


def _since(rec):
    """מאיזה רגע סופרים: שינוי הסטטוס האחרון (או יצירת ההקלטה, בהקלטות ישנות בלי status_at)."""
    return max(rec.created_at, rec.status_at) if rec.status_at else rec.created_at


def check_once():
    """מחזיר כמה התראות חדשות נרשמו."""
    from app import db
    from models import Recording, ManagerMessage
    from services.recording_alerts import notify_recording_error
    pg = db.engine.dialect.name == 'postgresql'
    if pg:   # נעילה לכל משך הבדיקה וההתראות (לא רק עד ה-commit הראשון) - בלי התראות כפולות מכמה workers
        got = db.session.execute(db.text('SELECT pg_try_advisory_lock(:k)'), {'k': _LOCK_KEY}).scalar()
        db.session.commit()
        if not got:
            return 0
    try:
        _close_resolved()
        now = datetime.utcnow()
        candidates = (Recording.query
                      .filter(Recording.status.in_(ACTIVE + EXTERNAL),
                              Recording.created_at < now - timedelta(minutes=BASE_MINUTES),
                              Recording.created_at > now - timedelta(days=LOOKBACK_DAYS))
                      .all())
        stuck = [r for r in candidates if (now - _since(r)).total_seconds() > _limit_seconds(r)]
        if not stuck:
            db.session.commit()
            return 0
        already = {m.call_id for m in ManagerMessage.query.with_entities(ManagerMessage.call_id)
                   .filter(ManagerMessage.source == 'recording_error',
                           ManagerMessage.call_id.in_([r.call_id for r in stuck]),
                           ManagerMessage.transcript.contains('תקועה')).all()}
        targets = [(r.call_id, r.status, int((now - _since(r)).total_seconds() // 60)) for r in stuck
                   if r.call_id not in already]
        db.session.commit()
        from services.rec_status import STATUS
        n = 0
        for call_id, status, minutes in targets:
            label = STATUS.get(status, (status,))[0]
            when = f'{minutes // 60} שעות ו-{minutes % 60} דקות' if minutes >= 60 else f'{minutes} דקות'
            notify_recording_error(call_id, f'ההקלטה תקועה: כבר {when} בסטטוס "{label}". '
                                            'כנראה העיבוד נעצר באמצע (דפלוי / קריסה / שירות חיצוני שלא ענה). '
                                            'אפשר להפעיל "תמלול חוזר" מהכרטיס של ההקלטה.', mark_error=False)
            n += 1
        if n:
            log.info(f'stuck_watch: {n} התראות על הקלטות תקועות')
        return n
    finally:
        if pg:
            try:
                db.session.execute(db.text('SELECT pg_advisory_unlock(:k)'), {'k': _LOCK_KEY})
                db.session.commit()
            except Exception:
                db.session.rollback()


def _close_resolved():
    """התראת "תקועה" על הקלטה שבסוף נשלחה ללקוח - נסגרת לבד (לא נשארת כ"חדשה" בהודעות למנהל)."""
    from app import db
    from models import Recording, ManagerMessage
    open_alerts = (ManagerMessage.query
                   .filter(ManagerMessage.source == 'recording_error', ManagerMessage.status == 'new',
                           ManagerMessage.transcript.contains('תקועה'))
                   .limit(500).all())
    if not open_alerts:
        return 0
    done = {r.call_id for r in Recording.query.with_entities(Recording.call_id)
            .filter(Recording.call_id.in_([m.call_id for m in open_alerts]),
                    Recording.status.in_(('delivered',))).all()}
    n = 0
    for m in open_alerts:
        if m.call_id in done:
            m.status = 'done'
            m.admin_note = ((m.admin_note + '\n') if m.admin_note else '') + 'נסגר אוטומטית: ההקלטה נשלחה ללקוח בסוף.'
            n += 1
    if n:
        db.session.commit()
        log.info(f'stuck_watch: {n} התראות "תקועה" נסגרו - ההקלטות נשלחו')
    return n


def _loop(app):
    time.sleep(90)    # לא מיד בעליית השרת - קודם שהעבודות שבתור יתחילו
    while True:
        try:
            with app.app_context():
                check_once()
        except Exception as e:
            log.warning(f'stuck_watch: {e}')
            try:
                with app.app_context():
                    from app import db
                    db.session.rollback()
            except Exception:
                pass
        time.sleep(CHECK_EVERY)


def start(app):
    global _started
    if _started or os.environ.get('STUCK_WATCH_DISABLED') == '1':
        return
    _started = True
    threading.Thread(target=_loop, args=(app,), daemon=True, name='stuck-watch').start()
