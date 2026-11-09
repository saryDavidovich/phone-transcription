"""
מצב תחזוקה - שחרור העבודות שהמתינו, וכיבוי אוטומטי אחרי פריסה (דפלוי) חדשה.

מדליקים מצב תחזוקה לפני דפלוי, כדי שעבודות לא ייקטעו באמצע. כדי שלא יקרה ששוכחים
אותו דלוק והמערכת מושבתת: כשהגרסה החדשה עולה, אם המצב הודלק בגרסה הקודמת (מזהה
הפריסה של Railway שונה), הוא נכבה אוטומטית אחרי כחצי דקה וכל מה שהמתין משתחרר.
אפשר לבטל את הכיבוי האוטומטי בדף מצב התחזוקה (maintenance_auto_off = 0).
"""
import logging
import os
import threading
import time
from datetime import datetime

log = logging.getLogger(__name__)
_LOCK_KEY = 74120933
_started = False


def deployment_id():
    return (os.environ.get('RAILWAY_DEPLOYMENT_ID') or '').strip()


def release_all():
    """משחרר את כל מה שהמתין בזמן מצב התחזוקה. מחזיר dict של כמויות."""
    from services.transcribe import resume_queued_recordings
    from routes.email_inbound import resume_queued_ocr
    from services.customer_mail import resume_queued as resume_mail
    out = {'calls': resume_queued_recordings(), 'ocr': resume_queued_ocr(), 'email_out': 0}
    try:
        out['email_out'] = resume_mail()
    except Exception as e:
        log.error(f'maintenance: שחרור מיילים לאנשי קשר נכשל: {e}')
    return out


def set_mode(on):
    """הדלקה / כיבוי (מהממשק). בהדלקה נשמר מזהה הפריסה - לכיבוי האוטומטי בפריסה הבאה."""
    from routes.admin import set_setting
    set_setting('maintenance_mode', '1' if on else '0')
    if on:
        set_setting('maintenance_on_deploy', deployment_id())
        set_setting('maintenance_on_at', datetime.utcnow().isoformat(timespec='seconds'))
        return None
    return release_all()


def _auto_off(app):
    time.sleep(30)          # שהגרסה החדשה תסיים לעלות (והישנה תיסגר) לפני שמשחררים עבודות
    try:
        with app.app_context():
            from app import db
            from routes.admin import get_setting, set_setting
            if db.engine.dialect.name == 'postgresql':
                if not db.session.execute(db.text('SELECT pg_try_advisory_lock(:k)'), {'k': _LOCK_KEY}).scalar():
                    db.session.rollback()
                    return
            try:
                dep = deployment_id()
                if (get_setting('maintenance_mode', '0') != '1' or get_setting('maintenance_auto_off', '1') != '1'
                        or not dep or get_setting('maintenance_on_deploy', '') == dep):
                    return
                released = set_mode(False)
                set_setting('maintenance_auto_off_last', f'{datetime.utcnow().isoformat(timespec="seconds")}|{released}')
                log.warning(f'maintenance: מצב תחזוקה כובה אוטומטית אחרי פריסה חדשה ({dep[:8]}), שוחררו: {released}')
                try:
                    from models import ManagerMessage
                    db.session.add(ManagerMessage(
                        phone='—', name='מצב תחזוקה', source='recording_error',
                        transcript=(f'מצב התחזוקה כובה אוטומטית אחרי עליית גרסה חדשה. שוחררו: '
                                    f'{released["calls"]} הקלטות, {released["ocr"]} תמונות OCR, '
                                    f'{released["email_out"]} מיילים לאנשי קשר.')))
                    db.session.commit()
                except Exception:
                    db.session.rollback()
            finally:
                if db.engine.dialect.name == 'postgresql':
                    db.session.execute(db.text('SELECT pg_advisory_unlock(:k)'), {'k': _LOCK_KEY})
                    db.session.commit()
    except Exception as e:
        log.error(f'maintenance: כיבוי אוטומטי נכשל: {e}')


def start(app):
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_auto_off, args=(app,), daemon=True, name='maintenance-auto-off').start()
