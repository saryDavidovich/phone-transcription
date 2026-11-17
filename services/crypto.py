"""
הצפנת שדות רגישים בבסיס הנתונים - AES-256-GCM (כמו בתוכנת חדר המחשבים).

- המפתח: משתנה הסביבה ENCRYPTION_KEY (כל מחרוזת ארוכה; נגזר ממנה מפתח של 256 ביט).
  אם הוא לא הוגדר - נגזר מפתח מ-SECRET_KEY (עובד, אבל מומלץ להגדיר מפתח נפרד ולא להחליף אותו).
- ערך מוצפן נשמר כ-"enc1:" + base64(nonce 12 בתים + צופן + תג אימות). כל הצפנה עם nonce חדש,
  ולכן אותו ערך מוצפן פעמיים נראה שונה. שינוי של בית אחד בבסיס הנתונים = הפענוח נכשל (GCM מאמת).
- decrypt() על ערך ישן שלא הוצפן מחזיר אותו כמו שהוא (מעבר הדרגתי בלי לשבור נתונים קיימים).
- סיסמאות וקודי כניסה לא מוצפנים אלא עוברים גיבוב חד-כיווני (scrypt) - אי אפשר לשחזר אותם בכלל.
"""
import os
import base64
import hashlib
import logging

log = logging.getLogger(__name__)
PREFIX = 'enc1:'
_warned = {'v': False}


def _key():
    raw = (os.environ.get('ENCRYPTION_KEY') or '').strip()
    if not raw:
        if not _warned['v']:
            log.warning('crypto: ENCRYPTION_KEY לא הוגדר - משתמשים במפתח שנגזר מ-SECRET_KEY')
            _warned['v'] = True
        from flask import current_app
        raw = 'derived|' + (current_app.secret_key or '')
    return hashlib.sha256(raw.encode()).digest()       # 32 בתים = AES-256


def is_encrypted(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(value):
    if value in (None, '') or is_encrypted(value):
        return value
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    ct = AESGCM(_key()).encrypt(nonce, str(value).encode(), b'tamlul-phone')
    return PREFIX + base64.b64encode(nonce + ct).decode()


def decrypt(value):
    if not is_encrypted(value):
        return value
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        blob = base64.b64decode(value[len(PREFIX):])
        return AESGCM(_key()).decrypt(blob[:12], blob[12:], b'tamlul-phone').decode()
    except Exception:
        log.error('crypto: פענוח נכשל (מפתח ההצפנה הוחלף או שהערך שונה)')
        return None


def mask(value, keep=4):
    """לתצוגה בלבד: רק 4 התווים האחרונים."""
    v = decrypt(value) if is_encrypted(value) else value
    if not v:
        return ''
    v = str(v)
    return ('•' * max(0, min(8, len(v) - keep))) + v[-keep:]
