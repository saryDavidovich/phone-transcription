"""
הודעות קוליות ללקוחות (פרטית לכל לקוח, או תפוצתית לכל הלקוחות הרשומים).

הלקוח שומע הודעה שממתינה לו בכניסה למערכת - פעם אחת עד הסוף - ואחר כך אפשר
לשמוע אותה שוב בשלוחה 9 (ה-IVR: /api/voice-messages/<phone>, /api/voice-messages/heard).

ארבעה מקורות ליצירה בממשק: קובץ שמע שמעלים, הקלטה ישירה בדפדפן, טקסט בקול הקריין
(services/narrator - הקול היציב) - שלושתם מומרים ל-WAV ומועלים לימות, לתיקייה /voice_messages -
או טקסט שימות מקריאים בקול ממוחשב. לכל הודעה: כמה כניסות הלקוח ישמע אותה (repeat_times).
בשמיעה החוזרת בשלוחה 9 יש צפצוף בין הודעה להודעה (beep_path). הודעות נשמרות כתבניות כלליות
(טקסט או הקלטה - VoiceTemplate), לשימוש חוזר אצל כל לקוח ובתפוצה.
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
    heard = {h.message_id: (h.times or 1) for h in VoiceMessageHeard.query.filter_by(customer_id=customer.id).all()}
    # ממתינה = עוד לא נשמעה מספר הפעמים שנקבע לה (repeat_times)
    pending = [message_payload(m) for m in msgs if heard.get(m.id, 0) < max(1, m.repeat_times or 1)]
    since = datetime.utcnow() - timedelta(days=REPLAY_DAYS)
    replay = [message_payload(m) for m in reversed(msgs) if m.created_at and m.created_at >= since][:REPLAY_LIMIT]
    return {'pending': pending, 'replay': replay, 'beep': beep_path()}


# ---------------------------------------------------------------------------
# צפצוף בין הודעה להודעה בשמיעה החוזרת (שלוחה 9): קובץ קצר שנוצר כאן ומועלה לימות פעם אחת
# ---------------------------------------------------------------------------
BEEP_PATH = f'{YEMOT_FOLDER}/beep'
_beep_started = False


def _make_beep():
    from pydub.generators import Sine
    from pydub import AudioSegment
    tone = Sine(880).to_audio_segment(duration=220, volume=-9).fade_in(15).fade_out(40)
    seg = AudioSegment.silent(duration=250) + tone + AudioSegment.silent(duration=350)
    seg = seg.set_channels(1).set_frame_rate(8000).set_sample_width(2)
    out = io.BytesIO()
    seg.export(out, format='wav')
    return out.getvalue()


def beep_path():
    """הנתיב בימות של הצפצוף, או None אם עוד לא הועלה (ההעלאה רצה ברקע פעם אחת)."""
    global _beep_started
    from models import Settings
    row = Settings.query.filter_by(key='voice_beep_uploaded').first()
    if row and row.value == BEEP_PATH:
        return BEEP_PATH
    if not _beep_started:
        _beep_started = True
        import threading
        from flask import current_app
        app = current_app._get_current_object()

        def work():
            try:
                upload_to_yemot(_make_beep(), BEEP_PATH + '.wav')
                with app.app_context():
                    from app import db
                    r = Settings.query.filter_by(key='voice_beep_uploaded').first() or Settings(key='voice_beep_uploaded')
                    r.value = BEEP_PATH
                    db.session.add(r)
                    db.session.commit()
                log.info('voice_messages: הצפצוף הועלה לימות')
            except Exception as e:
                global _beep_started
                _beep_started = False
                log.warning(f'voice_messages: העלאת הצפצוף נכשלה: {e}')
        threading.Thread(target=work, daemon=True).start()
    return None


# ---------------------------------------------------------------------------
# צינתוקים - רק דרך מודול הצינתוקים החינמיים של ימות (phones=tzl:<רשימה>):
# - לכל לקוח רשימה אישית משלו, ובה רק הוא (c<טלפון>). כשהוא מקיש 4 בשלוחה 9, המערכת
#   יוצרת לו (פעם אחת) שלוחת צינתוקים אישית בימות - <TZINTUK_BASE>/<טלפון> - ומעבירה
#   אותו אליה כדי להירשם. צינתוק ללקוח = הפעלת הרשימה האישית שלו.
# - תפוצה: הרשימה הכללית (TZINTUK_LIST, ברירת מחדל clients) + כל הרשימות האישיות.
# צינתוק ישיר למספר (phones=<טלפון>) לא בשימוש - ימות גובים עליו.
# ---------------------------------------------------------------------------
def _digits(v):
    return ''.join(ch for ch in str(v or '') if ch.isdigit())


def tzintuk_base():
    return '/' + (os.environ.get('TZINTUK_BASE') or 'tz').strip().strip('/')


def personal_list(phone):
    return 'c' + _digits(phone)


def prepare_personal(phone):
    """יוצר בימות את שלוחת הצינתוקים האישית של המתקשר (אם עוד לא קיימת) ומחזיר את הנתיב שלה.
    מחזיר (נתיב או None, שגיאה או None)."""
    from app import db
    from models import Customer
    from services.customer_phones import find_customer_by_phone
    ph = _digits(phone)
    if len(ph) < 9:
        return None, 'מספר לא מזוהה'
    token = os.environ.get('YEMOT_TOKEN', '')
    if not token:
        return None, 'YEMOT_TOKEN לא מוגדר בשרת'
    folder = f'{tzintuk_base()}/{ph}'
    lst = personal_list(ph)
    customer = find_customer_by_phone(ph)
    if not (customer and customer.tzintuk_list == lst):
        contents = f'type=tzintuk\nlist_tzintuk={lst}\ntzintuk_end=/\n'
        try:
            r = requests.post('https://www.call2all.co.il/ym/api/UploadTextFile',
                              data={'token': token, 'what': f'ivr2:{folder}/ext.ini', 'contents': contents}, timeout=30)
            body = r.json()
        except Exception as e:
            return None, f'יצירת השלוחה בימות נכשלה: {e}'
        if str(body.get('responseStatus', '')).upper() != 'OK':
            return None, f'ימות דחו את יצירת השלוחה: {body.get("message") or body}'
        if customer:
            customer.tzintuk_list = lst
            db.session.commit()
        log.info(f'tzintuk: נוצרה שלוחה אישית {folder} (רשימה {lst})')
    return folder, None


def run_tzintuk_list(lst):
    """הפעלת רשימת צינתוקים (מודול החינמי). מחזיר (הצליח?, הודעה)."""
    token = os.environ.get('YEMOT_TOKEN', '')
    if not token:
        return False, 'YEMOT_TOKEN לא מוגדר בשרת'
    params = {'token': token, 'phones': f'tzl:{lst}', 'TzintukTimeOut': 8}
    caller = (os.environ.get('TZINTUK_CALLER_ID') or '').strip()
    if caller:
        params['callerId'] = caller
    try:
        r = requests.get('https://www.call2all.co.il/ym/api/RunTzintuk', params=params, timeout=30)
        body = r.json()
    except Exception as e:
        log.warning(f'tzintuk {lst} failed: {e}')
        return False, f'הצינתוק נכשל: {e}'
    log.info(f'tzintuk {lst}: {body}')
    if str(body.get('responseStatus', '')).upper() == 'OK':
        return True, 'הצינתוק נשלח'
    return False, f'ימות דחו את הצינתוק: {body.get("message") or body}'


def tzintuk_customer(customer):
    if not customer or not customer.tzintuk_list:
        return False, 'הלקוח עוד לא נרשם לצינתוקים (שלוחה 9, מקש 4)'
    return run_tzintuk_list(customer.tzintuk_list)


def tzintuk_everyone_async():
    """תפוצה: הרשימה הכללית + כל הרשימות האישיות, ברקע (קריאה לכל רשימה). מחזיר כמה רשימות."""
    import threading
    import time
    from models import Customer
    lists = [(os.environ.get('TZINTUK_LIST') or 'clients').strip()]
    lists += [r[0] for r in Customer.query.with_entities(Customer.tzintuk_list)
              .filter(Customer.tzintuk_list.isnot(None)).distinct().all()]

    def work():
        ok = 0
        for lst in lists:
            if run_tzintuk_list(lst)[0]:
                ok += 1
            time.sleep(0.3)
        log.info(f'tzintuk broadcast: {ok}/{len(lists)} רשימות הופעלו')
    threading.Thread(target=work, daemon=True).start()
    return len(lists)


# ---------------------------------------------------------------------------
# תבניות - כלליות לכל הלקוחות. טקסט, או הקלטה מוכנה (מהקריין / קובץ / הקלטה בממשק)
# ---------------------------------------------------------------------------
def templates():
    """רשימה לתצוגה: [{id, name, text, audio}] (בלי תוכן הקובץ)."""
    from app import db
    from models import VoiceTemplate
    from sqlalchemy.orm import defer
    rows = VoiceTemplate.query.options(defer(VoiceTemplate.file_data)).order_by(VoiceTemplate.id.desc()).all()
    has_audio = {r[0] for r in db.session.query(VoiceTemplate.id).filter(VoiceTemplate.file_data.isnot(None)).all()}
    return [{'id': t.id, 'name': t.name, 'text': t.text or '', 'audio': t.id in has_audio} for t in rows]


def save_template(text, wav=None, name_hint=''):
    """שומר תבנית (נוסח אותו טקסט + אותו סוג - מחליף את הקודמת)."""
    from app import db
    from models import VoiceTemplate
    text = (text or '').strip()
    if not text and not wav:
        return None
    base = text or name_hint or 'הקלטה'
    name = base if len(base) <= 60 else base[:58].rsplit(' ', 1)[0] + '…'
    if wav:
        name = '🔊 ' + name
    for old in VoiceTemplate.query.filter(VoiceTemplate.text == (text or None)).all():
        if bool(old.file_data) == bool(wav):
            db.session.delete(old)
    t = VoiceTemplate(name=name, text=text or None, file_data=wav)
    db.session.add(t)
    db.session.commit()
    return t


def delete_template(tid):
    from app import db
    from models import VoiceTemplate
    t = VoiceTemplate.query.get(int(tid))
    if t:
        db.session.delete(t)
        db.session.commit()


def _order_col():
    from models import VoiceMessage
    return VoiceMessage.created_at.asc()


def mark_heard(customer, ids, phone=''):
    from app import db
    from models import VoiceMessageHeard
    for mid in ids:
        h = VoiceMessageHeard.query.filter_by(message_id=mid, customer_id=customer.id).first()
        if h:
            h.times = (h.times or 1) + 1
        else:
            db.session.add(VoiceMessageHeard(message_id=mid, customer_id=customer.id, phone=phone, times=1))
    db.session.commit()


def create(customer_id, kind, text='', audio=None, filename='', repeat_times=1):
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
    m = VoiceMessage(customer_id=customer_id, kind=kind, text=text or None, file_data=wav, active=True,
                     repeat_times=max(1, min(int(repeat_times or 1), 10)))
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
