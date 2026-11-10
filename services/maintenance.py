"""
מצב תחזוקה - שחרור העבודות שהמתינו, וכיבוי אוטומטי אחרי פריסה (דפלוי) חדשה.

מדליקים מצב תחזוקה לפני דפלוי, כדי שעבודות לא ייקטעו באמצע. כדי שלא יקרה ששוכחים
אותו דלוק והמערכת מושבתת: כשהגרסה החדשה עולה, אם המצב הודלק בגרסה הקודמת (מזהה
הפריסה של Railway שונה), הוא נכבה אוטומטית אחרי כחצי דקה וכל מה שהמתין משתחרר.
אפשר לבטל את הכיבוי האוטומטי בדף מצב התחזוקה (maintenance_auto_off = 0).

הדלקה אוטומטית: גיטהאב שולח webhook בכל העלאה ל-main (/api/github-push, חתום ב-GITHUB_WEBHOOK_SECRET)
והמצב נדלק מיד - כך שעבודות חדשות ממתינות בזמן הבנייה, והקיימות מסתיימות לפני שהגרסה מתחלפת.
אם הגרסה החדשה לא עולה תוך 20 דקות (בנייה שנכשלה) - המצב נכבה והמנהל מקבל הודעה.
"""
import json
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


def set_mode(on, source='admin'):
    """הדלקה / כיבוי. בהדלקה נשמר מזהה הפריסה - לכיבוי האוטומטי בפריסה הבאה.
    source: 'admin' (מהממשק) / 'github' (אוטומטית בהעלאת קוד לגיטהאב)."""
    from routes.admin import set_setting
    set_setting('maintenance_mode', '1' if on else '0')
    if on:
        set_setting('maintenance_on_deploy', deployment_id())
        set_setting('maintenance_on_at', datetime.utcnow().isoformat(timespec='seconds'))
        set_setting('maintenance_on_source', source)
        return None
    set_setting('maintenance_on_source', '')
    return release_all()


GITHUB_MAX_MINUTES = 20     # הודלק בהעלאה לגיטהאב ולא עלתה גרסה חדשה (בנייה נכשלה?) - כיבוי אחרי 20 דקות


def on_github_push(sha='', message=''):
    """העלאת קוד לגיטהאב (webhook): נרשם ביומן, ומדליק מצב תחזוקה עד שהגרסה החדשה עולה. כבר דלוק - לא נוגע."""
    from routes.admin import get_setting
    already = get_setting('maintenance_mode', '0') == '1'
    record_push(sha, message, maintenance_on=True)
    if already:
        return False
    set_mode(True, source='github')
    log.warning('maintenance: הודלק אוטומטית - העלאת קוד לגיטהאב, ייכבה כשהגרסה החדשה תעלה')
    return True


# ---------------------------------------------------------------------------
# יומן עדכוני גרסה
# ---------------------------------------------------------------------------
_LOG_KEY = 'deploy_log'
_LOG_MAX = 20


def _released_text(r):
    r = r or {}
    return f'{r.get("calls", 0)} הקלטות, {r.get("ocr", 0)} תמונות OCR, {r.get("email_out", 0)} מיילים לאנשי קשר'


def deploy_log():
    from routes.admin import get_setting
    try:
        return json.loads(get_setting(_LOG_KEY, '') or '[]')
    except ValueError:
        return []


def _save_log(items, last_status):
    from routes.admin import set_setting
    set_setting(_LOG_KEY, json.dumps(items[:_LOG_MAX], ensure_ascii=False))
    set_setting('deploy_last_status', last_status)


def record_push(sha='', message='', maintenance_on=False):
    """העלאה לגיטהאב: רשומה חדשה במצב 'בבנייה'."""
    items = deploy_log()
    items.insert(0, {'sha': (sha or '')[:7], 'msg': (message or '').split('\n')[0][:200],
                     'pushed_at': datetime.utcnow().isoformat(timespec='seconds'), 'status': 'building',
                     'maintenance': bool(maintenance_on)})
    _save_log(items, 'building')


def mark_up(dep='', note=''):
    """הגרסה החדשה עלתה. העלאות שעוד היו 'בבנייה' - הגרסה האחרונה כוללת אותן."""
    items = deploy_log()
    now = datetime.utcnow().isoformat(timespec='seconds')
    building = [i for i in items if i.get('status') == 'building']
    if building:
        for i in building:
            i.update(status='ok', up_at=now, deploy=(dep or '')[:8])
        building[0]['note'] = note
    else:            # עלתה גרסה בלי העלאה מגיטהאב (Redeploy / שינוי משתנה ב-Railway)
        items.insert(0, {'sha': '', 'msg': 'הפעלה מחדש / פריסה מ-Railway', 'pushed_at': None, 'status': 'ok',
                         'up_at': now, 'deploy': (dep or '')[:8], 'note': note})
    _save_log(items, 'ok')


def mark_failed(note=''):
    items = deploy_log()
    hit = False
    for i in items:
        if i.get('status') == 'building':
            i.update(status='failed', failed_at=datetime.utcnow().isoformat(timespec='seconds'), note=note)
            hit = True
    if hit:
        _save_log(items, 'failed')


def _fail_stale_builds():
    """העלאה שנשארה 'בבנייה' יותר מ-20 דקות (גם בלי מצב תחזוקה) - נכשלה."""
    for i in deploy_log():
        if i.get('status') == 'building' and i.get('pushed_at'):
            try:
                if (datetime.utcnow() - datetime.fromisoformat(i['pushed_at'])).total_seconds() > GITHUB_MAX_MINUTES * 60:
                    mark_failed(f'גרסה חדשה לא עלתה תוך {GITHUB_MAX_MINUTES} דקות מההעלאה (אולי הבנייה ב-Railway נכשלה).')
                    return
            except ValueError:
                pass


def _watchdog_github(app):
    """הודלק בהעלאה לגיטהאב אבל הגרסה החדשה לא עלתה תוך 20 דקות - מכבים ומודיעים למנהל."""
    while True:
        time.sleep(60)
        try:
            with app.app_context():
                from app import db
                from routes.admin import get_setting, set_setting
                if get_setting('maintenance_mode', '0') != '1' or get_setting('maintenance_on_source', '') != 'github':
                    _fail_stale_builds()        # בלי מצב תחזוקה - רק מסמנים ביומן
                    continue
                try:
                    at = datetime.fromisoformat(get_setting('maintenance_on_at', ''))
                except ValueError:
                    continue
                if (datetime.utcnow() - at).total_seconds() < GITHUB_MAX_MINUTES * 60:
                    continue
                if db.engine.dialect.name == 'postgresql':
                    if not db.session.execute(db.text('SELECT pg_try_advisory_xact_lock(:k)'), {'k': _LOCK_KEY + 1}).scalar():
                        db.session.rollback()
                        continue
                if get_setting('maintenance_mode', '0') != '1':
                    continue
                released = set_mode(False)
                mark_failed(f'גרסה חדשה לא עלתה תוך {GITHUB_MAX_MINUTES} דקות (אולי הבנייה ב-Railway נכשלה). '
                            f'מצב התחזוקה כובה. שוחררו: {_released_text(released)}.')
                log.warning('maintenance: כובה - גרסה חדשה לא עלתה תוך 20 דקות מההעלאה לגיטהאב')
        except Exception as e:
            log.warning(f'maintenance watchdog: {e}')


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
                new_version = bool(dep) and get_setting('deploy_last_seen', '') != dep
                if new_version:
                    set_setting('deploy_last_seen', dep)
                if (get_setting('maintenance_mode', '0') != '1' or get_setting('maintenance_auto_off', '1') != '1'
                        or not dep or get_setting('maintenance_on_deploy', '') == dep):
                    if new_version:
                        mark_up(dep)
                    return
                released = set_mode(False)
                mark_up(dep, f'מצב התחזוקה כובה אוטומטית. שוחררו: {_released_text(released)}.')
                set_setting('maintenance_auto_off_last', f'{datetime.utcnow().isoformat(timespec="seconds")}|{released}')
                log.warning(f'maintenance: מצב תחזוקה כובה אוטומטית אחרי פריסה חדשה ({dep[:8]}), שוחררו: {released}')
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
    threading.Thread(target=_watchdog_github, args=(app,), daemon=True, name='maintenance-watchdog').start()
