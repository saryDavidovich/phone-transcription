# הגדרות gunicorn (נטען אוטומטית; מספר ה-workers נקבע ב-Procfile).
#
# gthread: כל worker מטפל בכמה בקשות במקביל (threads), במקום בקשה אחת בכל רגע.
# בלי זה, בקשה ארוכה אחת (למשל ה-IVR שמחכה לגימיני בזיהוי מייל/עמודות, או
# העלאת קובץ) תפסה worker שלם, והממשק חיכה בתור. 3 workers x 4 = 12 בקשות במקביל.
# הקוד כבר בטוח לריבוי תהליכונים: Flask-SQLAlchemy פותח session נפרד לכל בקשה,
# ומצב משותף בזיכרון מוגן במנעולים.
worker_class = 'gthread'
threads = 4


def on_starting(server):
    pass  # AlefBot uses webhooks, no scheduler needed
