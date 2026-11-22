"""
מנוע התמלול v3 - "תמלול עם ידע תורני", בלי לאמן מודל ובלי הקלטות של הלקוחות.

לכל קטע (5 דקות) בנפרד:
1. (רשות, בהגדרות) ניקוי שמע ועותק מואט נוסף, ומודל Pro - רק בקטעים התורניים.
2. זיהוי מסלול: שליחת הדקה הראשונה של הקטע ושאלה אחת - תורני / פשוט / מעורב.
3. פשוט   -> בדיוק המנוע הרגיל (v2).
   מעורב  -> v2 + ראשי תיבות ושמות נפוצים; ציטוט שזוהה בבירור - נבדק מול המקור.
   תורני  -> v2 + מילון תורני מלא + כללי הגייה אשכנזית/ישיבתית + המקורות שנמצאו בקטעים הקודמים;
             אחרי התמלול: זיהוי המקורות שצוטטו, שליפה מספריא, ותיקון הציטוטים לנוסח המדויק.
טעות בזיהוי לעולם לא מורידה מתחת לאיכות של v2: התיקון נוגע רק בציטוטים, ונבדק (אורך, שפה).

המסלול נקבע גם ידנית: הגדרת מנהל transcribe_route_mode (auto/torah/simple), ולכל לקוח / מוסד
עמודת transcribe_route. הקורא מגדיר את הרמז עם route_context(...) (contextvars - לכל תהליך בנפרד).
"""
import contextvars
import html
import io
import json
import logging
import re
import shutil
import subprocess
import threading
import time
import wave

from services import transcribe_v2 as TV2
from services import torah_lexicon as LX

log = logging.getLogger(__name__)

MODEL = 'gemini-3.5-flash'
ROUTE_NAMES = {'torah': 'תורני', 'simple': 'פשוט', 'mixed': 'מעורב'}
SEFARIA = 'https://www.sefaria.org/api/texts/{ref}'
MAX_SOURCE_CHARS = 9000          # כל המקורות יחד לפרומפט של קטע
MAX_REF_CHARS = 3500             # מקור אחד

_route_hint = contextvars.ContextVar('transcribe_route_hint', default=None)
_report = contextvars.ContextVar('transcribe_v3_report', default=None)
engine_override = contextvars.ContextVar('transcribe_engine_override', default=None)
options_override = contextvars.ContextVar('transcribe_v3_options', default=None)   # כלי ההשוואה

MODELS = {'flash': 'gemini-3.5-flash', 'pro': 'gemini-3.1-pro-preview'}
SLOW_TEMPO = 0.8


def options():
    """אפשרויות v3 לקטעים התורניים: model (flash/pro), slow (עותק איטי נוסף), clean (ניקוי שמע).
    ברירת מחדל - מההגדרות; כלי ההשוואה דורס עם options_override."""
    o = {'model': 'flash', 'slow': False, 'clean': False}
    try:
        from models import Settings
        rows = {r.key: r.value for r in Settings.query.filter(Settings.key.in_(
            ['transcribe_v3_model', 'transcribe_v3_slow', 'transcribe_v3_clean'])).all()}
        o['model'] = rows.get('transcribe_v3_model') if rows.get('transcribe_v3_model') in MODELS else 'flash'
        o['slow'] = rows.get('transcribe_v3_slow') == '1'
        o['clean'] = rows.get('transcribe_v3_clean') == '1'
    except Exception:
        pass
    ov = options_override.get()
    if isinstance(ov, dict):
        o.update({k: v for k, v in ov.items() if k in o})
    return o


class route_context:
    """with route_context('torah'): ... - מסלול קבוע לתמלול הזה (None/'auto' = זיהוי אוטומטי).
    report=list() - מקבל פירוט לכל קטע (לכלי ההשוואה בממשק המנהל)."""

    def __init__(self, route=None, report=None):
        self.route, self.report = route, report

    def __enter__(self):
        self._t1 = _route_hint.set(self.route if self.route in ('torah', 'simple', 'mixed') else None)
        self._t2 = _report.set(self.report)
        return self

    def __exit__(self, *a):
        _route_hint.reset(self._t1)
        _report.reset(self._t2)


def route_for(customer=None, institution=None):
    """המסלול הקבוע של לקוח / מוסד (תלמיד יורש מהמוסד), או None לזיהוי אוטומטי."""
    for o in (customer, institution):
        r = getattr(o, 'transcribe_route', None) if o is not None else None
        if r in ('torah', 'simple', 'mixed'):
            return r
    return None


def route_for_ids(customer_id=None, institution_id=None):
    try:
        from models import Customer, Institution
        c = Customer.query.get(customer_id) if customer_id else None
        inst = Institution.query.get(institution_id or getattr(c, 'institution_id', None)) \
            if (institution_id or getattr(c, 'institution_id', None)) else None
        return route_for(c, inst)
    except Exception:
        return None


def global_mode():
    try:
        from models import Settings
        row = Settings.query.filter_by(key='transcribe_route_mode').first()
        v = (row.value if row else '') or 'auto'
        return v if v in ('auto', 'torah', 'simple') else 'auto'
    except Exception:
        return 'auto'


# ------------------------------------------------------------------ 1. ניקוי שמע
def _ffmpeg_wav(chunk_bytes, af, in_fmt=None, timeout=180):
    """מריץ מסנן ffmpeg ומחזיר WAV 16kHz מונו עם כותרת תקינה (פלט לקובץ זמני - בפלט לצינור
    ffmpeg כותב אורך "לא ידוע" בכותרת). נכשל - None."""
    import os
    import tempfile
    ff = shutil.which('ffmpeg')
    if not ff or not chunk_bytes:
        return None
    fd, path = tempfile.mkstemp(suffix='.wav')
    os.close(fd)
    try:
        res = subprocess.run(
            [ff, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y'] + (['-f', in_fmt] if in_fmt else []) +
            ['-i', 'pipe:0', '-af', af, '-ac', '1', '-ar', '16000', '-sample_fmt', 's16', path],
            input=chunk_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if res.returncode != 0:
            log.info(f'v3 ffmpeg {af}: {res.stderr.decode("utf-8", "replace")[-200:]}')
            return None
        with open(path, 'rb') as f:
            out = f.read()
        return out if len(out) > 1000 else None
    except Exception as e:
        log.info(f'v3 ffmpeg {af}: {e}')
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def clean_audio(chunk_bytes):
    """סינון זמזום (מתחת ל-70Hz), הפחתת רעש עדינה ואיזון עוצמה. WAV נכנס - WAV יוצא; נכשל - המקור."""
    out = _ffmpeg_wav(chunk_bytes, 'highpass=f=70,afftdn=nf=-25,dynaudnorm=f=200:g=11', in_fmt='wav', timeout=120)
    return out or chunk_bytes


def slow_copy(chunk_bytes, tempo=SLOW_TEMPO):
    """אותו קטע מואט (בלי שינוי גובה הקול), WAV 16kHz מונו. נכשל - None."""
    return _ffmpeg_wav(chunk_bytes, f'atempo={tempo}')


def wav_head(chunk_bytes, seconds=60):
    """הדקה הראשונה של קטע WAV (לזיהוי המסלול). לא WAV - מחזיר את הכל."""
    try:
        with wave.open(io.BytesIO(chunk_bytes), 'rb') as w:
            params = (w.getnchannels(), w.getsampwidth(), w.getframerate())
            data = w.readframes(params[2] * seconds)
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as out:
            out.setnchannels(params[0])
            out.setsampwidth(params[1])
            out.setframerate(params[2])
            out.writeframes(data)
        return buf.getvalue()
    except Exception:
        return chunk_bytes


# ------------------------------------------------------------------ 2. זיהוי מסלול
CLASSIFY_PROMPT = """Listen to this audio excerpt and classify its CONTENT (not the speaker's accent):
- "torah": a Torah lesson / shiur / Talmud (Gemara) study / halacha / derasha / sicha - most of the speech is Torah learning.
- "simple": an everyday conversation, phone call, business, a message, a dictation, news - no Torah learning.
- "mixed": mostly everyday speech with a few Torah quotes or terms (e.g. a speech at a family event).
Also give a short Hebrew topic (e.g. "בבא מציעא - שנים אוחזין בטלית", "הלכות שבת - בורר", "שיחת עבודה").
Return JSON only."""


def classify(client, gtypes, chunk_bytes, mime_type='audio/wav'):
    try:
        resp = client.models.generate_content(
            model=MODEL,
            contents=[CLASSIFY_PROMPT, gtypes.Part.from_bytes(data=wav_head(chunk_bytes, 60), mime_type=mime_type)],
            config=gtypes.GenerateContentConfig(
                response_mime_type='application/json',
                response_schema={'type': 'OBJECT', 'properties': {
                    'route': {'type': 'STRING', 'enum': ['torah', 'simple', 'mixed']},
                    'topic': {'type': 'STRING'}}, 'required': ['route']},
                thinking_config=gtypes.ThinkingConfig(thinking_budget=0), temperature=0))
        d = json.loads(resp.text or '{}')
        route = d.get('route') if d.get('route') in ROUTE_NAMES else 'simple'
        return route, str(d.get('topic') or '')[:120]
    except Exception as e:
        log.warning(f'v3 classify failed ({e}) - simple route')
        return 'simple', ''


# ------------------------------------------------------------------ 3. מקורות מספריא
_NIKUD = re.compile(r'[֑-ׇ]')
_TAGS = re.compile(r'<[^>]+>')
_cache = {}
_cache_lock = threading.Lock()


def _flat(x):
    if isinstance(x, list):
        return ' '.join(_flat(i) for i in x)
    return str(x or '')


def plain(text):
    t = _TAGS.sub(' ', html.unescape(text or ''))
    t = _NIKUD.sub('', t).replace('־', ' ').replace('׀', ' ')
    return re.sub(r'\s+', ' ', t).strip()


def fetch_source(ref):
    """(שם המקור בעברית, טקסט בלי ניקוד) או None. נשמר בזיכרון לשעה."""
    import requests
    ref = (ref or '').strip()
    if not ref or len(ref) > 120:
        return None
    key = ref.lower()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
    val = None
    try:
        url = SEFARIA.format(ref=requests.utils.quote(ref.replace(' ', '_'), safe='_.,:-'))
        r = requests.get(url, params={'context': 0, 'commentary': 0, 'pad': 0}, timeout=12)
        d = r.json()
        if not d.get('error'):
            txt = plain(_flat(d.get('he')))
            if len(txt) > 20:
                val = (d.get('heRef') or ref, txt[:MAX_REF_CHARS])
    except Exception as e:
        log.info(f'v3 sefaria {ref}: {e}')
    with _cache_lock:
        if len(_cache) > 500:
            _cache.clear()
        _cache[key] = (time.time(), val)
    return val


REFS_PROMPT = """Below is a Hebrew transcript of a Torah lesson segment. Identify the Jewish texts the speaker is READING
or QUOTING word by word (a verse, a Mishna, a Gemara passage, Rashi/Tosafot, Rambam, Shulchan Aruch, Mishna Berura...),
at most 5. The transcript may contain mistakes, including in the name of the book (e.g. "הרב" heard instead of
"הרמב\"ם") - identify the source by the quoted words themselves.
IMPORTANT: when the quoted passage is a Rambam / Gemara / Shulchan Aruch text that itself cites a verse, give the
Rambam / Gemara / Shulchan Aruch passage - NOT the verse it cites. Give a verse only when the speaker quotes the verse
itself as the subject. Never give a source just because of a common phrase or a topic.
Give each as a Sefaria reference in English, e.g. "Genesis 1:1", "Bava Metzia 2a", "Rashi on Bava Metzia 2a",
"Mishnah Berakhot 1:1", "Mishneh Torah, Torah Study 1:8", "Shulchan Arukh, Orach Chayim 319:1".
If none - an empty list. Return JSON only."""


def find_refs(client, gtypes, transcript):
    try:
        resp = client.models.generate_content(
            model=MODEL, contents=[REFS_PROMPT + '\n\nTRANSCRIPT:\n' + transcript[:20000]],
            config=gtypes.GenerateContentConfig(
                response_mime_type='application/json',
                response_schema={'type': 'OBJECT', 'properties': {'refs': {'type': 'ARRAY', 'items': {'type': 'STRING'}}},
                                 'required': ['refs']},
                thinking_config=gtypes.ThinkingConfig(thinking_budget=0), temperature=0))
        refs = json.loads(resp.text or '{}').get('refs') or []
        return [str(r)[:120] for r in refs if str(r).strip()][:5]
    except Exception as e:
        log.info(f'v3 find_refs failed: {e}')
        return []


_NORM_RE = re.compile(r'[^\u05d0-\u05ea ]+')


def _norm_words(text, strip_yv=True):
    """מילים עבריות בלבד; בלי י/ו באמצע (כתיב חסר/מלא) ובלי אותיות סופיות שונות."""
    t = _NORM_RE.sub(' ', (text or '').translate(str.maketrans('ךםןףץ', 'כמנפצ')))
    out = []
    for w in t.split():
        if strip_yv and len(w) > 2:
            w = w[0] + w[1:-1].replace('י', '').replace('ו', '') + w[-1]
        out.append(w)
    return out


def _grams(words, n=3):
    return {' '.join(words[i:i + n]) for i in range(len(words) - n + 1)}


def relevant_sources(transcript, found, min_hits=2):
    """משאיר רק מקור שהמילים שלו באמת נאמרו (לפחות 2 רצפים של 3 מילים), ומוריד מקור שכל מה שנאמר
    ממנו כבר מכוסה במקור אחר (פסוק שמובא בתוך הרמב"ם - נשאר הרמב"ם)."""
    tg = _grams(_norm_words(transcript))
    hits = []
    for name, txt in found:
        h = tg & _grams(_norm_words(txt))
        if len(h) >= min_hits:
            hits.append((name, txt, h))
    keep = []
    for i, (name, txt, h) in enumerate(hits):
        covered = any(j != i and h <= h2 and (len(h2) > len(h) or j < i) for j, (_n, _t, h2) in enumerate(hits))
        if not covered:
            keep.append((name, txt))
    return keep


def keep_full_spelling(original, fixed):
    """התיקון לא מחליף מילה שההבדל בה רק י/ו (חייב -> חיב): משאירים את הכתיב של התמלול."""
    import difflib
    a, b = original.split(' '), fixed.split(' ')
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == 'replace' and i2 - i1 == j2 - j1:
            for x, y in zip(a[i1:i2], b[j1:j2]):
                same = _norm_words(x) == _norm_words(y) and _norm_words(x, False) != _norm_words(y, False)
                out.append(x if same and len(y) < len(x) else y)
        else:
            out.extend(b[j1:j2])
    return ' '.join(out)


def sources_block(sources):
    out, total = [], 0
    for name, txt in sources:
        if total + len(txt) > MAX_SOURCE_CHARS:
            txt = txt[:max(0, MAX_SOURCE_CHARS - total)]
        if not txt:
            break
        out.append(f'[{name}]\n{txt}')
        total += len(txt)
    return '\n\n'.join(out)


# ------------------------------------------------------------------ 5. תיקון ציטוטים
QUOTE_FIX = """You are proof-reading a Hebrew transcript of a Torah lesson against the exact source texts below.
Fix ONLY words inside passages where the speaker is clearly QUOTING one of these sources (reading a verse, a Mishna,
the Gemara, Rashi etc.), so that the quoted words match the source spelling exactly (without nikud).
Do NOT change anything else: not the speaker's own explanations, not the order, not the punctuation of his words,
and do not add parts of the source that the speaker did not say. If nothing needs fixing, return the transcript unchanged.
The sources are printed in defective spelling (כתיב חסר, e.g. חיב, עני); keep the transcript's FULL spelling
(כתיב מלא, e.g. חייב) - change a word only when it is a different word, never just to add or drop י / ו.
Keep the paragraph breaks exactly as they are. Return JSON {"transcript": "..."} only."""


def fix_quotes(client, gtypes, transcript, sources):
    block = sources_block(sources)
    if not block or not transcript:
        return transcript, False
    try:
        resp = client.models.generate_content(
            model=MODEL,
            contents=[QUOTE_FIX + '\n\nSOURCES:\n' + block + '\n\nTRANSCRIPT:\n' + transcript],
            config=gtypes.GenerateContentConfig(
                response_mime_type='application/json',
                response_schema={'type': 'OBJECT', 'properties': {'transcript': {'type': 'STRING'}},
                                 'required': ['transcript']},
                thinking_config=gtypes.ThinkingConfig(thinking_budget=0), temperature=0))
        fixed = TV2.parse_json_transcript(resp.text or '').strip()
    except Exception as e:
        log.info(f'v3 fix_quotes failed: {e}')
        return transcript, False
    # שמירה: אורך כמעט זהה, אותה שפה, ובלי סימני "מודל מדבר" - אחרת משאירים את המקור
    if not fixed or abs(len(fixed) - len(transcript)) > max(80, len(transcript) * 0.08):
        return transcript, False
    if TV2.latin_ratio(fixed) > TV2.latin_ratio(transcript) + 0.02 or TV2._META_RE.search(fixed):
        return transcript, False
    fixed = '\n'.join(keep_full_spelling(o, f) for o, f in zip(transcript.split('\n'), fixed.split('\n'))) \
        if transcript.count('\n') == fixed.count('\n') else fixed
    return fixed, fixed != transcript


# ------------------------------------------------------------------ קטע אחד
class State:
    """מה שעובר מקטע לקטע באותה הקלטה: המקורות שנמצאו (הסוגיה בדרך כלל ממשיכה)."""

    def __init__(self):
        self.sources = []          # [(שם, טקסט)]
        self.names = set()

    def add(self, items):
        for name, txt in items:
            if name not in self.names:
                self.names.add(name)
                self.sources.append((name, txt))
        self.sources = self.sources[-6:]


def transcribe_chunk(client, gtypes, chunk_bytes, mime_type, language, output_language, prev_tail,
                     chunk_num, n_chunks, seconds, state):
    started = time.time()
    forced = _route_hint.get() or ({'torah': 'torah', 'simple': 'simple'}.get(global_mode()))
    if forced:
        route, topic = forced, ''
    else:
        route, topic = classify(client, gtypes, chunk_bytes, mime_type)
    info = {'chunk': chunk_num, 'route': route, 'topic': topic, 'forced': bool(forced), 'refs': [], 'fixed': False}

    if route == 'simple':
        # פשוט = בדיוק v2: אותו שמע, אותו מודל, בלי תוספות
        text = TV2.transcribe_chunk(client, gtypes, chunk_bytes, mime_type, language, output_language, prev_tail,
                                    chunk_num, n_chunks, seconds)
    else:
        full = route == 'torah'
        opt = options()
        audio, amime = chunk_bytes, mime_type
        if opt['clean'] and mime_type in ('audio/wav', 'audio/x-wav'):
            audio = clean_audio(chunk_bytes)
        slow = slow_copy(audio) if (full and opt['slow']) else None
        model = MODELS[opt['model']] if full else MODEL
        info.update({'model': opt['model'] if full else 'flash', 'slow': bool(slow), 'clean': audio is not chunk_bytes})
        rules = '\nTORAH CONTENT\n' + (LX.PRONUNCIATION_RULES + '\n' if full else '') + (
            '- Correct spelling for names, books and terms that may be heard (use only if actually said): '
            + LX.glossary(full=full) + '\n'
            '- Write in standard Hebrew FULL spelling (כתיב מלא), also inside quotes.\n')
        ctx = ''
        if full and state.sources:
            ctx = ('The lesson is studying these sources (for spelling of quotes only - transcribe only what is '
                   'actually said):\n' + sources_block(state.sources))
        try:
            text = TV2.transcribe_chunk(client, gtypes, audio, amime, language, output_language, prev_tail,
                                        chunk_num, n_chunks, seconds, model=model, extra_rules=rules, context=ctx,
                                        extra_audio=slow)
        except Exception as e:
            if model == MODEL and not slow:
                raise
            # Pro / עותק איטי נכשלו - הקטע לא הולך לאיבוד: אותו דבר ב-Flash בלי עותק
            log.warning(f'v3 chunk {chunk_num}: {opt} failed ({e}) - falling back to flash')
            info.update({'model': 'flash', 'slow': False, 'fallback': True})
            text = TV2.transcribe_chunk(client, gtypes, audio, amime, language, output_language, prev_tail,
                                        chunk_num, n_chunks, seconds, extra_rules=rules, context=ctx)
        if text and output_language in ('he', 'original') and language in ('he', 'yi'):
            refs = find_refs(client, gtypes, text)
            found = relevant_sources(text, [s for s in (fetch_source(r) for r in refs) if s])
            info['refs'] = [n for n, _ in found]
            if found:
                state.add(found)
                text, info['fixed'] = fix_quotes(client, gtypes, text, found)
    info['seconds'] = round(time.time() - started, 1)
    log.info(f'v3 chunk {chunk_num}/{n_chunks}: {info}')
    rep = _report.get()
    if isinstance(rep, list):
        rep.append(info)
    return text
