"""
מנוע התמלול v3 - "תמלול עם ידע תורני", בלי לאמן מודל ובלי הקלטות של הלקוחות.

לכל קטע (5 דקות) בנפרד:
1. ניקוי שמע (ffmpeg: סינון זמזום ורעש, איזון עוצמה). נכשל - ממשיכים עם המקור.
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
def clean_audio(chunk_bytes):
    """סינון זמזום (מתחת ל-70Hz), הפחתת רעש עדינה ואיזון עוצמה. WAV נכנס - WAV יוצא."""
    ff = shutil.which('ffmpeg')
    if not ff or not chunk_bytes:
        return chunk_bytes
    try:
        res = subprocess.run(
            [ff, '-nostdin', '-hide_banner', '-loglevel', 'error', '-f', 'wav', '-i', 'pipe:0',
             '-af', 'highpass=f=70,afftdn=nf=-25,dynaudnorm=f=200:g=11',
             '-ac', '1', '-ar', '16000', '-sample_fmt', 's16', '-f', 'wav', 'pipe:1'],
            input=chunk_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        out = res.stdout
        if res.returncode != 0 or len(out) < len(chunk_bytes) * 0.8:
            log.info(f'v3 clean_audio skipped: {res.stderr.decode("utf-8", "replace")[-200:]}')
            return chunk_bytes
        return out
    except Exception as e:
        log.info(f'v3 clean_audio failed: {e}')
        return chunk_bytes


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


REFS_PROMPT = """Below is a Hebrew transcript of a Torah lesson segment. List the Jewish sources that are QUOTED in it
(a verse, a Mishna, a Gemara passage, Rashi/Tosafot, Rambam, Shulchan Aruch) - only sources whose words are actually
quoted or closely paraphrased, at most 5. Give each as a Sefaria reference in English, e.g.
"Genesis 1:1", "Bava Metzia 2a", "Rashi on Bava Metzia 2a", "Mishnah Berakhot 1:1", "Mishneh Torah, Sabbath 8:11",
"Shulchan Arukh, Orach Chayim 319:1". If unsure of the exact location, give the most likely one. If none - an empty list.
Return JSON only."""


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
    audio = clean_audio(chunk_bytes) if mime_type in ('audio/wav', 'audio/x-wav') else chunk_bytes
    forced = _route_hint.get() or ({'torah': 'torah', 'simple': 'simple'}.get(global_mode()))
    if forced:
        route, topic = forced, ''
    else:
        route, topic = classify(client, gtypes, audio, mime_type)
    info = {'chunk': chunk_num, 'route': route, 'topic': topic, 'forced': bool(forced), 'refs': [], 'fixed': False}

    if route == 'simple':
        text = TV2.transcribe_chunk(client, gtypes, audio, mime_type, language, output_language, prev_tail,
                                    chunk_num, n_chunks, seconds)
    else:
        full = route == 'torah'
        rules = '\nTORAH CONTENT\n' + (LX.PRONUNCIATION_RULES + '\n' if full else '') + (
            '- Correct spelling for names, books and terms that may be heard (use only if actually said): '
            + LX.glossary(full=full) + '\n')
        ctx = ''
        if full and state.sources:
            ctx = ('The lesson is studying these sources (for spelling of quotes only - transcribe only what is '
                   'actually said):\n' + sources_block(state.sources))
        text = TV2.transcribe_chunk(client, gtypes, audio, mime_type, language, output_language, prev_tail,
                                    chunk_num, n_chunks, seconds, extra_rules=rules, context=ctx)
        if text and output_language in ('he', 'original') and language in ('he', 'yi'):
            refs = find_refs(client, gtypes, text)
            found = [s for s in (fetch_source(r) for r in refs) if s]
            info['refs'] = [n for n, _ in found]
            if found:
                state.add(found)
                text, info['fixed'] = fix_quotes(client, gtypes, text, found)
    info['seconds'] = round(time.time() - started, 1)
    log.info(f'v3 chunk {chunk_num}/{n_chunks}: route={route} topic={topic!r} refs={info["refs"]} fixed={info["fixed"]}')
    rep = _report.get()
    if isinstance(rep, list):
        rep.append(info)
    return text
