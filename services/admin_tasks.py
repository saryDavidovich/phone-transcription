"""
משימות למנהל: לוח שנה (לו"ז) + תזכורות. ראה models.AdminTask.

- משימה מתוזמנת: בזמן שלה צץ חלון בכל עמוד בממשק הניהול (static: base.html בודק כל 30 שניות),
  ואי אפשר לסגור אותו עד שרושמים "בוצע" עם הסבר.
- תזכורת: בלי תאריך, חוזרת כל X דקות. "אחר כך" סוגר עד הפעם הבאה; "בוצע" (הסבר רשות) מסיים.
- מהטלפון (תפריט מנהל ← 8): מקליטים בדיבור חופשי - Gemini מתמלל ומבין מתי (services/tasks.analyze).
  עם מועד = משימה מתוזמנת; בלי מועד = תזכורת (כל יום / כל שבוע לפי מה שנאמר, אחרת כל 3 שעות).
"""
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

INTERVALS = [(30, 'כל חצי שעה'), (60, 'כל שעה'), (120, 'כל שעתיים'), (180, 'כל 3 שעות'),
             (360, 'כל 6 שעות'), (1440, 'כל יום'), (10080, 'כל שבוע'), (43200, 'כל חודש')]
DEFAULT_PHONE_INTERVAL = 180
# (מפתח, שם, צבע) - הצבע של המשימה בלוח
CATEGORIES = [('general', 'כללי', '#64748b'), ('customers', 'לקוחות', '#3b82f6'), ('money', 'כספים', '#10b981'),
              ('tech', 'טכני', '#f59e0b'), ('personal', 'אישי', '#a855f7')]
CAT_COLOR = {k: c for k, _n, c in CATEGORIES}
CAT_NAME = {k: n for k, n, _c in CATEGORIES}
LATE_MINUTES = 30            # "בוצע באיחור" = סומן בוצע יותר מחצי שעה אחרי המועד
ADMIN_PHONE = '0527134491'


def _tz():
    from zoneinfo import ZoneInfo
    return ZoneInfo('Asia/Jerusalem')


def to_utc(local_naive):
    return local_naive.replace(tzinfo=_tz()).astimezone(timezone.utc).replace(tzinfo=None)


def to_local(utc_naive):
    return utc_naive.replace(tzinfo=timezone.utc).astimezone(_tz()) if utc_naive else None


def interval_label(m):
    return dict(INTERVALS).get(m) or (f'כל {m} דקות' if m else '')


def create(title, details='', kind='scheduled', due_local=None, interval_minutes=None,
           source='web', rec_url=None, customer_id=None, category=None):
    """מחזיר (task, שגיאה)."""
    from app import db
    from models import AdminTask
    title = (title or '').strip()[:200]
    if not title:
        return None, 'חובה לכתוב את המשימה'
    t = AdminTask(kind=kind, title=title, details=(details or '').strip() or None, source=source,
                  rec_url=rec_url, customer_id=customer_id,
                  category=category if category in CAT_COLOR else ('customers' if customer_id else 'general'))
    if kind == 'scheduled':
        if not due_local:
            return None, 'במשימה עם מועד חובה לבחור תאריך ושעה'
        if not t.details and source == 'web' and not customer_id:   # מכרטיס לקוח - הפירוט רשות
            return None, 'במשימה עם מועד חובה לפרט את המשימה'
        t.due_at = to_utc(due_local)
    else:
        try:
            m = int(interval_minutes or DEFAULT_PHONE_INTERVAL)
        except (TypeError, ValueError):
            m = DEFAULT_PHONE_INTERVAL
        t.kind = 'reminder'
        t.interval_minutes = max(5, m)
        t.next_remind_at = datetime.utcnow() + timedelta(minutes=t.interval_minutes)
    db.session.add(t)
    db.session.commit()
    return t, None


def due_now(now=None):
    """המשימות שהחלון שלהן צריך להיות פתוח עכשיו."""
    from models import AdminTask
    now = now or datetime.utcnow()
    sched = (AdminTask.query.filter(AdminTask.status == 'open', AdminTask.kind == 'scheduled',
                                    AdminTask.due_at <= now).order_by(AdminTask.due_at).all())
    rem = (AdminTask.query.filter(AdminTask.status == 'open', AdminTask.kind == 'reminder',
                                  AdminTask.next_remind_at <= now).order_by(AdminTask.next_remind_at).all())
    return sched + rem


def payload(t):
    loc = to_local(t.due_at)
    return {'id': t.id, 'kind': t.kind, 'title': t.title, 'details': t.details or '',
            'when': loc.strftime('%d/%m %H:%M') if loc else interval_label(t.interval_minutes),
            'source': t.source, 'has_audio': bool(t.rec_url), 'customer_id': t.customer_id,
            'customer_name': _customer_name(t.customer_id)}


def _customer_name(cid):
    if not cid:
        return ''
    from models import Customer
    c = Customer.query.get(cid)
    return (c.name or c.phone or '') if c else ''


def mark_done(task_id, note=''):
    """מחזיר שגיאה או None. משימה מתוזמנת - חובה הסבר."""
    from app import db
    from models import AdminTask
    t = AdminTask.query.get(task_id)
    if not t or t.status != 'open':
        return 'המשימה לא נמצאה'
    note = (note or '').strip()
    if t.kind == 'scheduled' and len(note) < 2:
        return 'חובה לכתוב הסבר על הביצוע'
    t.status, t.done_note, t.done_at = 'done', note or None, datetime.utcnow()
    db.session.commit()
    return None


def snooze(task_id):
    """תזכורת: נסגרת עד הפעם הבאה (משימה מתוזמנת לא נסגרת בלי בוצע)."""
    from app import db
    from models import AdminTask
    t = AdminTask.query.get(task_id)
    if not t or t.status != 'open' or t.kind != 'reminder':
        return 'אי אפשר לדחות את המשימה הזו'
    t.next_remind_at = datetime.utcnow() + timedelta(minutes=t.interval_minutes or DEFAULT_PHONE_INTERVAL)
    db.session.commit()
    return None


def move(task_id, date_iso):
    """גרירה בלוח: משימה מתוזמנת עוברת ליום אחר באותה שעה."""
    from app import db
    from models import AdminTask
    t = AdminTask.query.get(task_id)
    if not t or t.status != 'open' or t.kind != 'scheduled':
        return 'אפשר להזיז רק משימה פתוחה עם מועד'
    try:
        d = datetime.strptime(date_iso, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return 'תאריך לא תקין'
    loc = to_local(t.due_at)
    t.due_at = to_utc(datetime(d.year, d.month, d.day, loc.hour, loc.minute))
    t.notified_at = None
    db.session.commit()
    return None


def cancel(task_id):
    from app import db
    from models import AdminTask
    t = AdminTask.query.get(task_id)
    if t and t.status == 'open':
        t.status = 'cancelled'
        db.session.commit()
        return True
    return False


def month_grid(year, month):
    """לוח חודש: שבועות (ראשון-שבת) של {date, in_month, tasks}."""
    import calendar
    from models import AdminTask
    first = datetime(year, month, 1)
    start_local = first - timedelta(days=(first.weekday() + 1) % 7)          # ראשון
    end_local = start_local + timedelta(days=42)
    rows = (AdminTask.query.filter(AdminTask.kind == 'scheduled', AdminTask.status != 'cancelled',
                                   AdminTask.due_at >= to_utc(start_local), AdminTask.due_at < to_utc(end_local))
            .order_by(AdminTask.due_at).all())
    by_day = {}
    for t in rows:
        by_day.setdefault(to_local(t.due_at).date(), []).append(t)
    weeks, d = [], start_local
    for _w in range(6):
        week = []
        for _i in range(7):
            week.append({'date': d.date(), 'in_month': d.month == month, 'tasks': by_day.get(d.date(), [])})
            d += timedelta(days=1)
        weeks.append(week)
    return weeks


# ---------------------------------------------------------------------------
# טלפון (תפריט מנהל ← 8)
# ---------------------------------------------------------------------------
def create_from_phone(rec_url):
    """הקלטה -> משימה (Gemini). מחזיר dict להקראה."""
    import requests
    from services import tasks as TK
    from services.customer_mail import is_yemot_url
    if not is_yemot_url(rec_url):
        return {'ok': False, 'error': 'recording'}
    try:
        r = requests.get(rec_url, timeout=60)
        r.raise_for_status()
        transcript, text, due_utc, rec = TK.analyze(r.content)
    except Exception as e:
        log.error(f'admin_tasks: עיבוד הקלטה נכשל: {e}')
        return {'ok': False, 'error': 'ai'}
    title = (text or transcript or '').strip()
    if not title:
        return {'ok': False, 'error': 'empty'}
    if due_utc:
        t, err = create(title[:200], details=transcript, kind='scheduled', due_local=to_local(due_utc).replace(tzinfo=None),
                        source='phone', rec_url=rec_url)
        when = TK.when_text(due_utc)
    else:
        minutes = {'daily': 1440, 'weekly': 10080, 'monthly': 43200}.get(rec, DEFAULT_PHONE_INTERVAL)
        t, err = create(title[:200], details=transcript, kind='reminder', interval_minutes=minutes,
                        source='phone', rec_url=rec_url)
        when = f'תזכורת {interval_label(minutes)}'
    if err:
        return {'ok': False, 'error': 'save'}
    return {'ok': True, 'id': t.id, 'text': title, 'when': when}


def agenda_text(day_offset=0):
    """הלו"ז של היום (או מחר) + התזכורות הפתוחות - טקסט להקראה."""
    from models import AdminTask
    tz = _tz()
    day = (datetime.now(tz) + timedelta(days=day_offset)).date()
    start = datetime(day.year, day.month, day.day)
    rows = (AdminTask.query.filter(AdminTask.kind == 'scheduled', AdminTask.status == 'open',
                                   AdminTask.due_at >= to_utc(start), AdminTask.due_at < to_utc(start + timedelta(days=1)))
            .order_by(AdminTask.due_at).all())
    overdue = (AdminTask.query.filter(AdminTask.kind == 'scheduled', AdminTask.status == 'open',
                                      AdminTask.due_at < to_utc(start)).count()) if day_offset == 0 else 0
    rem = AdminTask.query.filter_by(kind='reminder', status='open').count()
    label = 'היום' if day_offset == 0 else 'מחר'
    parts = []
    if overdue:
        parts.append('יש משימה אחת שעבר זמנה ועוד לא סומנה כבוצעה.' if overdue == 1 else f'יש {overdue} משימות שעבר זמנן ועוד לא סומנו כבוצעו.')
    if rows:
        parts.append(f'{label} יש משימה אחת.' if len(rows) == 1 else f'{label} יש {len(rows)} משימות.')
        for t in rows:
            loc = to_local(t.due_at)
            parts.append(f'בשעה {loc.hour}:{loc.minute:02d}: {t.title}.')
    else:
        parts.append(f'{label} אין משימות בלוח.')
    if rem:
        parts.append('ויש תזכורת אחת פתוחה.' if rem == 1 else f'ויש {rem} תזכורות פתוחות.')
    return ' '.join(parts)


# ---------------------------------------------------------------------------
# הגדרות (נשמרות בטבלת settings)
# ---------------------------------------------------------------------------
SETTING_DEFAULTS = {'admin_tasks_tzintuk': '1', 'admin_tasks_tzintuk_reminders': '0',
                    'admin_tasks_weekly_email': '1', 'admin_tasks_report_email': ''}


def get_setting(key):
    from models import Settings
    r = Settings.query.filter_by(key=key).first()
    return r.value if r and r.value is not None else SETTING_DEFAULTS.get(key, '')


def set_setting(key, value):
    from app import db
    from models import Settings
    r = Settings.query.filter_by(key=key).first()
    if not r:
        r = Settings(key=key)
        db.session.add(r)
    r.value = value
    db.session.commit()


def report_email():
    e = (get_setting('admin_tasks_report_email') or '').strip()
    if e:
        return e
    from services.security import allowed_admin_emails
    lst = allowed_admin_emails()
    return lst[0] if lst else ''


# ---------------------------------------------------------------------------
# צינתוק לטלפון המנהל כשמגיע זמן משימה (רץ כל דקה מתוך הלולאה של services/tasks.py)
# ---------------------------------------------------------------------------
def _tzintuk_admin(text):
    """הקראה שמחכה בכניסה למערכת (אם המנהל רשום כלקוח) + צינתוק."""
    from services import voice_messages as VM
    from services.customer_phones import find_customer_by_phone
    c = find_customer_by_phone(ADMIN_PHONE)
    if c:
        try:
            VM.create(c.id, 'tts', text=text)
        except Exception as e:
            log.warning(f'admin_tasks: הקראה למנהל נכשלה: {e}')
        return VM.tzintuk_customer(c)
    return VM.run_tzintuk_paid(ADMIN_PHONE)


def run_notify(now=None):
    from app import db
    from models import AdminTask
    if get_setting('admin_tasks_tzintuk') != '1':
        return 0
    if db.engine.dialect.name == 'postgresql':
        if not db.session.execute(db.text('SELECT pg_try_advisory_xact_lock(74120935)')).scalar():
            db.session.rollback()
            return 0
    now = now or datetime.utcnow()
    rows = (AdminTask.query.filter(AdminTask.status == 'open', AdminTask.kind == 'scheduled',
                                   AdminTask.notified_at.is_(None), AdminTask.due_at <= now,
                                   AdminTask.due_at > now - timedelta(hours=2)).all())
    if get_setting('admin_tasks_tzintuk_reminders') == '1':
        rows += [t for t in AdminTask.query.filter(AdminTask.status == 'open', AdminTask.kind == 'reminder',
                                                     AdminTask.next_remind_at <= now).all()
                 if not t.notified_at or t.notified_at < t.next_remind_at]
    for t in rows:
        t.notified_at = now
    db.session.commit()       # מסמנים לפני השליחה - שלא יישלח פעמיים גם אם הצינתוק נתקע
    if not rows:
        return 0
    head = 'הגיע זמן משימה' if len(rows) == 1 else f'הגיע זמן של {len(rows)} משימות'
    text = head + ': ' + '. '.join(t.title for t in rows) + '.'
    try:
        ok, msg = _tzintuk_admin(text)
        if not ok:
            log.warning(f'admin_tasks: צינתוק נכשל: {msg}')
    except Exception as e:
        log.warning(f'admin_tasks: צינתוק נכשל: {e}')
    return len(rows)


# ---------------------------------------------------------------------------
# דוח שבועי (בדף, בטלפון, ובמייל ביום ראשון בבוקר)
# ---------------------------------------------------------------------------
def week_start(d):
    """יום ראשון של השבוע של התאריך d."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def weekly_report(start_date):
    from models import AdminTask
    start = datetime(start_date.year, start_date.month, start_date.day)
    s_utc, e_utc = to_utc(start), to_utc(start + timedelta(days=7))
    done = (AdminTask.query.filter(AdminTask.status == 'done', AdminTask.done_at >= s_utc, AdminTask.done_at < e_utc)
            .order_by(AdminTask.done_at).all())
    late = [t for t in done if t.kind == 'scheduled' and t.due_at and t.done_at > t.due_at + timedelta(minutes=LATE_MINUTES)]
    missed = (AdminTask.query.filter(AdminTask.status == 'open', AdminTask.kind == 'scheduled',
                                     AdminTask.due_at >= s_utc, AdminTask.due_at < min(e_utc, datetime.utcnow()))
              .order_by(AdminTask.due_at).all())
    added = AdminTask.query.filter(AdminTask.created_at >= s_utc, AdminTask.created_at < e_utc).count()
    late_ids = {t.id for t in late}
    return {'start': start_date, 'end': start_date + timedelta(days=6), 'done': done, 'late_ids': late_ids,
            'late': len(late), 'on_time': len([t for t in done if t.kind == 'scheduled']) - len(late),
            'reminders_done': len([t for t in done if t.kind == 'reminder']), 'missed': missed, 'added': added}


def weekly_text(start_date):
    r = weekly_report(start_date)
    parts = [f'בשבוע הזה סומנו {len(r["done"])} משימות כבוצעו.' if len(r['done']) != 1 else 'בשבוע הזה סומנה משימה אחת כבוצעה.']
    if r['on_time'] or r['late']:
        parts.append(f'בזמן: {r["on_time"]}. באיחור: {r["late"]}.')
    if r['reminders_done']:
        parts.append(f'תזכורות שהסתיימו: {r["reminders_done"]}.')
    if r['missed']:
        parts.append(f'משימות שעבר זמנן ועוד לא בוצעו: {len(r["missed"])}.')
    parts.append(f'נוספו השבוע {r["added"]} משימות חדשות.')
    return ' '.join(parts)


def weekly_html(start_date):
    from html import escape
    r = weekly_report(start_date)
    rows = ''.join(
        f'<tr><td style="padding:6px;border-bottom:1px solid #eee">{to_local(t.done_at).strftime("%d/%m %H:%M")}</td>'
        f'<td style="padding:6px;border-bottom:1px solid #eee">{escape(t.title)}'
        f'{" <b style=color:#dc2626>(באיחור)</b>" if t.id in r["late_ids"] else ""}</td>'
        f'<td style="padding:6px;border-bottom:1px solid #eee;color:#555">{escape(t.done_note or "")}</td></tr>'
        for t in r['done'])
    missed = ''.join(f'<li>{to_local(t.due_at).strftime("%d/%m %H:%M")} - {escape(t.title)}</li>' for t in r['missed'])
    return (f'<div dir="rtl" style="font-family:Arial,sans-serif;font-size:14px">'
            f'<h2 style="margin:0 0 8px">דוח משימות שבועי {r["start"].strftime("%d/%m")}–{r["end"].strftime("%d/%m")}</h2>'
            f'<p>{escape(weekly_text(start_date))}</p>'
            + (f'<h3>מה בוצע</h3><table style="border-collapse:collapse;width:100%">{rows}</table>' if rows else '')
            + (f'<h3 style="color:#dc2626">לא בוצעו</h3><ul>{missed}</ul>' if missed else '') + '</div>')


def run_weekly_email(now=None):
    """ביום ראשון אחרי 8:00 - שולח את הדוח של השבוע שעבר (פעם אחת לשבוע)."""
    if get_setting('admin_tasks_weekly_email') != '1':
        return False
    loc = (now.replace(tzinfo=timezone.utc).astimezone(_tz()) if now else datetime.now(_tz()))
    if loc.weekday() != 6 or loc.hour < 8:
        return False
    prev = week_start(loc.date()) - timedelta(days=7)
    key = prev.isoformat()
    if get_setting('admin_tasks_week_sent') == key:
        return False
    to = report_email()
    if not to:
        return False
    set_setting('admin_tasks_week_sent', key)
    try:
        from services.customer_mail import _send
        _send(to, f'דוח משימות שבועי {prev.strftime("%d/%m")}', weekly_html(prev), from_name='מערכת התמלול')
    except Exception as e:
        log.warning(f'admin_tasks: מייל דוח שבועי נכשל: {e}')
        return False
    return True
