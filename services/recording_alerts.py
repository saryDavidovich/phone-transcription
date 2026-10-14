"""
התראה למנהל (מסך "הודעות למנהל") על כל הקלטה שנכשלה - עם פירוט השגיאה והלקוח.

נקרא מכל מקום שבו הקלטה מסומנת 'error' (services/transcribe.py). ההתראה נשמרת
כ-ManagerMessage עם source='recording_error' - בלי rec_url, ולכן ה-source הוא
מה שמבדיל אותה מ"שריון ריק" של שלוחה 9 (ראה admin.manager_messages).
כפילויות: על אותה הקלטה נשלחת התראה אחת לכל סיבה (אם ה-webhook וה-polling של
אלף-בוט נכשלים שניהם, או שהמנהל שולח שוב ונכשל שוב מאותה סיבה - לא מציפים).
"""
import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

TIER_NAMES = {'gemini': 'רגיל', 'basic': 'רגיל', 'premium': 'מקצועי'}


def notify_recording_error(call_id, reason, mark_error=True):
    """רושם התראה למנהל. mark_error=True - גם מסמן את ההקלטה כ'error' (אם היא
    עדיין באמצע טיפול ולא חויבה/נשלחה). לעולם לא זורק שגיאה הלאה."""
    try:
        from app import db
        from models import Recording, Customer, ManagerMessage
        rec = Recording.query.filter_by(call_id=call_id).first() if call_id else None
        if rec and mark_error and rec.status not in ('delivered', 'transcribed', 'pending_payment', 'too_short'):
            rec.status = 'error'
        customer = Customer.query.get(rec.customer_id) if rec and rec.customer_id else None

        reason = (str(reason or 'שגיאה לא ידועה')).strip()[:1500]
        text = _build_text(rec, customer, reason)

        # לא מציפים: אותה הקלטה + אותה סיבה ב-24 השעות האחרונות = התראה אחת
        since = datetime.utcnow() - timedelta(hours=24)
        dup = (ManagerMessage.query
               .filter(ManagerMessage.source == 'recording_error',
                       ManagerMessage.call_id == (call_id or ''),
                       ManagerMessage.transcript == text,
                       ManagerMessage.created_at > since)
               .first())
        if not dup:
            db.session.add(ManagerMessage(
                phone=(customer.phone if customer else None) or '—',
                name=(customer.name if customer else None),
                email=(customer.email if customer else None),
                call_id=call_id or '',
                source='recording_error',
                transcript=text,
            ))
        db.session.commit()
        log.info(f"recording_alerts: התראה למנהל על {call_id}: {reason[:120]}")
    except Exception as e:
        log.error(f"recording_alerts: שליחת התראה נכשלה ({call_id}): {e}")
        try:
            from app import db
            db.session.rollback()
        except Exception:
            pass


def _build_text(rec, customer, reason):
    lines = [f'❌ הקלטה נכשלה: {reason}']
    if customer:
        lines.append(f'לקוח: {customer.name or "ללא שם"} ({customer.phone or "—"})'
                     + (f', מייל: {customer.email}' if customer.email else ''))
    if rec:
        src = 'מייל/דרייב' if (rec.call_id or '').startswith('email-') else 'טלפון'
        parts = [f'הקלטה #{rec.id}', f'מקור: {src}',
                 f'סוג: {TIER_NAMES.get(rec.transcription_tier, rec.transcription_tier or "—")}']
        if rec.duration_seconds:
            parts.append(f'אורך: {rec.duration_seconds // 60}:{rec.duration_seconds % 60:02d} דקות')
        if rec.source_filename:
            parts.append(f'קובץ: {rec.source_filename}')
        lines.append(', '.join(parts))
        lines.append(f'לפרטים ולשליחה מחדש: /admin/recordings/{rec.id}')
    return '\n'.join(lines)
