"""
פקודות ניהול מעמדת נדרים פלוס.

שולחים "הודעה לגבאי" מעמדת נדרים פלוס, מהמספר של המנהל (0527134491):
  בשורה אחת עם מקפים (בעמדת נדרים אנטר שולח את ההודעה):  קוד-פעולה-פרט-פרט
  למשל 123456-3-0501234567-7.5 . קוד = 6 הספרות של תפריט המנהל (ADMIN_PHONE_PIN),
  פעולה = מספר מ-COMMANDS. טלפון בלי מקפים. הפרט האחרון יכול להכיל מקפים.
  (גם הצורה הקודמת - כל חלק בשורה משלו - עדיין עובדת)

ההודעות נמשכות מנדרים פלוס כל 10 דקות (services/nedarim_messages.py), ומיד מבוצעות.
התוצאה נשמעת בטלפון: תפריט המנהל ← 6. רשימת כל הפקודות: תפריט המנהל ← 7.

אבטחה:
- רק הודעה מהמספר של המנהל שהשורה הראשונה שלה היא בדיוק 6 ספרות נחשבת לפקודה.
- 3 קודים שגויים ברצף - הפקודות ננעלות, ונשלחת התראה ל"הודעות למנהל". פתיחה בממשק
  (הודעות למנהל ← נדרים פלוס ← "פתיחת הפקודות").
- פקודה שנשלחה לפני יותר משעה לא מבוצעת (למשל אם המשיכה מנדרים הייתה תקועה).
- הקוד הסודי לא נשמר: בהודעה השמורה השורה הראשונה מוחלפת בכוכביות.
"""
import json
import logging
import os
import re
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

ADMIN_PHONE = '0527134491'
MAX_FAILS = 3
MAX_AGE_MIN = 60
MAX_AMOUNT = 500

# קוד: (שם, [שמות הפרטים, לפי הסדר], הסבר להקראה)
COMMANDS = {
    0: ('ביטול הפקודה האחרונה', [], 'מבטל את הפקודה האחרונה שבוצעה, אם אפשר לבטל אותה'),
    1: ('מצב המערכת', [], 'הודעות ממתינות, הקלטות היום, שגיאות היום, ומצב תחזוקה'),
    2: ('יתרת לקוח', ['טלפון'], 'פרט אחד: טלפון הלקוח'),
    3: ('זיכוי לקוח', ['טלפון', 'סכום'], 'פרט ראשון טלפון הלקוח, פרט שני הסכום בשקלים, אפשר עם נקודה, עד 500'),
    4: ('חיוב לקוח', ['טלפון', 'סכום'], 'פרט ראשון טלפון הלקוח, פרט שני הסכום בשקלים, עד 500'),
    5: ('מצב תחזוקה', ['1 או 0'], 'פרט אחד: 1 להפעלה, 0 לכיבוי'),
    6: ('חסימת לקוח', ['טלפון', '1 או 0'], 'פרט ראשון טלפון הלקוח, פרט שני 1 לחסימה או 0 לשחרור'),
    7: ('תמלול מחדש', ['מספר הקלטה', '1 או 2'], 'פרט ראשון מספר ההקלטה במערכת, פרט שני 1 לתמלול רגיל או 2 למקצועי. הלקוח מחויב כרגיל'),
    8: ('שליחה חוזרת של תמלול', ['מספר הקלטה'], 'פרט אחד: מספר ההקלטה במערכת. נשלח שוב למייל או לפקס של הלקוח'),
    9: ('עדכון מייל ללקוח', ['טלפון', 'מייל'], 'פרט ראשון טלפון הלקוח, פרט שני כתובת המייל'),
    10: ('הודעה קולית ללקוח', ['טלפון', 'נוסח'], 'פרט ראשון טלפון הלקוח, פרט שני נוסח ההודעה. הלקוח ישמע אותה בכניסה הבאה למערכת'),
    11: ('הודעה קולית לכל הלקוחות', ['נוסח'], 'פרט אחד: נוסח ההודעה'),
    12: ('סימון הודעה למנהל כטופלה', ['מספר הודעה', 'הסבר'], 'פרט ראשון מספר ההודעה מלשונית הודעות טלפון, פרט שני מה נעשה'),
    13: ('ספק הפקסים', ['1 או 2'], 'פרט אחד: 1 לאקסטרה מובייל, 2 לימות המשיח'),
}
NOT_UNDOABLE = {0, 1, 2, 7, 8}


# ---------------------------------------------------------------------------
# הגדרות ונעילה
# ---------------------------------------------------------------------------
def _get(key, default=''):
    from models import Settings
    s = Settings.query.filter_by(key=key).first()
    return s.value if s and s.value is not None else default


def _set(key, value):
    from app import db
    from models import Settings
    s = Settings.query.filter_by(key=key).first()
    if s is None:
        s = Settings(key=key)
        db.session.add(s)
    s.value = value


def is_locked():
    return _get('nedarim_cmd_locked', '0') == '1'


def unlock():
    from app import db
    _set('nedarim_cmd_locked', '0')
    _set('nedarim_cmd_fails', '0')
    db.session.commit()


def _pin():
    p = (os.environ.get('ADMIN_PHONE_PIN') or '').strip()
    return p if re.fullmatch(r'\d{6}', p) else ''


def _digits(v):
    return re.sub(r'\D', '', str(v or ''))


def _phone(v):
    p = _digits(v)
    if p.startswith('972'):
        p = '0' + p[3:]
    return p


def _money(v):
    v = round(float(v or 0), 2)
    sh, ag = int(v), int(round((v - int(v)) * 100))
    if not sh:
        return f'{ag} אגורות'
    t = 'שקל' if sh == 1 else f'{sh} שקל'
    return f'{t} ו {ag} אגורות' if ag else t


def _parse_date(s):
    for fmt in ('%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime((s or '').strip(), fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# זיהוי וביצוע
# ---------------------------------------------------------------------------
def _split(text):
    """פירוק הפקודה לחלקים. שתי צורות כתיבה:
    - שורה אחת עם מקפים (בעמדת נדרים אנטר שולח): 123456-3-0501234567-7.5
    - כל חלק בשורה משלו (הצורה הקודמת).
    בצורת המקפים, הפרט האחרון של הפעולה מקבל את כל מה שנשאר עד הסוף - כך מקף בתוך
    נוסח הודעה, הסבר או כתובת מייל לא שובר את הפקודה."""
    text = (text or '').strip()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) > 1:
        return lines
    parts = [t.strip() for t in text.split('-')]
    if len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) in COMMANDS:
        n = len(COMMANDS[int(parts[1])][1])
        if n:   # הפרט האחרון - בדיוק כפי שנכתב (כולל מקפים ורווחים)
            parts = [t.strip() for t in text.split('-', 1 + n)]
    return [t for t in parts if t != '']


def looks_like_command(msg):
    parts = _split(msg.message)
    return _phone(msg.client_phone) == ADMIN_PHONE and bool(parts) and bool(re.fullmatch(r'\d{6}', parts[0]))


def handle(msg):
    """נקרא לכל הודעה חדשה מנדרים פלוס. אם זו פקודה - מבצע ומחזיר True."""
    from app import db
    from models import NedarimCommand
    if not looks_like_command(msg):
        # ליתר ביטחון: אם הקוד הסודי הופיע בהודעה ממספר אחר - לא שומרים אותו
        pin = _pin()
        first = (_split(msg.message) or [''])[0]
        if pin and first == pin:
            msg.message = (msg.message or '').replace(pin, '••••••', 1)
            db.session.commit()
        return False
    lines = _split(msg.message)
    sent_pin, rest = lines[0], lines[1:]
    # הקוד הסודי לא נשמר במסד הנתונים
    msg.message = '••••••-' + '-'.join(rest)

    def finish(status, result, action=None, params=None, undo=None, mark_read=True):
        cmd = NedarimCommand(nedarim_message_id=msg.id, action=action, status=status, result=result,
                             params=json.dumps(params or [], ensure_ascii=False),
                             undo=json.dumps(undo, ensure_ascii=False) if undo else None)
        db.session.add(cmd)
        db.session.flush()
        if mark_read:
            msg.is_read = True
            msg.handled_at = datetime.utcnow()
        msg.handled_note = f'⚙ פקודה {cmd.id}: {result}'
        db.session.commit()
        log.info(f'nedarim command #{cmd.id}: action={action} status={status} result={result!r}')
        return True

    pin = _pin()
    if not pin:
        return finish('rejected', 'פקודות לא פעילות: לא הוגדר קוד סודי בן 6 ספרות בשרת')
    if is_locked():
        return finish('rejected', 'הפקודות נעולות. יש לפתוח אותן בממשק', mark_read=False)
    import hmac
    if not hmac.compare_digest(sent_pin.encode(), pin.encode()):
        fails = int(_get('nedarim_cmd_fails', '0') or 0) + 1
        _set('nedarim_cmd_fails', str(fails))
        if fails >= MAX_FAILS:
            _set('nedarim_cmd_locked', '1')
            _alert_locked()
            return finish('rejected', f'קוד שגוי. אחרי {fails} ניסיונות הפקודות ננעלו', mark_read=False)
        return finish('rejected', f'קוד שגוי (ניסיון {fails} מתוך {MAX_FAILS})', mark_read=False)
    _set('nedarim_cmd_fails', '0')

    sent_at = _parse_date(msg.send_date)          # שעון ישראל, כפי שנדרים פלוס מחזירים
    if sent_at:
        from zoneinfo import ZoneInfo
        now_il = datetime.now(ZoneInfo('Asia/Jerusalem')).replace(tzinfo=None)
        if now_il - sent_at > timedelta(minutes=MAX_AGE_MIN):
            return finish('rejected', 'הפקודה לא בוצעה: היא נשלחה לפני יותר משעה')

    if not rest or not rest[0].isdigit() or int(rest[0]) not in COMMANDS:
        return finish('failed', 'קוד פעולה לא מוכר. לרשימת הפקודות: תפריט המנהל, 7')
    action = int(rest[0])
    params = rest[1:]
    name, fields, _ = COMMANDS[action]
    if len(params) < len(fields):
        return finish('failed', f'{name}: חסרים פרטים. צריך: {", ".join(fields)}', action, params)
    try:
        ok, result, undo = EXEC[action](params)
    except Exception as e:
        db.session.rollback()
        log.exception('nedarim command failed')
        ok, result, undo = False, f'שגיאה בביצוע ({type(e).__name__})', None
    return finish('done' if ok else 'failed', f'{name}: {result}', action, params, undo)


def _alert_locked():
    from app import db
    from models import ManagerMessage
    db.session.add(ManagerMessage(
        phone=ADMIN_PHONE, source='nedarim_command', status='new',
        transcript='🔒 הפקודות מנדרים פלוס ננעלו אחרי 3 קודים שגויים. '
                   'לפתיחה: הודעות למנהל ← נדרים פלוס ← "פתיחת הפקודות".'))


# ---------------------------------------------------------------------------
# הפעולות. כל אחת מחזירה (הצליח?, תוצאה להקראה, נתוני ביטול או None)
# ---------------------------------------------------------------------------
def _customer(phone):
    from services.customer_phones import find_customer_by_phone
    return find_customer_by_phone(_phone(phone))


def _amount(v):
    a = round(float(str(v).replace(',', '.').replace('*', '.')), 2)
    if not (0 < a <= MAX_AMOUNT):
        raise ValueError
    return a


def _add_txn(c, amount, typ, desc):
    from app import db
    from models import Transaction
    c.balance = round((c.balance or 0) + amount, 2)
    db.session.add(Transaction(customer_id=c.id, amount=amount, type=typ, description=desc))


def a_status(p):
    from app import db
    from models import Recording
    from routes.admin import _message_tab_counts
    cnt = _message_tab_counts()
    since = datetime.utcnow() - timedelta(hours=24)
    recs = Recording.query.filter(Recording.created_at >= since).count()
    errs = Recording.query.filter(Recording.created_at >= since, Recording.status.in_(['error', 'failed'])).count()
    mt = 'דלוק' if _get('maintenance_mode', '0') == '1' else 'כבוי'
    return True, (f'ממתינות לטיפול {sum(cnt.values())} הודעות. הקלטות ביממה האחרונה {recs}, '
                  f'מהן שגיאות {errs}. מצב תחזוקה {mt}'), None


def a_balance(p):
    c = _customer(p[0])
    if not c:
        return False, 'לא נמצא לקוח עם המספר הזה', None
    return True, f'היתרה של {c.name or c.phone}: {_money(c.balance)}', None


def a_credit(p, sign=1):
    from app import db
    c = _customer(p[0])
    if not c:
        return False, 'לא נמצא לקוח עם המספר הזה', None
    try:
        a = _amount(p[1])
    except (ValueError, TypeError):
        return False, f'סכום לא תקין. אפשר עד {MAX_AMOUNT} שקל', None
    if sign > 0:
        _add_txn(c, a, 'credit', 'זיכוי בפקודה מנדרים פלוס')
    else:
        _add_txn(c, -a, 'debit', 'חיוב בפקודה מנדרים פלוס')
    db.session.commit()
    if sign > 0:
        try:
            from routes.dictate import trigger_pending_manuscripts
            trigger_pending_manuscripts(customer_id=c.id)
        except Exception:
            pass
    what = 'זוכה' if sign > 0 else 'חויב'
    return True, f'{c.name or c.phone} {what} ב-{_money(a)}. יתרה חדשה {_money(c.balance)}', \
        {'customer_id': c.id, 'amount': a * sign}


def a_maintenance(p):
    from app import db
    on = p[0] == '1'
    if p[0] not in ('0', '1'):
        return False, 'יש לכתוב 1 להפעלה או 0 לכיבוי', None
    prev = _get('maintenance_mode', '0')
    _set('maintenance_mode', '1' if on else '0')
    db.session.commit()
    extra = ''
    if not on and prev == '1':
        from services.transcribe import resume_queued_recordings
        from routes.email_inbound import resume_queued_ocr
        n = resume_queued_recordings() + resume_queued_ocr()
        extra = f'. שוחררו {n} פריטים שחיכו'
    return True, ('הופעל' if on else 'כובה') + extra, {'prev': prev}


def a_block(p):
    from app import db
    c = _customer(p[0])
    if not c:
        return False, 'לא נמצא לקוח עם המספר הזה', None
    if p[1] not in ('0', '1'):
        return False, 'יש לכתוב 1 לחסימה או 0 לשחרור', None
    prev = bool(c.is_blocked)
    c.is_blocked = p[1] == '1'
    db.session.commit()
    return True, f'{c.name or c.phone} ' + ('נחסם' if c.is_blocked else 'שוחרר'), \
        {'customer_id': c.id, 'prev': prev}


def _recording(num):
    from models import Recording
    try:
        return Recording.query.get(int(_digits(num)))
    except (ValueError, TypeError):
        return None


def a_retranscribe(p):
    from routes.admin import do_retranscribe
    rec = _recording(p[0])
    if not rec:
        return False, 'לא נמצאה הקלטה עם המספר הזה', None
    tier = {'1': 'gemini', '2': 'premium'}.get(p[1])
    if not tier:
        return False, 'יש לכתוב 1 לתמלול רגיל או 2 לתמלול מקצועי', None
    ok, msg = do_retranscribe(rec, tier)
    return ok, msg.replace('✔ ', ''), None


def a_resend(p):
    from services import document_history as DH
    rec = _recording(p[0])
    if not rec or not rec.customer:
        return False, 'לא נמצאה הקלטה עם המספר הזה', None
    r = DH.resend(rec.customer, 'recording', rec.id, confirm=True)
    texts = {'sent': 'נשלח שוב', 'no_target': 'אין ללקוח מייל או פקס', 'no_balance': 'אין מספיק יתרה לשליחה בפקס',
             'limit': 'הגעתם למגבלת השליחות החוזרות להיום', 'not_found': 'התמלול לא נמצא', 'failed': 'השליחה נכשלה'}
    return r.get('status') == 'sent', texts.get(r.get('status'), 'השליחה נכשלה'), None


def a_email(p):
    from app import db
    c = _customer(p[0])
    if not c:
        return False, 'לא נמצא לקוח עם המספר הזה', None
    email = p[1].strip()
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
        return False, 'כתובת המייל לא תקינה', None
    prev = c.email or ''
    c.email = email
    db.session.commit()
    return True, f'המייל של {c.name or c.phone} עודכן', {'customer_id': c.id, 'prev': prev}


def a_voice(p, everyone=False):
    from services import voice_messages as VM
    if everyone:
        m = VM.create(None, 'tts', text=' '.join(p))
        return True, 'ההודעה תושמע לכל הלקוחות בכניסה הבאה', {'voice_id': m.id}
    c = _customer(p[0])
    if not c:
        return False, 'לא נמצא לקוח עם המספר הזה', None
    m = VM.create(c.id, 'tts', text=' '.join(p[1:]))
    return True, f'ההודעה תושמע ל{c.name or c.phone} בכניסה הבאה', {'voice_id': m.id}


def a_handled(p):
    from app import db
    from models import ManagerMessage
    try:
        m = ManagerMessage.query.get(int(_digits(p[0])))
    except (ValueError, TypeError):
        m = None
    if not m:
        return False, 'לא נמצאה הודעה עם המספר הזה', None
    prev = {'status': m.status, 'note': m.admin_note}
    note = ' '.join(p[1:])
    m.admin_note = f'{m.admin_note}\n✔ טופל: {note}' if (m.admin_note or '').strip() else f'✔ טופל: {note}'
    m.status = 'done'
    db.session.commit()
    return True, f'הודעה {m.id} סומנה כטופלה', {'message_id': m.id, **prev}


def a_fax(p):
    from app import db
    v = {'1': 'exm', '2': 'yemot'}.get(p[0])
    if not v:
        return False, 'יש לכתוב 1 לאקסטרה מובייל או 2 לימות המשיח', None
    prev = _get('fax_provider', 'exm') or 'exm'
    _set('fax_provider', v)
    db.session.commit()
    return True, 'הפקסים יישלחו דרך ' + ('אקסטרה מובייל' if v == 'exm' else 'ימות המשיח'), {'prev': prev}


def a_undo(p):
    """מבטל את הפקודה האחרונה שבוצעה (ולא בוטלה כבר)."""
    from app import db
    from models import NedarimCommand, Customer, ManagerMessage, VoiceMessage
    last = (NedarimCommand.query.filter(NedarimCommand.status == 'done', NedarimCommand.action != 0)
            .order_by(NedarimCommand.id.desc()).first())
    if not last:
        return False, 'אין פקודה לבטל', None
    name = COMMANDS.get(last.action, ('',))[0]
    if last.action in NOT_UNDOABLE or not last.undo:
        return False, f'את הפקודה האחרונה ({name}) אי אפשר לבטל', None
    u = json.loads(last.undo)
    if last.action in (3, 4):
        c = Customer.query.get(u['customer_id'])
        amt = -u['amount']
        _add_txn(c, amt, 'credit' if amt > 0 else 'debit', f'ביטול פקודה {last.id} מנדרים פלוס')
        res = f'בוטל. יתרה חדשה {_money(c.balance)}'
    elif last.action == 5:
        _set('maintenance_mode', u['prev'])
        res = 'בוטל. מצב תחזוקה ' + ('דלוק' if u['prev'] == '1' else 'כבוי')
    elif last.action == 6:
        c = Customer.query.get(u['customer_id'])
        c.is_blocked = u['prev']
        res = 'בוטל. הלקוח ' + ('חסום' if c.is_blocked else 'לא חסום')
    elif last.action == 9:
        c = Customer.query.get(u['customer_id'])
        c.email = u['prev'] or None
        res = 'בוטל. המייל הקודם הוחזר'
    elif last.action in (10, 11):
        m = VoiceMessage.query.get(u['voice_id'])
        if m:
            m.active = False
        res = 'בוטל. ההודעה הקולית הופסקה'
    elif last.action == 12:
        m = ManagerMessage.query.get(u['message_id'])
        m.status, m.admin_note = u['status'], u['note']
        res = 'בוטל. ההודעה חזרה לסטטוס הקודם'
    elif last.action == 13:
        _set('fax_provider', u['prev'])
        res = 'בוטל. ספק הפקסים הוחזר'
    else:
        return False, 'אי אפשר לבטל', None
    last.status = 'undone'
    db.session.commit()
    if last.action == 5 and u['prev'] == '0':
        from services.transcribe import resume_queued_recordings
        resume_queued_recordings()
    return True, f'פקודה {last.id} ({name}) - {res}', None


EXEC = {
    0: a_undo, 1: a_status, 2: a_balance, 3: a_credit, 4: lambda p: a_credit(p, -1),
    5: a_maintenance, 6: a_block, 7: a_retranscribe, 8: a_resend, 9: a_email,
    10: a_voice, 11: lambda p: a_voice(p, everyone=True), 12: a_handled, 13: a_fax,
}


# ---------------------------------------------------------------------------
# לטלפון: תוצאות (שלוחה 6) והוראות (שלוחה 7)
# ---------------------------------------------------------------------------
def results_for_phone(limit=5):
    """תוצאות שעוד לא נשמעו (ומסמן אותן כנשמעו). אם אין חדשות - האחרונה שנשמעה."""
    from app import db
    from models import NedarimCommand
    new = NedarimCommand.query.filter_by(heard=False).order_by(NedarimCommand.id.asc()).all()
    if new:
        for c in new:
            c.heard = True
        db.session.commit()
        items = new[-limit:]
        if len(new) == 1:
            head = 'תוצאה חדשה אחת.'
        elif len(new) <= limit:
            head = f'יש {len(new)} תוצאות חדשות.'
        else:
            head = f'יש {len(new)} תוצאות חדשות. אלה {limit} האחרונות.'
    else:
        last = NedarimCommand.query.order_by(NedarimCommand.id.desc()).first()
        if not last:
            return 'עוד לא התקבלו פקודות מנדרים פלוס.'
        items, head = [last], 'אין תוצאות חדשות. התוצאה האחרונה:'
    parts = [head]
    for c in items:
        status = {'done': 'בוצעה', 'failed': 'לא בוצעה', 'rejected': 'נדחתה', 'undone': 'בוצעה ובוטלה'}.get(c.status, '')
        parts.append(f'פקודה {c.id}, {status}. {c.result}.')
    if is_locked():
        parts.append('שימו לב, הפקודות נעולות כרגע.')
    return ' '.join(parts)


def help_text():
    parts = ['הוראות לפקודות מנדרים פלוס. שולחים הודעה לגבאי מהמספר שלכם, בשורה אחת, '
             'עם מקף בין החלקים. קודם הקוד הסודי בן 6 הספרות, אחריו מקף וקוד הפעולה, '
             'ואחריו הפרטים, עם מקף לפני כל פרט. לדוגמה, זיכוי של 5 שקל: הקוד, מקף, 3, מקף, הטלפון, מקף, 5. '
             'מספר טלפון כותבים בלי מקפים. הפקודות נבדקות כל 10 דקות, '
             'והתוצאה נשמעת בתפריט המנהל בהקשה על 6. אלה הפעולות:']
    for code, (name, _fields, expl) in COMMANDS.items():
        parts.append(f'קוד {code}: {name}. {expl}.')
    parts.append(f'שימו לב: אחרי {MAX_FAILS} קודים שגויים הפקודות ננעלות, ופותחים אותן בממשק. '
                 'פקודה שנשלחה לפני יותר משעה לא מבוצעת.')
    return ' '.join(parts)
