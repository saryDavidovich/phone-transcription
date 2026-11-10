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
           source='web', rec_url=None, customer_id=None):
    """מחזיר (task, שגיאה)."""
    from app import db
    from models import AdminTask
    title = (title or '').strip()[:200]
    if not title:
        return None, 'חובה לכתוב את המשימה'
    t = AdminTask(kind=kind, title=title, details=(details or '').strip() or None, source=source,
                  rec_url=rec_url, customer_id=customer_id)
    if kind == 'scheduled':
        if not due_local:
            return None, 'במשימה עם מועד חובה לבחור תאריך ושעה'
        if not t.details and source == 'web':
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
            'source': t.source, 'has_audio': bool(t.rec_url), 'customer_id': t.customer_id}


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
