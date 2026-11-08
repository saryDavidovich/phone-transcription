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
    subj = (subject or '').lower()
    return ('mailer-daemon' in s and 'zoho' in s and
            (('forward' in subj and 'fail' in subj) or ('כשל' in subj and 'העבר' in subj)))


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
    if not ref.get('date'):
        body = (req.form.get('text') or '') + '\n' + (req.form.get('html') or '')
        m = re.search(r'Arrival-Date:\s*([^<\n]+)', body, re.I)
        if m:
            ref['date'] = m.group(1).strip()
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


LARGE_BYTES = 15 * 1024 * 1024     # מיילים גדולים מזה הם אלה ש-SendGrid עלול לדחות


def _since_date(ref):
    """IMAP SINCE (יום לפני ההגעה, או יומיים אחורה) - בפורמט 02-Nov-2026."""
    from datetime import datetime, timedelta
    from email.utils import parsedate_to_datetime
    d = None
    try:
        d = parsedate_to_datetime(ref.get('date')) if ref.get('date') else None
    except Exception:
        d = None
    d = (d or datetime.utcnow()) - timedelta(days=1 if d else 2)
    return d.strftime('%d-%b-%Y')


def _imap_find(M, ref):
    """מחפש את המייל המקורי. לפי Message-ID אם יש, אחרת לפי שולח, ואם אין שום פרט
    (זוהו לא צירפו את כותרות המקור) - כל המיילים הגדולים מהימים האחרונים; מיילים
    שכבר טופלו מדולגים ממילא (הגנת הכפילות לפי Message-ID במסלול הרגיל).
    מחזיר רשימה של (תיקייה, uid)."""
    folders = ['INBOX']
    try:
        typ, data = M.list()
        for line in data or []:
            name = line.decode('utf-8', 'replace').rsplit(' "/" ', 1)[-1].strip().strip('"')
            # גם פח האשפה: בזוהו מוגדר "העבר ומחק", כך שהמיילים שהועברו נמצאים שם
            if name and name.upper() != 'INBOX' and not re.search(r'(sent|draft|outbox|נשלח|טיוט|יוצא)', name, re.I):
                folders.append(name)
    except Exception:
        pass
    mid = ref.get('message-id')
    sender = re.search(r'[\w.+-]+@[\w-]+(\.[\w-]+)+', ref.get('from') or '')
    since = _since_date(ref)
    found = []
    for folder in folders:
        try:
            typ, _ = M.select(f'"{folder}"', readonly=True)
            if typ != 'OK':
                continue
            if mid:
                crit = ('HEADER', 'Message-ID', f'"{mid}"')
            elif sender:
                crit = ('SINCE', since, 'FROM', f'"{sender.group(0)}"')
            else:
                crit = ('SINCE', since, 'LARGER', str(LARGE_BYTES))
            typ, data = M.uid('SEARCH', None, *crit)
            uids = (data[0] or b'').split() if typ == 'OK' else []
            if mid or sender:
                uids = uids[-1:]
            found += [(folder, u) for u in uids]
            if (mid or sender) and found:
                break
        except Exception as e:
            log.warning(f'zoho_recover: חיפוש בתיקייה {folder} נכשל: {e}')
    return found


def _already_processed(message_id):
    if not message_id:
        return False
    from app import app
    from models import ProcessedWebhook
    with app.app_context():
        return ProcessedWebhook.query.filter_by(message_id=message_id).first() is not None


def fetch_originals(ref):
    """מוצא את המיילים המקוריים בזוהו ומחזיר אותם (bytes) אחד-אחד. מייל שכבר טופל
    (לפי Message-ID) מדולג עוד לפני שמורידים אותו. זורק שגיאה עם הסבר אם לא נמצא כלום."""
    user = os.environ.get('ZOHO_IMAP_USER', '').strip()
    pwd = os.environ.get('ZOHO_IMAP_PASSWORD', '').strip()
    host = os.environ.get('ZOHO_IMAP_HOST', '').strip() or 'imappro.zoho.com'
    if not user or not pwd:
        raise RuntimeError('לא הוגדרו ZOHO_IMAP_USER / ZOHO_IMAP_PASSWORD ב-Railway')
    for attempt in range(1, _SEARCH_ATTEMPTS + 1):
        M = imaplib.IMAP4_SSL(host, 993, timeout=120)
        found_any = False
        try:
            try:
                M.login(user, pwd)
            except imaplib.IMAP4.error as e:
                raise RuntimeError(f'זוהו דחו את ההתחברות ({host}, {user}) - בדוק שגישת IMAP פעילה '
                                   f'ושהסיסמה ב-ZOHO_IMAP_PASSWORD היא סיסמת אפליקציה: {e}')
            hits = _imap_find(M, ref)
            log.info(f'zoho_recover: נמצאו {len(hits)} מיילים מתאימים: {[f for f, _ in hits]}')
            for folder, uid in hits:
                found_any = True
                M.select(f'"{folder}"', readonly=True)
                typ, data = M.uid('FETCH', uid, '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
                hdr = b''.join(p[1] for p in (data or []) if isinstance(p, tuple) and len(p) > 1)
                m = re.search(rb'Message-ID:\s*(<[^>\s]+>)', hdr, re.I)
                mid = m.group(1).decode('utf-8', 'replace') if m else None
                if _already_processed(mid):
                    log.info(f'zoho_recover: {mid} כבר טופל בעבר - מדלגים')
                    continue
                typ, data = M.uid('FETCH', uid, '(BODY.PEEK[])')
                for part in data or []:
                    if isinstance(part, tuple) and len(part) > 1:
                        log.info(f'zoho_recover: מוריד מייל מ-{folder} ({len(part[1])} בתים, {mid})')
                        yield part[1]
                        break
        finally:
            try:
                M.logout()
            except Exception:
                pass
        if found_any:
            return
        if attempt < _SEARCH_ATTEMPTS:
            time.sleep(_SEARCH_DELAY)
    raise RuntimeError('המייל המקורי לא נמצא בתיבה בזוהו (חיפוש בכל התיקיות כולל פח האשפה)')


def build_form(raw):
    """ממיר מייל גולמי לשדות כמו ש-SendGrid שולח (from/to/subject/text/html/headers + קבצים)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    headers = '\n'.join(f'{k}: {v}' for k, v in msg.items())
    data = {
        'from': str(msg.get('From', '')), 'to': str(msg.get('To', '')),
        'subject': str(msg.get('Subject', '')), 'headers': headers, 'text': '', 'html': '',
    }
    n = 0
    info = {}   # כמו ה-attachment-info של SendGrid: תמונות מוטמעות בגוף (חתימה) מסומנות ב-content-id
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        ctype = part.get_content_type()
        if filename or part.get_content_disposition() == 'attachment':
            payload = part.get_payload(decode=True) or b''
            n += 1
            data[f'attachment{n}'] = (io.BytesIO(payload), filename or f'attachment{n}', ctype)
            cid = str(part.get('Content-ID') or '').strip('<> ')
            entry = {'filename': filename or f'attachment{n}', 'type': ctype}
            if cid:   # כמו SendGrid: כל חלק עם Content-ID
                entry['content-id'] = cid
            info[f'attachment{n}'] = entry
        elif ctype == 'text/plain' and not data['text']:
            data['text'] = part.get_content()
        elif ctype == 'text/html' and not data['html']:
            data['html'] = part.get_content()
    data['attachments'] = str(n)
    data['attachment-info'] = __import__('json').dumps(info)
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
        done = 0
        for raw in fetch_originals(ref):
            result = replay(raw)
            done += 1
            log.info(f'zoho_recover: מייל מזוהו ({desc}) עובד מחדש: {result}')
        if not done:
            raise RuntimeError('נמצאו בזוהו רק מיילים שכבר טופלו בעבר - לא נמצא מייל חדש לעיבוד')
    except Exception as e:
        log.error(f'zoho_recover: שחזור נכשל ({desc}): {e}')
        _alert('זוהו לא הצליחו להעביר מייל ללקוח (כנראה קובץ גדול), והמשיכה הישירה מזוהו נכשלה.\n'
               f'המייל: {desc or "לא ידוע"}\nסיבה: {e}\n'
               'יש לטפל ידנית: למצוא את המייל בתיבה בזוהו ולהעלות את הקובץ דרך "תמלול חוזר" בכרטיס הלקוח.')


def recover_async(ref):
    threading.Thread(target=_recover, args=(ref,), daemon=True).start()
