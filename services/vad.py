"""
זיהוי דיבור (VAD) לפני התמלול - Silero VAD (מודל ONNX קטן, ~2MB, רישיון MIT, רץ על המעבד של השרת).

למה: Gemini הוא מודל שפה. כשהוא מקבל שקט / רעש / קו גרוע הוא "ממשיך את השיחה" וממציא טקסט
(הקלטה 1817: 5 דקות כמעט בלי דיבור קיבלו מאות מילים מומצאות). לכן:
1. speech_segments() - רשימת קטעי הדיבור בהקלטה (שניות התחלה/סוף).
2. plan_chunks()     - מחלק לחלקים של עד 5 דקות דיבור, וחותך תמיד בהפסקה שבין שני קטעים
                       (לא באמצע מילה, כמו בחיתוך הקבוע כל 300 שניות).
3. iter_speech_chunks() - בונה לכל חלק WAV של הדיבור בלבד, עם חצי שנייה שקט בין קטע לקטע.
הקובץ המקורי לא משתנה (נגן, הורדה, חיוב הלקוח לפי האורך המקורי).
כל כשל (אין onnxruntime, קובץ לא 16kHz מונו) - None, והתמלול ממשיך בשיטה הרגילה.
"""
import io
import logging
import os
import time
import wave

log = logging.getLogger(__name__)

MODEL_PATH = os.path.join(os.path.dirname(__file__), '..', 'assets', 'models', 'silero_vad.onnx')
SR = 16000
WIN = 512                 # 32ms
CTX = 64                  # Silero v5: 64 דגימות הקשר לפני כל חלון
THRESHOLD = 0.25          # שמרני: דיבור שקט ברקע נשאר
NEG_THRESHOLD = 0.15
MIN_SPEECH_MS = 250
MIN_SILENCE_MS = 300      # שקט קצר מזה - אותו קטע
MERGE_GAP_S = 3.0         # רק הפסקה של 3 שניות ומעלה מוסרת
PAD_S = 0.5               # מרווח לפני ואחרי כל דיבור
JOIN_SILENCE_S = 0.5      # שקט שמוכנס בין קטעים בקובץ המקוצר

_session = None


def _sess():
    global _session
    if _session is None:
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.inter_op_num_threads = 1
        so.intra_op_num_threads = 1
        _session = ort.InferenceSession(os.path.abspath(MODEL_PATH), sess_options=so,
                                        providers=['CPUExecutionProvider'])
    return _session


def speech_segments(wav_path):
    """[(start, end)] בשניות, ומשך הקובץ. WAV שאינו 16kHz מונו 16bit - None."""
    import numpy as np
    t0 = time.time()
    with wave.open(wav_path, 'rb') as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (SR, 1, 2):
            return None
        total = w.getnframes() / SR
        sess = _sess()
        state = np.zeros((2, 1, 128), dtype=np.float32)
        ctx = np.zeros((1, CTX), dtype=np.float32)
        sr = np.array(SR, dtype=np.int64)
        probs = []
        block = WIN * 600                                   # ~19 שניות בכל קריאה מהדיסק
        while True:
            raw = w.readframes(block)
            if not raw:
                break
            a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            if len(a) % WIN:
                a = np.concatenate([a, np.zeros(WIN - len(a) % WIN, dtype=np.float32)])
            for i in range(0, len(a), WIN):
                x = np.concatenate([ctx, a[i:i + WIN][None, :]], axis=1)
                out, state = sess.run(None, {'input': x, 'state': state, 'sr': sr})
                ctx = x[:, -CTX:]
                probs.append(float(out[0][0]))
    frame = WIN / SR
    segs, start, silence_since = [], None, None
    for i, p in enumerate(probs):
        t = i * frame
        if p >= THRESHOLD:
            silence_since = None
            if start is None:
                start = t
        elif start is not None and p < NEG_THRESHOLD:
            if silence_since is None:
                silence_since = t
            if (t - silence_since) * 1000 >= MIN_SILENCE_MS:
                if (silence_since - start) * 1000 >= MIN_SPEECH_MS:
                    segs.append([start, silence_since])
                start, silence_since = None, None
    if start is not None and (len(probs) * frame - start) * 1000 >= MIN_SPEECH_MS:
        segs.append([start, len(probs) * frame])
    # מרווחים, ואיחוד הפסקות קצרות
    merged = []
    for s, e in segs:
        s, e = max(0.0, s - PAD_S), min(total, e + PAD_S)
        if merged and s - merged[-1][1] < MERGE_GAP_S:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    speech = sum(e - s for s, e in merged)
    log.info(f'vad: {len(merged)} speech segments, {speech:.0f}s speech of {total:.0f}s '
             f'({time.time() - t0:.1f}s)')
    return [(round(s, 3), round(e, 3)) for s, e in merged], total


def plan_chunks(segments, max_seconds=300):
    """חלוקה לחלקים של עד max_seconds דיבור (כולל השקט שמוכנס), חיתוך רק בין קטעים.
    קטע דיבור רציף ארוך מ-max_seconds - נחתך בכוח (נדיר)."""
    pieces = []
    for s, e in segments:
        while e - s > max_seconds:
            pieces.append((s, s + max_seconds))
            s += max_seconds
        pieces.append((s, e))
    chunks, cur, cur_len = [], [], 0.0
    for s, e in pieces:
        add = (e - s) + (JOIN_SILENCE_S if cur else 0)
        if cur and cur_len + add > max_seconds:
            chunks.append(cur)
            cur, cur_len = [], 0.0
            add = e - s
        cur.append((s, e))
        cur_len += add
    if cur:
        chunks.append(cur)
    return chunks


def chunk_seconds(chunk):
    return sum(e - s for s, e in chunk) + JOIN_SILENCE_S * max(0, len(chunk) - 1)


def iter_speech_chunks(wav_path, plan):
    """לכל חלק בתוכנית: WAV (bytes) של הדיבור בלבד."""
    gap = b'\x00\x00' * int(SR * JOIN_SILENCE_S)
    with wave.open(wav_path, 'rb') as w:
        for chunk in plan:
            buf = io.BytesIO()
            with wave.open(buf, 'wb') as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(SR)
                for j, (s, e) in enumerate(chunk):
                    if j:
                        out.writeframes(gap)
                    w.setpos(int(s * SR))
                    out.writeframes(w.readframes(int((e - s) * SR)))
            yield buf.getvalue()


def plan_for(wav_path, max_seconds=300):
    """(plan, speech_seconds, total_seconds) או None כשאין זיהוי דיבור / ההגדרה כבויה / כשל."""
    try:
        from models import Settings
        row = Settings.query.filter_by(key='vad_enabled').first()
        if row and row.value == '0':
            return None
    except Exception:
        pass
    try:
        res = speech_segments(wav_path)
    except Exception as e:
        log.warning(f'vad failed - transcribing without it: {e}')
        return None
    if not res:
        return None
    segs, total = res
    speech = sum(e - s for s, e in segs)
    return plan_chunks(segs, max_seconds), speech, total
