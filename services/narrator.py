"""
קריין: הפקת הודעות מערכת בקול קבוע (Gemini TTS), והעלאה לימות במקום הקלטה קיימת.

- הקול נבחר פעם אחת (הגדרה narrator_voice) ותמיד אותו קול, עם הוראת סגנון קבועה:
  קריין תפריט טלפוני - מהיר, ברור וסמכותי.
- הודעת מערכת מספר N בימות נמצאת ב-/הודעות מערכת/NNN/000.wav (כך המערכת שלנו
  משמיעה אותן - ראו MSG() ב-ivr.js). מעלים רק את הקובץ עצמו לנתיב הזה.
- לפני החלפה נשמר במסד הנתונים עותק של ההקלטה הקודמת מימות - לשחזור.
- שקט בהתחלה ובסוף נחתך אוטומטית.
"""
import base64
import io
import logging
import os
import re

import requests

log = logging.getLogger(__name__)

VOICES = {   # קולות של Gemini; הראשון - ברירת המחדל
    'Charon': 'צ׳רון - גברי, ענייני וברור',
    'Orus': 'אורוס - גברי, יציב וסמכותי',
    'Fenrir': 'פנריר - גברי, נמרץ ומהיר',
    'Iapetus': 'יאפטוס - גברי, צלול',
    'Kore': 'קורה - נשי, תקיף',
    'Aoede': 'איידי - נשי, קליל',
}
TTS_MODELS = ('gemini-3.1-flash-tts-preview', 'gemini-2.5-flash-preview-tts', 'gemini-2.5-flash-tts')
STYLE = ('קרא את הטקסט הבא בקול של קריין מערכת טלפונית: קצב מהיר, הגייה ברורה וטון סמכותי ובטוח, '
         'בלי הפסקות מיותרות ובלי רגש מוגזם. אל תוסיף ואל תשמיט מילים.\n\n')


def msg_path(num):
    return f'/הודעות מערכת/{int(num):03d}/000.wav'


def _setting(key, default=''):
    from models import Settings
    s = Settings.query.filter_by(key=key).first()
    return s.value if s and s.value else default


def current_voice():
    v = _setting('narrator_voice', 'Charon')
    return v if v in VOICES else 'Charon'


SPEEDS = {'1.0': 'רגיל', '1.1': 'מהיר', '1.2': 'מהיר מאוד'}


def current_speed():
    v = _setting('narrator_speed', '1.1')
    return v if v in SPEEDS else '1.1'


def synthesize(text, voice=None, speed=None):
    """טקסט -> (בייטים של WAV, שם המנוע). קודם Chirp 3 HD - אותו קול בדיוק בכל פעם;
    אם השירות לא מופעל בחשבון גוגל - Gemini עם הגדרות קבועות (מעט שונות בין הפקות)."""
    text = (text or '').strip()
    if not text:
        raise ValueError('יש לכתוב נוסח להקראה')
    voice = voice if voice in VOICES else current_voice()
    speed = speed if speed in SPEEDS else current_speed()
    errors = []
    try:
        return _chirp(text, voice, speed), 'chirp'
    except Exception as e:
        errors.append(f'Chirp: {e}')
        log.warning(f'narrator: Chirp 3 HD נכשל, עוברים ל-Gemini: {e}')
    try:
        return _gemini(text, voice), 'gemini'
    except Exception as e:
        errors.append(f'Gemini: {e}')
    raise RuntimeError('ההקראה נכשלה. ' + ' | '.join(errors))


def _chirp(text, voice, speed):
    """Google Cloud Text-to-Speech, קול Chirp 3 HD בעברית (he-IL-Chirp3-HD-<קול>).
    קול יציב - אותו קול בכל הפקה. דורש להפעיל פעם אחת את Cloud Text-to-Speech API בפרויקט של המפתח."""
    key = os.environ.get('GOOGLE_TTS_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    body = {'input': {'text': text},
            'voice': {'languageCode': 'he-IL', 'name': f'he-IL-Chirp3-HD-{voice}'},
            'audioConfig': {'audioEncoding': 'LINEAR16', 'sampleRateHertz': 24000, 'speakingRate': float(speed)}}
    r = requests.post('https://texttospeech.googleapis.com/v1/text:synthesize', params={'key': key}, json=body, timeout=60)
    if r.status_code == 400 and 'speakingRate' in r.text:          # אם הקול לא תומך בשינוי מהירות
        body['audioConfig'].pop('speakingRate')
        r = requests.post('https://texttospeech.googleapis.com/v1/text:synthesize', params={'key': key}, json=body, timeout=60)
    if r.status_code != 200:
        try:
            msg = r.json().get('error', {}).get('message', r.text[:200])
        except ValueError:
            msg = r.text[:200]
        raise RuntimeError(f'{r.status_code}: {msg}')
    audio = base64.b64decode(r.json().get('audioContent') or b'')
    if not audio:
        raise RuntimeError('לא התקבל שמע')
    log.info(f'narrator: הופקה הקראה (Chirp 3 HD, {voice}, מהירות {speed})')
    return audio


def _gemini(text, voice):
    """גיבוי: Gemini TTS. הגדרות קבועות (בלי אקראיות, זרע קבוע, עברית) כדי לצמצם הבדלים בין הפקות."""
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=os.environ.get('GOOGLE_API_KEY'))   # נשמר במשתנה עד סוף הבקשה
    cfg = types.GenerateContentConfig(
        response_modalities=['AUDIO'], temperature=0.0, seed=7,
        speech_config=types.SpeechConfig(language_code='he-IL', voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))))
    last = None
    pinned = _setting('narrator_model')
    for model in ([pinned] if pinned else []) + [m for m in TTS_MODELS if m != pinned]:
        try:
            resp = client.models.generate_content(model=model, contents=STYLE + text, config=cfg)
            part = resp.candidates[0].content.parts[0]
            pcm = part.inline_data.data
            if isinstance(pcm, str):
                pcm = base64.b64decode(pcm)
            if not pcm:
                raise RuntimeError('לא התקבל שמע')
            if not pinned:                                  # תמיד אותו מודל - מודל אחר = קול אחר
                _save_setting('narrator_model', model)
            if pcm[:4] == b'RIFF':
                return pcm
            rate = 24000
            m = re.search(r'rate=(\d+)', getattr(part.inline_data, 'mime_type', '') or '')
            if m:
                rate = int(m.group(1))
            import wave
            out = io.BytesIO()
            with wave.open(out, 'wb') as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm)
            log.info(f'narrator: הופקה הקראה (Gemini {model}, {voice})')
            return out.getvalue()
        except Exception as e:
            last = e
            log.warning(f'narrator: מודל {model} נכשל: {e}')
    raise RuntimeError(str(last))


def _save_setting(key, value):
    from app import db
    from models import Settings
    s = Settings.query.filter_by(key=key).first()
    if s:
        s.value = value
    else:
        db.session.add(Settings(key=key, value=value))
    db.session.commit()


def prepare(audio_bytes, trim=True):
    """כל קובץ שמע -> WAV מונו 8kHz 16bit (הפורמט של ימות), עם חיתוך שקט בהתחלה ובסוף."""
    from pydub import AudioSegment
    from pydub.silence import detect_leading_silence
    seg = AudioSegment.from_file(io.BytesIO(audio_bytes))
    if trim and len(seg) > 400:
        thresh = (seg.dBFS - 22) if seg.dBFS != float('-inf') else -50
        start = detect_leading_silence(seg, silence_threshold=thresh, chunk_size=5)
        end = detect_leading_silence(seg.reverse(), silence_threshold=thresh, chunk_size=5)
        start = max(0, start - 15)                    # לא לקטוע את תחילת ההגה
        end = max(0, end - 80)                        # זנב קצר כדי שהמילה האחרונה לא תיחתך
        if start + end < len(seg) - 200:
            seg = seg[start:len(seg) - end]
    seg = seg.set_channels(1).set_frame_rate(8000).set_sample_width(2)
    out = io.BytesIO()
    seg.export(out, format='wav')
    return out.getvalue(), round(len(seg) / 1000.0, 1)


def preview_mp3_b64(wav_bytes):
    """לשמיעה בממשק (מוטמע בדף - נטפרי חוסמים שמע שמגיע מהאתר שלנו)."""
    from pydub import AudioSegment
    out = io.BytesIO()
    AudioSegment.from_file(io.BytesIO(wav_bytes)).export(out, format='mp3', bitrate='48k')
    return base64.b64encode(out.getvalue()).decode('ascii')


def _token():
    t = os.environ.get('YEMOT_TOKEN', '')
    if not t:
        raise RuntimeError('YEMOT_TOKEN לא מוגדר בשרת')
    return t


def download_current(num):
    """ההקלטה שנמצאת עכשיו בימות להודעה הזו (bytes), או None אם אין."""
    try:
        r = requests.get('https://www.call2all.co.il/ym/api/DownloadFile',
                         params={'token': _token(), 'path': 'ivr2:' + msg_path(num)}, timeout=60)
        if r.status_code == 200 and r.content[:4] == b'RIFF':
            return r.content
    except Exception as e:
        log.warning(f'narrator: הורדת ההקלטה הקיימת {num} נכשלה: {e}')
    return None


def upload(num, wav_bytes):
    path = msg_path(num)
    r = requests.post('https://www.call2all.co.il/ym/api/UploadFile',
                      data={'token': _token(), 'path': 'ivr2:' + path, 'convertAudio': 1},
                      files={'file': ('000.wav', io.BytesIO(wav_bytes), 'audio/wav')}, timeout=120)
    try:
        body = r.json()
    except ValueError:
        body = {'raw': r.text[:200]}
    if r.status_code != 200 or str(body.get('responseStatus', '')).upper() != 'OK':
        raise RuntimeError(f'ימות דחו את ההעלאה ({r.status_code}): {body}')
    log.info(f'narrator: הועלה {path} ({len(wav_bytes)} בתים)')
    return path
