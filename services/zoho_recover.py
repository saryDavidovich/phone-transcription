"""
שחזור מיילים שזוהו לא הצליחו להעביר אלינו ("Mail Forward Failure").

הרקע: התיבה הראשית בזוהו מעבירה כל מייל ל-SendGrid (Inbound Parse) שמעביר
לשרת. ל-SendGrid יש תקרת גודל (כ-30MB למייל), וקובץ WAV של כמה דקות עובר
אותה - זוהו מחזירים "Mail Forward Failure" (שמגיע אלינו כמייל רגיל, כי גם הוא
מועבר), והמייל המקורי של הלקוח לא מגיע. לפי האזהרה בהודעה עצמה, הרבה כשלים
כאלה עלולים גם לכבות את ההעברה בזוהו לגמרי.

הפתרון: כשמגיעה הודעת כשל כזו, מזהים בה את המייל המקורי (Message-ID / שולח /
נושא מהכותרות המצורפות), מתחברים לתיבה בזוהו ב-IMAP, מושכים את המייל המקורי
במלואו ומריצים אותו דרך אותו מסלול בדיוק של מייל רגיל (routes/email_inbound) -
זיהוי לקוח, תמלול, חיוב ושליחה. אם המשיכה נכשלת - התראה במסך "הודעות למנהל".

משתני סביבה (ב-Railway): ZOHO_IMAP_USER (כתובת התיבה), ZOHO_IMAP_PASSWORD
(סיסמת אפליקציה מזוהו), ZOHO_IMAP_HOST (ברירת מחדל imappro.zoho.com - לתיבה
בדומיין של ארגון; לחשבון zoho.com פרטי: imap.zoho.com).
"""
import email
import email.policy
import imaplib
import io
import logging
import os
import re
import threading
import time
from email.parser import HeaderParser

log = logging.getLogger(__name__)

_SEARCH_ATTEMPTS = 3
_SEARCH_DELAY = 20      # שניות בין ניסיונות (אם המייל עוד לא נשמר בתיבה)


def is_forward_failure(sender_email, subject):
    s = (sender_email or '').lower()
    return ('mailer-daemon' in s and 'zoho' in s and
            'forward' in (subject or '').lower() and 'fail' in (subject or '').lower())


def _header_block(text):
    """מחזיר dict של הכותרות החשובות מתוך טקסט שנראה כמו כותרות מייל."""
    if not text or not re.search(r'^(Message-ID|From|Subject)\s*:', text, re.I | re.M):
        return {}
    h = HeaderParser(policy=email.policy.default).parsestr(text)
    out = {}
    for k in ('Message-ID', 'From', 'Subject', 'Date'):
        try:
            v = h.get(k)
        except Exception:
            v = None
        if v:
            out[k.lower()] = str(v).strip()
    return out


def extract_original_ref(req):
    """מזהה את המייל המקורי מתוך הודעת הכשל: הקובץ המצורף (כותרות המייל המקורי),
    ואם אין - מגוף ההודעה."""
    ref = {}
    for key in req.files:
        f = req.files[key]
        try:
            data = f.read(200_000)
            f.seek(0)
        except Exception:
            continue
        found = _header_block(data.decode('utf-8', 'replace'))
        if found.get('message-id') or found.get('from'):
            ref.update(found)
            break
    if not ref.get('message-id'):
        body = (req.form.get('text') or '') + '\n' + (req.form.get('html') or '')
        m = re.search(r'Message-ID:\s*(<[^>\s]+>)', body, re.I)
        if m:
            ref['message-id'] = m.group(1)
    return ref


def _alert(text):
    try:
        from app import app, db
        from models import ManagerMessage
        with app.app_context():
            db.session.add(ManagerMessage(phone='—', name='כשל העברת מייל מזוהו',
                                          source='recording_error', transcript=text[:1500]))
            db.session.commit()
    except Exception as e:
        log.error(f'zoho_recover: התראה למנהל נכשלה: {e}')


def _imap_find(M, ref):
    """מחפש את המייל המקורי: קודם לפי Message-ID, ואם אין - לפי שולח ונושא. מחזיר uid או None."""
    folders = ['INBOX']
    try:
        typ, data = M.list()
        for line in data or []:
            name = line.decode('utf-8', 'replace').rsplit(' "/" ', 1)[-1].strip().strip('"')
            if name and name.upper() != 'INBOX' and not re.search(r'(sent|draft|trash|spam|junk|נשלח|טיוט|אשפה)', name, re.I):
                folders.append(name)
    except Exception:
        pass
    mid = ref.get('message-id')
    sender = re.search(r'[\w.+-]+@[\w-]+(\.[\w-]+)+', ref.get('from') or '')
    for folder in folders:
        try:
            typ, _ = M.select(f'"{folder}"', readonly=True)
            if typ != 'OK':
                continue
            if mid:
                typ, data = M.uid('SEARCH', None, 'HEADER', 'Message-ID', f'"{mid}"')
                uids = (data[0] or b'').split() if typ == 'OK' else []
                if uids:
                    return folder, uids[-1]
            elif sender:
                typ, data = M.uid('SEARCH', None, 'FROM', f'"{sender.group(0)}"')
                uids = (data[0] or b'').split() if typ == 'OK' else []
                if uids:
                    return folder, uids[-1]       # האחרון מאותו שולח
        except Exception as e:
            log.warning(f'zoho_recover: חיפוש בתיקייה {folder} נכשל: {e}')
    return None, None


def fetch_original(ref):
    """מחזיר את המייל המקורי (bytes) מזוהו, או זורק שגיאה עם הסבר."""
    user = os.environ.get('ZOHO_IMAP_USER', '')
    pwd = os.environ.get('ZOHO_IMAP_PASSWORD', '')
    host = os.environ.get('ZOHO_IMAP_HOST', '') or 'imappro.zoho.com'
    if not user or not pwd:
        raise RuntimeError('לא הוגדרו ZOHO_IMAP_USER / ZOHO_IMAP_PASSWORD ב-Railway')
    for attempt in range(1, _SEARCH_ATTEMPTS + 1):
        M = imaplib.IMAP4_SSL(host, 993, timeout=60)
        try:
            M.login(user, pwd)
            folder, uid = _imap_find(M, ref)
            if uid:
                typ, data = M.uid('FETCH', uid, '(BODY.PEEK[])')
                for part in data or []:
                    if isinstance(part, tuple) and len(part) > 1:
                        log.info(f'zoho_recover: נמצא המייל המקורי בתיקייה {folder} ({len(part[1])} בתים)')
                        return part[1]
        finally:
            try:
                M.logout()
            except Exception:
                pass
        if attempt < _SEARCH_ATTEMPTS:
            time.sleep(_SEARCH_DELAY)
    raise RuntimeError('המייל המקורי לא נמצא בתיבה בזוהו')


def build_form(raw):
    """ממיר מייל גולמי לשדות כמו ש-SendGrid שולח (from/to/subject/text/html/headers + קבצים)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    headers = '\n'.join(f'{k}: {v}' for k, v in msg.items())
    data = {
        'from': str(msg.get('From', '')), 'to': str(msg.get('To', '')),
        'subject': str(msg.get('Subject', '')), 'headers': headers, 'text': '', 'html': '',
    }
    n = 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        ctype = part.get_content_type()
        if filename or part.get_content_disposition() == 'attachment':
            payload = part.get_payload(decode=True) or b''
            n += 1
            data[f'attachment{n}'] = (io.BytesIO(payload), filename or f'attachment{n}', ctype)
        elif ctype == 'text/plain' and not data['text']:
            data['text'] = part.get_content()
        elif ctype == 'text/html' and not data['html']:
            data['html'] = part.get_content()
    data['attachments'] = str(n)
    return data


def replay(raw):
    """מריץ את המייל דרך המסלול הרגיל של מייל נכנס, כאילו הגיע עכשיו מ-SendGrid."""
    from app import app
    from routes.email_inbound import email_inbound
    data = build_form(raw)
    with app.test_request_context('/api/email-inbound', method='POST', data=data,
                                  content_type='multipart/form-data'):
        resp = email_inbound()
    body = resp[0] if isinstance(resp, tuple) else resp
    try:
        return body.get_json()
    except Exception:
        return None


def _recover(ref):
    desc = ref.get('from') or ''
    if ref.get('subject'):
        desc += f" | נושא: {ref['subject']}"
    try:
        raw = fetch_original(ref)
        result = replay(raw)
        log.info(f'zoho_recover: המייל המקורי ({desc}) עובד מחדש: {result}')
    except Exception as e:
        log.error(f'zoho_recover: שחזור נכשל ({desc}): {e}')
        _alert('זוהו לא הצליחו להעביר מייל ללקוח (כנראה קובץ גדול), והמשיכה הישירה מזוהו נכשלה.\n'
               f'המייל: {desc or "לא ידוע"}\nסיבה: {e}\n'
               'יש לטפל ידנית: למצוא את המייל בתיבה בזוהו ולהעלות את הקובץ דרך "תמלול חוזר" בכרטיס הלקוח.')


def recover_async(ref):
    threading.Thread(target=_recover, args=(ref,), daemon=True).start()
