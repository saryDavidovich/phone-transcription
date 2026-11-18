"""
אזור המוסד - תמלולים, נגן, דוחות ועדכונים חיים:

* /institution/t/<kind>/<id>       דף תמלול: נגן שמע (מימות) + טקסט מסונכרן, עריכה, Word ממותג.
                                   kind = 'u' (תמלול שהמוסד העלה) / 'r' (הקלטה של תלמיד).
* /institution/audio/<kind>/<id>   הפניה (302) לקובץ השמע בימות - נטפרי חוסמת שמע מהאתר שלנו.
* /institution/transcripts         כל התמלולים: רשימה עם חיפוש, לוח שנה עברי, הורדה מרוכזת.
* /institution/reports             גרפים: שימוש שבועי, לפי קבוצה, תלמידים מובילים.
* /institution/pulse               "עכשיו במוסד" - אירועים אחרונים (לדשבורד ולהתראות בשולחן העבודה).
* /institution/settings/branding   לוגו ושם לכותרת קובצי ה-Word.
"""
import io
import os
import time
import hashlib
import tempfile
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo

from flask import Blueprint, render_template, request, jsonify, redirect, abort, flash, url_for
from flask_login import current_user
from sqlalchemy import or_, func
from sqlalchemy.orm import defer

from app import db
from models import InstitutionUpload, Recording, Customer, StudentGroup, InstitutionChatMessage
from routes.institution import institution_login_required

log = logging.getLogger(__name__)
institution_portal_bp = Blueprint('institution_portal', __name__)

IL = ZoneInfo('Asia/Jerusalem')
DONE_REC = ('completed', 'transcribed', 'delivered', 'sent', 'done')
BUSY_REC = ('processing', 'queued', 'transcribing', 'recording', 'pending')
MAX_EXPORT = 300


# ------------------------------------------------------------------ עזר
def _loc(dt):
    return dt.replace(tzinfo=timezone.utc).astimezone(IL) if dt else None


def _ts(dt):
    return dt.replace(tzinfo=timezone.utc).timestamp() if dt else 0


def _utc(d, end=False):
    """תאריך מקומי (ישראל) -> datetime UTC נאיבי (תחילת היום, או תחילת היום שאחריו)."""
    local = datetime(d.year, d.month, d.day, tzinfo=IL) + (timedelta(days=1) if end else timedelta())
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _parse_date(s):
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def _rec_state(r):
    if r.status in DONE_REC:          # לפי הסטטוס בלבד - בלי לטעון את הטקסט (רשימות ארוכות)
        return 'done'
    if r.status == 'pending_payment':
        return 'pending'
    if r.status in ('error', 'failed', 'cancelled', 'expired', 'too_short'):
        return 'error'
    return 'processing'


def _up_state(u):
    return {'done': 'done', 'pending_payment': 'pending', 'error': 'error'}.get(u.status, 'processing')


def _is_cust():
    """הקוד הזה משרת גם את אזור הלקוח הפרטי (/my, routes/customer_portal.py): שם רואים רק את
    ההקלטות של הלקוח עצמו - בלי העלאות מוסד, קבוצות, תלמידים או מיתוג."""
    return current_user.__class__.__name__ == 'Customer'


def _own():
    """תנאי הבעלות על הקלטות: של הלקוח עצמו, או של תלמידי המוסד."""
    return (Recording.customer_id == current_user.id) if _is_cust() else (Customer.institution_id == current_user.id)


def _P():
    return '/my' if _is_cust() else '/institution'


def _brand_owner():
    return None if _is_cust() else current_user


def _groups_q():
    return [] if _is_cust() else StudentGroup.query.filter_by(institution_id=current_user.id).order_by(StudentGroup.name).all()


def _students_q():
    if _is_cust():
        return Customer.query.filter(Customer.id == -1)
    return Customer.query.filter(Customer.institution_id == current_user.id)


def _get(kind, item_id):
    """(obj, student) - רק של המוסד המחובר."""
    if kind == 'u':
        if _is_cust():
            abort(404)
        obj = InstitutionUpload.query.filter_by(id=item_id, institution_id=current_user.id).first()
        if not obj:
            abort(404)
        return obj, None
    if kind == 'r':
        obj = Recording.query.options(defer(Recording.file_data), defer(Recording.excel_file)).get(item_id)
        student = Customer.query.get(obj.customer_id) if obj else None
        if not obj or not student:
            abort(404)
        if (student.id != current_user.id) if _is_cust() else (student.institution_id != current_user.id):
            abort(404)
        return obj, student
    abort(404)


def _title(kind, obj, student):
    if kind == 'u':
        return obj.original_filename or 'תמלול'
    if _is_cust():
        return obj.source_filename or 'שיחה מהטלפון'
    from routes.institution_students import recording_title
    return recording_title(obj, student)


def _meta(kind, obj, student):
    if kind == 'r':
        from routes.institution_students import recording_meta
        return recording_meta(obj, None if _is_cust() else student)
    from services import portal_docx as PD
    parts = [_loc(obj.created_at).strftime('%d/%m/%Y %H:%M') if obj.created_at else '']
    if obj.duration_seconds:
        parts.append(PD.minutes_label(obj.duration_seconds))
    parts.append('תמלול מקצועי' if obj.tier == 'premium' else 'תמלול רגיל')
    return ' · '.join(p for p in parts if p)


def _ser(kind, obj, student=None, groups=None, snippet=None):
    state = _up_state(obj) if kind == 'u' else _rec_state(obj)
    lt = _loc(obj.created_at)
    item = {
        'k': kind, 'id': obj.id,
        'title': _title(kind, obj, student),
        'who': '' if _is_cust() else ('המוסד' if kind == 'u' or (student and student.is_institution_self) else (student.name if student else '')),
        'sid': None if _is_cust() else (student.id if (student and not student.is_institution_self) else None),
        'group': (groups or {}).get(student.student_group_id) if student else None,
        'day': lt.strftime('%Y-%m-%d') if lt else '',
        'time': lt.strftime('%H:%M') if lt else '',
        'date': lt.strftime('%d/%m/%Y') if lt else '',
        'ts': _ts(obj.created_at),
        'min': max(1, round((obj.duration_seconds or 0) / 60)) if obj.duration_seconds else None,
        'tier': 'premium' if (obj.tier if kind == 'u' else obj.transcription_tier) == 'premium' else 'basic',
        'state': state,
        'edited': bool(obj.edited_at),
        'src': (obj.source or 'file') if kind == 'u' else ('phone' if not obj.source_filename else 'file'),
    }
    if state == 'done':
        item['view'] = f'/institution/t/{kind}/{obj.id}'
        item['word'] = f'/institution/t/{kind}/{obj.id}/download'
    if snippet:
        item['snip'] = snippet
    return item


def _snippet(text, q, width=70):
    if not text or not q:
        return None
    i = text.lower().find(q.lower())
    if i < 0:
        return None
    a, b = max(0, i - width), min(len(text), i + len(q) + width)
    return ('…' if a else '') + text[a:b].replace('\n', ' ') + ('…' if b < len(text) else '')


def _collect(args, with_text=False, limit=500):
    """רשימת תמלולים (הקלטות תלמידים + העלאות המוסד) לפי מסננים. מחזיר (items, total, objs)."""
    d_from, d_to = _parse_date(args.get('from')), _parse_date(args.get('to'))
    q = (args.get('q') or '').strip()
    group_id = args.get('group', type=int)
    student_id = args.get('student', type=int)
    scope = args.get('scope') or 'all'          # all / students / inst
    only_done = args.get('done') == '1'
    ids = set(x for x in (args.get('ids') or '').split(',') if x)

    groups = {g.id: g.name for g in _groups_q()}
    out = []
    if _is_cust():
        scope, group_id, student_id = 'students', None, None

    if scope in ('all', 'students') or student_id or group_id:
        rq = (db.session.query(Recording, Customer).join(Customer, Customer.id == Recording.customer_id)
              .filter(_own())
              .options(defer(Recording.file_data), defer(Recording.excel_file)))
        if not with_text and not q:
            rq = rq.options(defer(Recording.transcript), defer(Recording.transcript_original), defer(Recording.summary))
        if d_from:
            rq = rq.filter(Recording.created_at >= _utc(d_from))
        if d_to:
            rq = rq.filter(Recording.created_at < _utc(d_to, end=True))
        if student_id:
            rq = rq.filter(Customer.id == student_id)
        if group_id:
            rq = rq.filter(Customer.student_group_id == group_id)
        if scope == 'students' and not _is_cust():
            rq = rq.filter(Customer.is_institution_self.isnot(True))
        if only_done:
            rq = rq.filter(Recording.transcript.isnot(None), Recording.status != 'pending_payment')
        if q:
            like = f'%{q}%'
            rq = rq.filter(or_(Customer.name.ilike(like), Recording.source_filename.ilike(like),
                               Recording.transcript.ilike(like)))
        rq = rq.filter(Recording.status.notin_(('cancelled', 'too_short', 'recording')))
        for r, c in rq.order_by(Recording.created_at.desc()).limit(limit).all():
            if ids and f'r{r.id}' not in ids:
                continue
            out.append(('r', r, c))

    if scope in ('all', 'inst') and not student_id and not group_id and not _is_cust():
        uq = InstitutionUpload.query.filter_by(institution_id=current_user.id)
        if not with_text and not q:
            uq = uq.options(defer(InstitutionUpload.transcript), defer(InstitutionUpload.transcript_original))
        if d_from:
            uq = uq.filter(InstitutionUpload.created_at >= _utc(d_from))
        if d_to:
            uq = uq.filter(InstitutionUpload.created_at < _utc(d_to, end=True))
        if only_done:
            uq = uq.filter(InstitutionUpload.status == 'done')
        if q:
            like = f'%{q}%'
            uq = uq.filter(or_(InstitutionUpload.original_filename.ilike(like), InstitutionUpload.transcript.ilike(like)))
        for u in uq.order_by(InstitutionUpload.created_at.desc()).limit(limit).all():
            if ids and f'u{u.id}' not in ids:
                continue
            out.append(('u', u, None))

    out.sort(key=lambda t: t[1].created_at or datetime.min, reverse=True)
    total = len(out)
    out = out[:limit]
    items = [_ser(k, o, s, groups, _snippet(o.transcript, q) if q else None) for k, o, s in out]
    return items, total, out


# ------------------------------------------------------------------ דף תמלול + נגן
@institution_portal_bp.route('/institution/t/<kind>/<int:item_id>')
@institution_login_required
def transcript_page(kind, item_id):
    obj, student = _get(kind, item_id)
    state = _up_state(obj) if kind == 'u' else _rec_state(obj)
    if state != 'done' or not obj.transcript:
        flash('התמלול עדיין לא מוכן' if state == 'processing' else
              ('התמלול ממתין לטעינת יתרה' if state == 'pending' else 'אין תמלול להקלטה הזו'))
        return redirect(_P() + '/transcripts')
    if _is_cust():
        back = '/my/transcripts'
    else:
        back = (url_for('institution_students.student_detail', student_id=student.id)
                if student is not None and not student.is_institution_self else url_for('institution_portal.transcripts'))
    return render_template('institution/transcript.html', kind=kind, obj=obj, student=student,
                           title=_title(kind, obj, student), meta=_meta(kind, obj, student),
                           duration=obj.duration_seconds or 0, back=back,
                           can_revert=bool(obj.transcript_original))


@institution_portal_bp.route('/institution/t/<kind>/<int:item_id>/save', methods=['POST'])
@institution_login_required
def transcript_save(kind, item_id):
    obj, _student = _get(kind, item_id)
    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').replace('\r\n', '\n').strip()
    if not text:
        return jsonify({'error': 'אי אפשר לשמור תמלול ריק'}), 400
    if len(text) > 2_000_000:
        return jsonify({'error': 'הטקסט ארוך מדי'}), 400
    if text == (obj.transcript or '').strip():
        return jsonify({'ok': True, 'unchanged': True})
    if not obj.transcript_original:
        obj.transcript_original = obj.transcript
    obj.transcript = text
    obj.edited_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'canRevert': True, 'savedAt': _loc(obj.edited_at).strftime('%H:%M')})


@institution_portal_bp.route('/institution/t/<kind>/<int:item_id>/rename', methods=['POST'])
@institution_login_required
def transcript_rename(kind, item_id):
    obj, _student = _get(kind, item_id)
    name = ((request.get_json(silent=True) or {}).get('title') or '').strip()[:200]
    if not name:
        return jsonify({'error': 'יש להזין שם'}), 400
    if kind == 'u':
        obj.original_filename = name
    else:
        obj.source_filename = name
    db.session.commit()
    return jsonify({'ok': True, 'title': name})


@institution_portal_bp.route('/institution/t/<kind>/<int:item_id>/revert', methods=['POST'])
@institution_login_required
def transcript_revert(kind, item_id):
    obj, _student = _get(kind, item_id)
    if not obj.transcript_original:
        return jsonify({'error': 'אין גרסה מקורית לשחזור'}), 400
    obj.transcript = obj.transcript_original
    obj.transcript_original = None
    obj.edited_at = None
    db.session.commit()
    return jsonify({'ok': True, 'text': obj.transcript})


@institution_portal_bp.route('/institution/t/<kind>/<int:item_id>/download')
@institution_login_required
def transcript_download(kind, item_id):
    obj, student = _get(kind, item_id)
    state = _up_state(obj) if kind == 'u' else _rec_state(obj)
    if state != 'done' or not obj.transcript:
        return 'התמלול אינו זמין להורדה', 404
    from services import portal_docx as PD
    from routes.download_utils import render_data_uri_download_page
    title = _title(kind, obj, student)
    data = PD.single(_brand_owner(), title, obj.transcript, _meta(kind, obj, student))
    return render_data_uri_download_page(
        data, f'תמלול - {PD.safe_name(title)}.docx',
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')


# ------------------------------------------------------------------ שמע (מימות)
_pushing = set()
_push_lock = threading.Lock()


def _audio_target(kind, obj):
    """כתובת השמע בימות, או None. לא מעלה כלום."""
    from services import portal_media as PM
    if obj.play_url:
        return PM.play_url_for_path(obj.play_url)
    if kind == 'r' and obj.rec_url and not PM.is_local_url(obj.rec_url):
        from routes.recording_proxy import _best_yemot_url
        return _best_yemot_url(obj)
    return None


def _start_push(kind, obj):
    """קובץ שנמצא רק אצלנו (הקלטת מייל / העלאה ישנה) - מעלים לימות ברקע. True אם יש מה להעלות."""
    from services import portal_media as PM
    if kind != 'r' or not obj.rec_url or not PM.is_local_url(obj.rec_url):
        return False
    key = f'{kind}{obj.id}'
    with _push_lock:
        if key in _pushing:
            return True
        _pushing.add(key)
    from app import app as flask_app
    rec_id = obj.id

    def run():
        try:
            with flask_app.app_context():
                rec = Recording.query.options(defer(Recording.excel_file)).get(rec_id)
                path = PM.local_file_for_url(rec.rec_url)
                if not path and '/api/recordings-email/' in (rec.rec_url or ''):
                    from routes.admin import _recording_source_available
                    _recording_source_available(rec)          # משחזר לדיסק מה-DB
                    path = PM.local_file_for_url(rec.rec_url)
                if not path:
                    return
                play = PM.push_to_yemot(path, f'r{rec_id}_{uuid.uuid4().hex[:8]}')
                if play:
                    rec.play_url = play
                    db.session.commit()
        except Exception:
            log.exception(f'portal audio push {key} failed')
        finally:
            with _push_lock:
                _pushing.discard(key)
    threading.Thread(target=run, daemon=True).start()
    return True


@institution_portal_bp.route('/institution/t/<kind>/<int:item_id>/audio-state')
@institution_login_required
def audio_state(kind, item_id):
    obj, _student = _get(kind, item_id)
    if _audio_target(kind, obj):
        # בלי כתובת ימות לדפדפן: הכתובת נושאת מפתח הורדה של כל המערכת, ומי שמחזיק בו יכול היה
        # לנסות להוריד הקלטות של אחרים. השמע מגיע רק דרכנו (audio-chunk), אחרי בדיקת בעלות.
        return jsonify({'state': 'ready'})
    if _start_push(kind, obj):
        return jsonify({'state': 'preparing'})
    return jsonify({'state': 'none'})


# קפיצה בנגן: ימות לא תומכים בהורדה חלקית (Range), ולכן דפדפן שמנגן ישר משם לא יכול לקפוץ
# באמצע - כל הזזה מחזירה להתחלה. לכן הנגן מתחיל לנגן מימות מיד, ובמקביל מושך את הקובץ המלא
# דרכנו בחתיכות JSON (כמו ההורדות - נטפרי חוסמת קובצי שמע מהאתר שלנו אבל לא JSON), ומחליף
# לעותק המקומי בדפדפן - שם אפשר לקפוץ לכל מקום. אצלנו נשמר רק עותק MP3 זמני (נמחק אחרי כמה שעות).
AUDIO_CACHE = os.path.join(tempfile.gettempdir(), 'portal_audio_cache')
AUDIO_CHUNK = 1_500_000
_cache_locks = {}


def _cached_mp3(kind, obj):
    from services import portal_media as PM
    import requests
    target = _audio_target(kind, obj)
    if not target:
        return None
    os.makedirs(AUDIO_CACHE, exist_ok=True)
    tag = hashlib.sha1(target.split('token=')[-1].encode()).hexdigest()[:10]
    path = os.path.join(AUDIO_CACHE, f'{kind}{obj.id}_{tag}.mp3')
    if os.path.exists(path):
        os.utime(path)
        return path
    with _push_lock:
        lock = _cache_locks.setdefault(path, threading.Lock())
    with lock:
        if os.path.exists(path):
            return path
        now = time.time()
        for f in os.listdir(AUDIO_CACHE):          # ניקוי עותקים ישנים
            fp = os.path.join(AUDIO_CACHE, f)
            try:
                if now - os.path.getmtime(fp) > 6 * 3600:
                    os.remove(fp)
            except OSError:
                pass
        fd, raw = tempfile.mkstemp(dir=AUDIO_CACHE, suffix='.src')
        os.close(fd)
        try:
            with requests.get(target, stream=True, timeout=300, headers={'User-Agent': 'Mozilla/5.0'}) as r:
                r.raise_for_status()
                with open(raw, 'wb') as fh:
                    for chunk in r.iter_content(1024 * 1024):
                        fh.write(chunk)
            with open(raw, 'rb') as fh:
                head = fh.read(3)
            is_mp3 = head[:3] == b'ID3' or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)
            if is_mp3:
                os.replace(raw, path)
            else:
                mp3 = PM.to_mp3(raw, bitrate='32k')
                if not mp3:
                    return None
                os.replace(mp3, path)
            return path
        except Exception as e:
            log.warning(f'portal audio cache {kind}{obj.id} failed: {type(e).__name__}: {e}')
            return None
        finally:
            PM.remove_quietly(raw)


@institution_portal_bp.route('/institution/t/<kind>/<int:item_id>/audio-chunk/<int:n>')
@institution_login_required
def audio_chunk(kind, item_id, n):
    import base64
    obj, _student = _get(kind, item_id)
    path = _cached_mp3(kind, obj)
    if not path:
        return jsonify({'error': 'השמע לא זמין כרגע'}), 404
    size = os.path.getsize(path)
    total = max(1, -(-size // AUDIO_CHUNK))
    if n < 0 or n >= total:
        return jsonify({'error': 'אין חתיכה כזו'}), 404
    with open(path, 'rb') as fh:
        fh.seek(n * AUDIO_CHUNK)
        data = fh.read(AUDIO_CHUNK)
    resp = jsonify({'b64': base64.b64encode(data).decode(), 'n': n, 'total': total, 'size': size, 'mime': 'audio/mpeg'})
    resp.headers['Cache-Control'] = 'private, max-age=3600'
    return resp


# ------------------------------------------------------------------ כל התמלולים
@institution_portal_bp.route('/institution/transcripts')
@institution_login_required
def transcripts():
    groups = _groups_q()
    students = (_students_q().filter(Customer.is_institution_self.isnot(True))
                .with_entities(Customer.id, Customer.name).order_by(Customer.name).all())
    return render_template('institution/transcripts.html', groups=groups, students=students,
                           tab=request.args.get('tab') or 'list')


@institution_portal_bp.route('/institution/transcripts/data')
@institution_login_required
def transcripts_data():
    limit = min(request.args.get('limit', 300, type=int) or 300, 1000)
    items, total, _ = _collect(request.args, limit=limit)
    return jsonify({'items': items, 'total': total, 'limit': limit})


@institution_portal_bp.route('/institution/transcripts/export', methods=['POST'])
@institution_login_required
def transcripts_export():
    from services import portal_docx as PD
    from services import security as sec
    from routes.download_utils import render_data_uri_download_page
    rk = f'export:{current_user.get_id()}'
    if sec.hits_blocked(rk, 20, 3600):
        return 'יותר מדי הורדות מרוכזות בשעה האחרונה. נסו שוב מאוחר יותר', 429
    sec.hits_add(rk)
    args = request.form.copy()
    args['done'] = '1'
    _items, total, objs = _collect(args, with_text=True, limit=MAX_EXPORT)
    objs = [t for t in objs if t[1].transcript]
    if not objs:
        return 'לא נמצאו תמלולים מוכנים לפי הבחירה', 404
    fmt = request.form.get('format') or 'zip'
    d_from, d_to = request.form.get('from') or '', request.form.get('to') or ''
    period = ' - '.join(x for x in (d_from, d_to) if x)
    label = 'תמלולים'
    if request.form.get('student', type=int):
        s = _students_q().filter(Customer.id == request.form.get('student', type=int)).first()
        label += f' - {s.name}' if s and s.name else ''
    elif request.form.get('group', type=int) and not _is_cust():
        g = StudentGroup.query.filter_by(id=request.form.get('group', type=int), institution_id=current_user.id).first()
        label += f' - {g.name}' if g else ''
    if period:
        label += f' ({period})'
    if fmt == 'merged':
        data = PD.merged(_brand_owner(), [(_title(k, o, s), _meta(k, o, s), o.transcript) for k, o, s in reversed(objs)],
                         heading=label)
        return render_data_uri_download_page(
            data, f'{PD.safe_name(label)}.docx',
            mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
    entries = []
    for k, o, s in objs:
        lt = _loc(o.created_at)
        base = f"{lt.strftime('%Y-%m-%d') if lt else ''} {_title(k, o, s)}"
        if k == 'r' and s is not None and s.name and s.name not in base and not _is_cust():
            base += f' - {s.name}'
        entries.append((base, _title(k, o, s), _meta(k, o, s), o.transcript))
    return render_data_uri_download_page(PD.zip_of(_brand_owner(), entries), f'{PD.safe_name(label)}.zip',
                                         mimetype='application/zip')


# ------------------------------------------------------------------ דוחות וגרפים
@institution_portal_bp.route('/institution/reports')
@institution_login_required
def reports():
    groups = _groups_q()
    return render_template('institution/reports.html', groups=groups)


@institution_portal_bp.route('/institution/reports/data')
@institution_login_required
def reports_data():
    days = request.args.get('days', 90, type=int)
    days = days if days in (30, 90, 180, 365) else 90
    group_id = None if _is_cust() else request.args.get('group', type=int)
    now_l = datetime.now(IL)
    start_d = (now_l - timedelta(days=days - 1)).date()
    since = _utc(start_d)

    groups = {g.id: g.name for g in _groups_q()}
    rq = (db.session.query(Recording.created_at, Recording.duration_seconds, Recording.cost, Recording.transcription_tier,
                           Customer.id, Customer.name, Customer.student_group_id, Customer.is_institution_self)
          .join(Customer, Customer.id == Recording.customer_id)
          .filter(_own(), Recording.created_at >= since,
                  Recording.status.in_(DONE_REC + ('pending_payment',))))
    if group_id:
        rq = rq.filter(Customer.student_group_id == group_id)
    rows = rq.all()
    if _is_cust():          # לקוח פרטי: סדרה אחת, בלי קבוצות ותלמידים
        rows = [(a, b, c, d, e, f, g, True) for a, b, c, d, e, f, g, _h in rows]
    ups = [] if (group_id or _is_cust()) else (db.session.query(InstitutionUpload.created_at, InstitutionUpload.duration_seconds,
                                                InstitutionUpload.cost, InstitutionUpload.tier)
                               .filter(InstitutionUpload.institution_id == current_user.id,
                                       InstitutionUpload.created_at >= since,
                                       InstitutionUpload.status.in_(('done', 'pending_payment'))).all())

    # שבועות (ראשון-שבת) / חודשים לטווח ארוך
    monthly = days >= 180

    def bucket(dt):
        d = _loc(dt).date()
        if monthly:
            return d.strftime('%Y-%m')
        return (d - timedelta(days=(d.weekday() + 1) % 7)).strftime('%Y-%m-%d')

    keys = []
    d = start_d
    while d <= now_l.date():
        k = d.strftime('%Y-%m') if monthly else (d - timedelta(days=(d.weekday() + 1) % 7)).strftime('%Y-%m-%d')
        if k not in keys:
            keys.append(k)
        d += timedelta(days=1)
    series = {k: {'students': 0.0, 'inst': 0.0, 'count': 0} for k in keys}

    by_group, by_student = {}, {}
    tiers = {'basic': 0.0, 'premium': 0.0}
    hours = [0] * 24
    weekdays = [0] * 7
    tot_min = tot_cost = 0.0
    count = 0
    active = set()
    for created, dur, cost, tier, sid, sname, gid, is_self in rows:
        m = (dur or 0) / 60
        k = bucket(created)
        if k in series:
            series[k]['inst' if is_self else 'students'] += m
            series[k]['count'] += 1
        tiers['premium' if tier == 'premium' else 'basic'] += m
        lt = _loc(created)
        hours[lt.hour] += 1
        weekdays[(lt.weekday() + 1) % 7] += 1
        tot_min += m
        tot_cost += cost or 0
        count += 1
        if not is_self:
            active.add(sid)
            g = groups.get(gid) or 'ללא קבוצה'
            bg = by_group.setdefault(g, {'min': 0.0, 'count': 0, 'students': set()})
            bg['min'] += m
            bg['count'] += 1
            bg['students'].add(sid)
            bs = by_student.setdefault(sid, {'name': sname or f'#{sid}', 'min': 0.0, 'count': 0, 'cost': 0.0})
            bs['min'] += m
            bs['count'] += 1
            bs['cost'] += cost or 0
    for created, dur, cost, tier in ups:
        m = (dur or 0) / 60
        k = bucket(created)
        if k in series:
            series[k]['inst'] += m
            series[k]['count'] += 1
        tiers['premium' if tier == 'premium' else 'basic'] += m
        lt = _loc(created)
        hours[lt.hour] += 1
        weekdays[(lt.weekday() + 1) % 7] += 1
        tot_min += m
        tot_cost += cost or 0
        count += 1

    def label(k):
        if monthly:
            y, mo = k.split('-')
            return f'{int(mo)}/{y[2:]}'
        dd = datetime.strptime(k, '%Y-%m-%d')
        return f'{dd.day}/{dd.month}'

    return jsonify({
        'monthly': monthly,
        'series': [{'k': k, 'label': label(k), 'students': round(v['students'], 1), 'inst': round(v['inst'], 1),
                    'count': v['count']} for k, v in series.items()],
        'groups': sorted([{'name': g, 'min': round(v['min'], 1), 'count': v['count'], 'students': len(v['students'])}
                          for g, v in by_group.items()], key=lambda x: -x['min']),
        'top': sorted([{'id': sid, 'name': v['name'], 'min': round(v['min'], 1), 'count': v['count'],
                        'cost': round(v['cost'], 2)} for sid, v in by_student.items()], key=lambda x: -x['min'])[:10],
        'tiers': {k: round(v, 1) for k, v in tiers.items()},
        'hours': hours, 'weekdays': weekdays,
        'totals': {'minutes': round(tot_min), 'count': count, 'cost': round(tot_cost, 2), 'active': len(active)},
    })


# ------------------------------------------------------------------ עכשיו במוסד (פיד + התראות)
@institution_portal_bp.route('/institution/pulse')
@institution_login_required
def pulse():
    after = request.args.get('after', type=float)
    now = datetime.utcnow()
    since = datetime.utcfromtimestamp(after) if after else now - timedelta(days=3)
    since = max(since, now - timedelta(days=14))
    limit = 12 if not after else 30
    events = []
    if _is_cust():
        return _pulse_customer(now, since, limit)

    rq = (db.session.query(Recording.id, Recording.status, Recording.status_at, Recording.created_at,
                           Recording.source_filename, Recording.duration_seconds, Customer.id, Customer.name,
                           Customer.is_institution_self)
          .join(Customer, Customer.id == Recording.customer_id)
          .filter(Customer.institution_id == current_user.id,
                  func.coalesce(Recording.status_at, Recording.created_at) > since,
                  Recording.status.notin_(('recording', 'cancelled', 'too_short')))
          .order_by(func.coalesce(Recording.status_at, Recording.created_at).desc()).limit(limit))
    for rid, status, st_at, created, fname, dur, sid, sname, is_self in rq.all():
        t = st_at or created
        who = 'המוסד' if is_self else (sname or 'תלמיד')
        title = fname or 'הקלטה מהטלפון'
        if status in DONE_REC:
            ev = ('ready', f'התמלול של {who} מוכן', title, f'/institution/t/r/{rid}')
        elif status == 'pending_payment':
            ev = ('pending', f'ל{who} אין מספיק יתרה', f'{title} · יישלח אוטומטית אחרי זיכוי',
                  None if is_self else f'/institution/students/{sid}')
        elif status in ('error', 'failed'):
            ev = ('error', f'תמלול של {who} נכשל', title, None if is_self else f'/institution/students/{sid}')
        else:
            ev = ('new', f'הקלטה חדשה של {who}', f'{title} · בתמלול', None if is_self else f'/institution/students/{sid}')
        events.append({'id': f'r{rid}:{ev[0]}', 'type': ev[0], 'ts': _ts(t), 'title': ev[1], 'sub': ev[2], 'url': ev[3],
                       'min': max(1, round((dur or 0) / 60)) if dur else None})

    uq = (InstitutionUpload.query.filter(InstitutionUpload.institution_id == current_user.id,
                                         func.coalesce(InstitutionUpload.status_at, InstitutionUpload.created_at) > since)
          .options(defer(InstitutionUpload.transcript), defer(InstitutionUpload.transcript_original))
          .order_by(func.coalesce(InstitutionUpload.status_at, InstitutionUpload.created_at).desc()).limit(limit))
    for u in uq.all():
        t = u.status_at or u.created_at
        name = u.original_filename or 'קובץ'
        if u.status == 'done':
            ev = ('ready', 'תמלול של המוסד מוכן', name, f'/institution/t/u/{u.id}')
        elif u.status == 'pending_payment':
            ev = ('pending', 'תמלול ממתין לטעינת יתרה', name, '/institution/billing')
        elif u.status == 'error':
            ev = ('error', 'תמלול של המוסד נכשל', f'{name} · {u.error_message or ""}'.strip(' ·'), '/institution/transcribe')
        else:
            ev = ('new', 'קובץ חדש בתמלול', name, '/institution/transcribe')
        events.append({'id': f'u{u.id}:{ev[0]}', 'type': ev[0], 'ts': _ts(t), 'title': ev[1], 'sub': ev[2], 'url': ev[3],
                       'min': max(1, round((u.duration_seconds or 0) / 60)) if u.duration_seconds else None})

    for m in (InstitutionChatMessage.query.filter(InstitutionChatMessage.institution_id == current_user.id,
                                                  InstitutionChatMessage.direction == 'out',
                                                  InstitutionChatMessage.created_at > since)
              .order_by(InstitutionChatMessage.created_at.desc()).limit(5)):
        events.append({'id': f'c{m.id}', 'type': 'chat', 'ts': _ts(m.created_at), 'title': 'הודעה חדשה מהצוות',
                       'sub': (m.body or '')[:120], 'url': '/institution/contact'})

    events.sort(key=lambda e: -e['ts'])
    from services.institution_chat import unread_for_institution
    from services import institution_students_ops as ISO
    th = ISO.low_threshold(current_user)
    busy = (db.session.query(func.count(Recording.id)).join(Customer, Customer.id == Recording.customer_id)
            .filter(Customer.institution_id == current_user.id, Recording.status.in_(('processing', 'queued', 'transcribing')),
                    Recording.created_at > now - timedelta(days=2)).scalar() or 0) + \
        (InstitutionUpload.query.filter(InstitutionUpload.institution_id == current_user.id,
                                        InstitutionUpload.status == 'processing',
                                        InstitutionUpload.created_at > now - timedelta(days=2)).count())
    bal = round(current_user.balance or 0, 2)
    return jsonify({
        'now': _ts(now), 'events': events[:limit],
        'chat': unread_for_institution(current_user.id),
        'busy': busy, 'balance': bal, 'low': bal < max(th, 1),
    })


def customer_chat_unread(c):
    from models import CustomerMessage
    acc = c.account
    q = CustomerMessage.query.filter(CustomerMessage.customer_id == c.id, CustomerMessage.direction == 'out')
    if acc and acc.chat_seen_at:
        q = q.filter(CustomerMessage.created_at > acc.chat_seen_at)
    return q.count()


def _pulse_customer(now, since, limit):
    from models import CustomerMessage
    events = []
    rq = (Recording.query.filter(Recording.customer_id == current_user.id,
                                 func.coalesce(Recording.status_at, Recording.created_at) > since,
                                 Recording.status.notin_(('recording', 'cancelled', 'too_short')))
          .options(defer(Recording.file_data), defer(Recording.excel_file), defer(Recording.transcript),
                   defer(Recording.transcript_original), defer(Recording.summary))
          .order_by(func.coalesce(Recording.status_at, Recording.created_at).desc()).limit(limit))
    for r in rq.all():
        t = r.status_at or r.created_at
        title = r.source_filename or 'שיחה מהטלפון'
        if r.status in DONE_REC:
            ev = ('ready', 'התמלול מוכן', title, f'/my/t/r/{r.id}')
        elif r.status == 'pending_payment':
            ev = ('pending', 'ממתין לטעינת יתרה', f'{title} · יישלח אוטומטית אחרי הטעינה', '/my/billing')
        elif r.status in ('error', 'failed'):
            ev = ('error', 'התמלול נכשל', title, '/my/transcribe')
        else:
            ev = ('new', 'הקלטה חדשה בתמלול', title, '/my/transcribe')
        events.append({'id': f'r{r.id}:{ev[0]}', 'type': ev[0], 'ts': _ts(t), 'title': ev[1], 'sub': ev[2],
                       'url': ev[3], 'min': max(1, round((r.duration_seconds or 0) / 60)) if r.duration_seconds else None})
    for m in (CustomerMessage.query.filter(CustomerMessage.customer_id == current_user.id,
                                           CustomerMessage.direction == 'out', CustomerMessage.created_at > since)
              .order_by(CustomerMessage.created_at.desc()).limit(5)):
        events.append({'id': f'c{m.id}', 'type': 'chat', 'ts': _ts(m.created_at), 'title': 'הודעה חדשה מהצוות',
                       'sub': (m.body or '')[:120], 'url': '/my/contact'})
    events.sort(key=lambda e: -e['ts'])
    busy = Recording.query.filter(Recording.customer_id == current_user.id,
                                  Recording.status.in_(('processing', 'queued', 'transcribing')),
                                  Recording.created_at > now - timedelta(days=2)).count()
    bal = round(current_user.balance or 0, 2)
    return jsonify({'now': _ts(now), 'events': events[:limit], 'chat': customer_chat_unread(current_user),
                    'busy': busy, 'balance': bal, 'low': bal < 3})


# ------------------------------------------------------------------ מיתוג (לוגו + שם ל-Word)
@institution_portal_bp.route('/institution/settings/branding', methods=['POST'])
@institution_login_required
def branding():
    inst = current_user
    if request.form.get('remove_logo'):
        inst.logo_data = None
        inst.logo_mime = None
        db.session.commit()
        return jsonify({'ok': True, 'logo': None})
    inst.brand_title = (request.form.get('brand_title') or '').strip()[:200] or None
    inst.brand_subtitle = (request.form.get('brand_subtitle') or '').strip()[:200] or None
    f = request.files.get('logo')
    preview = None
    if f and f.filename:
        raw = f.read(6 * 1024 * 1024 + 1)
        if len(raw) > 6 * 1024 * 1024:
            return jsonify({'error': 'הקובץ גדול מדי (עד 6MB)'}), 400
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(raw))
            im.load()
            if im.mode not in ('RGB', 'RGBA'):
                im = im.convert('RGBA')
            im.thumbnail((600, 300))
            buf = io.BytesIO()
            im.save(buf, 'PNG', optimize=True)
            inst.logo_data = buf.getvalue()
            inst.logo_mime = 'image/png'
        except Exception:
            return jsonify({'error': 'הקובץ אינו תמונה תקינה (PNG / JPG)'}), 400
    db.session.commit()
    if inst.logo_mime:
        import base64
        preview = 'data:image/png;base64,' + base64.b64encode(inst.logo_data).decode()
    return jsonify({'ok': True, 'logo': preview})


@institution_portal_bp.route('/institution/settings/branding/sample/download')
@institution_login_required
def branding_sample():
    """קובץ Word לדוגמה - לראות איך הכותרת נראית."""
    from services import portal_docx as PD
    from routes.download_utils import render_data_uri_download_page
    data = PD.single(current_user, 'תמלול לדוגמה', 'כאן יופיע טקסט התמלול.\nכל פסקה בשורה נפרדת, מימין לשמאל.',
                     datetime.now(IL).strftime('%d/%m/%Y') + ' · 12 דקות · תמלול רגיל')
    return render_data_uri_download_page(
        data, 'דוגמת כותרת.docx',
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
