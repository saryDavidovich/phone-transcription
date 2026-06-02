import os, requests, logging, threading, tempfile, time, math
from openai import OpenAI

log = logging.getLogger(__name__)

def transcribe_async(call_id, rec_url, customer_id, delivery_method, delivered_to, duration_seconds, transcription_tier='basic'):
    t = threading.Thread(
        target=_process,
        args=(call_id, rec_url, customer_id, delivery_method, delivered_to, duration_seconds, transcription_tier),
        daemon=True
    )
    t.start()

def _process(call_id, rec_url, customer_id, delivery_method, delivered_to, duration_seconds, transcription_tier='basic'):
    from app import app, db
    from models import Recording, Customer, Transaction
    with app.app_context():
        try:
            rec = Recording.query.filter_by(call_id=call_id).first()
            if rec:
                rec.status = 'transcribing'
                db.session.commit()

            db.session.remove()

            tier = transcription_tier

            if tier == 'premium':
                log.info(f"Using Sofer.ai for customer {customer_id}")
                transcript_raw, actual_duration = _soferai_from_url(rec_url)
                transcript_fixed = transcript_raw
            else:
                log.info(f"Using Whisper for customer {customer_id}")
                transcript_raw, actual_duration = _whisper_from_url(rec_url)
                transcript_fixed = _fix_transcript(transcript_raw) if transcript_raw else None

            if actual_duration and actual_duration > 0:
                duration_seconds = actual_duration
                log.info(f"Actual duration from file: {duration_seconds}s")

            db.session.remove()
            rec = Recording.query.filter_by(call_id=call_id).first()

            if not transcript_raw:
                if rec:
                    rec.status = 'error'
                    db.session.commit()
                return

            if tier == 'premium':
                price_per_20min = float(_get_setting('price_per_20min_premium', '1.90'))
            else:
                price_per_20min = float(_get_setting('price_per_20min_basic', '0.90'))

            units = math.ceil(duration_seconds / 1200)
            cost = units * price_per_20min
            cost = round(cost, 2)

            if rec:
                rec.transcript = transcript_fixed
                rec.summary = transcript_raw if tier == 'basic' else ''
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
                    description=f'תמלול {duration_seconds//60} דקות ({tier})',
                    recording_id=rec.id if rec else None
                )
                db.session.add(txn)
                db.session.commit()

            if delivery_method == 'email':
                if tier == 'premium':
                    _send_email_premium(delivered_to, transcript_fixed, customer, rec_url, duration_seconds)
                else:
                    _send_email(delivered_to, transcript_raw, transcript_fixed, customer, rec_url, duration_seconds)
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

def _soferai_from_url(url):
    try:
        import base64, wave, audioop, io
        from soferai import SoferAI
        from soferai.transcribe import TranscriptionRequestInfo

        client = SoferAI(api_key=os.environ.get('SOFERAI_API_KEY'))

        r = requests.get(url, timeout=120)
        r.raise_for_status()
        log.info(f"Downloaded {len(r.content)} bytes for Sofer.ai")

        actual_duration = 0
        try:
            with wave.open(io.BytesIO(r.content)) as wav_in:
                frames = wav_in.readframes(wav_in.getnframes())
                sampwidth = wav_in.getsampwidth()
                nchannels = wav_in.getnchannels()
                framerate = wav_in.getframerate()
                actual_duration = wav_in.getnframes() // framerate

            log.info(f"Original WAV: {framerate}Hz, {sampwidth*8}bit, {nchannels}ch, {actual_duration}s")

            # Upsampling ל-16000Hz
            if framerate != 16000:
                frames, _ = audioop.ratecv(frames, sampwidth, nchannels, framerate, 16000, None)
                framerate = 16000
                log.info("Upsampled to 16000Hz for Sofer.ai")

            output_buffer = io.BytesIO()
            with wave.open(output_buffer, 'wb') as wav_out:
                wav_out.setnchannels(nchannels)
                wav_out.setsampwidth(sampwidth)
                wav_out.setframerate(framerate)
                wav_out.writeframes(frames)
            audio_content = output_buffer.getvalue()

        except Exception as e:
            log.warning(f"Could not process WAV: {e}, using original")
            audio_content = r.content

        base64_audio = base64.b64encode(audio_content).decode('utf-8')

        response = client.transcribe.create_transcription(
            audio_file=base64_audio,
            info=TranscriptionRequestInfo(
                model='v1',
                primary_language='he',
                hebrew_word_format=['he'],
                num_speakers=1,
            )
        )

        transcription_id = response
        log.info(f"Sofer.ai transcription created: {transcription_id}")

        for attempt in range(40):
            time.sleep(15)
            status = client.transcribe.get_transcription_status(
                transcription_id=transcription_id
            )
            log.info(f"Sofer.ai status: {status.status} (attempt {attempt+1})")

            if status.status.upper() == 'COMPLETED':
                result = client.transcribe.get_transcription(
                    transcription_id=transcription_id
                )
                log.info("Sofer.ai transcription completed")
                return result.text, actual_duration

            elif status.status.upper() in ('FAILED', 'ERROR', 'INSUFFICIENT_FUNDS'):
                log.error(f"Sofer.ai failed: {status.status}")
                return None, actual_duration

        log.error("Sofer.ai timeout")
        return None, actual_duration

    except Exception as e:
        log.error(f"Sofer.ai error: {e}")
        return None, 0

def _whisper_from_url(url):
    try:
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        content = r.content
        log.info(f"Downloaded {len(content)} bytes from {url}")
        log.info(f"Content preview: {content[:200]}")

        import wave, audioop, io

        with wave.open(io.BytesIO(content)) as wav_in:
            frames = wav_in.readframes(wav_in.getnframes())
            sampwidth = wav_in.getsampwidth()
            nchannels = wav_in.getnchannels()
            framerate = wav_in.getframerate()
            actual_duration = wav_in.getnframes() // framerate

        log.info(f"WAV: {framerate}Hz, {sampwidth*8}bit, {nchannels}ch, {actual_duration}s")

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

        return full_transcript.strip(), actual_duration

    except Exception as e:
        log.error(f"Whisper error: {e}")
        return None, 0

def _fix_transcript(transcript):
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

תקן את התמלול:
- החלף מילים שגויות במונחים הנכונים לפי ההקשר התורני
- כאשר מילה נשמעת דומה למונח תורני — העדף את המונח התורני
- שמור על כל המשמעות והתוכן המקורי
- אל תוסיף תוכן שלא היה בתמלול
- שמור על מבנה הפסקאות
- החזר רק את הטקסט המתוקן, ללא הסברים או כותרות

אם התמלול גרוע מאוד ואינך יכול לתקן — החזר את הטקסט המקורי כמות שהוא, ללא שום הערות.

תמלול לתיקון:
{transcript}"""

        msg = client.messages.create(
            model='claude-sonnet-4-5',
            max_tokens=4096,
            messages=[{'role': 'user', 'content': prompt}]
        )

        fixed = msg.content[0].text.strip()

        if len(fixed) < len(transcript) * 0.3:
            log.warning("Claude returned too short response, using original")
            return transcript

        log.info("Claude תיקון הושלם")
        return fixed

    except Exception as e:
        log.error(f"Claude error: {e}")
        return transcript

def _send_email(to, transcript_raw, transcript_fixed, customer, rec_url, duration_seconds):
    try:
        import sendgrid
        from sendgrid.helpers.mail import Mail
        name = customer.name if hasattr(customer, 'name') and customer.name else customer.phone if customer else ''

        minutes = duration_seconds // 60
        seconds = duration_seconds % 60
        duration_str = f"{minutes}:{seconds:02d}"

        html = f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
<h2 style="color:#1d4ed8">תמלול שיחה</h2>
<p style="color:#6b7280">לקוח: <b>{name}</b> | משך: <b>{duration_str}</b></p>

<div style="background:#f0fdf4;border-right:4px solid #10b981;padding:16px;margin:16px 0;border-radius:8px">
<h3 style="margin:0 0 12px;color:#065f46">✨ תמלול מעובד</h3>
<div style="line-height:1.8;white-space:pre-wrap">{transcript_fixed}</div>
</div>

<div style="background:#f9fafb;border-right:4px solid #9ca3af;padding:16px;margin:16px 0;border-radius:8px">
<h3 style="margin:0 0 12px;color:#6b7280">📝 תמלול מקורי (Whisper)</h3>
<div style="line-height:1.8;white-space:pre-wrap;color:#6b7280;font-size:13px">{transcript_raw}</div>
</div>

<div style="background:#fff7ed;border-right:4px solid #f97316;padding:16px;margin:16px 0;border-radius:8px">
<h3 style="margin:0 0 8px;color:#9a3412">🎧 האזנה להקלטה</h3>
<a href="{rec_url}" style="color:#ea580c;word-break:break-all">{rec_url}</a>
</div>

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

def _send_email_premium(to, transcript, customer, rec_url, duration_seconds):
    try:
        import sendgrid
        from sendgrid.helpers.mail import Mail
        name = customer.name if hasattr(customer, 'name') and customer.name else customer.phone if customer else ''

        minutes = duration_seconds // 60
        seconds = duration_seconds % 60
        duration_str = f"{minutes}:{seconds:02d}"

        html = f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
<h2 style="color:#7c3aed">תמלול שיחה — מסלול מקצועי</h2>
<p style="color:#6b7280">לקוח: <b>{name}</b> | משך: <b>{duration_str}</b></p>

<div style="background:#faf5ff;border-right:4px solid #7c3aed;padding:16px;margin:16px 0;border-radius:8px">
<h3 style="margin:0 0 12px;color:#581c87">⭐ תמלול מקצועי (Sofer.ai)</h3>
<div style="line-height:1.8;white-space:pre-wrap">{transcript}</div>
</div>

<div style="background:#fff7ed;border-right:4px solid #f97316;padding:16px;margin:16px 0;border-radius:8px">
<h3 style="margin:0 0 8px;color:#9a3412">🎧 האזנה להקלטה</h3>
<a href="{rec_url}" style="color:#ea580c;word-break:break-all">{rec_url}</a>
</div>

</div>'''

        sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))
        message = Mail(
            from_email=os.environ.get('SENDGRID_FROM_EMAIL', os.environ.get('GMAIL_USER', '')),
            to_emails=to,
            subject=f'תמלול שיחה מקצועי - {name}',
            html_content=html
        )
        sg.send(message)
        log.info(f"Premium email sent to {to}")
    except Exception as e:
        log.error(f"Email error: {e}")
