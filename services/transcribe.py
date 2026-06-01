import os, requests, logging, threading, tempfile, time
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

            # בדיקת tier של הלקוח
            customer = Customer.query.get(customer_id)
            tier = getattr(customer, 'transcription_tier', 'basic') or 'basic'

            if tier == 'premium':
                log.info(f"Using Sofer.ai for customer {customer_id}")
                transcript_raw = _soferai_from_url(rec_url)
                transcript_fixed = transcript_raw  # Sofer.ai כבר מתוקן
            else:
                log.info(f"Using Whisper for customer {customer_id}")
                transcript_raw = _whisper_from_url(rec_url)
                transcript_fixed = _fix_transcript(transcript_raw) if transcript_raw else None

            db.session.remove()
            rec = Recording.query.filter_by(call_id=call_id).first()

            if not transcript_raw:
                if rec:
                    rec.status = 'error'
                    db.session.commit()
                return

            price_per_30min = float(_get_setting('price_per_30min', '5.0'))
            if tier == 'premium':
                price_per_30min = float(_get_setting('price_per_30min_premium', str(float(_get_setting('price_per_30min', '5.0')) * 2)))
            cost = (duration_seconds / 1800) * price_per_30min
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
                    _send_email_premium(delivered_to, transcript_fixed, customer)
                else:
                    _send_email(delivered_to, transcript_raw, transcript_fixed, customer)
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
    """תמלול דרך Sofer.ai — מוריד את הקובץ ומעלה ל-API"""
    try:
        import io
        from soferai import SoferAI
        from soferai.transcribe import TranscriptionRequestInfo

        client = SoferAI(api_key=os.environ.get('SOFERAI_API_KEY'))

        # הורדת הקובץ מימות
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        log.info(f"Downloaded {len(r.content)} bytes for Sofer.ai")

        # העלאת הקובץ ל-Sofer.ai
        file_obj = io.BytesIO(r.content)
        file_obj.name = 'recording.wav'

        upload_response = client.files.upload(file=file_obj)
        file_id = upload_response.id
        log.info(f"Sofer.ai file uploaded: {file_id}")

        # יצירת בקשת תמלול
        batch_response = client.batch_transcribe.create_batch_transcription(
            batch_file_id=file_id,
            info=TranscriptionRequestInfo(
                model="v1",
                primary_language="he",
                hebrew_word_format=["he"],
                num_speakers=1,
            ),
            batch_title="shiur",
            processing_mode="standard",
        )
        batch_id = batch_response.id
        log.info(f"Sofer.ai batch created: {batch_id}")

        # המתנה לתוצאה — polling כל 15 שניות עד 10 דקות
        for attempt in range(40):
            time.sleep(15)
            status_response = client.batch_transcribe.get_batch_transcription(batch_id)
            status = status_response.status
            log.info(f"Sofer.ai status: {status} (attempt {attempt+1})")

            if status == 'completed':
                # שליפת הטקסט
                transcript_text = ''
                for segment in status_response.transcription.segments:
                    transcript_text += segment.text + ' '
                log.info("Sofer.ai transcription completed")
                return transcript_text.strip()

            elif status in ('failed', 'error'):
                log.error(f"Sofer.ai transcription failed: {status}")
                return None

        log.error("Sofer.ai timeout after 10 minutes")
        return None

    except Exception as e:
        log.error(f"Sofer.ai error: {e}")
        return None

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

def _send_email(to, transcript_raw, transcript_fixed, customer):
    try:
        import sendgrid
        from sendgrid.helpers.mail import Mail
        name = customer.name if hasattr(customer, 'name') and customer.name else customer.phone if customer else ''

        html = f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
<h2 style="color:#1d4ed8">תמלול שיחה</h2>
<p style="color:#6b7280">לקוח: <b>{name}</b></p>

<div style="background:#f0fdf4;border-right:4px solid #10b981;padding:16px;margin:16px 0;border-radius:8px">
<h3 style="margin:0 0 12px;color:#065f46">✨ תמלול מעובד</h3>
<div style="line-height:1.8;white-space:pre-wrap">{transcript_fixed}</div>
</div>

<div style="background:#f9fafb;border-right:4px solid #9ca3af;padding:16px;margin:16px 0;border-radius:8px">
<h3 style="margin:0 0 12px;color:#6b7280">📝 תמלול מקורי (Whisper)</h3>
<div style="line-height:1.8;white-space:pre-wrap;color:#6b7280;font-size:13px">{transcript_raw}</div>
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

def _send_email_premium(to, transcript, customer):
    try:
        import sendgrid
        from sendgrid.helpers.mail import Mail
        name = customer.name if hasattr(customer, 'name') and customer.name else customer.phone if customer else ''

        html = f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
<h2 style="color:#7c3aed">תמלול שיחה — מסלול מקצועי</h2>
<p style="color:#6b7280">לקוח: <b>{name}</b></p>

<div style="background:#faf5ff;border-right:4px solid #7c3aed;padding:16px;margin:16px 0;border-radius:8px">
<h3 style="margin:0 0 12px;color:#581c87">⭐ תמלול מקצועי (Sofer.ai)</h3>
<div style="line-height:1.8;white-space:pre-wrap">{transcript}</div>
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
