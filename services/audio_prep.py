"""
הכנת קובץ הקלטה לתמלול בלי לטעון אותו כולו לזיכרון.

הרקע (אוקטובר 2026): לקוח שלח קישורי Drive להקלטות WAV באיכות גבוהה מאוד
(48kHz סטריאו) - 94 דקות = 1.08GB, 161 דקות = כמעט 2GB. הקוד הישן הוריד את
כל הקובץ לזיכרון (r.content) ועשה ממנו עוד כמה עותקים (frames, המרת קצב,
פיצול) - פי כמה מגודל הקובץ - וה-worker ב-Railway נשאר בלי זיכרון ("cannot
allocate memory") או נהרג באמצע (הקלטות שנתקעו על "transcribing").

כאן הכל נעשה על הדיסק ובחלקים קטנים:
  1. open_source   - אם הקובץ כבר אצלנו בדיסק (הקלטת מייל/Drive) קוראים אותו
                     ישירות, בלי להוריד מעצמנו דרך האינטרנט (שגם נתקל במגבלת
                     30 השניות של gunicorn). אחרת - הורדה בזרימה לקובץ זמני.
  2. prepare_wav   - המרה ל-WAV של 16kHz, ערוץ אחד, 16 ביט (מספיק לגמרי לתמלול)
                     בבלוקים של 10 שניות. 94 דקות: 1.08GB -> כ-180MB.
  3. iter_wav_chunks - פיצול לחלקים של 5 דקות לתמלול, אחד-אחד מהדיסק.
הזיכרון בשימוש: עשרות מגה בכל רגע, בלי קשר לאורך ההקלטה.
"""
import os
import io
import re
import shutil
import logging
import tempfile
import subprocess
import wave

log = logging.getLogger(__name__)

TARGET_RATE = 16000
BLOCK_SECONDS = 10          # גודל בלוק בהמרה (48kHz סטריאו: ~1.9MB לבלוק)
DOWNLOAD_CHUNK = 1024 * 1024

_EMAIL_REC_RE = re.compile(r'/api/recordings-email/([0-9A-Za-z_.-]+)$')


def _email_recordings_dir():
    return os.environ.get('RECORDINGS_EMAIL_DIR', 'recordings_email')


def local_path_for_url(url):
    """אם ה-URL מצביע על הקלטת מייל שנמצאת אצלנו בדיסק - מחזיר את הנתיב, אחרת None."""
    m = _EMAIL_REC_RE.search((url or '').split('?')[0])
    if not m:
        return None
    name = m.group(1)
    if name.startswith('.') or '/' in name:
        return None
    path = os.path.join(_email_recordings_dir(), name)
    return path if os.path.isfile(path) else None


def open_source(url, timeout=300):
    """מחזיר (path, is_temp). קובץ מקומי אם יש, אחרת הורדה בזרימה לקובץ זמני."""
    local = local_path_for_url(url)
    if local:
        log.info(f"audio_prep: קובץ מקומי {local} ({os.path.getsize(local)} bytes) - בלי הורדה")
        return local, False
    import requests
    suffix = os.path.splitext((url or '').split('?')[0])[1][:6] or '.bin'
    fd, tmp = tempfile.mkstemp(prefix='rec_src_', suffix=suffix)
    try:
        with os.fdopen(fd, 'wb') as out, requests.get(url, timeout=timeout, stream=True) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=DOWNLOAD_CHUNK):
                if chunk:
                    out.write(chunk)
        log.info(f"audio_prep: הורד בזרימה {os.path.getsize(tmp)} bytes")
        return tmp, True
    except Exception:
        _rm(tmp)
        raise


def _rm(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def cleanup(*paths_and_flags):
    """cleanup((path, is_temp), ...) - מוחק רק קבצים זמניים שאנחנו יצרנו."""
    for item in paths_and_flags:
        if item and item[1]:
            _rm(item[0])


def wav_info(path):
    """(channels, sampwidth, rate, nframes) או None אם זה לא WAV PCM שפייתון קורא."""
    try:
        with wave.open(path, 'rb') as w:
            return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
    except Exception:
        return None


def _convert_python(src, dst):
    """המרת WAV PCM ל-16kHz מונו 16 ביט, בלוק אחרי בלוק (זיכרון קבוע)."""
    import audioop
    with wave.open(src, 'rb') as w_in, wave.open(dst, 'wb') as w_out:
        ch, sw, rate = w_in.getnchannels(), w_in.getsampwidth(), w_in.getframerate()
        w_out.setnchannels(1)
        w_out.setsampwidth(2)
        w_out.setframerate(TARGET_RATE)
        state = None
        block = max(1, rate * BLOCK_SECONDS)
        while True:
            data = w_in.readframes(block)
            if not data:
                break
            if sw == 1:   # WAV של 8 ביט שמור כ-unsigned
                data = audioop.bias(data, 1, -128)
            if sw != 2:
                data = audioop.lin2lin(data, sw, 2)
            if ch == 2:
                data = audioop.tomono(data, 2, 0.5, 0.5)
            elif ch > 2:   # נדיר - לוקחים ערוץ ראשון
                frame = 2 * ch
                data = b''.join(data[i:i + 2] for i in range(0, len(data), frame))
            if rate != TARGET_RATE:
                data, state = audioop.ratecv(data, 2, 1, rate, TARGET_RATE, state)
            w_out.writeframes(data)


def _convert_ffmpeg(src, dst):
    ff = shutil.which('ffmpeg')
    if not ff:
        return False
    res = subprocess.run([ff, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', src,
                          '-vn', '-ac', '1', '-ar', str(TARGET_RATE), '-sample_fmt', 's16', dst],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=3600)
    if res.returncode != 0:
        log.warning(f"audio_prep: ffmpeg נכשל: {res.stderr.decode('utf-8', 'replace')[-400:]}")
        return False
    return wav_info(dst) is not None


REPLACE_ORIGINAL_MIN = 50 * 1024 * 1024


def prepare_wav(src, replace_original=False):
    """מחזיר (path, is_temp, duration_seconds) של WAV 16kHz מונו 16 ביט, או None
    אם לא הצלחנו להמיר (פורמט לא מוכר ואין ffmpeg).
    replace_original=True (קובץ מקומי שלנו, למשל הקלטת מייל): אם המקור גדול,
    הגרסה המוקטנת מחליפה אותו בדיסק - חוסך מקום (קבצי מייל נשמרים יומיים) וגם
    שליחה חוזרת לתמלול כבר לא צריכה להמיר שוב."""
    res = _prepare_wav(src)
    if (res and res[1] and replace_original and src.lower().endswith('.wav')
            and os.path.getsize(src) >= REPLACE_ORIGINAL_MIN):
        try:
            shutil.move(res[0], src + '.tmp')
            os.chmod(src + '.tmp', 0o644)   # mkstemp יוצר 0600 - שיהיה כמו שאר הקבצים
            os.replace(src + '.tmp', src)
            log.info(f"audio_prep: המקור {src} הוחלף בגרסה המוקטנת ({os.path.getsize(src)} bytes)")
            return src, False, res[2]
        except OSError as e:
            log.warning(f"audio_prep: החלפת המקור נכשלה ({e}) - ממשיכים עם הקובץ הזמני")
            _rm(src + '.tmp')
    return res


def _prepare_wav(src):
    info = wav_info(src)
    if info and info[0] == 1 and info[1] == 2 and info[2] == TARGET_RATE:
        return src, False, info[3] // TARGET_RATE
    fd, dst = tempfile.mkstemp(prefix='rec_16k_', suffix='.wav')
    os.close(fd)
    try:
        if info:
            _convert_python(src, dst)
        elif not _convert_ffmpeg(src, dst):
            _rm(dst)
            return None
        out = wav_info(dst)
        # משך לפי הקובץ המקורי כשאפשר (ההמרה יכולה "לאבד" דגימה אחת בעיגול -> שנייה פחות בחיוב)
        dur = info[3] // info[2] if info else (out[3] // TARGET_RATE if out else 0)
        log.info(f"audio_prep: הומר {os.path.getsize(src)} -> {os.path.getsize(dst)} bytes, {dur}s")
        return dst, True, dur
    except Exception as e:
        log.warning(f"audio_prep: המרה נכשלה: {e}")
        _rm(dst)
        return None


def iter_wav_chunks(path, chunk_seconds=300):
    """מחזיר חלקי WAV (bytes) של chunk_seconds כל אחד, נקראים אחד-אחד מהדיסק."""
    with wave.open(path, 'rb') as w:
        params = (w.getnchannels(), w.getsampwidth(), w.getframerate())
        per_chunk = params[2] * chunk_seconds
        while True:
            data = w.readframes(per_chunk)
            if not data:
                break
            buf = io.BytesIO()
            with wave.open(buf, 'wb') as out:
                out.setnchannels(params[0])
                out.setsampwidth(params[1])
                out.setframerate(params[2])
                out.writeframes(data)
            yield buf.getvalue()


def iter_wav_chunks_at_silence(path, max_seconds=600, search_seconds=120, window_seconds=0.2):
    """כמו iter_wav_chunks, אבל כל חיתוך נעשה ברגע השקט ביותר שבתוך search_seconds
    האחרונות שלפני max_seconds (למשל בין דקה 8 לדקה 10) - כדי לא לחתוך באמצע מילה.
    כל חלק לכל היותר max_seconds. נקרא מהדיסק חלק-חלק (זיכרון קבוע).
    מחזיר bytes של WAV לכל חלק. מניח PCM 16 ביט (כמו הפלט של prepare_wav)."""
    import numpy as np
    with wave.open(path, 'rb') as w:
        ch, sw, rate, total = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        max_frames = int(rate * max_seconds)
        win = max(1, int(rate * window_seconds))
        pos = 0
        while pos < total:
            w.setpos(pos)
            data = w.readframes(max_frames)
            n = len(data) // (sw * ch)
            if not n:
                break
            cut = n
            if pos + n < total and sw == 2:
                # מחפשים את החלון השקט ביותר בקטע החיפוש שבסוף החלק
                samples = np.frombuffer(data, dtype='<i2')
                if ch > 1:
                    samples = samples[::ch]
                lo = max(0, n - int(rate * search_seconds))
                region = samples[lo:lo + ((n - lo) // win) * win].astype(np.float32)
                if region.size >= win:
                    rms = np.sqrt((region.reshape(-1, win) ** 2).mean(axis=1))
                    cut = lo + int(rms.argmin()) * win + win // 2
            chunk = data[:cut * sw * ch]
            buf = io.BytesIO()
            with wave.open(buf, 'wb') as out:
                out.setnchannels(ch)
                out.setsampwidth(sw)
                out.setframerate(rate)
                out.writeframes(chunk)
            yield buf.getvalue()
            pos += cut


def chunk_count(path, chunk_seconds=300):
    info = wav_info(path)
    if not info:
        return 0
    per = info[2] * chunk_seconds
    return max(1, -(-info[3] // per))


PROBE_LIMIT = 2 * 1024 * 1024   # 2MB - יותר מ-6 שניות בכל פורמט WAV סביר


def probe_duration(url, timeout=60):
    """בודק את אורך ההקלטה בלי להוריד את כולה: מוריד עד 2MB. אם הקובץ נגמר
    לפני כן - מחזיר את האורך המדויק בשניות (float). אם הוא גדול יותר - מחזיר
    float('inf') (בטוח ארוך מ-6 שניות). None - לא WAV תקין / לא ניתן להוריד."""
    local = local_path_for_url(url)
    if local:
        info = wav_info(local)
        return (info[3] / info[2]) if info and info[2] else None
    import requests
    data = bytearray()
    try:
        with requests.get(url, timeout=timeout, stream=True) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=64 * 1024):
                data.extend(chunk)
                if len(data) > PROBE_LIMIT:
                    return float('inf')
    except Exception as e:
        log.warning(f"audio_prep: probe נכשל: {e}")
        return None
    try:
        with wave.open(io.BytesIO(bytes(data)), 'rb') as w:
            return w.getnframes() / w.getframerate() if w.getframerate() else None
    except Exception:
        return None
