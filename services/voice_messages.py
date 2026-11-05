"""
הודעות קוליות ללקוחות (פרטית לכל לקוח, או תפוצתית לכל הלקוחות הרשומים).

הלקוח שומע הודעה שממתינה לו בכניסה למערכת - פעם אחת עד הסוף - ואחר כך אפשר
לשמוע אותה שוב בשלוחה 9 (ה-IVR: /api/voice-messages/<phone>, /api/voice-messages/heard).

שלושה מקורות ליצירה בממשק: קובץ שמע שמעלים, הקלטה ישירה בדפדפן (שני אלה מומרים
ל-WAV ומועלים לימות, לתיקייה /voice_messages), או טקסט שימות מקריאים בקול ממוחשב.
"""
import io
import logging
import os
from datetime import datetime, timedelta

import requests

log = logging.getLogger(__name__)

YEMOT_FOLDER = '/voice_messages'
REPLAY_DAYS = 60          # כמה זמן הודעה זמינה לשמיעה חוזרת בשלוחה 9
REPLAY_LIMIT = 5


def to_wav(data, filename=''):
    """כל קובץ שמע (גם מהקלטה בדפדפן - webm/ogg) -> WAV מונו 8kHz 16bit, הפורמט של ימות."""
    from pydub import AudioSegment
    seg = AudioSegment.from_file(io.BytesIO(data))
    seg = seg.set_channels(1).set_frame_rate(8000).set_sample_width(2)
    out = io.BytesIO()
    seg.export(out, format='wav')
    return out.getvalue(), len(seg) / 1000.0


def upload_to_yemot(wav_bytes, path):
    token = os.environ.get('YEMOT_TOKEN', '')
    if not token:
        raise RuntimeError('YEMOT_TOKEN לא מוגדר בשרת')
    r = requests.post('https://www.call2all.co.il/ym/api/UploadFile',
                      data={'token': token, 'path': f'ivr2:{path}', 'convertAudio': 1},
                      files={'file': (os.path.basename(path), io.BytesIO(wav_bytes), 'audio/wav')}, timeout=120)
    try:
        body = r.json()
    except ValueError:
        body = {'raw': r.text[:200]}
    if r.status_code != 200 or str(body.get('responseStatus', '')).upper() != 'OK':
        raise RuntimeError(f'העלאת הקובץ לימות נכשלה ({r.status_code}): {body}')


def _visible_query(customer):
    """ההודעות הפעילות שהלקוח הזה אמור לשמוע: הפרטיות שלו + התפוצתיות."""
    from models import VoiceMessage
    from sqlalchemy import or_
    return VoiceMessage.query.filter(
        VoiceMessage.active.is_(True),
        or_(VoiceMessage.customer_id == customer.id, VoiceMessage.customer_id.is_(None)))


def message_payload(m):
    if m.kind == 'file' and m.yemot_path:
        return {'id': m.id, 'type': 'file', 'path': m.yemot_path.rsplit('.', 1)[0]}
    return {'id': m.id, 'type': 'text', 'text': m.text or ''}


def for_caller(customer):
    """pending - לא נשמעו עדיין (מהישנה לחדשה); replay - לשמיעה חוזרת (מהחדשה לישנה)."""
    from models import VoiceMessageHeard
    if not customer:
        return {'pending': [], 'replay': []}
    msgs = _visible_query(customer).order_by(_order_col()).all()
    # הודעה תפוצתית - רק ללקוחות שכבר היו רשומים כששלחו אותה (מתקשר חדש לא שומע הודעות ישנות)
    joined = customer.created_at
    msgs = [m for m in msgs if m.customer_id is not None or not joined or not m.created_at or joined <= m.created_at]
    heard = {h.message_id for h in VoiceMessageHeard.query.filter_by(customer_id=customer.id).all()}
    pending = [message_payload(m) for m in msgs if m.id not in heard]
    since = datetime.utcnow() - timedelta(days=REPLAY_DAYS)
    replay = [message_payload(m) for m in reversed(msgs) if m.created_at and m.created_at >= since][:REPLAY_LIMIT]
    return {'pending': pending, 'replay': replay}


def _order_col():
    from models import VoiceMessage
    return VoiceMessage.created_at.asc()


def mark_heard(customer, ids, phone=''):
    from app import db
    from models import VoiceMessageHeard
    for mid in ids:
        if not VoiceMessageHeard.query.filter_by(message_id=mid, customer_id=customer.id).first():
            db.session.add(VoiceMessageHeard(message_id=mid, customer_id=customer.id, phone=phone))
    db.session.commit()


def create(customer_id, kind, text='', audio=None, filename=''):
    """יוצר הודעה. audio - בייטים של קובץ שמע (העלאה/הקלטה). מחזיר את ההודעה או זורק שגיאה."""
    from app import db
    from models import VoiceMessage
    text = (text or '').strip()
    if kind == 'tts' and not text:
        raise ValueError('יש לכתוב את נוסח ההודעה')
    if kind == 'file' and not audio:
        raise ValueError('לא התקבל קובץ שמע')
    wav = secs = None
    if kind == 'file':
        try:
            wav, secs = to_wav(audio, filename)
        except Exception as e:
            raise ValueError(f'לא הצלחנו לקרוא את קובץ השמע ({e})')
        if secs < 0.5:
            raise ValueError('ההקלטה ריקה או קצרה מדי')
    m = VoiceMessage(customer_id=customer_id, kind=kind, text=text or None, file_data=wav, active=True)
    db.session.add(m)
    db.session.flush()
    if kind == 'file':
        m.yemot_path = f'{YEMOT_FOLDER}/{m.id}.wav'
        try:
            upload_to_yemot(wav, m.yemot_path)
        except Exception:
            db.session.rollback()
            raise
    db.session.commit()
    log.info(f'voice_messages: נוצרה הודעה #{m.id} ({kind}, '
             f'{"תפוצתית" if customer_id is None else f"ללקוח {customer_id}"}{f", {secs:.0f} שניות" if secs else ""})')
    return m
