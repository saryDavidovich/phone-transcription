"""
יצירת דף להקראה (ManuscriptPage) מקבצים שהמנהל מעלה ידנית, או מקבצים שהגיעו
מצורפים להודעה (מייל בלי נושא תקין שנכנס כהודעה למנהל).

כמה קבצים (או קובץ ZIP של תיקייה) מאוחדים לקובץ PDF אחד, לפי סדר השמות:
מספרים לפי הערך (2 לפני 10), ואותיות לפי סדר הא-ב. תמונות הופכות לעמוד PDF
(כולל סיבוב לפי הטלפון שצילם), ו-PDF נכנס עם כל העמודים שלו.

הדף נכנס לתור ההקראה הרגיל (status='pending') - בדיוק כמו כתב יד שנשלח במייל,
והחיוב הוא כרגיל, בעת שליחת המסמך המוכן ללקוח (routes/dictate.py).
"""
import io
import os
import re
import zipfile
import logging

log = logging.getLogger(__name__)

IMAGE_EXT = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'tif', 'tiff', 'heic', 'heif'}
ACCEPT_EXT = IMAGE_EXT | {'pdf', 'zip'}
MAX_FILE_BYTES = 40 * 1024 * 1024
MAX_TOTAL_BYTES = 150 * 1024 * 1024
MAX_PAGES = 400


def ext_of(name):
    return os.path.splitext(name or '')[1].lstrip('.').lower()


def natural_key(name):
    """'דף 2.jpg' לפני 'דף 10.jpg'; אותיות לפי סדר הא-ב."""
    parts = re.split(r'(\d+)', (name or '').lower())
    return [(0, int(p), '') if p.isdigit() else (1, 0, p) for p in parts]


def expand(files):
    """files: [(שם, bytes)]. פותח ZIP (תיקייה דחוסה) ומשאיר רק תמונות ו-PDF.
    מחזיר [(שם, bytes)] ממוין לפי natural_key, ורשימת קבצים שדולגו."""
    out, skipped, total = [], [], 0
    for name, data in files:
        e = ext_of(name)
        if e == 'zip':
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for info in z.infolist():
                        inner = info.filename
                        base = os.path.basename(inner)
                        if info.is_dir() or not base or base.startswith('.') or '__MACOSX' in inner:
                            continue
                        if ext_of(base) not in IMAGE_EXT | {'pdf'}:
                            skipped.append(inner)
                            continue
                        if info.file_size > MAX_FILE_BYTES:
                            skipped.append(f'{inner} (גדול מדי)')
                            continue
                        total += info.file_size
                        if total > MAX_TOTAL_BYTES:
                            raise ValueError('הקבצים גדולים מדי יחד (מעל 150MB)')
                        out.append((inner, z.read(info)))
            except zipfile.BadZipFile:
                skipped.append(f'{name} (קובץ ZIP פגום)')
            continue
        if e not in IMAGE_EXT | {'pdf'}:
            skipped.append(name)
            continue
        if len(data) > MAX_FILE_BYTES:
            skipped.append(f'{name} (גדול מדי)')
            continue
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ValueError('הקבצים גדולים מדי יחד (מעל 150MB)')
        out.append((name, data))
    out.sort(key=lambda f: natural_key(f[0]))
    return out, skipped


def _image_to_pdf_bytes(data):
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    buf = io.BytesIO()
    img.save(buf, format='PDF', resolution=150)
    return buf.getvalue()


def merge_to_pdf(files):
    """files: [(שם, bytes)] כבר ממוינים. מחזיר (pdf_bytes, מספר עמודים, דולגו)."""
    import fitz
    doc = fitz.open()
    skipped = []
    for name, data in files:
        try:
            pdf = data if data[:5] == b'%PDF-' else _image_to_pdf_bytes(data)
            src = fitz.open(stream=pdf, filetype='pdf')
            doc.insert_pdf(src)
            src.close()
        except Exception as e:
            log.warning(f'manuscript merge: דילוג על {name}: {e}')
            skipped.append(f'{name} (לא נפתח)')
        if doc.page_count > MAX_PAGES:
            raise ValueError(f'יותר מ-{MAX_PAGES} עמודים')
    if doc.page_count == 0:
        raise ValueError('אף קובץ לא הכיל תמונה או PDF תקינים')
    out = doc.tobytes(garbage=3, deflate=True)
    pages = doc.page_count
    doc.close()
    return out, pages, skipped


def create_manuscript_page(customer, files, label=None):
    """files: [(שם, bytes)]. יוצר ManuscriptPage אחד (קובץ PDF מאוחד) בתור ההקראה.
    מחזיר (page, תיאור קצר להודעה למנהל). זורק ValueError עם הסבר בעברית."""
    from app import db
    from models import ManuscriptPage
    good, skipped = expand(files)
    if not good:
        raise ValueError('לא נמצאו תמונות או קובצי PDF' + (f' (דולגו: {", ".join(skipped[:5])})' if skipped else ''))
    pdf, pages, bad = merge_to_pdf(good)
    skipped += bad
    first = os.path.splitext(os.path.basename(good[0][0]))[0]
    name = (label or (first if len(good) == 1 else f'{first} ועוד {len(good) - 1} קבצים')).strip()[:200]
    if not name.lower().endswith('.pdf'):
        name += '.pdf'
    page = ManuscriptPage(customer_id=customer.id, original_filename=name, file_data=pdf, status='pending')
    db.session.add(page)
    db.session.commit()
    order = ', '.join(os.path.basename(n) for n, _ in good[:8]) + (' ...' if len(good) > 8 else '')
    note = f'{len(good)} קבצים אוחדו לקובץ אחד ({pages} עמודים), לפי הסדר: {order}'
    if skipped:
        note += f'. דולגו: {", ".join(skipped[:6])}'
    log.info(f'manuscript page {page.id} created for customer {customer.id}: {note}')
    return page, note
