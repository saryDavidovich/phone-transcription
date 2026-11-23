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
_terms = contextvars.ContextVar('transcribe_customer_terms', default='')         # שמות ומונחים של הלקוח
TERMS_MAX = 1500

# (מודל, רמת חשיבה). Flash בלי חשיבה = מה שרץ היום; "חושב" = אותו מודל עם חשיבה בינונית
MODELS = {'flash': ('gemini-3.5-flash', None), 'think': ('gemini-3.5-flash', 'medium'),
          'flash36': ('gemini-3.6-flash', None), 'pro': ('gemini-3.1-pro-preview', 'low')}
MODEL_NAMES = {'flash': 'Flash', 'think': 'Flash חושב', 'flash36': 'Flash 3.6', 'pro': 'Pro 3.1'}
SLOW_TEMPO = 0.8


def options():
    """אפשרויות v3 לקטעים התורניים: model (flash/pro), slow (עותק איטי נוסף), clean (ניקוי שמע).
    ברירת מחדל - מההגדרות; כלי ההשוואה דורס עם options_override."""
    o = {'model': 'flash', 'slow': False, 'clean': False, 'brief': True}
    try:
        from models import Settings
        rows = {r.key: r.value for r in Settings.query.filter(Settings.key.in_(
            ['transcribe_v3_model', 'transcribe_v3_slow', 'transcribe_v3_clean', 'transcribe_v3_brief'])).all()}
        o['model'] = rows.get('transcribe_v3_model') if rows.get('transcribe_v3_model') in MODELS else 'flash'
        o['slow'] = rows.get('transcribe_v3_slow') == '1'
        o['clean'] = rows.get('transcribe_v3_clean') == '1'
        o['brief'] = rows.get('transcribe_v3_brief', '1') != '0'
    except Exception:
        pass
    ov = options_override.get()
    if isinstance(ov, dict):
        o.update({k: v for k, v in ov.items() if k in o})
    return o


class route_context:
    """with route_context('torah'): ... - מסלול קבוע לתמלול הזה (None/'auto' = זיהוי אוטומטי).
    report=list() - מקבל פירוט לכל קטע (לכלי ההשוואה בממשק המנהל)."""

    def __init__(self, route=None, report=None, terms=None):
        self.route, self.report, self.terms = route, report, terms

    def __enter__(self):
        self._t1 = _route_hint.set(self.route if self.route in ('torah', 'simple', 'mixed') else None)
        self._t2 = _report.set(self.report)
        self._t3 = _terms.set(clean_terms(self.terms) if self.terms is not None else _terms.get())
        return self

    def __exit__(self, *a):
        _route_hint.reset(self._t1)
        _report.reset(self._t2)
        _terms.reset(self._t3)


def clean_terms(text):
    """שמות ומונחים שהלקוח כתב: שורות/פסיקים -> רשימה אחת מופרדת בפסיקים, בלי תווים מוזרים."""
    parts = re.split(r'[\n,;،]+', str(text or ''))
    out, seen = [], set()
    for p in parts:
        p = re.sub(r'[<>{}\[\]`\\]', '', p).strip()[:80]
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return ', '.join(out)[:TERMS_MAX]


def terms_rules():
    """תוספת לפרומפט של כל קטע (גם ב-v2) - רק כשללקוח יש שמות ומונחים."""
    t = _terms.get()
    if not t:
        return ''
    return ('\nNAMES AND TERMS GIVEN BY THE CUSTOMER (correct spelling - use exactly this spelling when such a word '
            'is actually said; never add them otherwise): ' + t + '\n')


def terms_for(customer=None, institution=None):
    vals = [getattr(o, 'transcribe_terms', None) for o in (customer, institution) if o is not None]
    return clean_terms('\n'.join(v for v in vals if v))


def ctx_for_ids(customer_id=None, institution_id=None):
    """route_context עם המסלול והשמות/מונחים של הלקוח (תלמיד יורש גם מהמוסד)."""
    try:
        from models import Customer, Institution
        c = Customer.query.get(customer_id) if customer_id else None
        iid = institution_id or getattr(c, 'institution_id', None)
        inst = Institution.query.get(iid) if iid else None
        return route_context(route_for(c, inst), terms=terms_for(c, inst))
    except Exception:
        return route_context(None)


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


# ------------------------------------------------------------------ 0. תדריך: האזנה להקלטה כולה
BRIEF_MAX_SECONDS = 3 * 3600
BRIEF_INLINE_MAX = 18 * 1024 * 1024

BRIEF_PROMPT = """Listen to the WHOLE recording. Before it is transcribed word by word (in another step), prepare a
briefing for the transcriber - like a human transcriber who first listens to the whole lesson to understand it, so
that later he knows what words to expect. Do NOT transcribe. Fill these fields, in Hebrew:
- content_type: "torah" (Torah lesson / shiur / derasha / halacha / Gemara study), "simple" (everyday speech, phone
  call, business, message - no Torah learning) or "mixed" (everyday speech with some Torah quotes).
- accent: the speaker's Hebrew pronunciation (הברה): אשכנזית / חסידית / ספרדית / ישראלית / תימנית / מעורבת,
  plus a short note about typical sounds (e.g. "ת רפה נשמעת ס, חולם נשמע אוי").
- speakers: how many speakers and who they are (e.g. "מגיד שיעור ושואלים מהקהל").
- topic: one line - what the recording is about (e.g. "בבא מציעא ב. - שנים אוחזין בטלית").
- summary: 5-12 sentences describing the course of the recording in order: what is discussed, which questions,
  answers and conclusions - so the transcriber can follow the argument.
- sources: the Jewish texts that are read or quoted (verse, Mishna, Gemara page, Rashi, Tosafot, Rambam, Shulchan
  Aruch...) as Sefaria references in English, e.g. "Bava Metzia 2a", "Mishneh Torah, Torah Study 1:8",
  "Shulchan Arukh, Orach Chayim 319:1". Only texts that are actually read or quoted - at most 8.
- names: people, rabbis, Tannaim/Amoraim, Rishonim/Acharonim, books, places and institutions mentioned - each in its
  CORRECT standard Hebrew spelling.
- terms: unusual words - Aramaic, Torah terms, acronyms (ראשי תיבות), foreign or professional words - in correct
  Hebrew spelling.
Return JSON only."""

BRIEF_SCHEMA = {'type': 'OBJECT', 'properties': {
    'content_type': {'type': 'STRING', 'enum': ['torah', 'simple', 'mixed']},
    'accent': {'type': 'STRING'}, 'speakers': {'type': 'STRING'}, 'topic': {'type': 'STRING'},
    'summary': {'type': 'STRING'},
    'sources': {'type': 'ARRAY', 'items': {'type': 'STRING'}},
    'names': {'type': 'ARRAY', 'items': {'type': 'STRING'}},
    'terms': {'type': 'ARRAY', 'items': {'type': 'STRING'}}},
    'required': ['content_type', 'accent', 'speakers', 'topic', 'summary', 'sources', 'names', 'terms']}


def _compact_audio(path):
    """כל ההקלטה (עד 3 שעות) כ-OGG/Opus מונו 24kbps - שעה ~ 11MB. נכשל - None."""
    import os
    import tempfile
    ff = shutil.which('ffmpeg')
    if not ff or not path:
        return None
    fd, out = tempfile.mkstemp(suffix='.ogg')
    os.close(fd)
    try:
        r = subprocess.run([ff, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', path, '-t',
                            str(BRIEF_MAX_SECONDS), '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'libopus',
                            '-b:a', '24k', out], capture_output=True, timeout=900)
        if r.returncode == 0 and os.path.getsize(out) > 1000:
            return out
        log.info(f'v3 briefing compact failed: {r.stderr[-200:]!r}')
    except Exception as e:
        log.info(f'v3 briefing compact failed: {e}')
    try:
        os.remove(out)
    except OSError:
        pass
    return None


def make_briefing(client, gtypes, audio_path, language='he'):
    """מאזין להקלטה כולה ומחזיר תדריך (dict) או None. קובץ גדול - דרך Files API."""
    import os
    small = _compact_audio(audio_path)
    if not small:
        return None
    uploaded = None
    try:
        if os.path.getsize(small) <= BRIEF_INLINE_MAX:
            with open(small, 'rb') as f:
                part = gtypes.Part.from_bytes(data=f.read(), mime_type='audio/ogg')
        else:
            uploaded = client.files.upload(file=small, config={'mime_type': 'audio/ogg'})
            for _ in range(60):                       # עד דקה לעיבוד אצל Google
                state = str(getattr(getattr(uploaded, 'state', None), 'name', getattr(uploaded, 'state', '')))
                if 'PROCESSING' not in state.upper():
                    break
                time.sleep(1)
                uploaded = client.files.get(name=uploaded.name)
            part = uploaded
        lang_note = {'yi': ' The speech is in Yiddish; still write the briefing in Hebrew.',
                     'en': ' The speech is in English; write names/terms as spoken.'}.get(language, '')
        if _terms.get():
            lang_note += ' The customer says these names/terms may appear (correct spelling): ' + _terms.get()
        resp = client.models.generate_content(
            model=MODEL, contents=[BRIEF_PROMPT + lang_note, part],
            config=gtypes.GenerateContentConfig(
                response_mime_type='application/json', response_schema=BRIEF_SCHEMA,
                thinking_config=gtypes.ThinkingConfig(thinking_budget=0), temperature=0.2))
        d = json.loads(resp.text or '{}')
        if not isinstance(d, dict) or not d.get('summary'):
            return None
        for k in ('sources', 'names', 'terms'):
            d[k] = [str(x).strip()[:120] for x in (d.get(k) or []) if str(x).strip()][:60]
        return d
    except Exception as e:
        log.warning(f'v3 briefing failed: {e}')
        return None
    finally:
        try:
            os.remove(small)
        except OSError:
            pass
        if uploaded is not None:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass


def briefing_text(b):
    """התדריך כטקסט לפרומפט של כל קטע."""
    if not b:
        return ''
    lines = [f"Topic: {b.get('topic', '')}", f"Speakers: {b.get('speakers', '')}",
             f"Pronunciation (הברה): {b.get('accent', '')}", f"Course of the recording: {b.get('summary', '')}"]
    if b.get('names'):
        lines.append('Names mentioned (correct spelling): ' + ', '.join(b['names']))
    if b.get('terms'):
        lines.append('Terms (correct spelling): ' + ', '.join(b['terms']))
    return ('BACKGROUND - from listening to the whole recording beforehand. Use it ONLY to understand unclear words '
            'and to spell names/terms correctly. Transcribe ONLY what is actually said in THIS segment; never add '
            'content from the background:\n' + '\n'.join(lines))


def prepare(client, gtypes, audio_path, language, state):
    """שלב 0 של v3 (לפני הקטעים): תדריך, ושליפת המקורות שבו מספריא מראש."""
    if not options().get('brief') or not audio_path:
        return
    t0 = time.time()
    b = make_briefing(client, gtypes, audio_path, language)
    if not b:
        return
    state.briefing = b
    if b.get('content_type') in ('torah', 'simple'):
        state.brief_route = b['content_type']
    found = [x for x in (fetch_source(r) for r in b.get('sources', [])[:8]) if x]
    if found:
        state.add(found)
    info = {'briefing': b, 'brief_sources': [n for n, _ in found], 'seconds': round(time.time() - t0, 1)}
    log.info(f"v3 briefing: type={b.get('content_type')} topic={b.get('topic')!r} sources={info['brief_sources']}")
    rep = _report.get()
    if isinstance(rep, list):
        rep.append(info)


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
        self.briefing = None       # תדריך: מה ששמענו בהאזנה להקלטה כולה (prepare)
        self.brief_route = None    # torah / simple כשהתדריך בטוח לגבי כל ההקלטה

    def add(self, items):
        for name, txt in items:
            if name not in self.names:
                self.names.add(name)
                self.sources.append((name, txt))
        self.sources = self.sources[-10:]


def transcribe_chunk(client, gtypes, chunk_bytes, mime_type, language, output_language, prev_tail,
                     chunk_num, n_chunks, seconds, state):
    started = time.time()
    forced = _route_hint.get() or ({'torah': 'torah', 'simple': 'simple'}.get(global_mode()))
    if forced:
        route, topic = forced, ''
    elif state.brief_route:
        route, topic = state.brief_route, (state.briefing or {}).get('topic', '')   # לפי התדריך
    else:
        route, topic = classify(client, gtypes, chunk_bytes, mime_type)
    info = {'chunk': chunk_num, 'route': route, 'topic': topic, 'forced': bool(forced), 'refs': [], 'fixed': False,
            'by_brief': bool(not forced and state.brief_route)}

    if route == 'simple':
        # פשוט = בדיוק v2: אותו שמע, אותו מודל, בלי תוספות
        text = TV2.transcribe_chunk(client, gtypes, chunk_bytes, mime_type, language, output_language, prev_tail,
                                    chunk_num, n_chunks, seconds, extra_rules=terms_rules())
    else:
        full = route == 'torah'
        opt = options()
        audio, amime = chunk_bytes, mime_type
        if opt['clean'] and mime_type in ('audio/wav', 'audio/x-wav'):
            audio = clean_audio(chunk_bytes)
        slow = slow_copy(audio) if (full and opt['slow']) else None
        model, thinking = MODELS[opt['model']] if full else (MODEL, None)
        info.update({'model': opt['model'] if full else 'flash', 'slow': bool(slow), 'clean': audio is not chunk_bytes})
        rules = '\nTORAH CONTENT\n' + (LX.PRONUNCIATION_RULES + '\n' if full else '') + (
            '- Correct spelling for names, books and terms that may be heard (use only if actually said): '
            + LX.glossary(full=full) + '\n'
            '- Write in standard Hebrew FULL spelling (כתיב מלא), also inside quotes.\n') + terms_rules()
        ctx = briefing_text(state.briefing)
        if full and state.sources:
            ctx += ('\n\nThe lesson is studying these sources (for spelling of quotes only - transcribe only what '
                    'is actually said):\n' + sources_block(state.sources))
        try:
            text = TV2.transcribe_chunk(client, gtypes, audio, amime, language, output_language, prev_tail,
                                        chunk_num, n_chunks, seconds, model=model, extra_rules=rules, context=ctx,
                                        extra_audio=slow, thinking=thinking)
        except Exception as e:
            if model == MODEL and not slow and not thinking:
                raise
            # Pro / עותק איטי נכשלו - הקטע לא הולך לאיבוד: אותו דבר ב-Flash בלי עותק
            log.warning(f'v3 chunk {chunk_num}: {opt} failed ({e}) - falling back to flash')
            info.update({'model': 'flash', 'slow': False, 'fallback': True, 'error': f'{type(e).__name__}: {e}'[:300]})
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


# ------------------------------------------------------------------ ניתוח השוואה (כלי ההשוואה במנהל)
ANALYZE_PROMPT = """You are the quality judge of Hebrew transcription engines. The audio recording is attached, with
several transcripts of it (v2, v3 - our engines; alef - a competitor). LISTEN to the audio and decide, for every
place where the transcripts differ in a meaningful way, what was ACTUALLY SAID and which transcript is right.
Ignore differences that are only paragraph breaks or trivial punctuation, unless they hurt reading.
Categories: names (people, rabbis, books, places), pronunciation (Ashkenazi/Chassidic sounds written wrongly),
quotes (verses, Mishna, Gemara, Rambam - wording), aramaic_terms (Aramaic, Torah terms, acronyms), wrong_word
(an ordinary word heard wrongly), missing (said but not written), extra (written but not said / repeated / invented),
punctuation (punctuation, sentence and paragraph structure).
Return JSON, all text in Hebrew:
- verdict: 2-4 sentences - which transcript is best overall and why.
- scores: for each transcript given: version, errors (number of real errors you found in it), strengths, weaknesses.
- issues: up to 40 concrete differences, most important first: category, said (what was actually said, by the audio),
  v2, v3, alef (what each wrote at that place; "" if that transcript is not given), best (v2 / v3 / alef / none).
- rules: up to 10 concrete instructions to add to OUR transcription prompt so that our engine gets these places right
  next time (general rules, not specific to this recording, unless it is a name/term to add to a glossary).
Return JSON only."""

ANALYZE_SCHEMA = {'type': 'OBJECT', 'properties': {
    'verdict': {'type': 'STRING'},
    'scores': {'type': 'ARRAY', 'items': {'type': 'OBJECT', 'properties': {
        'version': {'type': 'STRING'}, 'errors': {'type': 'INTEGER'},
        'strengths': {'type': 'STRING'}, 'weaknesses': {'type': 'STRING'}},
        'required': ['version', 'errors', 'strengths', 'weaknesses']}},
    'issues': {'type': 'ARRAY', 'items': {'type': 'OBJECT', 'properties': {
        'category': {'type': 'STRING', 'enum': ['names', 'pronunciation', 'quotes', 'aramaic_terms', 'wrong_word',
                                                'missing', 'extra', 'punctuation']},
        'said': {'type': 'STRING'}, 'v2': {'type': 'STRING'}, 'v3': {'type': 'STRING'}, 'alef': {'type': 'STRING'},
        'best': {'type': 'STRING', 'enum': ['v2', 'v3', 'alef', 'none']}},
        'required': ['category', 'said', 'v2', 'v3', 'alef', 'best']}},
    'rules': {'type': 'ARRAY', 'items': {'type': 'STRING'}}},
    'required': ['verdict', 'scores', 'issues', 'rules']}

ANALYZE_MAX_CHARS = 60000


def analyze(client, gtypes, audio_path, texts, briefing=None):
    """שופט: מאזין להקלטה ומשווה את התמלולים (dict גרסה -> טקסט). מחזיר dict או זורק שגיאה."""
    import os
    texts = {k: (v or '')[:ANALYZE_MAX_CHARS] for k, v in texts.items() if (v or '').strip()}
    if len(texts) < 2:
        raise ValueError('צריך לפחות שני תמלולים להשוואה')
    small = _compact_audio(audio_path) if audio_path else None
    uploaded = None
    try:
        parts = []
        if small:
            if os.path.getsize(small) <= BRIEF_INLINE_MAX:
                with open(small, 'rb') as f:
                    parts.append(gtypes.Part.from_bytes(data=f.read(), mime_type='audio/ogg'))
            else:
                uploaded = client.files.upload(file=small, config={'mime_type': 'audio/ogg'})
                for _ in range(60):
                    st = str(getattr(getattr(uploaded, 'state', None), 'name', getattr(uploaded, 'state', '')))
                    if 'PROCESSING' not in st.upper():
                        break
                    time.sleep(1)
                    uploaded = client.files.get(name=uploaded.name)
                parts.append(uploaded)
        body = ANALYZE_PROMPT + ('' if small else '\n(NO AUDIO AVAILABLE - judge by Hebrew/Torah knowledge only.)')
        if briefing:
            body += '\n\nBRIEFING OF THE RECORDING:\n' + json.dumps(briefing, ensure_ascii=False)[:4000]
        for k, v in texts.items():
            body += f'\n\n===== TRANSCRIPT {k} =====\n{v}'
        resp = client.models.generate_content(
            model=MODEL, contents=[body] + parts,
            config=gtypes.GenerateContentConfig(
                response_mime_type='application/json', response_schema=ANALYZE_SCHEMA,
                thinking_config=gtypes.ThinkingConfig(thinking_level='medium')))
        d = json.loads(resp.text or '{}')
        d['with_audio'] = bool(small)
        return d
    finally:
        if small:
            try:
                os.remove(small)
            except OSError:
                pass
        if uploaded is not None:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass
