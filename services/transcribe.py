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

            transcript = _whisper_from_url(rec_url)

            if not transcript:
                if rec: rec.status = 'error'; db.session.commit()
                return

            summary = _summarize(transcript)

            # Calculate cost
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

            # Deduct from wallet
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

            # Deliver
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
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        content = r.content
        log.info(f"Downloaded {len(content)} bytes from {url}")

        # ימות שולח WAV בפורמט PCM 8000Hz 16bit מונו
        # Whisper צריך לפחות 16000Hz — נמיר את הקובץ
        import wave, audioop, io

        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            tmp_path = f.name

        # קריאת ה-WAV המקורי
        with wave.open(io.BytesIO(content)) as wav_in:
            frames = wav_in.readframes(wav_in.getnframes())
            sampwidth = wav_in.getsampwidth()
            nchannels = wav_in.getnchannels()
            framerate = wav_in.getframerate()

        log.info(f"WAV: {framerate}Hz, {sampwidth*8}bit, {nchannels}ch")

        # המרה מ-8000Hz ל-16000Hz
        if framerate != 16000:
            frames, _ = audioop.ratecv(frames, sampwidth, nchannels, framerate, 16000, None)
            framerate = 16000

        # שמירת WAV חדש
        with wave.open(tmp_path, 'wb') as wav_out:
            wav_out.setnchannels(nchannels)
            wav_out.setsampwidth(sampwidth)
            wav_out.setframerate(framerate)
            wav_out.writeframes(frames)

        client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY'))
        with open(tmp_path, 'rb') as f:
            result = client.audio.transcriptions.create(
                model='whisper-1', file=f, language='he', response_format='text'
            )
        os.remove(tmp_path)
        log.info("Whisper הצליח!")
        return result

    except Exception as e:
        log.error(f"Whisper error: {e}")
        return None

def _summarize(transcript):
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))
        msg = client.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=300,
            messages=[{'role':'user','content':f'סכם ב-3-4 נקודות בעברית:\n\n{transcript}'}]
        )
        return msg.content[0].text
    except:
        return ''

def _send_email(to, transcript, summary, customer):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    try:
        gmail_user = os.environ.get('GMAIL_USER','')
        gmail_pass = os.environ.get('GMAIL_APP_PASSWORD','')
        name = customer.name or customer.phone if customer else ''
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
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f'תמלול שיחה - {name}'
        msg['From'] = gmail_user
        msg['To'] = to
        msg.attach(MIMEText(html, 'html', 'utf-8'))
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(gmail_user, gmail_pass)
            s.sendmail(gmail_user, to, msg.as_string())
        log.info(f"Email sent to {to}")
    except Exception as e:
        log.error(f"Email error: {e}")
