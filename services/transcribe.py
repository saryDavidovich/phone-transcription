import os, requests, logging, threading, tempfile
from openai import OpenAI

log = logging.getLogger(__name__)

def transcribe_async(call_id, rec_url, customer_id, delivery_method, delivered_to, duration_seconds):
    t = threading.Thread(
        target=_process,
        args=(call_id, rec_url, customer_id, delivery_method, delivered_to, duration_seconds),
        daemon=True
    )
    t.start()

def _process(call_id, rec_url, customer_id, delivery_method, delivered_to, duration_seconds):
    from app import app, db
    from models import Recording, Customer, Transaction
    with app.app_context():
        try:
            rec = Recording.query.filter_by(call_id=call_id).first()
            if rec:
                rec.status = 'transcribing'
                db.session.commit()

            db.session.remove()

            transcript = _whisper_from_url(rec_url)

            db.session.remove()
            rec = Recording.query.filter_by(call_id=call_id).first()

            if not transcript:
                if rec:
                    rec.status = 'error'
                    db.session.commit()
                return

            # תיקון + סיכום ביחד בקריאה אחת ל-Claude
            transcript, summary = _summarize(transcript)

            price_per_30min = float(_get_setting('price_per_30min', '5.0'))
            cost = (duration_seconds / 1800) * price_per_30min
            cost = round(cost, 2)

            if rec:
                rec.transcript = transcript
                rec.summary = summary
                rec.status = 'transcribed'
                rec.cost = cost
                rec.duration_seconds = duration_seconds
                db.session.commit()

            customer = Customer.query.get(customer_id)
            if customer:
                customer.balance -= cost
                txn = Transaction(
                    customer_id=customer_id,
                    amount=-cost,
                    type='debit',
                    description=f'תמלול {duration_seconds//60} דקות',
                    recording_id=rec.id if rec else None
                )
                db.session.add(txn)
                db.session.commit()

            if delivery_method == 'email':
                _send_email(delivered_to, transcript, summary, customer)
            else:
                log.info(f"Fax delivery to {delivered_to} - configure Interfax")

            if rec:
                rec.status = 'delivered'
                db.session.commit()

        except Exception as e:
            log.error(f"Error processing {call_id}: {e}")

def _get_setting(key, default=''):
    from models import Settings
    s = Settings.query.filter_by(key=key).first()
    return s.value if s else default

def _whisper_from_url(url):
    try:
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        content = r.content
        log.info(f"Downloaded {len(content)} bytes from {url}")
        log.info(f"Content preview: {content[:200]}")

        import wave, audioop, io, math

        with wave.open(io.BytesIO(content)) as wav_in:
            frames = wav_in.readframes(wav_in.getnframes())
            sampwidth = wav_in.getsampwidth()
            nchannels = wav_in.getnchannels()
            framerate = wav_in.getframerate()

        log.info(f"WAV: {framerate}Hz, {sampwidth*8}bit, {nchannels}ch")

        if framerate != 16000:
            frames, _ = audioop.ratecv(frames, sampwidth, nchannels, framerate, 16000, None)
            framerate = 16000

        chunk_seconds = 600
        bytes_per_second = framerate * sampwidth * nchannels
        chunk_size = chunk_seconds * bytes_per_second
        total_chunks = math.ceil(len(frames) / chunk_size)

        client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))
        full_transcript = ''

        try:
            with open('whisper_prompt.txt', 'r', encoding='utf-8') as pf:
                whisper_prompt = pf.read().strip()[:800]
        except:
            whisper_prompt = 'ישיבה, גמרא, הלכה, רמב"ם, תלמוד, ראשונים, אחרונים, אברכים, בית מדרש, קושיא, תירוץ, חידוש'

        for i in range(total_chunks):
            chunk_frames = frames[i * chunk_size:(i + 1) * chunk_size]
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
                tmp_path = f.name
            with wave.open(tmp_path, 'wb') as wav_out:
                wav_out.setnchannels(nchannels)
                wav_out.setsampwidth(sampwidth)
                wav_out.setframerate(framerate)
                wav_out.writeframes(chunk_frames)
            with open(tmp_path, 'rb') as f:
                result = client.audio.transcriptions.create(
                    model='whisper-1',
                    file=f,
                    language='he',
                    response_format='text',
                    prompt=whisper_prompt
                )
            os.remove(tmp_path)
            full_transcript += result + ' '
            log.info(f"חלק {i+1}/{total_chunks} תומלל")

        return full_transcript.strip()

    except Exception as e:
        log.error(f"Whisper error: {e}")
        return None

def _summarize(transcript):
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

        try:
            with open('claude_terms.txt', 'r', encoding='utf-8') as f:
                terms = f.read().strip()
        except:
            terms = 'רבי, תורה, גמרא, משנה, הלכה, שבת, תפילה, ישיבה, חסידות, קבלה, תשובה, מצווה, ברכה, קדושה, ראש ישיבה, בית מדרש, חברותא, קושיא, תירוץ, חידוש, פלפול'

        prompt = f"""אתה מומחה לתמלול שיעורי תורה בעברית. קיבלת תמלול אוטומטי שנעשה על ידי Whisper ויש בו שגיאות.

רשימת מושגים תורניים — השתמש בהם לתיקון:
{terms[:4000]}

בצע שתי משימות:

1. תקן את התמלול:
   - החלף מילים שגויות במונחים הנכונים לפי ההקשר התורני
   - כאשר מילה נשמעת דומה למונח תורני — העדף את המונח התורני
   - שמור על כל המשמעות והתוכן המקורי
   - אל תוסיף תוכן שלא היה בתמלול
   - שמור על מבנה הפסקאות

2. סכם ב-3-4 נקודות קצרות בעברית את עיקרי הדברים

החזר בפורמט הבא בדיוק, ללא שום טקסט נוסף:
FIXED:
[הטקסט המתוקן המלא]

SUMMARY:
[הסיכום ב-3-4 נקודות]

תמלול לתיקון:
{transcript}"""

        msg = client.messages.create(
            model='claude-sonnet-4-5',
            max_tokens=4096,
            messages=[{'role': 'user', 'content': prompt}]
        )

        response = msg.content[0].text.strip()

        if 'FIXED:' in response and 'SUMMARY:' in response:
            parts = response.split('SUMMARY:')
            fixed_transcript = parts[0].replace('FIXED:', '').strip()
            summary = parts[1].strip()
        else:
            log.warning("Claude response format unexpected, using fallback")
            fixed_transcript = transcript
            summary = response[:500]

        log.info("Claude תיקון וסיכום הושלמו")
        return fixed_transcript, summary

    except Exception as e:
        log.error(f"Claude error: {e}")
        return transcript, ''

def _send_email(to, transcript, summary, customer):
    try:
        import sendgrid
        from sendgrid.helpers.mail import Mail
        name = customer.name if hasattr(customer, 'name') and customer.name else customer.phone if customer else ''
        summary_html = ''
        if summary:
            lines = ''.join(f'<li>{l.strip()}</li>' for l in summary.split('\n') if l.strip())
            summary_html = f'<div style="background:#f0f7ff;border-right:4px solid #2563eb;padding:16px;margin:16px 0;border-radius:6px"><strong>סיכום:</strong><ul style="margin:8px 0 0;padding-right:20px">{lines}</ul></div>'
        html = f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
<h2 style="color:#1d4ed8">תמלול שיחה</h2>
<p style="color:#6b7280">לקוח: <b>{name}</b></p>
{summary_html}
<h3>תמלול מלא:</h3>
<div style="background:#f9fafb;border:1px solid #e5e7eb;padding:16px;border-radius:8px;line-height:1.8;white-space:pre-wrap">{transcript}</div>
</div>'''
        sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))
        message = Mail(
            from_email=os.environ.get('SENDGRID_FROM_EMAIL', os.environ.get('GMAIL_USER', '')),
            to_emails=to,
            subject=f'תמלול שיחה - {name}',
            html_content=html
        )
        sg.send(message)
        log.info(f"Email sent to {to}")
    except Exception as e:
        log.error(f"Email error: {e}")
