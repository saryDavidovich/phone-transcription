"""
"הקלטה עם עריכה" בטלפון (ניסיוני - כרגע רק מתפריט המנהל ב-IVR).

איך זה עובד: בשלוחת ה-API הייעודית בימות מוגדר "כל מקש עוצר את ההקלטה".
הלקוח מקליט, ובכל רגע מקיש כוכבית ומיד מספר פעולה (ירידת שורה, הדגשה, קו
תחתון, סוגריים, מקף, נקודתיים, סימן שאלה, מירכאות). כל עצירה סוגרת "קטע"
הקלטה, וה-IVR מקליט מיד את הקטע הבא. ה-IVR שולח לכאן את רשימת הקטעים לפי
הסדר - קטעי אודיו (קובץ בימות + מצב ההדגשה/קו תחתון באותו רגע) וקטעי "סימן"
(סוגר, מקף וכו') - והעיצוב נכנס בדיוק בגבול בין הקטעים, בלי שום ניחוש.

התמלול: OpenAI (gpt-transcribe), קטע-קטע - אותה פונקציה של אולפן ההקראה
(routes/dictate._transcribe_segment_openai). ההרכבה ל-Word: אותו מנגנון של
ההקראה (_build_content_from_segments + _build_manuscript_docx).
בשלב הניסיון: בלי חיוב, ורק למספרים שמוגדרים ב-PHONE_EDIT_PHONES (ברירת מחדל:
מספר המנהל) - כדי שאי אפשר יהיה לקבל תמלול חינם דרך ה-API.
"""
import os
import base64
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)

DEFAULT_ALLOWED = '0527134491'
MAX_SEGMENTS = 400


def allowed_phone(phone):
    allowed = [p.strip() for p in os.environ.get('PHONE_EDIT_PHONES', DEFAULT_ALLOWED).split(',') if p.strip()]
    return (phone or '').strip() in allowed


def _clean_segments(raw):
    """משאיר רק שדות מוכרים. סימנים מותרים בלבד - לא טקסט חופשי מבחוץ."""
    allowed_literals = {'[', ']', '(', ')', '-', ':', '?', '"'}
    out = []
    for s in (raw or [])[:MAX_SEGMENTS]:
        if not isinstance(s, dict):
            continue
        base = {'bold': bool(s.get('bold')), 'underline': bool(s.get('underline')),
                'new_paragraph': bool(s.get('new_paragraph')), 'heading': False,
                'no_space_before': bool(s.get('no_space_before')), 'no_space_after': bool(s.get('no_space_after'))}
        if s.get('type') == 'literal' and s.get('text') in allowed_literals:
            out.append(dict(base, type='literal', text=s['text']))
        elif s.get('type') == 'audio' and s.get('rec_url'):
            out.append(dict(base, type='audio', rec_url=str(s['rec_url'])))
    return out


def _transcribe_audio(rec_url, client):
    """מוריד קטע אחד, מכין WAV 16kHz מונו ומתמלל. קטע שלא ירד/ריק - טקסט ריק."""
    from services import audio_prep
    from routes.dictate import _transcribe_segment_openai
    src = prepared = None
    try:
        src = audio_prep.open_source(rec_url)
        prepared = audio_prep.prepare_wav(src[0])
        if not prepared or prepared[2] < 1:
            return '', 0
        with open(prepared[0], 'rb') as fh:
            return _transcribe_segment_openai(fh.read(), client), prepared[2]
    except Exception as e:
        log.warning(f"phone_edit: קטע לא תומלל ({rec_url[-60:]}): {e}")
        return '', 0
    finally:
        audio_prep.cleanup(prepared, src)


def _plain_text(content):
    return '\n\n'.join(''.join(r['text'] for r in p['runs']) for p in content)


def process(call_id, customer_id, segments):
    """עבודת רקע: תמלול, הרכבה, שמירה ושליחה ללקוח."""
    from app import app, db
    from models import Recording, Customer
    from services.job_tracker import tracked
    from services.recording_alerts import notify_recording_error
    with app.app_context(), tracked('call', label=f'הקלטה עם עריכה {call_id}'):
        try:
            from openai import OpenAI
            from routes.dictate import _build_content_from_segments, _build_manuscript_docx
            client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))
            audio_idx = [i for i, s in enumerate(segments) if s['type'] == 'audio']
            with ThreadPoolExecutor(max_workers=4) as ex:
                results = list(ex.map(lambda i: _transcribe_audio(segments[i]['rec_url'], client), audio_idx))
            total_secs = 0
            for i, (text, secs) in zip(audio_idx, results):
                segments[i]['text'] = text
                total_secs += secs
            # לוג מפורט: כל קטע לפי הסדר - סוג, קובץ/סימן, עיצוב, אורך ותחילת הטקסט
            for n, sg in enumerate(segments, 1):
                src = sg.get('rec_url', '').split('path=ivr2:')[-1] if sg['type'] == 'audio' else sg.get('text')
                log.info(f"phone_edit {call_id} seg {n}/{len(segments)}: {sg['type']} {src!r} "
                         f"bold={sg['bold']} underline={sg['underline']} new_par={sg['new_paragraph']} "
                         f"text={(sg.get('text') or '')[:60]!r}")
            content = _build_content_from_segments(segments)
            text = _plain_text(content)
            log.info(f"phone_edit {call_id}: {len(content)} פסקאות, ריצות: " + ' | '.join(
                f"[{'B' if r['bold'] else ''}{'U' if r['underline'] else ''}]{r['text'][:25]!r}"
                for p in content for r in p['runs']))

            customer = Customer.query.get(customer_id)
            rec = Recording.query.filter_by(call_id=call_id).first()
            if not text.strip():
                notify_recording_error(call_id, 'הקלטה עם עריכה: לא זוהה טקסט באף קטע')
                return
            rec.transcript = text
            rec.duration_seconds = int(total_secs)
            rec.cost = 0.0
            rec.status = 'transcribed'
            db.session.commit()

            docx = _build_manuscript_docx(customer.name or '', 'הקלטה עם עריכה', content)
            if rec.delivery_method == 'fax' and rec.delivered_to:
                from services.transcribe import convert_docx_bytes_to_pdf_bytes, send_pdf_fax
                res = send_pdf_fax(rec.delivered_to, convert_docx_bytes_to_pdf_bytes(docx),
                                   filename_hint=f'phone_edit_{rec.id}.pdf')
                if not res.get('ok'):
                    raise RuntimeError(f"שליחת פקס נכשלה: {res.get('error')}")
            else:
                _send_email(rec.delivered_to, customer, docx, text)
            rec.status = 'delivered'
            db.session.commit()
            log.info(f"phone_edit {call_id}: {len(audio_idx)} קטעים, {total_secs}s, נשלח ל-{rec.delivered_to}")
        except Exception as e:
            log.error(f"phone_edit {call_id} error: {e}", exc_info=True)
            try:
                db.session.rollback()
            except Exception:
                pass
            notify_recording_error(call_id, f'הקלטה עם עריכה: {type(e).__name__}: {e}')


def _send_email(to, customer, docx_bytes, text):
    import sendgrid
    from html import escape
    from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition, Email
    if not to:
        raise RuntimeError('אין כתובת מייל ללקוח')
    html = (f'<div dir="rtl" style="font-family:Arial,sans-serif;max-width:640px;margin:auto">'
            f'<h2 style="color:#1d4ed8">תמלול - הקלטה עם עריכה</h2>'
            f'<div style="white-space:pre-wrap;line-height:1.8">{escape(text)}</div>'
            f'<p style="color:#6b7280;font-size:13px">הקובץ המעוצב מצורף כקובץ Word.<br>מערכת תמלול פון</p></div>')
    msg = Mail(from_email=Email(os.environ.get('SENDGRID_FROM_EMAIL', ''), 'תמלול פון'),
               to_emails=to, subject='תמלול - הקלטה עם עריכה', html_content=html)
    msg.attachment = Attachment(FileContent(base64.b64encode(docx_bytes).decode()), FileName('תמלול_עם_עריכה.docx'),
                                FileType('application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                                Disposition('attachment'))
    sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY')).send(msg)


def start(phone, call_id, raw_segments):
    """נקרא מה-API. מחזיר (dict, http_status)."""
    from app import db
    from models import Recording
    from services.customer_phones import find_customer_by_phone
    from services.document_history import delivery_target
    if not allowed_phone(phone):
        return {'ok': False, 'error': 'not_allowed'}, 403
    customer = find_customer_by_phone(phone)
    if not customer:
        return {'ok': False, 'error': 'customer_not_found'}, 404
    segments = _clean_segments(raw_segments)
    log.info(f"phone_edit start {call_id}: התקבלו {len(raw_segments or [])} קטעים, תקינים {len(segments)}")
    if not any(s['type'] == 'audio' for s in segments):
        return {'ok': False, 'error': 'no_audio'}, 400
    via, to = delivery_target(customer)
    if not via:
        return {'ok': False, 'error': 'no_target'}, 400
    call_id = (call_id or '').strip()[:100] or None
    if not call_id or Recording.query.filter_by(call_id=call_id).first():
        import uuid
        call_id = f'edit-{uuid.uuid4().hex}'
    first_url = next(s['rec_url'] for s in segments if s['type'] == 'audio')
    db.session.add(Recording(customer_id=customer.id, call_id=call_id, status='transcribing',
                             rec_url=first_url, transcription_tier='phone_edit',
                             delivery_method=via, delivered_to=to, source_filename='הקלטה עם עריכה'))
    db.session.commit()
    threading.Thread(target=process, args=(call_id, customer.id, segments), daemon=True).start()
    return {'ok': True, 'call_id': call_id, 'segments': len(segments)}, 200
