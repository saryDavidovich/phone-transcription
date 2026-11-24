"""
תמלול מקישור: יוטיוב וקול הלשון (בנוסף ל-Drive / ג'מבו שב-services/portal_media.py).

יוטיוב  - yt-dlp, שמע בלבד (bestaudio). פתרון "האתגרים" של יוטיוב דורש מנוע JS - node (nixpacks.toml).
קול הלשון - מזהה השיעור מהקישור (playShiur/<מספר>, FileName=<מספר>, או מספר של 6-8 ספרות),
           ואז: (1) קובץ ה-MP3 הציבורי mp3/NewArchive/<5 ספרות>/<8 ספרות>.mp3;
                (2) אותו קובץ דרך cloudscraper (האתר מאחורי Cloudflare);
                (3) כתובת הניגון של נגן האתר (api/files/GetPlayToken) - שמע, ואם אין - וידאו.
כל כשל - ValueError עם הסבר בעברית (מוצג ללקוח).
"""
import logging
import os
import random
import re
import string
import uuid

log = logging.getLogger(__name__)

MAX_SECONDS = 6 * 3600
MAX_BYTES = 1500 * 1024 * 1024
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/131.0.0.0 Safari/537.36')

_YT_RE = re.compile(r'^https?://(?:www\.|m\.|music\.)?(?:youtube\.com/(?:watch|shorts/|live/|embed/)|youtu\.be/)', re.I)
_KH_RE = re.compile(r'^https?://(?:www\.)?kolhalashon\.com/', re.I)
_KH_ID = re.compile(r'(?<!\d)(\d{6,8})(?!\d)')


def kind(url):
    u = (url or '').strip()
    if _YT_RE.match(u):
        return 'youtube'
    if _KH_RE.match(u):
        return 'kolhalashon'
    return None


LABELS = {'youtube': 'יוטיוב', 'kolhalashon': 'קול הלשון'}


# ------------------------------------------------------------------ יוטיוב
def fetch_youtube(url, dest_dir):
    try:
        import yt_dlp
    except ImportError:
        raise ValueError('הורדה מיוטיוב לא זמינה כרגע בשרת')
    os.makedirs(dest_dir, exist_ok=True)
    base = os.path.join(dest_dir, f'yt_{uuid.uuid4().hex}')
    opts = {
        'format': 'bestaudio/best', 'noplaylist': True, 'quiet': True, 'no_warnings': True,
        'outtmpl': base + '.%(ext)s', 'max_filesize': MAX_BYTES, 'socket_timeout': 30, 'retries': 3,
        'js_runtimes': {'node': {}, 'deno': {}},
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get('_type') == 'playlist':
                raise ValueError('זה קישור לפלייליסט - יש להדביק קישור לסרטון אחד')
            if info.get('is_live'):
                raise ValueError('אי אפשר לתמלל שידור חי שעוד לא הסתיים')
            if (info.get('duration') or 0) > MAX_SECONDS:
                raise ValueError('הסרטון ארוך מדי (מעל 6 שעות)')
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
    except ValueError:
        raise
    except Exception as e:
        msg = str(e)
        log.warning(f'youtube download failed: {msg[:300]}')
        if 'Sign in' in msg or 'bot' in msg.lower():
            raise ValueError('יוטיוב חסם את ההורדה מהשרת שלנו. אפשר להוריד את הקובץ ולהעלות אותו כקובץ')
        if 'Private' in msg or 'unavailable' in msg.lower():
            raise ValueError('הסרטון פרטי או לא זמין')
        raise ValueError('ההורדה מיוטיוב נכשלה - נסו שוב, או הורידו את הקובץ והעלו אותו')
    if not os.path.exists(path):
        cands = [os.path.join(dest_dir, f) for f in os.listdir(dest_dir) if f.startswith(os.path.basename(base))]
        path = cands[0] if cands else None
    if not path or os.path.getsize(path) < 1000:
        raise ValueError('ההורדה מיוטיוב נכשלה')
    return path, (info.get('title') or 'סרטון יוטיוב')[:200]


# ------------------------------------------------------------------ קול הלשון
def kol_halashon_id(url):
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(url).query)
    for key in ('FileName', 'filename', 'fileId', 'FileID'):
        v = (q.get(key) or [''])[0].strip()
        if v.isdigit() and 4 <= len(v) <= 8:
            return int(v)
    m = re.search(r'/playShiur/(\d{1,8})(?:[/?#]|$)', url, re.I)
    if m:
        return int(m.group(1))
    m = _KH_ID.search(urlparse(url).path + '?' + urlparse(url).query)
    return int(m.group(1)) if m else None


def _kh_mp3_url(fid):
    name = str(fid).zfill(8)
    return f'https://www.kolhalashon.com/mp3/NewArchive/{name[:5]}/{name}.mp3'


def _stream_to(sess, url, path, headers=None):
    """מוריד ל-path. מחזיר True כשהגיע קובץ מדיה אמיתי (לא דף HTML של Cloudflare)."""
    with sess.get(url, headers=headers or {}, stream=True, timeout=(20, 600), allow_redirects=True) as r:
        ctype = (r.headers.get('content-type') or '').lower()
        if r.status_code != 200 or 'html' in ctype or 'json' in ctype:
            log.info(f'kolhalashon: {url[:90]} -> {r.status_code} {ctype}')
            return False
        size = 0
        with open(path, 'wb') as f:
            for chunk in r.iter_content(1024 * 256):
                if chunk:
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError('הקובץ גדול מדי')
                    f.write(chunk)
    return size > 10000


def _sessions():
    import requests
    s1 = requests.Session()
    s1.headers.update({'User-Agent': UA, 'Referer': 'https://www.kolhalashon.com/',
                       'Accept-Language': 'he-IL,he;q=0.9,en;q=0.8'})
    yield 'requests', s1
    try:
        import cloudscraper
        s2 = cloudscraper.create_scraper()
        s2.headers.update({'Referer': 'https://www.kolhalashon.com/', 'Origin': 'https://www.kolhalashon.com',
                           'Accept-Language': 'he-IL,he;q=0.9,en;q=0.8'})
        yield 'cloudscraper', s2
    except Exception as e:
        log.info(f'kolhalashon: no cloudscraper ({e})')


def fetch_kol_halashon(url, dest_dir):
    fid = kol_halashon_id(url)
    if not fid:
        raise ValueError('לא מצאנו את מספר השיעור בקישור של קול הלשון. יש להעתיק את הקישור מדף השיעור')
    os.makedirs(dest_dir, exist_ok=True)
    tried = []
    for label, sess in _sessions():
        path = os.path.join(dest_dir, f'kh_{fid}_{uuid.uuid4().hex[:8]}.mp3')
        try:
            if _stream_to(sess, _kh_mp3_url(fid), path):
                return path, f'קול הלשון - שיעור {fid}'
            tried.append(f'{label}:mp3')
            # נגן האתר: אסימון ניגון (שמע=1, וידאו=2)
            for q, ext in ((1, 'mp3'), (2, 'mp4')):
                key = 'Bearer ' + ''.join(random.choices(string.ascii_lowercase + string.digits, k=7))
                r = sess.get(f'https://www.kolhalashon.com/api/files/GetPlayToken/{fid}/{q}',
                             headers={'authorization-site-key': key, 'Cache-Control': 'no-store'}, timeout=30)
                if r.status_code != 200 or 'json' not in (r.headers.get('content-type') or ''):
                    tried.append(f'{label}:token{q}={r.status_code}')
                    continue
                d = r.json() or {}
                src = d.get('src') or (f"https://www.kolhalashon.com/api/files/GetFileToPlay/{fid}/{q}/{d['token']}"
                                       if d.get('token') else None)
                if src and src.endswith('.m3u8'):
                    tried.append(f'{label}:hls')
                    continue
                path2 = path[:-3] + ext
                if src and _stream_to(sess, src, path2, headers={'authorization-site-key': key}):
                    return path2, f'קול הלשון - שיעור {fid}'
                tried.append(f'{label}:src{q}')
        except ValueError:
            raise
        except Exception as e:
            tried.append(f'{label}:{type(e).__name__}')
        finally:
            for p in (path, path[:-3] + 'mp4'):
                if os.path.exists(p) and os.path.getsize(p) <= 10000:
                    os.remove(p)
    log.warning(f'kolhalashon {fid}: failed ({", ".join(tried)})')
    raise ValueError('לא הצלחנו להוריד את השיעור מקול הלשון. ייתכן שהשיעור לא פתוח להורדה, '
                     'או שהאתר חוסם כרגע - אפשר להוריד את הקובץ ולהעלות אותו')


def fetch(url, dest_dir):
    k = kind(url)
    if k == 'youtube':
        return fetch_youtube(url, dest_dir)
    if k == 'kolhalashon':
        return fetch_kol_halashon(url, dest_dir)
    raise ValueError('קישור לא נתמך')
