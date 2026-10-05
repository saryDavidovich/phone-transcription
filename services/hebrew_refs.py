"""כלי עזר להכתבה תורנית: ראשי תיבות ומראי מקומות.

הבעיה: בכתבים תורניים יש הרבה ראשי תיבות ומראי מקומות ("שו"ע או"ח סי' קנ"ד
סעי' י'", "רי"ף בכורות ל"ז ע"ב", "סקקל"ד") שאי אפשר להגות כמילה, ומנוע
התמלול לא יכול לנחש את האיות שלהם. הפתרון: הדובר לוחץ בסטודיו על כפתור
"איות" או "מספר" ואומר שמות אותיות / מספר רגיל, והקוד (לא המנוע) הופך את זה
לכתב, באופן קבוע ומדויק:

  * איות:  "שין וו עין"               -> שו"ע   (גרשיים לפני האות האחרונה)
           "סמך יוד גרש"                -> סי'    (גרש מפורש = קיצור, בלי גרשיים)
           "וו בית מם בית"              -> ובמ"ב
           "שין עין רווח אלף ..."       -> "רווח" מפריד בין מילים באותו קטע
  * מספר:  "מאתיים חמישים ושלוש"        -> רנ"ג   (גימטריה, 15=ט"ו, 16=ט"ז)

המנוע נדרש רק לזהות שמות אותיות / מספרים - אוצר מילים סגור וחד-משמעי.
(הגרשיים והגרש בפלט הם התווים העבריים ״ ׳ )
"""
import difflib
import re

GERESH = '׳'      # ׳
GERSHAYIM = '״'   # ״

# שם אות (מנורמל, בלי ניקוד/גרשיים) -> אות. כולל כתיבים נפוצים שמנועי תמלול
# מחזירים לשמות האותיות.
_LETTER_NAMES = {
    'א': ['אלף', 'אלפ', 'אלפא', 'אלפה'],
    'ב': ['בית', 'בט', 'ביית', 'בת'],
    'ג': ['גימל', 'גמל', 'גימאל', 'גימל'],
    'ד': ['דלת', 'דלט', 'דאלת', 'דלד'],
    'ה': ['הא', 'הה', 'האה', 'אי', 'איי', 'אה', 'האא', 'האי', 'ay', 'ah', 'ha', 'haa', 'a'],
    'ו': ['וו', 'ואו', 'ווא', 'ווו'],
    'ז': ['זין', 'זיין', 'זן'],
    'ח': ['חית', 'חט', 'חיט', 'חיית', 'חד', 'חת', 'חיד'],
    'ט': ['טית', 'טט', 'טיט', 'תת', 'תית', 'טת', 'טד'],
    'י': ['יוד', 'יאוד', 'יוד'],
    'כ': ['כף', 'כאף', 'כפ', 'כפה', 'חף', 'חפ', 'חאף', 'כב'],
    'ל': ['למד', 'לאמד', 'למאד', 'למדה'],
    'מ': ['מם', 'מאם', 'מימ'],
    'נ': ['נון', 'נוון'],
    'ס': ['סמך', 'סמכ', 'סמק'],
    'ע': ['עין', 'עיין', 'אין', 'אן', 'עיןן'],
    'פ': ['פא', 'פה', 'פאה', 'פי', 'פיי', 'pe', 'pa'],
    'צ': ['צדי', 'צדיק', 'צאדי', 'צדיי', 'צדה'],
    'ק': ['קוף', 'קופ', 'קוב', 'קף', 'כוף'],
    'ר': ['ריש', 'רש', 'ראש'],
    'ש': ['שין', 'שיין', 'שן'],
    'ת': ['תו', 'תיו', 'תאו'],
}
_NAME_TO_LETTER = {}
for _l, _names in _LETTER_NAMES.items():
    for _n in _names:
        _NAME_TO_LETTER[_n] = _l

# צ' נשארת רגילה גם בסוף ראשי תיבות (שעה"צ, הגה"צ) - כך נהוג וכך כתב המשתמש; כ/מ/נ/פ סופיות (רמב"ם, רי"ף)
_FINAL = {'כ': 'ך', 'מ': 'ם', 'נ': 'ן', 'פ': 'ף'}
_NONFINAL = {v: k for k, v in _FINAL.items()}
_NONFINAL['ץ'] = 'צ'

_WORD_BREAK = {'רווח', 'רווחים', 'רוח', 'רווחה', 'מילה', 'הבא', 'הבאה', 'נקודה'}
_IGNORED = {'סופית', 'סופי', 'סופיות', 'אות', 'האות', 'ואז'}
_TOK_GERESH = {'גרש', 'גרשים', 'גרשה', 'גרש.'}
_TOK_GERSHAYIM = {'גרשיים', 'גרשיים', 'גרשיים'}

_NIQQUD_RE = re.compile('[֑-ׇ]')
_STRIP_RE = re.compile('["\'`׳״’“”־,.;:!?()\\[\\]‏‎\\-]')
_HEB_LETTER_RE = re.compile('^[א-ת]$')


def _norm(tok):
    return _STRIP_RE.sub('', _NIQQUD_RE.sub('', tok)).strip()


def _split_tokens(text):
    # מפרידים לפי רווחים/פסיקים/מקפים; שומרים על "ש"ע" כטוקן אחד (גרשיים בתוכו)
    return [t for t in re.split(r'[\s,;/|]+', text or '') if t]


def _letter_for_token(raw):
    """מחזיר (kind, value): ('letters', 'שוע') / ('break', None) / ('geresh', None)
    / ('gershayim', None) / ('ignore', None) / ('unknown', raw)."""
    n = _norm(raw)
    if re.fullmatch('[A-Za-z]+', n):
        n = n.lower()
    if not n:
        return 'ignore', None
    if n in _WORD_BREAK:
        return 'break', None
    if n in _TOK_GERSHAYIM:
        return 'gershayim', None
    if n in _TOK_GERESH:
        return 'geresh', None
    if n in _IGNORED:
        return 'ignore', None
    if n in _NAME_TO_LETTER:
        return 'letters', _NAME_TO_LETTER[n]
    # המנוע החזיר כבר אות בודדת ("ש") או ראשי תיבות ("ש"ע" / "שוע")
    if _HEB_LETTER_RE.match(n):
        return 'letters', _NONFINAL.get(n, n)
    if re.search('["״]', raw) and re.fullmatch('[א-ת]+', n):
        return 'letters', ''.join(_NONFINAL.get(c, c) for c in n)
    # התאמה מקורבת לשם אות
    close = difflib.get_close_matches(n, list(_NAME_TO_LETTER.keys()), n=1, cutoff=0.78)
    if close:
        return 'letters', _NAME_TO_LETTER[close[0]]
    return 'unknown', raw


def _format_word(letters, explicit_geresh, explicit_gershayim_at):
    """letters: מחרוזת אותיות. explicit_geresh: גרש בסוף. explicit_gershayim_at:
    אינדקס (מספר אותיות לפני הגרשיים) או None."""
    if not letters:
        return ''
    chars = [_NONFINAL.get(c, c) for c in letters]
    if chars:
        chars[-1] = _FINAL.get(chars[-1], chars[-1])
    if explicit_gershayim_at is not None and 0 < explicit_gershayim_at < len(chars):
        # גרשיים מפורשים באמצע; אות סופית רק בסוף המילה
        return ''.join(chars[:explicit_gershayim_at]) + GERSHAYIM + ''.join(chars[explicit_gershayim_at:])
    if explicit_geresh:
        return ''.join(chars) + GERESH
    if len(chars) == 1:
        return chars[0] + GERESH
    return ''.join(chars[:-1]) + GERSHAYIM + chars[-1]


def spelling_to_text(text):
    """הופך תמלול של איות אותיות (שמות אותיות) לראשי תיבות. מחזיר מחרוזת עם
    רווחים בין מילים. טוקן לא מזוהה נשמר כמו שהוא (העורך יראה ויתקן)."""
    words = []
    cur = []              # אותיות במילה הנוכחית
    state = {'geresh': False, 'gi': None}

    def flush():
        if cur or state['geresh']:
            words.append(_format_word(''.join(cur), state['geresh'], state['gi']))
        cur.clear()
        state['geresh'] = False
        state['gi'] = None

    for raw in _split_tokens(text):
        kind, val = _letter_for_token(raw)
        if kind == 'letters':
            cur.extend(list(val))
        elif kind == 'break':
            flush()
        elif kind == 'geresh':
            state['geresh'] = True
            flush()
        elif kind == 'gershayim':
            state['gi'] = len(cur)
        elif kind == 'unknown':
            flush()
            # לא מזוהה - מסמנים במפורש כדי שהנציג יראה ויתקן בעורך (במקום מילה
            # "שנראית תקינה" ששורדת בשקט עד ללקוח)
            words.append('[?' + _norm(val) + '?]')
        # ignore - מדלגים
    flush()
    return ' '.join(w for w in words if w)


# ---------------------------------------------------------------- מספרים
_UNITS = {
    'אחד': 1, 'אחת': 1, 'שניים': 2, 'שנים': 2, 'שתיים': 2, 'שתים': 2, 'שני': 2, 'שתי': 2,
    'שלושה': 3, 'שלוש': 3, 'ארבעה': 4, 'ארבע': 4, 'חמישה': 5, 'חמש': 5,
    'שישה': 6, 'שש': 6, 'שבעה': 7, 'שבע': 7, 'שמונה': 8, 'תשעה': 9, 'תשע': 9,
    'עשרה': 10, 'עשר': 10,
}
_TENS = {'עשרים': 20, 'שלושים': 30, 'ארבעים': 40, 'חמישים': 50, 'שישים': 60,
         'שבעים': 70, 'שמונים': 80, 'תשעים': 90}
_HUNDRED_WORDS = {'מאה', 'מאות'}
_SPECIAL = {'מאתיים': 200, 'מאתים': 200}


def _num_norm(tok):
    t = _norm(tok)
    return t


def words_to_int(text):
    """'מאתיים חמישים ושלוש' / '253' -> 253. None אם לא ניתן לפענח."""
    toks = [_num_norm(t) for t in _split_tokens(text)]
    toks = [t for t in toks if t]
    if not toks:
        return None
    if len(toks) == 1 and toks[0].isdigit():
        return int(toks[0])
    total = 0
    current = 0
    seen = False
    for t in toks:
        base = t
        if base not in _UNITS and base not in _TENS and base not in _HUNDRED_WORDS \
                and base not in _SPECIAL and base.startswith('ו') and base[1:]:
            base = base[1:]
        if base in _SPECIAL:
            total += current
            current = _SPECIAL[base]
        elif base in _HUNDRED_WORDS:
            current = (current or 1) * 100
        elif base in _TENS:
            current += _TENS[base]
        elif base in _UNITS:
            current += _UNITS[base]
        elif base.isdigit():
            current += int(base)
        else:
            return None
        seen = True
    total += current
    return total if seen else None


_ONES = ['', 'א', 'ב', 'ג', 'ד', 'ה', 'ו', 'ז', 'ח', 'ט']
_TENS_L = ['', 'י', 'כ', 'ל', 'מ', 'נ', 'ס', 'ע', 'פ', 'צ']
_HUNDREDS_L = ['', 'ק', 'ר', 'ש', 'ת']


def int_to_gematria(n):
    """1..999 -> רנ"ג; 15->ט"ו, 16->ט"ז; אות בודדת מקבלת גרש (י'). מחוץ לטווח - None."""
    if not isinstance(n, int) or n < 1 or n > 999:
        return None
    letters = ''
    while n >= 400:
        letters += 'ת'
        n -= 400
    letters += _HUNDREDS_L[n // 100]
    n %= 100
    if n in (15, 16):
        letters += 'ט' + ('ו' if n == 15 else 'ז')
    else:
        letters += _TENS_L[n // 10] + _ONES[n % 10]
    letters = ''.join(_NONFINAL.get(c, c) for c in letters)
    if len(letters) == 1:
        return letters + GERESH
    return letters[:-1] + GERSHAYIM + letters[-1]


def number_to_text(text):
    """תמלול של מספר (במילים או בספרות; כמה מספרים מופרדים ב'רווח') -> גימטריה.
    אם אי אפשר להמיר (למשל מעל 999) - מחזיר את התמלול המקורי כמו שהוא."""
    if not (text or '').strip():
        return ''
    # מספרים מרובים: מופרדים במילה "רווח" או בפסיק/נקודה-פסיק
    parts = re.split(r'\bרווח\b|[,;]', text)
    out = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        n = words_to_int(part)
        g = int_to_gematria(n) if n is not None else None
        out.append(g if g else part)
    return ' '.join(out)


def apply_mode(text, mode):
    """נקודת הכניסה מה-worker: mode ריק => הטקסט כמו שהוא."""
    if mode == 'spell':
        return spelling_to_text(text)
    if mode == 'number':
        return number_to_text(text)
    return restore_known_abbreviations(text)


# ------------------------------------------------------------------
# ראשי תיבות מוכרים שהמנוע כותב בלי גרשיים ("רשי", "הרמבם") - מחזירים את
# הגרשיים בדיבור רגיל. רשימה שמרנית בכוונה: רק צורות שלא משמשות כמילה
# רגילה. מותר קידומת דקדוקית אחת (ה/ו/ל/ב/מ/כ/ש/וה/ול...) לפני הצורה.
# ------------------------------------------------------------------
_KNOWN_ABBREVIATIONS = [
    'רשי', 'רמבם', 'רמבן', 'ריטבא', 'רשבא', 'רשבם', 'ריבש', 'מהרשא', 'מהרם',
    'מהרל', 'מהרשל', 'ראבד', 'הגראנט', 'שועהרב', 'חידא', 'מהריל', 'מהרימט',
]
_PREFIXES = ['', 'ה', 'ו', 'ל', 'ב', 'מ', 'כ', 'ש', 'וה', 'ול', 'וב', 'ומ', 'וכ', 'וש', 'שה', 'כש', 'לה', 'מה', 'בה']
_ABBR_MAP = {}
for _a in _KNOWN_ABBREVIATIONS:
    _ABBR_MAP[_a] = _a[:-1] + GERSHAYIM + _a[-1]
_ABBR_RE = re.compile(
    '(?<![\u05d0-\u05ea])(' + '|'.join(sorted(_PREFIXES, key=len, reverse=True) if False else [p for p in sorted(_PREFIXES, key=len, reverse=True) if p]) + ')?('
    + '|'.join(sorted(_KNOWN_ABBREVIATIONS, key=len, reverse=True)) + ')(?![\u05d0-\u05ea"\u05f4\'])'
)


def restore_known_abbreviations(text):
    """רשי -> רש״י, והרמבם -> הרמב״ם (מילה שלמה בלבד, בלי גרשיים קיימים)."""
    if not text:
        return text
    return _ABBR_RE.sub(lambda m: (m.group(1) or '') + _ABBR_MAP[m.group(2)], text)


# ------------------------------------------------------------------
# גודל כתב בסוגריים: טקסט בין "(" ל-")" תואמים מוצג מעט קטן יותר.
# "(" בלי ")" מתאים, או ")" בלי "(" (למשל רשימה "1) 2)") - נשארים בכתב רגיל,
# כך שטעות בסוגריים לא "גוררת" את שאר המסמך לכתב קטן. ההתאמה היא בתוך פסקה
# אחת בלבד, ומחושבת על טקסט הפסקה כולו (גם אם הסוגריים חוצים ריצות עיצוב).
# הלוגיקה זהה לפונקציה parenMask ב-dictate_studio.html / proof_studio.html.
# ------------------------------------------------------------------
def paren_mask(text):
    """רשימת בוליאנים באורך הטקסט: True = התו בתוך זוג סוגריים עגולים תואם
    (כולל הסוגריים עצמם)."""
    mask = [False] * len(text or '')
    stack = []
    for i, ch in enumerate(text or ''):
        if ch == '(':
            stack.append(i)
        elif ch == ')' and stack:
            start = stack.pop()
            for k in range(start, i + 1):
                mask[k] = True
    return mask
