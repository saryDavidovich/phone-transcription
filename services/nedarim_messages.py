"""
משיכת "הודעות לגבאי" מנדרים פלוס (הודעות שאנשים שולחים להנהלה דרך נדרים פלוס).

לפי התיעוד של נדרים פלוס:
  GET https://matara.pro/nedarimplus/Reports/Manage3.aspx
      Action=GetUserMessage, MosadId, ApiPassword (מפתח API שמתחיל ב-npk_),
      LastId (מאיזה ID - לא כולל), MaxId (עד 1000), NoAutoRead=1
  תשובה: {"ReadFlow":"1","Logs":[{ID, SendDate, ClientName, ClientAdresse, ClientPhone,
          ClientId, ClientZeout, Message, Readed, MasofName, RecordFile?}, ...]}

NoAutoRead=1 - חשוב: בלי זה נדרים פלוס מסמנים את ההודעות כ"נקראו" אצלם.

רץ ברקע בכל תהליך של השרת, אבל בפועל מושך רק פעם ב-10 דקות לכלל השרת: לפני
כל משיכה "תופסים" את הזמן בהגדרה nedarim_msgs_last_poll (עדכון מותנה במסד),
וכפילויות נחסמות גם ע"י nedarim_id ייחודי.

פרטי הגישה: מספר מוסד - NEDARIM_MOSAD (או ההגדרה nedarim_mosad_number);
מפתח API - NEDARIM_API_KEY (או ההגדרה nedarim_api_password, אם הוא מתחיל ב-npk_).
"""
import logging
import os
import threading
import time
from datetime import datetime, timedelta

import requests

log = logging.getLogger(__name__)

URL = 'https://matara.pro/nedarimplus/Reports/Manage3.aspx'
POLL_MINUTES = 10
RECORD_URL = 'https://images.matara.pro/IVR/Record/0_{}.wav'
_started = False


def _setting(key, default=''):
    from models import Settings
    s = Settings.query.filter_by(key=key).first()
    return s.value if s and s.value else default


def _credentials():
    mosad = os.environ.get('NEDARIM_MOSAD') or _setting('nedarim_mosad_number')
    key = os.environ.get('NEDARIM_API_KEY') or ''
    if not key:
        legacy = _setting('nedarim_api_password')
        if legacy.startswith('npk_'):
            key = legacy
    return (mosad or '').strip(), (key or '').strip()


def is_configured():
    try:
        m, k = _credentials()
        return bool(m and k)
    except Exception:
        return False


def _claim_poll_slot(force=False):
    """מחזיר True אם התהליך הזה הוא שצריך למשוך עכשיו (עברו 10 דקות מהמשיכה האחרונה)."""
    from app import db
    from models import Settings
    now = datetime.utcnow()
    row = Settings.query.filter_by(key='nedarim_msgs_last_poll').first()
    if row is None:
        db.session.add(Settings(key='nedarim_msgs_last_poll', value=now.isoformat()))
        db.session.commit()
        return True
    try:
        last = datetime.fromisoformat(row.value)
    except (TypeError, ValueError):
        last = datetime.min
    if not force and now - last < timedelta(minutes=POLL_MINUTES - 0.5):
        return False
    # עדכון מותנה - רק תהליך אחד מצליח לתפוס את אותו חלון
    res = db.session.execute(
        db.text("UPDATE settings SET value = :new WHERE key = 'nedarim_msgs_last_poll' AND value = :old"),
        {'new': now.isoformat(), 'old': row.value})
    db.session.commit()
    return res.rowcount == 1


def fetch_new(force=False):
    """מושך הודעות חדשות ושומר אותן. מחזיר (מספר חדשות, שגיאה או None)."""
    from app import db
    from models import NedarimMessage, Settings
    mosad, key = _credentials()
    if not (mosad and key):
        return 0, 'חסרים מספר מוסד או מפתח API של נדרים פלוס'
    if not _claim_poll_slot(force):
        return 0, None
    last_id = db.session.query(db.func.max(NedarimMessage.nedarim_id)).scalar()
    params = {'Action': 'GetUserMessage', 'MosadId': mosad, 'ApiPassword': key,
              'MaxId': 1000, 'NoAutoRead': 1}
    if last_id:
        params['LastId'] = last_id
    try:
        r = requests.get(URL, params=params, timeout=30)
        try:
            data = r.json()
        except ValueError:
            return 0, f'תשובה לא צפויה מנדרים פלוס: {r.text[:200]}'
    except Exception as e:
        return 0, f'החיבור לנדרים פלוס נכשל: {e}'
    if isinstance(data, dict) and str(data.get('Result', '')).lower() == 'error':
        return 0, str(data.get('Message') or 'שגיאה מנדרים פלוס')[:300]
    logs = data.get('Logs') if isinstance(data, dict) else data
    if not isinstance(logs, list):
        return 0, f'תשובה לא צפויה מנדרים פלוס: {str(data)[:200]}'
    added = 0
    new_msgs = []
    for item in logs:
        try:
            nid = int(item.get('ID'))
        except (TypeError, ValueError):
            continue
        if NedarimMessage.query.filter_by(nedarim_id=nid).first():
            continue
        nm = NedarimMessage(
            nedarim_id=nid,
            send_date=str(item.get('SendDate') or '')[:40],
            client_name=str(item.get('ClientName') or '')[:200],
            client_phone=str(item.get('ClientPhone') or '')[:40],
            client_address=str(item.get('ClientAdresse') or '')[:300],
            client_id=str(item.get('ClientId') or '')[:40],
            message=str(item.get('Message') or ''),
            masof_name=str(item.get('MasofName') or '')[:200],
            record_file=(str(item.get('RecordFile')) if item.get('RecordFile') else None),
            is_read=False)
        db.session.add(nm)
        new_msgs.append(nm)
        added += 1
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return 0, f'שמירת ההודעות נכשלה: {e}'
    # פקודות ניהול מהמנהל (services/nedarim_commands.py) - לפי סדר השליחה
    from services import nedarim_commands
    for nm in sorted(new_msgs, key=lambda m: m.nedarim_id):
        try:
            nedarim_commands.handle(nm)
        except Exception as e:
            db.session.rollback()
            log.exception(f'nedarim command error: {e}')
    row = Settings.query.filter_by(key='nedarim_msgs_last_error').first() or Settings(key='nedarim_msgs_last_error')
    row.value = ''
    db.session.add(row)
    db.session.commit()
    if added:
        log.info(f'nedarim messages: נוספו {added} הודעות חדשות')
    return added, None


def _save_error(err):
    from app import db
    from models import Settings
    row = Settings.query.filter_by(key='nedarim_msgs_last_error').first() or Settings(key='nedarim_msgs_last_error')
    row.value = f'{datetime.utcnow().isoformat()}|{err}'[:490]
    db.session.add(row)
    db.session.commit()


def _loop(app):
    time.sleep(60)                       # לתת לשרת לעלות
    while True:
        try:
            with app.app_context():
                if is_configured():
                    _, err = fetch_new()
                    if err:
                        log.warning(f'nedarim messages: {err}')
                        _save_error(err)
        except Exception as e:
            log.warning(f'nedarim messages loop: {e}')
        time.sleep(60)                   # בודקים כל דקה אם הגיע הזמן (בפועל משיכה פעם ב-10 דקות)


def start(app):
    global _started
    if _started or os.environ.get('NEDARIM_MESSAGES_DISABLED') == '1':
        return
    _started = True
    threading.Thread(target=_loop, args=(app,), daemon=True, name='nedarim-messages').start()


def last_error():
    v = _setting('nedarim_msgs_last_error')
    if not v or '|' not in v:
        return None
    at, err = v.split('|', 1)
    return err
