"""
סטטוס הקלטה בעברית מלאה + התקדמות, לתצוגה בממשק (רשימת הקלטות, כרטיס לקוח,
דף הקלטה) וללוח החי (routes/admin.recordings_live).

active=True - ההקלטה עדיין בטיפול: מוצג גלגל שיניים מסתובב, והלוח החי ממשיך
לעקוב אחריה עד שהיא מגיעה למצב סופי.
"""
# status -> (תווית, צבע התג, בטיפול?, הסבר במעבר עכבר)
STATUS = {
    'recording':          ('התקבלה',               'badge-gray',  True,  'ההקלטה התקבלה ועוד לא נכנסה לתמלול'),
    'processing':         ('בתור לתמלול',          'badge-gray',  True,  'ממתינה לתורה לתמלול'),
    'queued':             ('בתור לתמלול',          'badge-gray',  True,  'ממתינה לתורה לתמלול'),
    'queued_maintenance': ('ממתינה - מצב תחזוקה',  'badge-amber', False, 'תיכנס לתמלול כשמצב התחזוקה יכבה'),
    'transcribing':       ('מתמללת',               'badge-amber', True,  'התמלול רץ עכשיו'),
    'alefbot_pending':    ('בתמלול מקצועי',        'badge-amber', True,  'נשלחה לתמלול המקצועי - ממתינה לתוצאה'),
    'alefbot_finalizing': ('מסיימת תמלול מקצועי',  'badge-amber', True,  'התמלול המקצועי חזר - מכינים ושולחים'),
    'transcribed':        ('שולחת ללקוח',          'badge-amber', True,  'התמלול מוכן - נשלח במייל/פקס'),
    'delivered':          ('נשלחה',                'badge-green', False, 'התמלול נשלח ללקוח'),
    'pending_payment':    ('ממתינה לתשלום',        'badge-amber', False, 'אין מספיק יתרה - תישלח אחרי טעינת ארנק'),
    'awaiting_topup':     ('ממתינה לטעינת יתרה',   'badge-amber', False, 'אין מספיק יתרה'),
    'error':              ('שגיאה',                'badge-red',   False, 'התמלול נכשל - פרטים בהודעות למנהל'),
    'failed':             ('שגיאה',                'badge-red',   False, 'התמלול נכשל'),
    'too_short':          ('הקלטת טעות',           'badge-gray',  False, 'קצרה מ-6 שניות - לא נשלחה לתמלול, בלי חיוב'),
}

# אחוז ברירת מחדל לכל שלב, כשאין עדיין מדידה מדויקת מהתמלול עצמו
_STAGE_PCT = {'recording': 0, 'processing': 0, 'queued': 0, 'transcribing': 3,
              'alefbot_pending': None, 'alefbot_finalizing': 95, 'transcribed': 96}


# תמלול שנשלח בפקס: התג מראה מה קרה לפקס בפועל (Recording.fax_status), ולא תמיד "נשלחה"
FAX_STATUS = {
    'sent':      ('פקס - ממתין לאישור', 'badge-amber', 'הפקס נמסר לשליחה - עוד לא התקבל אישור שהגיע'),
    'sending':   ('פקס - בשליחה',       'badge-amber', 'הפקס נשלח עכשיו'),
    'delivered': ('פקס התקבל',          'badge-green', 'אושר שהפקס הגיע ליעד'),
    'failed':    ('פקס נכשל',           'badge-red',   'הפקס לא הגיע ליעד'),
    'no_answer': ('פקס נכשל - אין מענה', 'badge-red',  'מכשיר הפקס לא ענה'),
    'error':     ('שליחת הפקס נכשלה',   'badge-red',   'הפקס לא נשלח'),
}


def status_info(rec):
    status = rec.status or ''
    label, cls, active, title = STATUS.get(status, (status or 'לא ידוע', 'badge-gray', False, ''))
    fax = getattr(rec, 'fax_status', None)
    if status == 'delivered' and fax in FAX_STATUS:
        label, cls, title = FAX_STATUS[fax]
        note = getattr(rec, 'fax_status_note', None)
        if note:
            title = f'{title} - {note}'
    pct = None
    if active:
        pct = rec.progress if (rec.progress is not None and status == 'transcribing') else _STAGE_PCT.get(status)
        if status == 'transcribed' and rec.progress and rec.progress > 96:
            pct = rec.progress
    return {'label': label, 'cls': cls, 'active': active, 'title': title, 'pct': pct}
