"""
תמלול רגיל - גרסה 2 (נובמבר 2026).

מה היה לא טוב בגרסה הקודמת (נבדק על תמלול אמיתי של שעתיים):
* המודל "חשב בקול" באנגלית בתוך התמלול ("The user wants a precise, word-for-word Hebrew transcription...",
  "Let's do a refined listen"), עבר לרשימת חותמות זמן ("- 0:28: ...") ואף תמלל את אותו קטע פעמיים.
* בחלק מהקטעים - כל משפט בשורה נפרדת (מאות ירידות שורה).

מה עושים עכשיו (לפי ההמלצות של Google לתמלול עם Gemini ולפי מה שמקובל בכלי תמלול):
1. ההוראות עוברות כ-system instruction באנגלית (המודל מציית להן טוב יותר מעברית), והתוצאה עצמה בעברית.
2. פלט מובנה (JSON עם שדה transcript יחיד) - אין מקום ל"הסברים", כותרות או ניתוח.
3. הקשר בין חלקים: סוף החלק הקודם נשלח כ"רקע בלבד" - ממשיכים בדיוק מאיפה שנעצר, בלי לחזור.
4. בדיקת איכות לכל חלק: אנגלית בתמלול עברי, שורות "מטא", חותמות זמן, אורך חריג (כפילות) -
   ניסיון חוזר עם תזכורת, ואם עדיין - ניקוי.
5. סידור פסקאות: שורות קצרות רבות מאוחדות לפסקאות של כמה משפטים.

אפשרות נוספת (הגדרה במנהל: transcribe_engine=transcribe): המודל הייעודי של Google לתמלול
(gemini-3.5-transcribe) - מודל דיבור-לטקסט, לא מודל שיחה, ולכן לא "מדבר" בתוך התמלול. עם מילון
מונחים תורניים. אם הוא נכשל - חוזרים אוטומטית לגרסה 2.
"""
import json
import logging
import re

log = logging.getLogger(__name__)

LANG_NAMES = {'he': 'Hebrew', 'yi': 'Yiddish', 'en': 'English'}

# מונחים שחוזרים בהקלטות של הלקוחות - עוזרים למודל לכתוב אותם נכון
TORAH_VOCAB = [
    'גמרא', 'משנה', 'תוספות', 'רש"י', 'רמב"ם', 'שולחן ערוך', 'משנה ברורה', 'פוסקים', 'סוגיא', 'הלכה',
    'קושיא', 'תירוץ', 'מחלוקת', 'ברייתא', 'אמורא', 'תנא', 'קל וחומר', 'גזירה שווה', 'מסכת', 'דף', 'עמוד',
    'פרשת', 'חומש', 'תהילים', 'נביאים', 'כתובים', 'זוהר', 'קבלה', 'חסידות', 'תניא', 'ליקוטי מוהר"ן',
    'אדמו"ר', 'הרה"ק', 'זצ"ל', 'שליט"א', 'זי"ע', 'הקב"ה', 'השם יתברך', 'בעזרת השם', 'בס"ד', 'אי"ה',
    'ישיבה', 'כולל', 'שיעור', 'שבת', 'יום טוב', 'ראש חודש', 'מוצאי שבת', 'קידוש', 'הבדלה', 'מזל טוב',
    'ברוך השם', 'אמן', 'צדקה', 'מצווה', 'יראת שמים', 'בעל תשובה', 'רבנית', 'חברותא', 'סברא',
]


def system_instruction(language, output_language, extra_rules=''):
    src = LANG_NAMES.get(language, 'Hebrew')
    if output_language == 'original':
        output_language = language
    if output_language == 'he':
        out_rule = ('Write the transcript in HEBREW (Hebrew script) only. Never switch to English or any other '
                    'language, and never use Latin letters, except a foreign word or name that was actually '
                    'spoken in a foreign language.')
        if language == 'yi':
            out_rule += (' The speaker speaks Yiddish: translate everything into natural Modern Hebrew, keeping '
                         'Hebrew/Aramaic quotes, verses and Torah terms exactly as they are.')
        elif language == 'en':
            out_rule += ' The speaker speaks English: translate it faithfully into natural Hebrew.'
    elif output_language == 'yi':
        out_rule = ('Write the transcript in YIDDISH (Hebrew script, standard YIVO-like spelling). Keep '
                    'Hebrew/Aramaic phrases, verses and Torah terms in their standard Hebrew spelling.')
    else:
        out_rule = 'Write the transcript in ENGLISH only.'
    return f"""You are a professional verbatim transcriber of Hebrew-speaking (often Haredi / Orthodox Jewish) audio:
phone calls, lectures (shiurim), classes and meetings. The spoken language is {src}.

OUTPUT LANGUAGE: {out_rule}

TRANSCRIPTION RULES
- Transcribe every word that is spoken, from the first second to the last. Do not skip, summarize, shorten or paraphrase.
- Do not correct, "improve" or reorder the speaker's words. Keep repetitions and false starts as spoken.
- Keep filler sounds only when they carry meaning; drop pure "אממ"/"אה" noises.
- Names of people, rabbis, books, places and institutions: write exactly as heard, even if unfamiliar. Never replace a name with a better-known one.
- Torah, Talmudic and Aramaic terms and acronyms (ראשי תיבות): use the standard Hebrew spelling (e.g. רש"י, הקב"ה, זצ"ל).
- Ashkenazi / Hasidic pronunciation ("בוריך אתוה", "תוירה", "שבס") must be written in standard Hebrew spelling ("ברוך אתה", "תורה", "שבת").
- If a word is truly unintelligible, write your best guess; do not add notes like [לא ברור].
- Silence, music or background noise: write nothing for it.

FORMATTING
- Plain continuous prose with normal punctuation (. , ? ! :).
- Group the text into paragraphs of about 3-6 sentences (roughly 300-800 characters). Start a new paragraph only on a change of topic or speaker. NEVER put each sentence or phrase on its own line.
- Separate paragraphs with a single blank line.
- No timestamps, no speaker labels, no headings, no bullet points, no quotation marks around the whole text.
{extra_rules}
STRICT OUTPUT CONTRACT
- Return ONLY JSON of the form {{"transcript": "..."}}.
- The "transcript" value contains only the words spoken in this audio segment - no introduction, no explanation of what you are doing, no analysis, no notes, no second "refined" version, no self-check. Transcribe the audio exactly once."""


def user_prompt(prev_tail, chunk_num, n_chunks, context=''):
    p = f'Transcribe this audio segment ({chunk_num} of {n_chunks}) according to the instructions.'
    if prev_tail:
        p += ('\n\nFor context only - the previous segment ended with the following words. Do NOT repeat them; '
              f'continue from where the speech continues in this segment:\n«{prev_tail}»')
    if context:
        p += '\n\n' + context
    return p


_META_RE = re.compile(
    r'(?im)^\s*[-*•]?\s*(the user\b|user wants|key guidelines|guidelines|audio (analysis|detail|check)|'
    r"let'?s\b|transcription:|here is|here's|note:|i will|i'll\b|refined listen|double-check|word[- ]for[- ]word|"
    r'output:|\(?background noise)')
_TS_LINE_RE = re.compile(r'^\s*[-*•]?\s*\(?\d{1,2}:\d{2}(?::\d{2})?\)?\s*[:\-–]\s*')
_HEB = re.compile(r'[֐-׿]')
_LAT = re.compile(r'[A-Za-z]')


def latin_ratio(text):
    h, l = len(_HEB.findall(text or '')), len(_LAT.findall(text or ''))
    return l / max(1, h + l)


def problems(text, language, output_language, seconds):
    """רשימת בעיות בתמלול של חלק (ריקה = תקין)."""
    out = []
    if not text or not text.strip():
        return ['empty']
    tgt = language if output_language == 'original' else output_language
    if tgt in ('he', 'yi') and latin_ratio(text) > 0.08:
        out.append('latin')
    if len(_META_RE.findall(text)) >= 1:
        out.append('meta')
    if sum(1 for l in text.split('\n') if _TS_LINE_RE.match(l)) >= 3:
        out.append('timestamps')
    if seconds and len(text) > max(2500, seconds * 38):
        out.append('too_long')
    lines = [l for l in text.split('\n') if l.strip()]
    if len(lines) >= 12 and sorted(len(l) for l in lines)[len(lines) // 2] < 60:
        out.append('short_lines')
    return out


def sanitize(text, language, output_language):
    """ניקוי אחרון כשגם הניסיון החוזר לא יצא נקי: מוריד שורות מטא/אנגלית, חותמות זמן ומרכאות של רשימה,
    וחלק שתומלל פעמיים."""
    tgt = language if output_language == 'original' else output_language
    keep = []
    for line in (text or '').split('\n'):
        s = line.strip()
        if not s:
            keep.append('')
            continue
        if _META_RE.match(s):
            continue
        if tgt in ('he', 'yi') and latin_ratio(s) > 0.5:
            continue
        s = _TS_LINE_RE.sub('', s)
        if len(s) > 2 and s[0] in '"“”' and s[-1] in '"“”':
            s = s[1:-1].strip()
        keep.append(s)
    t = re.sub(r'\n{3,}', '\n\n', '\n'.join(keep)).strip()
    # אותו קטע פעמיים (המודל "בודק את עצמו"): אם 120 התווים הראשונים של פסקה כבר הופיעו - מדלגים עליה
    paras, seen, out = t.split('\n\n'), set(), []
    for p in paras:
        key = re.sub(r'\W+', '', p)[:120]
        if key and len(key) >= 60 and key in seen:
            continue
        seen.add(key)
        out.append(p)
    return '\n\n'.join(out)


_END = re.compile(r'[.?!:׃…"”)]\s*$')


def reflow(text, target=550):
    """מאחד שורות קצרות לפסקאות: שורה קצרה (פחות מ-90 תווים) מצטרפת לפסקה עד שהיא מגיעה לגודל סביר
    ונגמרת בסוף משפט. פסקאות ארוכות שכבר תקינות - לא נוגעים."""
    blocks = [b for b in re.split(r'\n\s*\n', (text or '').strip()) if b.strip()]
    lines = []
    for b in blocks:
        lines.extend([l.strip() for l in b.split('\n') if l.strip()])
        lines.append('')
    paras, cur = [], ''
    for l in lines:
        if not l:
            if cur and len(cur) >= target * 0.6:
                paras.append(cur)
                cur = ''
            continue
        if len(l) >= 300 and not cur:
            paras.append(l)
            continue
        cur = (cur + ' ' + l).strip() if cur else l
        if len(cur) >= target and _END.search(cur):
            paras.append(cur)
            cur = ''
    if cur:
        paras.append(cur)
    return '\n\n'.join(paras)


def parse_json_transcript(raw):
    raw = (raw or '').strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw)
    try:
        val = json.loads(raw)
        if isinstance(val, dict):
            return str(val.get('transcript') or '')
        if isinstance(val, str):
            return val
    except ValueError:
        pass
    m = re.search(r'"transcript"\s*:\s*"(.*)', raw, re.S)     # JSON שנקטע באמצע
    if m:
        body = m.group(1)
        body = re.sub(r'"\s*}\s*$', '', body)
        try:
            return json.loads('"' + body.replace('\n', '\\n') + '"')
        except ValueError:
            return body.replace('\\n', '\n').replace('\\"', '"')
    return raw


SLOW_COPY_NOTE = (
    '\n\nTWO AUDIO PARTS ARE ATTACHED: the FIRST is this segment at normal speed; the SECOND is the very same '
    'segment slowed down, given ONLY to help you hear unclear or fast words. Transcribe the content ONE time '
    '(never twice), following the first part from start to end.')


def transcribe_chunk(client, gtypes, chunk_bytes, mime_type, language, output_language, prev_tail,
                     chunk_num, n_chunks, seconds, model='gemini-3.5-flash', extra_rules='', context='',
                     extra_audio=None):
    """תמלול של חלק אחד: עד 3 ניסיונות כשהתוצאה לא תקינה (ועוד ניסיונות על שגיאות רשת - אצל הקורא).
    מחזיר טקסט נקי (או None אם לא התקבל כלום)."""
    sysi = system_instruction(language, output_language, extra_rules)
    best, best_score = None, None
    for attempt in range(3):
        prompt = user_prompt(prev_tail, chunk_num, n_chunks, context)
        if extra_audio:
            prompt += SLOW_COPY_NOTE
        if attempt:
            prompt += ('\n\nIMPORTANT: your previous answer broke the rules (' + ', '.join(best_issues) +
                       '). Output ONLY the spoken words of this segment, once, in the required language, '
                       'as paragraphs - nothing else.')
        resp = client.models.generate_content(
            model=model,
            contents=[prompt, gtypes.Part.from_bytes(data=chunk_bytes, mime_type=mime_type)] + (
                [gtypes.Part.from_bytes(data=extra_audio, mime_type='audio/wav')] if extra_audio else []),
            config=gtypes.GenerateContentConfig(
                system_instruction=sysi,
                response_mime_type='application/json',
                response_schema={'type': 'OBJECT', 'properties': {'transcript': {'type': 'STRING'}},
                                 'required': ['transcript']},
                # מודלי Pro לא עובדים בלי "חשיבה" - שם חשיבה נמוכה; Flash - בלי חשיבה בכלל
                thinking_config=(gtypes.ThinkingConfig(thinking_level='low') if '-pro' in model
                                 else gtypes.ThinkingConfig(thinking_budget=0)),
            ),
        )
        text = parse_json_transcript(resp.text or '')
        issues = problems(text, language, output_language, seconds)
        score = len(issues)
        if best is None or score < best_score:
            best, best_score, best_issues = text, score, issues
        if not issues:
            break
        log.warning(f'transcribe_v2: chunk {chunk_num} attempt {attempt + 1} issues={issues}')
    if best is None:
        return None
    text = best
    if best_issues and best_issues != ['short_lines']:
        text = sanitize(text, language, output_language)
    if best_issues or problems(text, language, output_language, 0) == ['short_lines']:
        text = reflow(text)          # רק כשיש בעיית שורות - פסקאות תקינות של המודל לא נוגעים
    return text.strip()


def transcribe_dedicated(client, chunk_bytes, mime_type, language, output_language):
    """המודל הייעודי לתמלול (gemini-3.5-transcribe). מחזיר טקסט (בשפת הדיבור) או זורק שגיאה."""
    import io
    lang_code = {'he': 'he-IL', 'yi': 'yi', 'en': 'en-US'}.get(language, 'he-IL')
    up = client.files.upload(file=io.BytesIO(chunk_bytes), config={'mime_type': mime_type})
    try:
        cfg = {'transcription_config': {'language_codes': [lang_code], 'mode': 'smart'}}
        if language == 'he':
            cfg['transcription_config']['custom_vocabulary'] = TORAH_VOCAB[:100]
        res = client.interactions.create(model='gemini-3.5-transcribe',
                                         input=[{'type': 'audio', 'uri': up.uri, 'mime_type': up.mime_type}],
                                         generation_config=cfg)
        text = (getattr(res, 'output_text', None) or '').strip()
        if not text:
            raise RuntimeError('empty transcript')
        return reflow(text)
    finally:
        try:
            client.files.delete(name=up.name)
        except Exception:
            pass
