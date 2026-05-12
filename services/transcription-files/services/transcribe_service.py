"""
services/transcribe_service.py
הורדת הקלטה + תמלול Whisper + סיכום Claude + שליחה
"""
import threading, requests, os, logging, tempfile
from openai import OpenAI
from services.database import save_transcript, get_call, update_call_status
from services.deliver_service import deliver

log = logging.getLogger(__name__)


def transcribe_async(call_id, rec_url, caller):
    """מפעיל תמלול בthread נפרד כדי לא לחסום את ה-IVR"""
    t = threading.Thread(target=_process, args=(call_id, rec_url, caller), daemon=True)
    t.start()


def _process(call_id, rec_url, caller):
    try:
        log.info(f"מתחיל עיבוד: {call_id}")
        update_call_status(call_id, 'downloading')

        # הורדת הקלטה
        audio_path = _download_recording(rec_url, call_id)
        if not audio_path:
            update_call_status(call_id, 'error_download')
            return

        # תמלול
        update_call_status(call_id, 'transcribing')
        transcript = _transcribe(audio_path)
        if not transcript:
            update_call_status(call_id, 'error_transcription')
            return

        # סיכום AI (אופציונלי)
        summary = _summarize(transcript) if os.environ.get('ENABLE_AI_SUMMARY') else ''

        # שמירה ב-DB
        save_transcript(call_id, transcript, summary)
        log.info(f"תמלול הושלם: {call_id} ({len(transcript)} תווים)")

        # שליחה
        call_data = get_call(call_id)
        if call_data:
            deliver(call_data)

        # ניקוי קובץ
        os.remove(audio_path)

    except Exception as e:
        log.error(f"שגיאה בעיבוד {call_id}: {e}")
        update_call_status(call_id, 'error')


def _download_recording(url, call_id):
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        path = os.path.join('recordings', f"{call_id}.wav")
        with open(path, 'wb') as f:
            f.write(r.content)
        return path
    except Exception as e:
        log.error(f"שגיאה בהורדה: {e}")
        return None


def _transcribe(audio_path):
    try:
        client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))
        with open(audio_path, 'rb') as f:
            result = client.audio.transcriptions.create(
                model='whisper-1',
                file=f,
                language='he',
                response_format='text'
            )
        return result
    except Exception as e:
        log.error(f"שגיאה בתמלול: {e}")
        return None


def _summarize(transcript):
    """סיכום קצר עם Claude"""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
        msg = client.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=300,
            messages=[{
                'role': 'user',
                'content': f"סכם את הדברים הבאים ב-3-4 נקודות קצרות בעברית:\n\n{transcript}"
            }]
        )
        return msg.content[0].text
    except Exception as e:
        log.warning(f"לא הצלחתי לסכם: {e}")
        return ''


def transcribe_email_voice(rec_url: str) -> str:
    """
    Transcribe a short voice recording of an email address.
    Returns the email string or empty string on failure.
    """
    import tempfile, re
    try:
        r = requests.get(rec_url, timeout=30)
        r.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            f.write(r.content)
            tmp_path = f.name

        client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))
        with open(tmp_path, 'rb') as f:
            result = client.audio.transcriptions.create(
                model='whisper-1',
                file=f,
                language='he',
                response_format='text',
                prompt='email address: user at gmail dot com'
            )
        os.remove(tmp_path)

        # Normalize spoken email to actual email
        email = result.lower().strip()
        email = email.replace(' shtrudel ', '@').replace(' at ', '@')
        email = email.replace(' nekuda ', '.').replace(' dot ', '.')
        email = email.replace(' dash ', '-').replace(' makar ', '-')
        email = re.sub(r'\s+', '', email)

        log.info(f"voice email transcribed: '{result}' -> '{email}'")
        return email

    except Exception as e:
        log.error(f"email transcription error: {e}")
        return ''
