"""
משימות / תזכורות (שלוחה 1 ← 5).

הלקוח מקליט את המשימה ואומר בדיבור חופשי מתי לקבל תזכורת (וגם אם היא חוזרת: כל
יום / כל שבוע / כל חודש). Gemini מתמלל את ההקלטה ומחלץ ממנה בקריאה אחת את תוכן
המשימה, את מועד התזכורת (שעון ישראל) ואת החזרתיות. הלקוח
שומע מה הובן ומאשר, ובוחר איך לקבל את התזכורת:
  - מייל: הודעה עם תוכן המשימה וקישור להאזנה להקלטה (בלי קובץ Word).
  - פקס: בעלות נוספת לכל שליחה (task_fax_price).
  - צינתוק: צינתוק ללקוח, וכשהוא מתקשר הוא שומע את התזכורת בהקראה ממוחשבת
    (הודעה קולית בכניסה). לקוח שעוד לא רשום לצינתוק החינמי - עובר מיד לרישום.

חיוב: לפי יחידות של 2 דקות הקלטה (price_per_2min_task), באישור המשימה. פקס - בכל
שליחה. לולאת רקע בודקת כל דקה אם יש תזכורות שהגיע זמנן (נעילה ב-Postgres - worker
אחד בכל פעם).
"""
import json
import logging
import math
import os
import secrets
import threading
import time
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

DEFAULT_PRICE = '0.30'
DEFAULT_FAX_PRICE = '1.00'
MAX_ACTIVE = 50                     # משימות פעילות ללקוח
RECURRENCES = ('none', 'daily', 'weekly', 'monthly')
REC_HE = {'none': '', 'daily': 'כל יום', 'weekly': 'כל שבוע', 'monthly': 'כל חודש'}
DAYS_HE = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']
MONTHS_HE = ['', 'ינואר', 'פברואר', 'מרץ', 'אפריל', 'מאי', 'יוני', 'יולי', 'אוגוסט', 'ספטמבר', 'אוקטובר', 'נובמבר', 'דצמבר']
_LOCK_KEY = 74120932
_started = False


def _tz():
    from zoneinfo import ZoneInfo
    return ZoneInfo('Asia/Jerusalem')


def _setting(key, default):
    from models import Settings
    s = Settings.query.filter_by(key=key).first()
    return s.value if s and s.value not in (None, '') else default


def enabled():
    return _setting('tasks_enabled', '0') == '1'


def unit_price():
    try:
        return float(_setting('price_per_2min_task', DEFAULT_PRICE))
    except (TypeError, ValueError):
        return float(DEFAULT_PRICE)


def fax_price():
    try:
        return float(_setting('task_fax_price', DEFAULT_FAX_PRICE))
    except (TypeError, ValueError):
        return float(DEFAULT_FAX_PRICE)


def cost_for(seconds):
    return round(max(1, math.ceil((seconds or 0) / 120)) * unit_price(), 2)


# ---------------------------------------------------------------------------
# תמלול וזיהוי הזמן
# ---------------------------------------------------------------------------
def _download(url):
    import requests
    r = requests.get(url, timeout=90)
    r.raise_for_status()
    return r.content


def _wav_seconds(data):
    import io
    import wave
    try:
        with wave.open(io.BytesIO(data)) as w:
            return int(round(w.getnframes() / float(w.getframerate() or 8000)))
    except Exception:
        return 0


_PROMPT = """בהקלטה המצורפת אדם מגדיר לעצמו משימה / תזכורת, ובתוכה הוא אומר בדיבור חופשי מתי לקבל את התזכורת.
התאריך והשעה עכשיו (שעון ישראל): {now} (יום {weekday}).
החזר JSON בלבד, במבנה:
{{"transcript": "תמלול מלא ומדויק של כל מה שנאמר", "text": "תוכן המשימה עצמה, בלי ההוראות על הזמן", "due": "YYYY-MM-DD HH:MM" או null, "recurrence": "none" | "daily" | "weekly" | "monthly"}}
כללים:
- due בשעון ישראל, תמיד בעתיד. "מחר בבוקר" = מחר 08:00; "בערב" = 19:00; "בצהריים" = 12:00; "בעוד שעה" = עכשיו + שעה; "בעוד עשר דקות" = עכשיו + 10 דקות.
- שעה בלי בוקר/ערב: לפי ההקשר - "בשמונה" בבוקר אם השעה עוד לא עברה היום, אחרת הקרובה בעתיד.
- אם נאמר תאריך בלי שעה - 09:00. אם נאמרה שעה שכבר עברה היום - מחר באותה שעה.
- יום בשבוע ("ביום שישי") או תאריך עברי ("בראש חודש") - חשב לתאריך הלועזי הקרוב בעתיד.
- "כל יום" / "כל בוקר" = daily, "כל שבוע" / "כל יום שני" = weekly, "כל חודש" = monthly. אחרת none.
- אם לא נאמר שום זמן - due = null."""


def analyze(data, now=None):
    """Gemini: מתמלל את ההקלטה ומחלץ ממנה את המשימה, המועד והחזרתיות - בקריאה אחת.
    מחזיר (transcript, text, due_utc או None, recurrence)."""
    from datetime import timezone
    from google import genai
    from google.genai import types as gtypes
    tz = _tz()
    now_il = (now or datetime.now(tz)).astimezone(tz)
    client = genai.Client(api_key=os.environ.get('GOOGLE_API_KEY'))
    prompt = _PROMPT.format(now=now_il.strftime('%Y-%m-%d %H:%M'), weekday=DAYS_HE[now_il.weekday()])
    last = None
    for attempt in range(3):
        try:
            res = client.models.generate_content(
                model=os.environ.get('TASKS_GEMINI_MODEL', 'gemini-3.5-flash'),
                contents=[prompt, gtypes.Part.from_bytes(data=data, mime_type='audio/wav')],
                config=gtypes.GenerateContentConfig(
                    response_mime_type='application/json',
                    thinking_config=gtypes.ThinkingConfig(thinking_budget=1024)))
            out = json.loads(res.text or '{}')
            break
        except Exception as e:
            last = e
            log.warning(f'tasks: Gemini נכשל (ניסיון {attempt + 1}): {e}')
            time.sleep(2)
    else:
        raise RuntimeError(f'עיבוד ההקלטה נכשל: {last}')
    transcript = (out.get('transcript') or '').strip()
    text = (out.get('text') or transcript).strip()
    rec = out.get('recurrence') if out.get('recurrence') in RECURRENCES else 'none'
    due_utc = None
    if out.get('due'):
        try:
            due_il = datetime.strptime(str(out['due']).strip()[:16], '%Y-%m-%d %H:%M').replace(tzinfo=tz)
            if due_il <= now_il:
                due_il += timedelta(days=1)
            due_utc = due_il.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            due_utc = None
    log.info(f'tasks: Gemini הבין: "{text[:80]}" | מועד {out.get("due")} | {rec}')
    return transcript, text, due_utc, rec


def when_text(due_utc, recurrence='none'):
    """'ביום שני, 10/11 בשעה 08:00' (+ 'וחוזר כל יום')."""
    from datetime import timezone
    tz = _tz()
    d = due_utc.replace(tzinfo=timezone.utc).astimezone(tz)
    today = datetime.now(tz).date()
    if d.date() == today:
        day = 'היום'
    elif d.date() == today + timedelta(days=1):
        day = 'מחר'
    else:
        day = f'ביום {DAYS_HE[d.weekday()]}, {d.day} ב{MONTHS_HE[d.month]}'
    s = f'{day} בשעה {d.hour}:{d.minute:02d}'
    if recurrence and recurrence != 'none':
        s += f', וחוזר {REC_HE[recurrence]}'
    return s


# ---------------------------------------------------------------------------
# יצירה ואישור (מה-IVR)
# ---------------------------------------------------------------------------
def create_from_phone(phone, rec_url, call_id):
    from app import db
    from models import CustomerTask, Recording
    from services.customer_phones import find_customer_by_phone
    customer = find_customer_by_phone(phone)
    if not customer:
        return {'ok': False, 'error': 'customer'}
    from services.customer_mail import is_yemot_url
    if not is_yemot_url(rec_url):
        return {'ok': False, 'error': 'recording'}
    if CustomerTask.query.filter_by(customer_id=customer.id, status='active').count() >= MAX_ACTIVE:
        return {'ok': False, 'error': 'limit'}
    try:
        data = _download(rec_url)
    except Exception as e:
        log.warning(f'tasks: הורדת ההקלטה נכשלה: {e}')
        return {'ok': False, 'error': 'recording'}
    secs = _wav_seconds(data)
    cost = cost_for(secs)
    if (customer.balance or 0) < cost:
        return {'ok': False, 'error': 'balance', 'cost': cost}
    try:
        transcript, text, due, rec_kind = analyze(data)
        if not (transcript or text):
            return {'ok': False, 'error': 'empty'}
    except Exception as e:
        log.error(f'tasks: עיבוד המשימה נכשל: {e}')
        return {'ok': False, 'error': 'ai'}
    rec = Recording(customer_id=customer.id, call_id=call_id or f'task-{secrets.token_hex(8)}',
                    duration_seconds=secs, status='delivered', rec_url=rec_url, transcription_tier='task',
                    language='he', output_language='he', transcript=transcript, cost=0.0,
                    source_filename='משימה / תזכורת')
    db.session.add(rec)
    db.session.flush()
    t = CustomerTask(customer_id=customer.id, recording_id=rec.id, transcript=transcript, text=text,
                     duration_seconds=secs, due_at=due, recurrence=rec_kind, status='draft', cost=cost)
    db.session.add(t)
    db.session.commit()
    return {'ok': True, 'task_id': t.id, 'text': text, 'has_time': bool(due),
            'when': when_text(due, rec_kind) if due else '', 'cost': cost,
            'email': customer.email or '', 'fax': customer.fax or '',
            'fax_price': fax_price(), 'tzintuk_registered': bool(customer.tzintuk_list)}


CHANNELS = ('email', 'fax', 'tzintuk')
CHANNEL_HE = {'email': 'מייל', 'fax': 'פקס', 'tzintuk': 'צינתוק'}


def channels_of(value):
    """'email,tzintuk' -> ['email', 'tzintuk'] (סדר קבוע, בלי כפילויות / ערכים לא מוכרים)."""
    got = {(c or '').strip() for c in str(value or '').split(',')}
    return [c for c in CHANNELS if c in got]


def channels_he(value):
    return ' + '.join(CHANNEL_HE[c] for c in channels_of(value)) or '—'


def confirm(phone, task_id, channel):
    """הלקוח אישר ובחר ערוץ. מחייב ומפעיל. מחזיר dict."""
    from app import db
    from models import CustomerTask, Transaction
    from services.customer_phones import find_customer_by_phone
    customer = find_customer_by_phone(phone)
    t = CustomerTask.query.filter_by(id=int(task_id or 0), customer_id=customer.id if customer else -1).first()
    if not customer or not t or t.status != 'draft':
        return {'ok': False, 'error': 'task'}
    chans = channels_of(channel)
    if not chans or ('email' in chans and 'fax' in chans):
        return {'ok': False, 'error': 'channel'}
    channel = ','.join(chans)
    if not t.due_at:
        return {'ok': False, 'error': 'time'}
    target = customer.email if 'email' in chans else customer.fax if 'fax' in chans else None
    if ('email' in chans or 'fax' in chans) and not target:
        return {'ok': False, 'error': 'no_target'}
    if (customer.balance or 0) < t.cost:
        return {'ok': False, 'error': 'balance', 'cost': t.cost}
    customer.balance -= t.cost
    db.session.add(Transaction(customer_id=customer.id, amount=-t.cost, type='debit', recording_id=t.recording_id,
                               description=f'משימה / תזכורת ({max(1, math.ceil((t.duration_seconds or 0) / 120)) * 2} דק׳)'))
    t.channel, t.target, t.status = channel, target, 'active'
    db.session.commit()
    tz_folder = None
    if 'tzintuk' in chans:
        from services import voice_messages as VM
        try:
            VM.sync_tzintuk(max_age=0)
            db.session.refresh(customer)
        except Exception:
            pass
        if not customer.tzintuk_list:     # עוד לא רשום לצינתוק החינמי - מעבר מיד לרישום
            tz_folder, _err = VM.prepare_personal(phone, 'add')
    log.info(f'tasks: משימה {t.id} הופעלה ({channel}) ללקוח {customer.id}, {when_text(t.due_at, t.recurrence)}')
    return {'ok': True, 'tzintuk_folder': tz_folder}


def discard(phone, task_id):
    from app import db
    from models import CustomerTask
    from services.customer_phones import find_customer_by_phone
    customer = find_customer_by_phone(phone)
    t = CustomerTask.query.filter_by(id=int(task_id or 0), customer_id=customer.id if customer else -1).first()
    if t and t.status == 'draft':
        t.status = 'cancelled'
        db.session.commit()
    return {'ok': True}


def active_tasks(customer):
    from models import CustomerTask
    return (CustomerTask.query.filter_by(customer_id=customer.id, status='active')
            .order_by(CustomerTask.due_at.asc()).all())


def cancel(customer, task_id):
    from app import db
    from models import CustomerTask
    t = CustomerTask.query.filter_by(id=int(task_id or 0), customer_id=customer.id).first()
    if t and t.status in ('active', 'draft'):
        t.status = 'cancelled'
        db.session.commit()
        return True
    return False


# ---------------------------------------------------------------------------
# שליחת תזכורות
# ---------------------------------------------------------------------------
def _next_due(t):
    from datetime import timezone
    if not t.recurrence or t.recurrence == 'none':
        return None
    tz = _tz()
    d = t.due_at.replace(tzinfo=timezone.utc).astimezone(tz)
    now = datetime.now(tz)
    while d <= now:
        if t.recurrence == 'daily':
            d += timedelta(days=1)
        elif t.recurrence == 'weekly':
            d += timedelta(days=7)
        elif t.recurrence == 'monthly':
            m = d.month + 1
            y = d.year + (1 if m > 12 else 0)
            m = 1 if m > 12 else m
            import calendar
            d = d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))
        else:
            return None
    return d.astimezone(timezone.utc).replace(tzinfo=None)


def reminder_html(t, customer, listen_url):
    from html import escape
    return f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:600px;margin:auto;color:#111827;line-height:1.8">
<h2 style="color:#1d4ed8;margin:0 0 10px">⏰ תזכורת</h2>
<div style="background:#eff6ff;border-right:4px solid #3b82f6;padding:16px;border-radius:8px;font-size:17px">{escape(t.text or t.transcript or '')}</div>
{f'<p style="margin:18px 0"><a href="{escape(listen_url)}" style="background:#2563eb;color:#fff;text-decoration:none;padding:10px 22px;border-radius:8px;font-weight:700;display:inline-block">🎧 להאזנה להקלטה</a></p>' if listen_url else ''}
{f'<p style="color:#6b7280;font-size:13px">התזכורת חוזרת {REC_HE[t.recurrence]}. לביטול: בטלפון, שלוחה 1 ואז 5.</p>' if t.recurrence != 'none' else ''}
<p style="color:#6b7280;font-size:13px">מערכת תמלול פון 03-3131795</p></div>'''


def deliver(t):
    """שולח תזכורת אחת בכל הערוצים שנבחרו (מייל / פקס / צינתוק, או שילוב). זורק שגיאה
    רק אם כל הערוצים נכשלו; כשל חלקי נרשם ב-last_error."""
    customer_errors = []
    sent = 0
    for ch in channels_of(t.channel):
        try:
            _deliver_one(t, ch)
            sent += 1
        except Exception as e:
            log.error(f'tasks: משימה {t.id} - {CHANNEL_HE[ch]} נכשל: {e}')
            customer_errors.append(f'{CHANNEL_HE[ch]}: {e}')
    if customer_errors and not sent:
        raise RuntimeError('; '.join(customer_errors))
    if customer_errors:
        t.last_error = '; '.join(customer_errors)[:500]


def _deliver_one(t, channel):
    from app import db
    from models import Customer, Transaction
    customer = Customer.query.get(t.customer_id)
    if channel == 'email':
        from services.customer_mail import _send
        from services.email_layout import wrap_existing
        from routes.recording_proxy import recording_download_url
        _send(t.target, f'תזכורת: {(t.text or "")[:60]}',
              wrap_existing(reminder_html(t, customer, recording_download_url(t.recording_id)), phone=customer.phone))
    elif channel == 'fax':
        price = fax_price()
        if (customer.balance or 0) < price:
            raise RuntimeError('אין מספיק יתרה לשליחת פקס')
        from services.transcribe import _build_pdf_for_fax, send_pdf_fax
        pdf = _build_pdf_for_fax(customer.name or customer.phone, 'תזכורת', t.text or t.transcript or '')
        res = send_pdf_fax(t.target, pdf, filename_hint=f'task_{t.id}.pdf')
        if not res.get('ok'):
            raise RuntimeError(res.get('error') or 'שליחת הפקס נכשלה')
        customer.balance -= price
        db.session.add(Transaction(customer_id=customer.id, amount=-price, type='debit', recording_id=t.recording_id,
                                   description='תזכורת בפקס'))
    elif channel == 'tzintuk':
        from services import voice_messages as VM
        VM.create(customer.id, 'tts', text=f'תזכורת: {t.text or t.transcript or ""}')
        ok, msg = VM.tzintuk_customer(customer)
        if not ok:
            log.warning(f'tasks: צינתוק למשימה {t.id} נכשל: {msg} (ההקראה תחכה בכניסה)')


def run_due(now=None):
    from app import db
    from models import CustomerTask
    if _setting('maintenance_mode', '0') == '1':
        return 0          # מצב תחזוקה - התזכורות יישלחו מיד כשהמצב יכובה (באיחור של דקות)
    if db.engine.dialect.name == 'postgresql':
        if not db.session.execute(db.text('SELECT pg_try_advisory_xact_lock(:k)'), {'k': _LOCK_KEY}).scalar():
            db.session.rollback()
            return 0
    now = now or datetime.utcnow()
    due = (CustomerTask.query.filter(CustomerTask.status == 'active', CustomerTask.due_at <= now)
           .order_by(CustomerTask.due_at.asc()).limit(50).all())
    n = 0
    from services.job_tracker import tracked
    for t in due:
        failed = False
        try:
            t.last_error = None
            with tracked('task', label=f'תזכורת #{t.id}'):
                deliver(t)
            t.sent_count = (t.sent_count or 0) + 1
            t.last_sent_at = datetime.utcnow()
            n += 1
        except Exception as e:
            failed = True
            log.error(f'tasks: שליחת משימה {t.id} נכשלה: {e}')
            t.last_error = str(e)[:500]
        nxt = _next_due(t)
        if nxt:
            t.due_at = nxt
        else:
            t.status = 'failed' if failed else 'done'
        db.session.commit()
    if not due:
        db.session.commit()
    return n


def _loop(app):
    time.sleep(45)
    while True:
        try:
            with app.app_context():
                run_due()
        except Exception as e:
            log.warning(f'tasks loop: {e}')
            try:
                with app.app_context():
                    from app import db
                    db.session.rollback()
            except Exception:
                pass
        time.sleep(60)


def start(app):
    global _started
    if _started or os.environ.get('TASKS_LOOP_DISABLED') == '1':
        return
    _started = True
    threading.Thread(target=_loop, args=(app,), daemon=True, name='tasks').start()
