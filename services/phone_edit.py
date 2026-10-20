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
import re
import wave
import base64
import shutil
import logging
import posixpath
import tempfile
import threading
from urllib.parse import urlparse, parse_qs

import requests
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)

DEFAULT_ALLOWED = '0527134491'
MAX_SEGMENTS = 400
CHUNK_SECONDS = 600         # 10 דקות = כ-19MB ב-WAV 16kHz מונו, מתחת למגבלת 25MB של OpenAI
CHUNK_SEARCH_SECONDS = 120  # החיתוך ברגע השקט ביותר בשתי הדקות האחרונות של כל חלק


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


def _transcribe_audio(rec_url, client, keep_path=None):
    """מוריד קטע אחד, מכין WAV 16kHz מונו ומתמלל. קטע שלא ירד/ריק - טקסט ריק.
    keep_path: אם ניתן, ה-WAV המוכן מועבר לשם (קובץ זמני) לצורך איחוד הקטעים."""
    from services import audio_prep
    from routes.dictate import _transcribe_segment_openai
    src = prepared = None
    try:
        src = audio_prep.open_source(rec_url)
        prepared = audio_prep.prepare_wav(src[0])
        if not prepared:
            return '', 0
        if keep_path:
            shutil.copyfile(prepared[0], keep_path)
        if prepared[2] < 1:
            return '', 0
        # OpenAI מקבל עד 25MB לבקשה. WAV של 16kHz מונו = כ-1.9MB לדקה, ולכן קטע ארוך
        # (למשל שעה בלי אף לחיצה) נשלח בחלקים של עד 10 דקות, והטקסט מחובר לפי הסדר.
        # כל חיתוך נעשה ברגע השקט ביותר שבין דקה 8 לדקה 10 של החלק - לא באמצע מילה.
        # בלי זה OpenAI מחזיר 400 "audio file might be corrupted" והקטע יוצא ריק.
        texts = [_transcribe_segment_openai(chunk, client)
                 for chunk in audio_prep.iter_wav_chunks_at_silence(prepared[0], max_seconds=CHUNK_SECONDS,
                                                                    search_seconds=CHUNK_SEARCH_SECONDS)]
        return ' '.join(t for t in texts if t), prepared[2]
    except Exception as e:
        log.warning(f"phone_edit: קטע לא תומלל ({rec_url[-60:]}): {e}")
        return '', 0
    finally:
        audio_prep.cleanup(prepared, src)


def _yemot_token_and_path(rec_url):
    """מתוך כתובת DownloadFile של ימות: (token, '/recordings_edit/023.wav')."""
    q = parse_qs(urlparse(rec_url or '').query)
    token = (q.get('token') or [''])[0]
    path = (q.get('path') or [''])[0]
    if path.startswith('ivr2:'):
        path = path[5:]
    return token, path


def _join_and_upload(wav_paths, first_rec_url, call_id):
    """מחבר את כל הקטעים (לפי הסדר) לקובץ WAV אחד ומעלה אותו לימות, לתיקייה
    /recordings_edit/full, באותו מספר כמו הקטע הראשון (למשל 023.wav).
    הקובץ המאוחד נבנה בקובץ זמני ונמחק מיד אחרי ההעלאה - לא נשמר אצלנו.
    מחזיר את כתובת ה-DownloadFile של הקובץ המאוחד, או None אם נכשל."""
    token, first_path = _yemot_token_and_path(first_rec_url)
    if not token or not first_path:
        log.warning(f"phone_edit {call_id}: אין טוקן/נתיב בכתובת הקטע הראשון - לא מאחדים")
        return None
    folder, name = posixpath.split(first_path)
    target = posixpath.join(folder, 'full', name if name.lower().endswith('.wav') else name + '.wav')
    fd, joined = tempfile.mkstemp(suffix='.wav')
    os.close(fd)
    try:
        frames = 0
        with wave.open(joined, 'wb') as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000)
            for p in wav_paths:
                with wave.open(p, 'rb') as w:
                    if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, 16000):
                        log.warning(f"phone_edit {call_id}: קטע בפורמט לא צפוי, מדלגים: {p}")
                        continue
                    while True:
                        data = w.readframes(16000 * 30)
                        if not data:
                            break
                        out.writeframes(data)
                        frames += len(data) // 2
        if not frames:
            return None
        with open(joined, 'rb') as fh:
            r = requests.post('https://www.call2all.co.il/ym/api/UploadFile',
                              data={'token': token, 'path': f'ivr2:{target}', 'convertAudio': 1},
                              files={'file': (posixpath.basename(target), fh, 'audio/wav')}, timeout=300)
        try:
            body = r.json()
        except ValueError:
            body = {'raw': r.text[:200]}
        if r.status_code != 200 or str(body.get('responseStatus', '')).upper() != 'OK':
            log.warning(f"phone_edit {call_id}: העלאת הקובץ המאוחד לימות נכשלה ({r.status_code}): {body}")
            return None
        log.info(f"phone_edit {call_id}: הקובץ המאוחד הועלה לימות - {target} ({frames // 16000} שניות, {len(wav_paths)} קטעים)")
        return f'https://www.call2all.co.il/ym/api/DownloadFile?token={token}&path=ivr2:{target}'
    except Exception as e:
        log.warning(f"phone_edit {call_id}: איחוד/העלאה נכשל: {type(e).__name__}: {e}")
        return None
    finally:
        try:
            os.remove(joined)
        except OSError:
            pass


_ELLIPSIS_EDGES = re.compile(r'^(?:\s|\.{2,}|…)+|(?:\s|\.{2,}|…)+$')


def _clean_segment_text(text):
    """כל קטע נחתך באמצע דיבור (בגלל הסולמית), והמתמלל מוסיף לפעמים "..." בהתחלה/בסוף
    או מחזיר "..." בלבד על קטע שקט. מורידים את שלוש הנקודות בקצוות, וקטע בלי אות
    או ספרה נחשב ריק."""
    t = _ELLIPSIS_EDGES.sub('', text or '')
    return t if re.search(r'\w', t) else ''


def _plain_text(content):
    return '\n\n'.join(''.join(r['text'] for r in p['runs']) for p in content)


def process(call_id, customer_id, segments):
    """עבודת רקע: תמלול, הרכבה, שמירה ושליחה ללקוח."""
    from app import app, db
    from models import Recording, Customer
    from services.job_tracker import tracked
    from services.recording_alerts import notify_recording_error
    keep_dir = None
    with app.app_context(), tracked('call', label=f'הקלטה עם עריכה {call_id}'):
        try:
            from openai import OpenAI
            from routes.dictate import _build_content_from_segments, _build_manuscript_docx
            client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))
            audio_idx = [i for i, s in enumerate(segments) if s['type'] == 'audio']
            keep_dir = tempfile.mkdtemp(prefix='phone_edit_')   # קבצים זמניים לאיחוד - נמחקים בסוף
            keep = {i: os.path.join(keep_dir, f'{n:04d}.wav') for n, i in enumerate(audio_idx)}
            with ThreadPoolExecutor(max_workers=4) as ex:
                results = list(ex.map(lambda i: _transcribe_audio(segments[i]['rec_url'], client, keep[i]), audio_idx))
            total_secs = 0
            for i, (text, secs) in zip(audio_idx, results):
                segments[i]['raw_text'] = text
                segments[i]['text'] = _clean_segment_text(text)
                total_secs += secs
            # לוג מפורט: כל קטע לפי הסדר - סוג, קובץ/סימן, עיצוב, אורך ותחילת הטקסט
            for n, sg in enumerate(segments, 1):
                src = sg.get('rec_url', '').split('path=ivr2:')[-1] if sg['type'] == 'audio' else sg.get('text')
                log.info(f"phone_edit {call_id} seg {n}/{len(segments)}: {sg['type']} {src!r} "
                         f"bold={sg['bold']} underline={sg['underline']} new_par={sg['new_paragraph']} "
                         f"text={(sg.get('text') or '')[:60]!r} raw={(sg.get('raw_text') or '')[:60]!r}")
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
            # הקלטה אחת מלאה בימות (לשמיעה בשלוחה 8 ולקישור במייל) במקום הקטע הראשון בלבד
            parts = [keep[i] for i in audio_idx if os.path.isfile(keep[i])]
            joined_url = _join_and_upload(parts, segments[audio_idx[0]]['rec_url'], call_id)
            if joined_url:
                rec.rec_url = joined_url
            rec.transcript = text
            rec.duration_seconds = int(total_secs)
            rec.cost = 0.0
            rec.status = 'transcribed'
            db.session.commit()

            docx = _build_manuscript_docx(customer.name or '', 'הקלטה עם עריכה', content,
                                          title_text='תמלול - הקלטה עם עריכה')
            if rec.delivery_method == 'fax' and rec.delivered_to:
                from services.transcribe import convert_docx_bytes_to_pdf_bytes, send_pdf_fax
                res = send_pdf_fax(rec.delivered_to, convert_docx_bytes_to_pdf_bytes(docx),
                                   filename_hint=f'phone_edit_{rec.id}.pdf')
                if not res.get('ok'):
                    raise RuntimeError(f"שליחת פקס נכשלה: {res.get('error')}")
            else:
                from routes.recording_proxy import recording_download_url
                _send_email(rec.delivered_to, customer, docx, text, recording_download_url(rec.id))
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
        finally:
            if keep_dir:
                shutil.rmtree(keep_dir, ignore_errors=True)


def _send_email(to, customer, docx_bytes, text, rec_link=None):
    import sendgrid
    from html import escape
    from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition, Email
    if not to:
        raise RuntimeError('אין כתובת מייל ללקוח')
    from services import email_layout as L
    card = (L.title(f'שלום {escape((customer.name or "").split()[0])}, התמלול מוכן' if customer.name else 'התמלול מוכן')
            + L.meta('הקלטה עם עריכה · הקובץ המעוצב מצורף כקובץ Word')
            + L.text_block(text))
    if rec_link:
        card += L.button(rec_link, 'להורדת ההקלטה')
    html = L.wrap(card, phone=customer.phone)
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
