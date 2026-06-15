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
                source_filename = rec.source_filename if rec else None
                _send_email(delivered_to, transcript_fixed, customer, rec_url, duration_seconds, source_filename=source_filename)
            elif delivery_method == 'fax':
                _send_fax(delivered_to, transcript_fixed, customer, duration_seconds, call_id)

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
                _send_email(rec.delivered_to, transcript_text, customer, rec_url, duration_seconds, source_filename=rec.source_filename)
            elif rec.delivery_method == 'fax':
                _send_fax(rec.delivered_to, transcript_text, customer, duration_seconds, call_id)

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


def _gemini_review_pass(url, language='he', output_language='he'):
    """
    גרסה נסיונית - 'תמלול מקצועי' חדש המבוסס על Gemini בלבד (במקום אלף בוט).

    שלב 1: תמלול ראשוני רגיל (כמו ב-_gemini_from_url).
    שלב 2: Gemini מאזין לקובץ האודיו שוב, **תוך כדי קריאת התמלול הראשוני**,
            ומתבקש לאתר ולתקן טעויות - מילים שלא הובנו נכון, שמות, מינוחים
            תורניים, פיסוק - מבלי לשנות את התוכן/המשמעות.

    נגיש רק דרך ממשק הניהול (בדיקת תמלול), לא דרך ה-IVR או המייל,
    כך שניתן להשוות תוצאות לפני החלפת אלף בוט.

    מחזיר: (transcript_final, actual_duration, transcript_raw_first_pass)
    """
    transcript_raw, actual_duration = _gemini_from_url(url, language, output_language)
    if not transcript_raw:
        return None, 0, None

    try:
        import wave, audioop, io
        from google import genai
        from google.genai import types as gtypes

        api_key = os.environ.get('GOOGLE_API_KEY')
        client = genai.Client(api_key=api_key)

        r = requests.get(url, timeout=300)
        r.raise_for_status()
        audio_content = r.content

        try:
            with wave.open(io.BytesIO(r.content)) as wav_in:
                frames = wav_in.readframes(wav_in.getnframes())
                sampwidth = wav_in.getsampwidth()
                nchannels = wav_in.getnchannels()
                framerate = wav_in.getframerate()

            if framerate != 16000:
                frames, _ = audioop.ratecv(frames, sampwidth, nchannels, framerate, 16000, None)
                output_buffer = io.BytesIO()
                with wave.open(output_buffer, 'wb') as wav_out:
                    wav_out.setnchannels(nchannels)
                    wav_out.setsampwidth(sampwidth)
                    wav_out.setframerate(16000)
                    wav_out.writeframes(frames)
                audio_content = output_buffer.getvalue()
        except Exception as e:
            log.warning(f"review pass: could not process WAV: {e}, using original")

        output_lang_map = {'he': 'עברית', 'yi': 'יידיש', 'en': 'English'}
        output_lang_name = output_lang_map.get(output_language, 'עברית')

        review_prompt = f"""לפניך הקלטת שמע ותמלול ראשוני שלה (בשפה: {output_lang_name}).
האזן להקלטה במלואה תוך כדי קריאת התמלול הראשוני, והפק גרסה מתוקנת ומדויקת יותר.

הנחיות:
- תקן מילים שתומללו בצורה שגויה (כתיב, שמות, מינוחים תורניים, ראשי תיבות, ביטויים בארמית/עברית).
- תקן פיסוק וחלוקה למשפטים/פסקאות לפי מה שנשמע בפועל.
- אם יש מילים/קטעים שבתמלול הראשוני סומנו כלא ברורים או הושמטו, האזן להם שוב ונסה למלא אותם.
- אל תשנה את המשמעות, אל תסכם, אל תקצר, אל תוסיף תוכן שלא נאמר.
- אם התמלול הראשוני נכון ומדויק כפי שהוא - החזר אותו כמו שהוא, ללא שינוי מיותר.
- החזר רק את התמלול המתוקן הסופי, ללא הערות, הסברים, או ציון השינויים שנעשו.

תמלול ראשוני:
{transcript_raw}"""

        transcript_final = transcript_raw
        for attempt in range(5):
            try:
                response = client.models.generate_content(
                    model='gemini-3.5-flash',
                    contents=[
                        review_prompt,
                        gtypes.Part.from_bytes(data=audio_content, mime_type='audio/wav'),
                    ],
                )
                reviewed = response.text.strip()
                if reviewed:
                    transcript_final = reviewed
                log.info(f"Gemini review pass completed, {len(transcript_final)} chars")
                break
            except Exception as ge:
                log.warning(f"Gemini review pass attempt {attempt+1} failed: {ge}")
                if attempt < 4:
                    time.sleep(15)
                else:
                    log.error("Gemini review pass failed after 5 attempts, using first-pass transcript")

        return transcript_final, actual_duration, transcript_raw

    except Exception as e:
        log.error(f"Gemini review pass error: {e}")
        return transcript_raw, actual_duration, transcript_raw


def _get_setting(key, default=''):
    from models import Settings
    s = Settings.query.filter_by(key=key).first()
    return s.value if s else default


def _build_word_doc(name, duration_str, transcript_fixed, transcript_raw=None, title='תמלול שיחה'):
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

    def add_bottom_border(paragraph):
        from docx.oxml.ns import qn
        pPr = paragraph._p.get_or_add_pPr()
        pBdr = OxmlElement('w:pBdr')
        bottom = OxmlElement('w:bottom')
        bottom.set(qn('w:val'), 'single')
        bottom.set(qn('w:sz'), '6')
        bottom.set(qn('w:space'), '4')
        bottom.set(qn('w:color'), '999999')
        pBdr.append(bottom)
        pPr.append(pBdr)
        
    def add_footer(doc):
        section = doc.sections[0]
        footer = section.footer
        footer_para = footer.paragraphs[0]
        footer_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        pPr = footer_para._p.get_or_add_pPr()
        bidi = OxmlElement('w:bidi')
        pPr.append(bidi)
        run = footer_para.add_run('הופק באמצעות מערכת תמלולפון 03-3131795')
        run.font.size = Pt(11)
        run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)

    doc = Document()
    section = doc.sections[0]
    sectPr = section._sectPr
    bidi_doc = OxmlElement('w:bidi')
    sectPr.append(bidi_doc)
    add_footer(doc)

    title_heading = doc.add_heading(title, 0)
    set_rtl(title_heading)
    p_info = doc.add_paragraph(f'לקוח: {name} | משך: {duration_str}')
    set_rtl(p_info)
    add_bottom_border(p_info)
    h1 = doc.add_heading('תמלול', level=1)
    set_rtl(h1)
    p = doc.add_paragraph(transcript_fixed or '')
    set_rtl(p)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.read()


def _build_pdf_for_fax(name, duration_str, transcript_fixed):
    """
    בונה PDF עם תמלול בעברית עבור שליחת פקס.
    משתמש באותו מסמך Word שנבנה למייל (_build_word_doc, RTL תקני דרך w:bidi)
    וממיר אותו ל-PDF באמצעות LibreOffice headless.
    גישה זו מטמיעה את הפונטים כ-PDF וקטורי תקני (TrueType subset עם cmap מלא),
    ונמנעת מבעיות ריבועים שחורים / טשטוש שקרו עם reportlab + רסטריזציה.
    """
    import subprocess
    import tempfile
    import uuid

    try:
        docx_bytes = _build_word_doc(name, duration_str, transcript_fixed)

        import shutil
        if not shutil.which('soffice'):
            log.error("PDF build error: soffice (LibreOffice) not found on PATH - check nixpacks.toml")
            return None

        with tempfile.TemporaryDirectory() as tmpdir:
            docx_path = os.path.join(tmpdir, f'{uuid.uuid4().hex}.docx')
            with open(docx_path, 'wb') as f:
                f.write(docx_bytes)

            result = subprocess.run(
                ['soffice', '--headless', '--convert-to', 'pdf', '--outdir', tmpdir, docx_path],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                log.error(f"LibreOffice convert error: {result.stderr}")
                return None

            pdf_path = os.path.splitext(docx_path)[0] + '.pdf'
            if not os.path.exists(pdf_path):
                log.error(f"LibreOffice did not produce PDF. stdout={result.stdout} stderr={result.stderr}")
                return None

            with open(pdf_path, 'rb') as f:
                return f.read()

    except Exception as e:
        log.error(f"PDF build error: {e}")
        return None


def _normalize_israeli_phone(raw):
    """מנקה ומנרמל מספר טלפון ישראלי לפורמט מקומי (05XXXXXXXX / 0XXXXXXXXX)."""
    phone = (raw or '').strip().replace('-', '').replace(' ', '')
    if phone.startswith('+972'):
        phone = '0' + phone[4:]
    elif phone.startswith('972'):
        phone = '0' + phone[3:]
    return phone


def _send_fax(to_number, transcript_fixed, customer, duration_seconds, call_id=None):
    """
    שולח את התמלול כפקס באמצעות ה-API של ימות המשיח (SendFax).
    מעלה את ה-PDF בעצמו (pdfFile=UPLOAD) ומבקש דוח מסירה ל-deliveryUrl,
    כדי שסטטוס השליחה יתעדכן ויוצג בממשק הניהול.
    """
    try:
        name = customer.name if hasattr(customer, 'name') and customer.name else customer.phone if customer else ''
        minutes = duration_seconds // 60
        seconds = duration_seconds % 60
        duration_str = f"{minutes}:{seconds:02d}"

        pdf_bytes = _build_pdf_for_fax(name, duration_str, transcript_fixed)
        if not pdf_bytes:
            log.error("Failed to build PDF for fax")
            return

        yemot_token = os.environ.get('YEMOT_TOKEN')
        if not yemot_token:
            log.error("YEMOT_TOKEN not configured - cannot send fax")
            return

        caller_id = os.environ.get('YEMOT_FAX_CALLER_ID', '')
        base_url = os.environ.get('APP_BASE_URL', '').rstrip('/')

        # מספר היעד הוא מה שהלקוח הזין במערכת הטלפונית (to_number)
        fax_number = _normalize_israeli_phone(to_number)

        files = {
            'fileToUpload': (f'transcript_{call_id or "fax"}.pdf', pdf_bytes, 'application/pdf'),
        }
        data = {
            'token': yemot_token,
            'pdfFile': 'UPLOAD',
            'phone': fax_number,
        }
        if caller_id:
            data['callerId'] = caller_id
        if base_url and call_id:
            data['deliveryUrl'] = f'{base_url}/api/fax-delivery-webhook'

        response = requests.post(
            'https://www.call2all.co.il/ym/api/SendFax',
            data=data,
            files=files,
            timeout=120,
        )
        result = response.json() if response.headers.get('content-type', '').startswith('application/json') else {}

        if result.get('responseStatus') == 'OK':
            campaign_id = result.get('CampaignId')
            log.info(f"Fax queued via Yemot to {fax_number}, CampaignId: {campaign_id}")
            if call_id and campaign_id:
                _save_fax_campaign(call_id, campaign_id)
        else:
            log.error(f"Yemot SendFax failed: {response.text}")
            if call_id:
                _update_fax_status(call_id, status='error', note=response.text[:500])

    except Exception as e:
        log.error(f"Fax error: {e}")
        if call_id:
            _update_fax_status(call_id, status='error', note=str(e)[:500])


def _save_fax_campaign(call_id, campaign_id):
    from app import app, db
    from models import Recording
    with app.app_context():
        try:
            db.session.remove()
            rec = Recording.query.filter_by(call_id=call_id).first()
            if rec:
                rec.fax_campaign_id = campaign_id
                rec.fax_status = 'sent'
                db.session.commit()
        except Exception as e:
            log.error(f"_save_fax_campaign error: {e}")


def _update_fax_status(call_id, status, note=''):
    from app import app, db
    from models import Recording
    with app.app_context():
        try:
            db.session.remove()
            rec = Recording.query.filter_by(call_id=call_id).first()
            if rec:
                rec.fax_status = status
                if note:
                    rec.fax_status_note = note
                db.session.commit()
        except Exception as e:
            log.error(f"_update_fax_status error: {e}")


def handle_fax_delivery_webhook(data):
    """
    מטפל ב-callback של deliveryUrl מימות המשיח עבור SendFax.
    מעדכן את סטטוס הפקס של ההקלטה המתאימה (לפי CampaignId) כדי שיוצג בממשק הניהול.

    שדות אפשריים מימות:
    - CampaignId
    - Delivery: Answer / NoAnswer / End
    - DIALSTATUS (אם Delivery=NoAnswer)
    - status (אם Delivery=End) - SUCCESS במקרה של מסירה מוצלחת
    """
    from app import app, db
    from models import Recording

    campaign_id = data.get('CampaignId', '')
    delivery = data.get('Delivery', '')
    end_status = data.get('status', '')
    dial_status = data.get('DIALSTATUS', '')

    if not campaign_id:
        return

    with app.app_context():
        try:
            db.session.remove()
            rec = Recording.query.filter_by(fax_campaign_id=campaign_id).first()
            if not rec:
                log.warning(f"Fax delivery webhook: no recording for CampaignId {campaign_id}")
                return

            if delivery == 'Answer':
                rec.fax_status = 'sending'
            elif delivery == 'NoAnswer':
                rec.fax_status = 'no_answer'
                rec.fax_status_note = dial_status
            elif delivery == 'End':
                if end_status == 'SUCCESS':
                    rec.fax_status = 'delivered'
                else:
                    rec.fax_status = 'failed'
                    rec.fax_status_note = end_status

            db.session.commit()
            log.info(f"Fax status updated for CampaignId {campaign_id}: {rec.fax_status}")

        except Exception as e:
            log.error(f"handle_fax_delivery_webhook error: {e}")


def _send_email(to, transcript, customer, rec_url, duration_seconds, source_filename=None):
    try:
        import sendgrid, base64
        from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition

        name = customer.name if hasattr(customer, 'name') and customer.name else customer.phone if customer else ''
        minutes = duration_seconds // 60
        seconds = duration_seconds % 60
        duration_str = f"{minutes}:{seconds:02d}"

        # אם ההקלטה התקבלה במייל - הכותרת היא שם הקובץ שנשלח, ולא נכלל קישור להורדת ההקלטה
        title = source_filename if source_filename else 'תמלול שיחה'

        word_bytes = _build_word_doc(name, duration_str, transcript, title=title)
        word_b64 = base64.b64encode(word_bytes).decode('utf-8')

        download_block = ''
        if not source_filename:
            download_block = f'''<div style="background:#fff7ed;border-right:4px solid #f97316;padding:16px;margin:16px 0;border-radius:8px">
<a href="{rec_url}" style="color:#ea580c;font-weight:600;font-size:15px;text-decoration:none">⬇️ להורדת ההקלטה לחצו כאן</a>
</div>'''

        html = f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:600px;margin:auto">
<h2 style="color:#1d4ed8">{title}</h2>
<p style="color:#6b7280">לקוח: <b>{name}</b> | משך: <b>{duration_str}</b></p>
<div style="background:#f0fdf4;border-right:4px solid #10b981;padding:16px;margin:16px 0;border-radius:8px">
<h3 style="margin:0 0 12px;color:#065f46">✨ תמלול</h3>
<div style="line-height:1.8;white-space:pre-wrap;text-align:justify">{transcript}</div>
</div>
{download_block}
</div>'''

        sg = sendgrid.SendGridAPIClient(api_key=os.environ.get('SENDGRID_API_KEY'))
        message = Mail(
            from_email=os.environ.get('SENDGRID_FROM_EMAIL', os.environ.get('GMAIL_USER', '')),
            to_emails=to,
            subject=f'תמלול שיחה - {title}' if source_filename else f'תמלול שיחה - {name}',
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
