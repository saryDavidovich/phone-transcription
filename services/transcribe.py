import os, requests, logging, threading, time, math

log = logging.getLogger(__name__)


def transcribe_async(call_id, rec_url, customer_id, delivery_method, delivered_to, duration_seconds, transcription_tier='basic', language='he', output_language='he'):
    t = threading.Thread(
        target=_process,
        args=(call_id, rec_url, customer_id, delivery_method, delivered_to, duration_seconds, transcription_tier, language, output_language),
    )
    t.start()


def _process(call_id, rec_url, customer_id, delivery_method, delivered_to, duration_seconds, transcription_tier='basic', language='he', output_language='he'):
    from app import app, db
    from models import Recording, Customer, Transaction
    with app.app_context():
        try:
            rec = Recording.query.filter_by(call_id=call_id).first()
            if rec:
                rec.status = 'transcribing'
                rec.rec_url = rec_url
                db.session.commit()

            db.session.remove()

            tier = transcription_tier

            if tier == 'premium':
                log.info(f"Using AlefBot for customer {customer_id}")
                job_id, actual_duration = _alefbot_submit(rec_url, call_id)

                if job_id:
                    db.session.remove()
                    rec = Recording.query.filter_by(call_id=call_id).first()
                    if rec:
                        rec.alefbot_job_id = job_id
                        rec.status = 'alefbot_pending'
                        if actual_duration:
                            rec.duration_seconds = actual_duration
                        db.session.commit()
                    log.info(f"AlefBot job submitted: {job_id}, waiting for webhook")
                    return
                else:
                    db.session.remove()
                    rec = Recording.query.filter_by(call_id=call_id).first()
                    if rec:
                        rec.status = 'error'
                        db.session.commit()
                    return

            else:
                # gemini או כל ברירת מחדל
                log.info(f"Using Gemini for customer {customer_id}")
                transcript_raw, actual_duration = _gemini_from_url(rec_url, language, output_language)
                transcript_fixed = transcript_raw
                price_key = 'price_per_20min_basic'
                description_tier = 'רגיל'

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

            price_per_20min = float(_get_setting(price_key, '0.90'))
            units = math.ceil(duration_seconds / 1200)
            cost = round(units * price_per_20min, 2)

            if rec:
                rec.transcript = transcript_fixed
                rec.summary = ''
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
                    description=f'תמלול {duration_seconds//60} דקות ({description_tier})',
                    recording_id=rec.id if rec else None
                )
                db.session.add(txn)
                db.session.commit()

            if delivery_method == 'email':
                _send_email(delivered_to, transcript_fixed, customer, rec_url, duration_seconds)
            elif delivery_method == 'fax':
                _send_fax(delivered_to, transcript_fixed, customer, duration_seconds)

            if rec:
                rec.status = 'delivered'
                db.session.commit()

        except Exception as e:
            log.error(f"Error processing {call_id}: {e}")


def _alefbot_submit(rec_url, call_id):
    """שולח ל-AlefBot ומחזיר job_id מיד"""
    try:
        import wave, io

        api_key = os.environ.get('ALEFBOT_API_KEY')
        base_url = 'https://alef-bot.top/api/v1'
        webhook_url = os.environ.get('APP_BASE_URL', '').rstrip('/') + '/api/alefbot-webhook'

        r = requests.get(rec_url, timeout=300)
        r.raise_for_status()
        file_bytes = r.content
        log.info(f"Downloaded {len(file_bytes)} bytes for AlefBot")

        actual_duration = 0
        try:
            with wave.open(io.BytesIO(file_bytes)) as wav_in:
                actual_duration = wav_in.getnframes() // wav_in.getframerate()
            log.info(f"Duration: {actual_duration}s")
        except Exception as e:
            log.warning(f"Could not read WAV metadata: {e}")

        # שלב 1 — צור upload slot
        upload_res = requests.post(
            f'{base_url}/uploads',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={'filename': f'{call_id}.wav', 'content_type': 'audio/wav', 'size_bytes': len(file_bytes)},
            timeout=30
        )
        upload_res.raise_for_status()
        upload_id = upload_res.json().get('upload_id')
        log.info(f"AlefBot upload slot created: {upload_id}")

        # שלב 2 — העלה את הקובץ
        put_res = requests.put(
            f'{base_url}/uploads/{upload_id}/binary',
            headers={'Authorization': f'Bearer {api_key}'},
            data=file_bytes,
            timeout=300
        )
        put_res.raise_for_status()
        log.info(f"AlefBot file uploaded")

        # שלב 3 — צור תמלול עם webhook
        transcribe_res = requests.post(
            f'{base_url}/transcriptions',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={
                'upload_id': upload_id,
                'output_format': 'txt',
                'webhook_url': webhook_url,
                'client_reference': call_id,
            },
            timeout=30
        )
        transcribe_res.raise_for_status()
        job_id = transcribe_res.json().get('job_id')
        log.info(f"AlefBot job created: {job_id} for call {call_id}")
        return job_id, actual_duration

    except Exception as e:
        log.error(f"AlefBot submit error: {e}")
        return None, 0


def finalize_alefbot_recording(call_id, transcript_text):
    """נקרא מה-webhook כשאלף בוט מסיים"""
    from app import app, db
    from models import Recording, Customer, Transaction

    with app.app_context():
        try:
            db.session.remove()
            rec = Recording.query.filter_by(call_id=call_id).first()
            if not rec:
                log.error(f"AlefBot webhook: recording not found for call {call_id}")
                return

            duration_seconds = rec.duration_seconds or 0
            price_per_20min = float(_get_setting('price_per_20min_premium', '1.90'))
            units = math.ceil(duration_seconds / 1200) if duration_seconds > 0 else 1
            cost = round(units * price_per_20min, 2)

            rec.transcript = transcript_text
            rec.summary = ''
            rec.status = 'transcribed'
            rec.cost = cost
            rec.alefbot_job_id = None
            db.session.commit()

            customer = Customer.query.get(rec.customer_id)
            if customer:
                customer.balance -= cost
                txn = Transaction(
                    customer_id=rec.customer_id,
                    amount=-cost,
                    type='debit',
                    description=f'תמלול {duration_seconds//60} דקות (מקצועי)',
                    recording_id=rec.id
                )
                db.session.add(txn)
                db.session.commit()

            rec_url = rec.rec_url or ''
            if rec.delivery_method == 'email':
                _send_email(rec.delivered_to, transcript_text, customer, rec_url, duration_seconds)
            elif rec.delivery_method == 'fax':
                _send_fax(rec.delivered_to, transcript_text, customer, duration_seconds)

            rec.status = 'delivered'
            db.session.commit()
            log.info(f"AlefBot recording {call_id} finalized and delivered")

        except Exception as e:
            log.error(f"AlefBot finalize error for {call_id}: {e}")


def _gemini_from_url(url, language='he', output_language='he'):
    log.info(f"Gemini: language={language}, output_language={output_language}")
    try:
        import wave, audioop, io
        from google import genai
        from google.genai import types as gtypes

        api_key = os.environ.get('GOOGLE_API_KEY')
        client = genai.Client(api_key=api_key)

        r = requests.get(url, timeout=300)
        r.raise_for_status()
        log.info(f"Downloaded {len(r.content)} bytes for Gemini")

        actual_duration = 0
        audio_content = r.content

        try:
            with wave.open(io.BytesIO(r.content)) as wav_in:
                frames = wav_in.readframes(wav_in.getnframes())
                sampwidth = wav_in.getsampwidth()
                nchannels = wav_in.getnchannels()
                framerate = wav_in.getframerate()
                actual_duration = wav_in.getnframes() // framerate

            log.info(f"Duration: {actual_duration}s, framerate: {framerate}Hz")

            if framerate != 16000:
                frames, _ = audioop.ratecv(frames, sampwidth, nchannels, framerate, 16000, None)
                framerate = 16000
                log.info("Upsampled to 16000Hz for Gemini")

                output_buffer = io.BytesIO()
                with wave.open(output_buffer, 'wb') as wav_out:
                    wav_out.setnchannels(nchannels)
                    wav_out.setsampwidth(sampwidth)
                    wav_out.setframerate(16000)
                    wav_out.writeframes(frames)
                audio_content = output_buffer.getvalue()

        except Exception as e:
            log.warning(f"Could not process WAV: {e}, using original")

        input_lang_map = {'he': 'עברית', 'yi': 'יידיש', 'en': 'English'}
        input_lang_name = input_lang_map.get(language, 'עברית')

        if output_language == 'he':
            output_instruction = 'כתוב את התמלול בעברית בלבד. אל תשתמש באותיות לטיניות.'
        elif output_language == 'yi':
            output_instruction = 'שרייב די טראנסקריפציע אויף יידיש בלעבד.'
        else:
            output_instruction = 'Write the transcription in English only.'

        if language == 'yi' and output_language == 'yi':
            yiddish_instruction = """
הדובר מדבר יידיש אשכנזית חסידית. שים לב:
- ישנם ביטויים, פסוקים וציטוטים בעברית/ארמית בהגייה אשכנזית — השאר אותם כפי שנאמרו בעברית, אל תתרגם ליידיש.
- מילים עבריות כמו "תורה", "שבת", "גמרא", "רבי" — כתוב בעברית.
- רק המשפטים שנאמרו ביידיש — כתוב ביידיש."""
        elif language == 'yi' and output_language == 'he':
            yiddish_instruction = """
הדובר מדבר יידיש אשכנזית חסידית. תרגם הכל לעברית תקנית.
ביטויים ופסוקים בעברית/ארמית — כתוב בעברית כפי שהם.
אל תשאיר אף מילה ביידיש — תרגם הכל לעברית."""
        else:
            yiddish_instruction = ''

        prompt = f"""תמלל את קובץ השמע הזה במדויק.
שפת הדובר: {input_lang_name}.
{output_instruction}
{yiddish_instruction}
חשוב ביותר — תמלול מדויק ומלא:
- תמלל כל מילה ומילה ללא יוצא מן הכלל.
- אל תדלג על אף מילה, אפילו אם הקול לא ברור — כתוב את מה שנשמע גם אם אינך בטוח.
- אל תסכם, אל תקצר, אל תדלג על חלקים.
- שמור על מינוח תורני נכון, ארמית, ראשי תיבות וגרסאות.
- החזר רק את הטקסט המתומלל ללא הערות נוספות."""

        transcript = None
        for attempt in range(5):
            try:
                response = client.models.generate_content(
                    model='gemini-3.5-flash',
                    contents=[
                        prompt,
                        gtypes.Part.from_bytes(data=audio_content, mime_type='audio/wav'),
                    ],
                )
                transcript = response.text.strip()
                log.info(f"Gemini transcription completed, {len(transcript)} chars")
                break
            except Exception as ge:
                log.warning(f"Gemini attempt {attempt+1} failed: {ge}")
                if attempt < 4:
                    time.sleep(15)
                else:
                    raise

        if language == 'yi' and output_language == 'he':
            log.info("Translating Yiddish to Hebrew...")
            for attempt in range(5):
                try:
                    translate_response = client.models.generate_content(
                        model='gemini-3.5-flash',
                        contents=[f"""תרגם את הטקסט הבא מיידיש לעברית תקנית.
ביטויים ופסוקים בעברית/ארמית — השאר כפי שהם.
החזר רק את הטקסט המתורגם ללא הערות.

טקסט לתרגום:
{transcript}"""],
                    )
                    transcript = translate_response.text.strip()
                    log.info(f"Translation completed, {len(transcript)} chars")
                    break
                except Exception as te:
                    log.warning(f"Translation attempt {attempt+1} failed: {te}")
                    if attempt < 4:
                        time.sleep(10)
                    else:
                        log.error("Translation failed after 5 attempts, using original")

        return transcript, actual_duration

    except Exception as e:
        log.error(f"Gemini error: {e}")
        return None, 0


def _get_setting(key, default=''):
    from models import Settings
    s = Settings.query.filter_by(key=key).first()
    return s.value if s else default


def _build_word_doc(name, duration_str, transcript_fixed, transcript_raw=None):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
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
    h1 = doc.add_heading('תמלול', level=1)
    set_rtl(h1)
    p = doc.add_paragraph(transcript_fixed or '')
    set_rtl(p)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.read()


def _build_pdf_for_fax(name, duration_str, transcript_fixed):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        from reportlab.lib.enums import TA_RIGHT, TA_CENTER
        from reportlab.lib.units import cm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        import io

        font_paths = [
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
            '/usr/share/fonts/truetype/freefont/FreeSans.ttf',
            '/usr/share/fonts/TTF/DejaVuSans.ttf',
        ]
        font_registered = False
        for font_path in font_paths:
            if os.path.exists(font_path):
                pdfmetrics.registerFont(TTFont('Hebrew', font_path))
                font_registered = True
                break

        font_name = 'Hebrew' if font_registered else 'Helvetica'
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4,
                                rightMargin=2*cm, leftMargin=2*cm,
                                topMargin=2*cm, bottomMargin=2*cm)

        rtl_style = ParagraphStyle('RTL', fontName=font_name, alignment=TA_RIGHT, fontSize=11, leading=18)
        title_style = ParagraphStyle('Title', fontName=font_name, alignment=TA_RIGHT, fontSize=16, spaceAfter=12)
        footer_style = ParagraphStyle('Footer', fontName=font_name, alignment=TA_CENTER, fontSize=8, textColor='grey')

        story = []
        story.append(Paragraph('תמלול שיחה', title_style))
        story.append(Spacer(1, 0.3*cm))
        story.append(Paragraph(f'לקוח: {name} | משך: {duration_str}', rtl_style))
        story.append(Spacer(1, 0.5*cm))

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
        import uuid
        name = customer.name if hasattr(customer, 'name') and customer.name else customer.phone if customer else ''
        minutes = duration_seconds // 60
        seconds = duration_seconds % 60
        duration_str = f"{minutes}:{seconds:02d}"

        pdf_bytes = _build_pdf_for_fax(name, duration_str, transcript_fixed)
        if not pdf_bytes:
            log.error("Failed to build PDF for fax")
            return

        api_key = os.environ.get('TELNYX_API_KEY')
        connection_id = os.environ.get('TELNYX_CONNECTION_ID', '2973595690996860264')
        from_number = os.environ.get('TELNYX_FAX_FROM', '+13644443976')
        base_url = os.environ.get('APP_BASE_URL', '').rstrip('/')

        fax_number = to_number.strip().replace('-', '').replace(' ', '')
        if not fax_number.startswith('+'):
            if fax_number.startswith('0'):
                fax_number = '+972' + fax_number[1:]
            else:
                fax_number = '+972' + fax_number

        filename = f"fax_{uuid.uuid4().hex}.pdf"
        static_dir = os.path.join(os.path.dirname(__file__), '..', 'static', 'fax_tmp')
        os.makedirs(static_dir, exist_ok=True)
        pdf_path = os.path.join(static_dir, filename)

        with open(pdf_path, 'wb') as f:
            f.write(pdf_bytes)

        media_url = f"{base_url}/static/fax_tmp/{filename}"
        fax_response = requests.post(
            'https://api.telnyx.com/v2/faxes',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={'connection_id': connection_id, 'to': fax_number, 'from': from_number, 'media_url': media_url}
        )

        if fax_response.status_code in (200, 201, 202):
            fax_id = fax_response.json().get('data', {}).get('id')
            log.info(f"Fax sent to {fax_number}, fax_id: {fax_id}")
            threading.Thread(target=_check_fax_status, args=(fax_id, api_key), daemon=True).start()
        else:
            log.error(f"Fax send failed: {fax_response.text}")

        def cleanup():
            time.sleep(600)
            try:
                os.remove(pdf_path)
            except:
                pass
        threading.Thread(target=cleanup, daemon=True).start()

    except Exception as e:
        log.error(f"Fax error: {e}")


def _check_fax_status(fax_id, api_key):
    time.sleep(60)
    try:
        r = requests.get(f'https://api.telnyx.com/v2/faxes/{fax_id}',
                         headers={'Authorization': f'Bearer {api_key}'})
        data = r.json().get('data', {})
        log.info(f"Fax {fax_id} status: {data.get('status')} | reason: {data.get('failure_reason', '')}")
    except Exception as e:
        log.error(f"Fax status check error: {e}")


def _send_email(to, transcript, customer, rec_url, duration_seconds):
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
<h2 style="color:#1d4ed8">תמלול שיחה</h2>
<p style="color:#6b7280">לקוח: <b>{name}</b> | משך: <b>{duration_str}</b></p>
<div style="background:#f0fdf4;border-right:4px solid #10b981;padding:16px;margin:16px 0;border-radius:8px">
<h3 style="margin:0 0 12px;color:#065f46">✨ תמלול</h3>
<div style="line-height:1.8;white-space:pre-wrap;text-align:justify">{transcript}</div>
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
