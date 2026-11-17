"""
פעולות על תלמידי מוסד שמשותפות לממשק ולתהליכי הרקע:
- זיכוי / הורדת יתרה לתלמיד (מיתרת המוסד ואליה) - פעולה אחת, אחידה, גם לזיכוי בודד, לזיכוי לכמה
  תלמידים יחד ולזיכוי החודשי האוטומטי.
- שימוש החודש לכל תלמיד (דקות ועלות).
- הזיכוי החודשי האוטומטי והמייל השבועי על יתרה נמוכה (נקראים מלולאת הרקע ב-services/tasks.py).
"""
import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
IL = ZoneInfo('Asia/Jerusalem')

CREDIT_DESC = 'זיכוי ע"י המוסד'
MONTHLY_DESC = 'זיכוי חודשי אוטומטי ע"י המוסד'
TAKE_DESC = 'הורדת יתרה ע"י המוסד (הוחזר ליתרת המוסד)'
DEFAULT_LOW = 5.0


def low_threshold(inst):
    return inst.low_balance_threshold if inst.low_balance_threshold is not None else DEFAULT_LOW


def month_start_utc(now=None):
    now = (now or datetime.now(IL)).astimezone(IL)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(ZoneInfo('UTC')).replace(tzinfo=None)


def change_balance(inst, student, amount, desc=None):
    """זיכוי (חיובי, יורד מיתרת המוסד) או הורדה (שלילי, חוזר למוסד). מחזיר הודעת שגיאה או None.
    לא מבצע commit - הקורא אחראי (כדי שפעולה על כמה תלמידים תהיה אטומית ככל האפשר)."""
    from app import db
    from models import Transaction
    amount = round(float(amount), 2)
    if not amount:
        return 'סכום לא תקין'
    if amount > 0:
        if (inst.balance or 0) + 1e-9 < amount:
            return f'אין למוסד מספיק יתרה (יתרת המוסד: {inst.balance or 0:.2f} ₪)'
        inst.balance = round((inst.balance or 0) - amount, 2)
    else:
        take = -amount
        from services.student_links import institution_withdrawable
        can_take = institution_withdrawable(student)
        if take > can_take + 1e-9:
            own = round(student.self_funded or 0, 2)
            extra = f' (מתוך היתרה {own:.2f} ₪ הם כסף של התלמיד עצמו)' if own > 0 else ''
            return f'אפשר להוריד ל{student.student_display_name or student.name or "תלמיד"} עד {can_take:.2f} ₪{extra}'
        inst.balance = round((inst.balance or 0) + take, 2)
    student.balance = round((student.balance or 0) + amount, 2)
    db.session.add(Transaction(customer_id=student.id, amount=amount, type='credit' if amount > 0 else 'debit',
                               description=desc or (CREDIT_DESC if amount > 0 else TAKE_DESC)))
    return None


def after_credit(student_ids):
    """אחרי זיכוי: הקלטות וכתבי יד שחיכו לתשלום נשלחים עכשיו (כמו אחרי טעינת ארנק)."""
    for sid in student_ids:
        try:
            from routes.dictate import trigger_pending_manuscripts
            trigger_pending_manuscripts(customer_id=sid)
        except Exception:
            pass
        try:
            from services.transcribe import process_pending_recordings
            threading.Thread(target=process_pending_recordings, args=(sid,), daemon=True).start()
        except Exception:
            pass


def month_usage(student_ids):
    """{student_id: {'cost': ₪, 'minutes': דקות}} לחודש הנוכחי (שעון ישראל)."""
    from app import db
    from sqlalchemy import func
    from models import Transaction, Recording
    if not student_ids:
        return {}
    since = month_start_utc()
    out = {sid: {'cost': 0.0, 'minutes': 0} for sid in student_ids}
    for sid, total in (db.session.query(Transaction.customer_id, func.coalesce(func.sum(Transaction.amount), 0))
                       .filter(Transaction.customer_id.in_(student_ids), Transaction.amount < 0,
                               Transaction.created_at >= since,
                               ~Transaction.description.like('הורדת יתרה ע"י המוסד%'))
                       .group_by(Transaction.customer_id).all()):
        out[sid]['cost'] = round(-float(total or 0), 2)
    for sid, secs in (db.session.query(Recording.customer_id, func.coalesce(func.sum(Recording.duration_seconds), 0))
                      .filter(Recording.customer_id.in_(student_ids), Recording.created_at >= since,
                              Recording.cost > 0)
                      .group_by(Recording.customer_id).all()):
        out[sid]['minutes'] = int(round((secs or 0) / 60))
    return out


def active_students(inst, group_id=None):
    from models import Customer
    q = (Customer.query.filter_by(institution_id=inst.id)
         .filter(Customer.is_institution_self.isnot(True))
         .filter(Customer.is_blocked.isnot(True)))
    if group_id:
        q = q.filter(Customer.student_group_id == group_id)
    return q.order_by(Customer.id).all()


# ---------------------------------------------------------------- זיכוי חודשי
def run_monthly_credits():
    """בתחילת כל חודש (שעון ישראל, מ-06:00): כל מוסד שהגדיר זיכוי חודשי מזכה את התלמידים הפעילים.
    "תפיסה" אטומית של החודש בעדכון אחד - כך שגם עם כמה שרתים במקביל זה רץ פעם אחת בלבד."""
    from app import db
    from models import Institution
    now = datetime.now(IL)
    if now.hour < 6:
        return 0
    month = now.strftime('%Y-%m')
    done = 0
    for inst_id, in (db.session.query(Institution.id).filter(Institution.monthly_credit_amount > 0,
                                                              Institution.is_blocked.isnot(True)).all()):
        claimed = db.session.execute(db.text(
            "UPDATE institutions SET monthly_credit_last = :m WHERE id = :i "
            "AND (monthly_credit_last IS NULL OR monthly_credit_last <> :m) RETURNING id"),
            {'m': month, 'i': inst_id}).fetchone()
        db.session.commit()
        if not claimed:
            continue
        try:
            _credit_institution_month(inst_id, month)
            done += 1
        except Exception:
            db.session.rollback()
            log.exception(f'monthly credit failed for institution {inst_id}')
    return done


def _credit_institution_month(inst_id, month):
    from app import db
    from models import Institution
    inst = Institution.query.filter_by(id=inst_id).populate_existing().with_for_update().first()
    amount = float(inst.monthly_credit_amount or 0)
    students = active_students(inst, inst.monthly_credit_group_id)
    ok_ids, skipped = [], []
    for st in students:
        err = change_balance(inst, st, amount, MONTHLY_DESC)
        if err:
            skipped.append(st)
        else:
            ok_ids.append(st.id)
    db.session.commit()
    after_credit(ok_ids)
    log.info(f'monthly credit {month}: institution {inst_id} credited {len(ok_ids)} students x {amount}, '
             f'{len(skipped)} skipped (balance)')
    try:
        _email_monthly_summary(inst, month, amount, len(ok_ids), skipped)
    except Exception:
        log.exception('monthly credit summary email failed')


def _email_monthly_summary(inst, month, amount, ok, skipped):
    to = inst.notify_email or inst.email
    if not to:
        return
    lines = [f'הזיכוי החודשי לחודש {month[5:]}/{month[:4]} בוצע: {ok} תלמידים קיבלו {amount:.2f} ₪ כל אחד '
             f'(סה"כ {ok * amount:.2f} ₪ מיתרת המוסד).',
             f'יתרת המוסד אחרי הזיכוי: {inst.balance or 0:.2f} ₪.']
    if skipped:
        names = ', '.join((s.student_display_name or s.name or s.student_number or '') for s in skipped[:30])
        lines.append(f'\nשימו לב: {len(skipped)} תלמידים לא זוכו כי יתרת המוסד לא הספיקה: {names}'
                     + (' ועוד' if len(skipped) > 30 else '') + '. אחרי טעינת יתרה אפשר לזכות אותם מרשימת התלמידים.')
    _send(to, f'זיכוי חודשי לתלמידים - {month[5:]}/{month[:4]}', 'הזיכוי החודשי בוצע', '\n'.join(lines))


# ---------------------------------------------------------------- יתרה נמוכה
def run_low_balance_emails():
    """פעם בשבוע (יום ראשון, מ-09:00 שעון ישראל): מייל למוסד עם רשימת התלמידים שהיתרה שלהם נמוכה."""
    from app import db
    from models import Institution, Customer
    now = datetime.now(IL)
    if now.weekday() != 6 or now.hour < 9:          # Python: ראשון = 6
        return 0
    cutoff = datetime.utcnow() - timedelta(days=6)
    sent = 0
    for inst in Institution.query.filter(Institution.low_balance_email.is_(True), Institution.is_blocked.isnot(True)).all():
        claimed = db.session.execute(db.text(
            "UPDATE institutions SET low_balance_last_sent = :n WHERE id = :i "
            "AND (low_balance_last_sent IS NULL OR low_balance_last_sent < :c) RETURNING id"),
            {'n': datetime.utcnow(), 'i': inst.id, 'c': cutoff}).fetchone()
        db.session.commit()
        if not claimed:
            continue
        th = low_threshold(inst)
        low = (Customer.query.filter_by(institution_id=inst.id).filter(Customer.is_institution_self.isnot(True))
               .filter(Customer.is_blocked.isnot(True)).filter(db.func.coalesce(Customer.balance, 0) < th)
               .order_by(Customer.balance).all())
        if not low:
            continue
        to = inst.notify_email or inst.email
        if not to:
            continue
        rows = '\n'.join(f'• {s.student_display_name or s.name or "—"} ({s.student_number or s.phone or ""}): {s.balance or 0:.2f} ₪'
                         for s in low[:80])
        body = (f'{len(low)} תלמידים עם יתרה נמוכה מ-{th:.2f} ₪:\n\n{rows}'
                + ('\n…' if len(low) > 80 else '')
                + f'\n\nיתרת המוסד: {inst.balance or 0:.2f} ₪. אפשר לזכות כמה תלמידים יחד מרשימת התלמידים (סימון ← "זיכוי").')
        try:
            _send(to, f'תלמידים עם יתרה נמוכה ({len(low)})', 'תלמידים עם יתרה נמוכה', body, button='/institution/students?f=low')
            sent += 1
        except Exception:
            log.exception(f'low balance email failed for institution {inst.id}')
    return sent


def _send(to, subject, title, body, button=None):
    import os
    import sendgrid
    from sendgrid.helpers.mail import Mail, Email
    from services import email_layout as L
    base = os.environ.get('APP_BASE_URL', os.environ.get('APP_URL', '')).rstrip('/')
    card = L.title(title) + L.text_block(body)
    if button and base:
        card += L.button(base + button, 'לרשימת התלמידים')
    card += L.signature(can_reply=True)
    msg = Mail(from_email=Email(os.environ.get('SENDGRID_FROM_EMAIL', os.environ.get('GMAIL_USER', '')), 'תמלול פון'),
               to_emails=to, subject=subject, html_content=L.wrap(card, show_circles=False))
    sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY')).send(msg)
