"""
שמע וקבצים לאזור המוסד:

* השמעה: נטפרי חוסמת שמע שמוגש מהאתר שלנו, לכן כל קובץ שמע שהמוסד מנגן נמצא
  בימות המשיח (שלוחה נפרדת ivr2:/portal_audio/) והדפדפן מופנה לשם (302).
  הקלטות שהגיעו מהטלפון כבר נמצאות בימות (rec_url) - אין צורך להעלות.
  קובץ שהועלה לאתר (העלאה / הקלטה בדפדפן / קישור Drive או ג'מבו) מומר ל-MP3
  קטן (דיבור - מונו 24kbps), מועלה לימות, והעותק אצלנו נמחק.
  בעמודה play_url נשמר רק הנתיב (ivr2:/portal_audio/...), בלי טוקן - הכתובת
  נבנית בזמן ההשמעה עם המפתח המוגבל להורדה (YEMOT_DOWNLOAD_API_KEY).
* קישורים: הורדת קובץ מקישור Google Drive או ג'מבו מייל (אותו קוד כמו במיילים).
* המרה: הקלטה מהדפדפן (webm/ogg) מומרת ל-MP3 לפני התמלול.
"""
import os
import uuid
import logging
import subprocess
import tempfile

log = logging.getLogger(__name__)

YEMOT_API = 'https://www.call2all.co.il/ym/api'
PORTAL_DIR = 'ivr2:/portal_audio'


def _ffmpeg():
    import shutil
    return shutil.which('ffmpeg') or ('/usr/local/bin/ffmpeg' if os.path.exists('/usr/local/bin/ffmpeg') else None)


def upload_token():
    t = os.environ.get('YEMOT_TOKEN', '')
    if not t:
        try:
            from models import Settings
            row = Settings.query.filter_by(key='yemot_token').first()
            t = (row.value if row else '') or ''
        except Exception:
            t = ''
    return t


def to_mp3(src, bitrate='24k', rate=22050):
    """ממיר כל קובץ שמע/וידאו ל-MP3 מונו קטן (מתאים לדיבור). מחזיר נתיב לקובץ זמני או None."""
    ff = _ffmpeg()
    if not ff or not src or not os.path.exists(src):
        return None
    fd, out = tempfile.mkstemp(suffix='.mp3')
    os.close(fd)
    try:
        r = subprocess.run([ff, '-y', '-hide_banner', '-loglevel', 'error', '-i', src, '-vn', '-ac', '1',
                            '-ar', str(rate), '-b:a', bitrate, out], capture_output=True, timeout=1800)
        if r.returncode == 0 and os.path.getsize(out) > 0:
            return out
        log.warning(f'portal_media.to_mp3 נכשל: {r.stderr[-300:]!r}')
    except Exception as e:
        log.warning(f'portal_media.to_mp3 נכשל: {e}')
    try:
        os.remove(out)
    except OSError:
        pass
    return None


def _post_upload(token, path, fh, name, mime, convert):
    import requests
    r = requests.post(f'{YEMOT_API}/UploadFile', data={'token': token, 'path': path, 'convertAudio': 1 if convert else 0},
                      files={'file': (name, fh, mime)}, timeout=600)
    try:
        body = r.json()
    except ValueError:
        body = {'raw': r.text[:200]}
    ok = r.status_code == 200 and str(body.get('responseStatus', '')).upper() == 'OK'
    if not ok:
        log.warning(f'portal_media: העלאה לימות נכשלה {path} ({r.status_code}): {body}')
    return ok


def push_to_yemot(src, key):
    """מעלה קובץ מקומי לימות (כ-MP3; אם ימות לא מקבלים - כ-WAV שהם ממירים). מחזיר את הנתיב
    בימות (ivr2:/portal_audio/<key>.mp3|.wav) או None. לא מוחק את src."""
    token = upload_token()
    if not token:
        log.warning('portal_media: אין YEMOT_TOKEN - לא מעלים לימות')
        return None
    mp3 = to_mp3(src)
    try:
        if mp3:
            target = f'{PORTAL_DIR}/{key}.mp3'
            with open(mp3, 'rb') as fh:
                if _post_upload(token, target, fh, f'{key}.mp3', 'audio/mpeg', False):
                    log.info(f'portal_media: הועלה לימות {target} ({os.path.getsize(mp3)} bytes)')
                    return target
        target = f'{PORTAL_DIR}/{key}.wav'      # גיבוי: ימות ממירים בעצמם (convertAudio)
        with open(mp3 or src, 'rb') as fh:
            if _post_upload(token, target, fh, os.path.basename(mp3 or src), 'application/octet-stream', True):
                log.info(f'portal_media: הועלה לימות (המרה שלהם) {target}')
                return target
    except Exception as e:
        log.warning(f'portal_media: העלאה לימות נכשלה ({key}): {type(e).__name__}: {e}')
    finally:
        if mp3:
            try:
                os.remove(mp3)
            except OSError:
                pass
    return None


def play_url_for_path(path):
    """כתובת השמעה (DownloadFile) לנתיב בימות - עם המפתח המוגבל אם מוגדר."""
    from urllib.parse import quote
    key = os.environ.get('YEMOT_DOWNLOAD_API_KEY', '') or upload_token()
    if not key or not path:
        return None
    return f'{YEMOT_API}/DownloadFile?token={quote(key, safe="")}&path={quote(path, safe=":/")}'


def is_local_url(url):
    """כתובת לקובץ שנמצא אצלנו (העלאה / מייל) - לא בימות."""
    u = (url or '').split('?')[0]
    return '/static/fax_tmp/' in u or '/api/recordings-email/' in u


def local_file_for_url(url):
    """הנתיב בדיסק לקובץ שהכתובת שלו אצלנו, או None."""
    u = (url or '').split('?')[0]
    name = u.rsplit('/', 1)[-1]
    if not name or name.startswith('.') or '..' in name:
        return None
    if '/static/fax_tmp/' in u:
        p = os.path.join(os.path.dirname(__file__), '..', 'static', 'fax_tmp', name)
        return p if os.path.isfile(p) else None
    if '/api/recordings-email/' in u:
        from services.audio_prep import local_path_for_url
        return local_path_for_url(u)
    return None


def remove_quietly(path):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------- קישורים
LINK_HELP = 'יש להדביק קישור שיתוף של Google Drive או של ג\'מבו מייל'


def link_kind(url):
    from routes.email_inbound import _extract_gdrive_file_id, _jumbo_find_link
    url = (url or '').strip()
    if not url.lower().startswith(('http://', 'https://')):
        return None
    if _extract_gdrive_file_id(url):
        return 'drive'
    if _jumbo_find_link(url) or 'jumbomail' in url.lower() or 'jmb.' in url.lower():
        return 'jumbo'
    return None


def fetch_link(url, dest_dir):
    """מוריד קובץ שמע/וידאו מקישור Drive / ג'מבו לתיקייה. מחזיר (path, original_name).
    זורק ValueError עם הודעה בעברית בכשלון."""
    from routes import email_inbound as E
    os.makedirs(dest_dir, exist_ok=True)
    kind = link_kind(url)
    if kind == 'drive':
        fid = E._extract_gdrive_file_id(url)
        path, name, _dest = E._download_gdrive_file(fid, dest_dir)
        if not path:
            raise ValueError('לא הצלחנו להוריד מ-Drive. ודאו שהקובץ משותף כ"כל מי שיש לו את הקישור"')
    elif kind == 'jumbo':
        info = E._jumbo_resolve(E._jumbo_find_link(url) or url)
        if not info:
            raise ValueError('קישור הג\'מבו לא תקין או שפג תוקפו')
        fid, fname = None, None
        for f_id, f_name, _s in info.get('files') or []:
            if os.path.splitext(f_name)[1].lstrip('.').lower() in E.AUDIO_VIDEO_EXTENSIONS:
                fid, fname = f_id, f_name
                break
        if not fid:
            raise ValueError('בהעברה הזו אין קובץ שמע או וידאו')
        path, name, _dest = E._download_jumbo_file(info, fid, fname, dest_dir)
        if not path:
            raise ValueError('ההורדה מג\'מבו נכשלה - נסו שוב בעוד רגע')
    else:
        raise ValueError(LINK_HELP)
    ext = os.path.splitext(path)[1].lstrip('.').lower()
    if ext not in E.AUDIO_VIDEO_EXTENSIONS:
        remove_quietly(path)
        raise ValueError('הקובץ בקישור אינו קובץ שמע או וידאו')
    return path, name or os.path.basename(path)


def needs_conversion(path):
    """הקלטה מהדפדפן (webm/ogg/opus) - ממירים ל-MP3 לפני התמלול (המקצועי לא מקבל webm)."""
    return os.path.splitext(path or '')[1].lower() in ('.webm', '.ogg', '.oga', '.opus', '.weba')


def convert_in_place(path):
    """ממיר קובץ לתמלול ל-MP3 באותה תיקייה (שם חדש). מחזיר את הנתיב החדש (או הישן אם נכשל)."""
    mp3 = to_mp3(path, bitrate='64k', rate=16000)
    if not mp3:
        return path
    new = os.path.join(os.path.dirname(path), f'{uuid.uuid4().hex}.mp3')
    import shutil
    shutil.move(mp3, new)
    remove_quietly(path)
    return new
