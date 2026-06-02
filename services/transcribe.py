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
            elif delivery_method == 'fax':
                _send_fax(delivered_to, transcript_fixed, customer, duration_seconds)
            else:
                log.info(f"Unknown delivery method: {delivery_method}")

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

def _build_word_doc(name, duration_str, transcript_fixed, transcript_raw=None):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    from docx.shared import Pt, RGBColor
    import io

    def set_rtl(paragraph):
        paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        pPr = paragraph._p.get_or_add_pPr()
        bidi = OxmlElement('w:bidi')
        pPr.append(bidi)

    def add_footer(doc):
        section = doc.sections[0]
        footer = section.footer
        footer_para = footer.paragraphs[0]
        footer_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        pPr = footer_para._p.get_or_add_pPr()
        bidi = OxmlElement('w:bidi')
        pPr.append(bidi)
        run = footer_para.add_run('נערך ע"י מערכת תמלולפון 03-3131795')
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(0x99, 0x99, 0x99)

    doc = Document()

    section = doc.sections[0]
    sectPr = section._sectPr
    bidi_doc = OxmlElement('w:bidi')
    sectPr.append(bidi_doc)

    add_footer(doc)

    title = doc.add_heading('תמלול שיחה', 0)
    set_rtl(title)

    p_info = doc.add_paragraph(f'לקוח: {name} | משך: {duration_str}')
    set_rtl(p_info)

    set_rtl(doc.add_paragraph('─' * 50))

    h1 = doc.add_heading('תמלול מעובד', level=1)
    set_rtl(h1)

    p = doc.add_paragraph(transcript_fixed or '')
    set_rtl(p)

    if transcript_raw:
        set_rtl(doc.add_paragraph('─' * 50))
        h2 = doc.add_heading('תמלול מקורי', level=1)
        set_rtl(h2)
        p2 = doc.add_paragraph(transcript_raw)
        set_rtl(p2)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.read()

def _build_pdf_for_fax(name, duration_str, transcript_fixed):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        from reportlab.lib.enums import TA_RIGHT, TA_CENTER
        from reportlab.lib.units import cm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        import io

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4,
                                rightMargin=2*cm, leftMargin=2*cm,
                                topMargin=2*cm, bottomMargin=2*cm)

        styles = getSampleStyleSheet()
        rtl_style = ParagraphStyle(
            'RTL',
            parent=styles['Normal'],
            alignment=TA_RIGHT,
            fontSize=11,
            leading=18,
            wordWrap='RTL',
        )
        title_style = ParagraphStyle(
            'Title',
            parent=styles['Heading1'],
            alignment=TA_RIGHT,
            fontSize=16,
        )
        footer_style = ParagraphStyle(
            'Footer',
            parent=styles['Normal'],
            alignment=TA_CENTER,
            fontSize=8,
            textColor='grey',
        )

        story = []
        story.append(Paragraph('תמלול שיחה', title_style))
        story.append(Spacer(1, 0.3*cm))
        story.append(Paragraph(f'לקוח: {name} | משך: {duration_str}', rtl_style))
        story.append(Spacer(1, 0.5*cm))
        story.append(Paragraph('─' * 60, rtl_style))
        story.append(Spacer(1, 0.3*cm))

        # פיצול לפסקאות
        for para in (transcript_fixed or '').split('\n'):
            if para.strip():
                story.append(Paragraph(para.strip(), rtl_style))
                story.append(Spacer(1, 0.2*cm))

        story.append(Spacer(1, 1*cm))
        story.append(Paragraph('נערך ע"י מערכת תמלולפון 03-3131795', footer_style))

        doc.build(story)
        buf.seek(0)
        return buf.read()

    except Exception as e:
        log.error(f"PDF build error: {e}")
        return None

def _send_fax(to_number, transcript_fixed, customer, duration_seconds):
    try:
        import tempfile, base64

        name = customer.name if hasattr(customer, 'name') and customer.name else customer.phone if customer else ''
        minutes = duration_seconds // 60
        seconds = duration_seconds % 60
        duration_str = f"{minutes}:{seconds:02d}"

        # בניית PDF
        pdf_bytes = _build_pdf_for_fax(name, duration_str, transcript_fixed)
        if not pdf_bytes:
            log.error("Failed to build PDF for fax")
            return

        # שמירה זמנית והעלאה ל-Telnyx
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
            f.write(pdf_bytes)
            tmp_path = f.name

        # העלאת ה-PDF ל-Telnyx Storage
        api_key = os.environ.get('TELNYX_API_KEY')
        connection_id = os.environ.get('TELNYX_CONNECTION_ID', '2973595690996860264')
        from_number = os.environ.get('TELNYX_FAX_FROM', '+13644443976')

        # נירמול מספר הפקס לפורמט E.164
        fax_number = to_number.strip().replace('-', '').replace(' ', '')
        if not fax_number.startswith('+'):
            if fax_number.startswith('0'):
                fax_number = '+972' + fax_number[1:]
            else:
                fax_number = '+972' + fax_number

        # העלאת הקובץ ל-Telnyx
        with open(tmp_path, 'rb') as f:
            upload_response = requests.post(
                'https://api.telnyx.com/v2/files',
                headers={'Authorization': f'Bearer {api_key}'},
                files={'file': ('transcript.pdf', f, 'application/pdf')},
                data={'purpose': 'fax'}
            )

        os.remove(tmp_path)

        if upload_response.status_code not in (200, 201):
            log.error(f"Telnyx file upload failed: {upload_response.text}")
            return

        file_url = upload_response.json().get('data', {}).get('url')
        if not file_url:
            log.error("No file URL returned from Telnyx")
            return

        log.info(f"PDF uploaded to Telnyx: {file_url}")

        # שליחת הפקס
        fax_response = requests.post(
            'https://api.telnyx.com/v2/faxes',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json'
            },
            json={
                'connection_id': connection_id,
                'to': fax_number,
                'from': from_number,
                'media_url': file_url,
            }
        )

        if fax_response.status_code in (200, 201, 202):
            fax_id = fax_response.json().get('data', {}).get('id')
            log.info(f"Fax sent successfully to {fax_number}, fax_id: {fax_id}")
        else:
            log.error(f"Fax send failed: {fax_response.text}")

    except Exception as e:
        log.error(f"Fax error: {e}")

def _send_email(to, transcript_raw, transcript_fixed, customer, rec_url, duration_seconds):
    try:
        import sendgrid, base64
        from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition

        name = customer.name if hasattr(customer, 'name') and customer.name else customer.phone if customer else ''
        minutes = duration_seconds // 60
        seconds = duration_seconds % 60
        duration_str = f"{minutes}:{seconds:02d}"

        word_bytes = _build_word_doc(name, duration_str, transcript_fixed, transcript_raw)
        word_b64 = base64.b64encode(word_bytes).decode('utf-8')

        html = f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
<h2 style="color:#1d4ed8">תמלול שיחה</h2>
<p style="color:#6b7280">לקוח: <b>{name}</b> | משך: <b>{duration_str}</b></p>

<div style="background:#f0fdf4;border-right:4px solid #10b981;padding:16px;margin:16px 0;border-radius:8px">
<h3 style="margin:0 0 12px;color:#065f46">✨ תמלול מעובד</h3>
<div style="line-height:1.8;white-space:pre-wrap">{transcript_fixed}</div>
</div>

<div style="background:#f9fafb;border-right:4px solid #9ca3af;padding:16px;margin:16px 0;border-radius:8px">
<h3 style="margin:0 0 12px;color:#6b7280">📝 תמלול מקורי</h3>
<div style="line-height:1.8;white-space:pre-wrap;color:#6b7280;font-size:13px">{transcript_raw}</div>
</div>

<div style="background:#fff7ed;border-right:4px solid #f97316;padding:16px;margin:16px 0;border-radius:8px">
<a href="{rec_url}" style="color:#ea580c;font-weight:600;font-size:15px;text-decoration:none">⬇️ להורדת ההקלטה לחצו כאן</a>
</div>

</div>'''

        sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))
        message = Mail(
            from_email=os.environ.get('SENDGRID_FROM_EMAIL', os.environ.get('GMAIL_USER', '')),
            to_emails=to,
            subject=f'תמלול שיחה - {name}',
            html_content=html
        )
        message.attachment = Attachment(
            FileContent(word_b64),
            FileName(f'תמלול_{name}.docx'),
            FileType('application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
            Disposition('attachment')
        )
        sg.send(message)
        log.info(f"Email sent to {to}")
    except Exception as e:
        log.error(f"Email error: {e}")

def _send_email_premium(to, transcript, customer, rec_url, duration_seconds):
    try:
        import sendgrid, base64
        from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition

        name = customer.name if hasattr(customer, 'name') and customer.name else customer.phone if customer else ''
        minutes = duration_seconds // 60
        seconds = duration_seconds % 60
        duration_str = f"{minutes}:{seconds:02d}"

        word_bytes = _build_word_doc(name, duration_str, transcript)
        word_b64 = base64.b64encode(word_bytes).decode('utf-8')

        html = f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
<h2 style="color:#7c3aed">תמלול שיחה — מסלול מקצועי</h2>
<p style="color:#6b7280">לקוח: <b>{name}</b> | משך: <b>{duration_str}</b></p>

<div style="background:#faf5ff;border-right:4px solid #7c3aed;padding:16px;margin:16px 0;border-radius:8px">
<h3 style="margin:0 0 12px;color:#581c87">⭐ תמלול מקצועי</h3>
<div style="line-height:1.8;white-space:pre-wrap">{transcript}</div>
</div>

<div style="background:#fff7ed;border-right:4px solid #f97316;padding:16px;margin:16px 0;border-radius:8px">
<a href="{rec_url}" style="color:#ea580c;font-weight:600;font-size:15px;text-decoration:none">⬇️ להורדת ההקלטה לחצו כאן</a>
</div>

</div>'''

        sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))
        message = Mail(
            from_email=os.environ.get('SENDGRID_FROM_EMAIL', os.environ.get('GMAIL_USER', '')),
            to_emails=to,
            subject=f'תמלול שיחה מקצועי - {name}',
            html_content=html
        )
        message.attachment = Attachment(
            FileContent(word_b64),
            FileName(f'תמלול_{name}.docx'),
            FileType('application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
            Disposition('attachment')
        )
        sg.send(message)
        log.info(f"Premium email sent to {to}")
    except Exception as e:
        log.error(f"Email error: {e}")
