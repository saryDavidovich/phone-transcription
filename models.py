from app import db
from flask_login import UserMixin
from datetime import datetime

class Customer(db.Model):
    __tablename__ = 'customers'
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), unique=True, nullable=True, index=True)
    name = db.Column(db.String(100))
    email = db.Column(db.String(200))
    fax = db.Column(db.String(20))
    # קוד אישי (5 ספרות, ייחודי) שהלקוח כותב יחד עם הטלפון שלו בעמוד הראשון
    # כשהוא שולח פקס נכנס למערכת - עוזר לנציג לזהות בוודאות למי הפקס שייך
    # (בנוסף לטלפון שכתוב), במיוחד אם הכתב יד לא ברור. נוצר עצלנית (lazy) -
    # ראה routes/dictate.py._ensure_customer_fax_code - ולא בהכרח קיים לכל
    # לקוח ישן. ראה גם models.IncomingFax לשיוך פקסים נכנסים שהתקבלו.
    fax_code = db.Column(db.String(10), unique=True, nullable=True, index=True)
    balance = db.Column(db.Float, default=0.0)
    is_blocked = db.Column(db.Boolean, default=False)
    delivery_method = db.Column(db.String(10), default='email')
    default_settings = db.Column(db.JSON, nullable=True)  # ברירות מחדל: tier, language, output_language
    transcription_tier = db.Column(db.String(10), default='basic')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    recordings = db.relationship('Recording', backref='customer', lazy=True)
    transactions = db.relationship('Transaction', backref='customer', lazy=True)

    # --- שיוך למוסד (תלמיד) ---
    # לקוח "רגיל" (הרשמה עצמאית) - institution_id ריק. "תלמיד" ששייך למוסד -
    # אותה טבלה בדיוק, כדי לעשות שימוש חוזר מלא בצנרת ההקלטה/תמלול/חיוב
    # הקיימת, רק עם institution_id ומספר תלמיד (student_number) ממולאים.
    institution_id = db.Column(db.Integer, db.ForeignKey('institutions.id'), nullable=True, index=True)
    student_number = db.Column(db.String(10), unique=True, nullable=True, index=True)  # 6 ספרות, לזיהוי בשלוחה 7
    student_display_name = db.Column(db.String(100), nullable=True)  # שם שהמוסד נתן לתלמיד (עשוי להיות שונה מ-name)


class Institution(db.Model, UserMixin):
    """מוסד משלם - מקבל ממשק ניהול לתלמידים משלו. ההתחברות נפרדת לגמרי
    מ-AdminUser (מנהל-העל של כל המערכת); ראה routes/institution.py."""
    __tablename__ = 'institutions'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200), nullable=True)   # התחברות אפשרית לפי מייל
    phone = db.Column(db.String(20), nullable=True)    # התחברות אפשרית לפי טלפון
    login_code = db.Column(db.String(20), nullable=True)      # קוד שנוצר ע"י מנהל-העל, בשימוש עד שהמוסד מגדיר סיסמה
    password_hash = db.Column(db.String(256), nullable=True)  # מוגדר בכניסה ראשונה, מחליף את login_code
    google_id = db.Column(db.String(100), nullable=True, unique=True)  # אימות גוגל אופציונלי, ללא קוד
    balance = db.Column(db.Float, default=0.0)   # יתרה כללית של המוסד (מוזן ע"י מנהל-העל / חיוב אשראי)
    is_blocked = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # --- הגדרות מוסד ---
    max_usage_per_student = db.Column(db.Float, nullable=True)  # ישן, לא בשימוש יותר - הוחלף ע"י max_minutes_per_period+limit_period
    max_minutes_per_period = db.Column(db.Float, nullable=True)  # מגבלת דקות תמלול לתלמיד בתקופה (יומית/שבועית), ריק = ללא הגבלה
    limit_period = db.Column(db.String(10), nullable=True)  # 'day' או 'week'
    allowed_hours_start = db.Column(db.String(5), nullable=True)  # "HH:MM", ריק = ללא הגבלת שעות
    allowed_hours_end = db.Column(db.String(5), nullable=True)
    notify_email = db.Column(db.String(200), nullable=True)  # מייל שאליו מגיעים כל התמלולים עם פרטי התלמיד
    notify_fax = db.Column(db.String(20), nullable=True)
    authorized_logins = db.Column(db.JSON, nullable=True)  # [{"email":..., "name":...}, ...] - מורשי כניסה נוספים

    # --- הגדרות חיוב (נדרים פלוס - ראו routes/institution_billing.py) ---
    card_last4 = db.Column(db.String(4), nullable=True)
    nedarim_token = db.Column(db.String(200), nullable=True)  # טוקן חיוב קבוע, אם נדרים פלוס מספקים כזה

    students = db.relationship('Customer', backref='institution', lazy=True,
                                foreign_keys='Customer.institution_id')

    def get_id(self):
        # קידומת ייחודית - כדי שה-user_loader המשותף (routes/admin.py) יידע
        # להבדיל בין התחברות מוסד להתחברות מנהל-על, ששתיהן משתמשות באותו
        # Flask-Login LoginManager יחיד.
        return f'inst-{self.id}'


class InstitutionChargeLog(db.Model):
    """יומן חיובי אשראי בפועל של המוסד (נדרים פלוס), נפרד מ-Transaction
    שמתעד תנועות ביתרת תלמיד בודד."""
    __tablename__ = 'institution_charge_logs'
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institutions.id'), nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default='pending')  # pending / success / failed
    provider_ref = db.Column(db.String(200), nullable=True)  # מזהה עסקה אצל נדרים פלוס
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    institution = db.relationship('Institution', backref='charge_logs')


class InstitutionUpload(db.Model):
    """תמלול אד-הוק שמנהל המוסד מעלה בעצמו דרך לשונית 'יצירת תמלול' (לא
    קשור לתלמיד ספציפי, ולא צורך את יתרת תלמיד - זה כלי עבודה של המוסד
    עצמו). מעובד ברקע (thread) כדי לא לחסום את הדפדפן; הלשונית מבצעת
    polling לפי status ומציגה גלגל שיניים עד ל-done."""
    __tablename__ = 'institution_uploads'
    id = db.Column(db.Integer, primary_key=True)
    institution_id = db.Column(db.Integer, db.ForeignKey('institutions.id'), nullable=False, index=True)
    original_filename = db.Column(db.String(255))
    tier = db.Column(db.String(20), default='gemini')
    status = db.Column(db.String(20), default='processing')  # processing / done / error
    transcript = db.Column(db.Text)
    docx_filename = db.Column(db.String(255))  # שם קובץ ה-Word המוכן בתוך static/fax_tmp
    error_message = db.Column(db.Text)
    duration_seconds = db.Column(db.Integer, nullable=True)
    cost = db.Column(db.Float, nullable=True)  # מנוכה מיתרת המוסד (Institution.balance)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    institution = db.relationship('Institution', backref='uploads')

class Recording(db.Model):
    __tablename__ = 'recordings'
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False, index=True)
    call_id = db.Column(db.String(100), unique=True)
    duration_seconds = db.Column(db.Integer, default=0)
    cost = db.Column(db.Float, default=0.0)
    transcript = db.Column(db.Text)
    summary = db.Column(db.Text)
    status = db.Column(db.String(30), default='recording')
    delivery_method = db.Column(db.String(10))
    delivered_to = db.Column(db.String(200))
    alefbot_job_id = db.Column(db.String(100))
    rec_url = db.Column(db.String(500))
    source_filename = db.Column(db.String(255), nullable=True)
    fax_campaign_id = db.Column(db.String(64), nullable=True, index=True)
    fax_status = db.Column(db.String(32), nullable=True)
    fax_status_note = db.Column(db.Text, nullable=True)
    # הקלטות ממתינות לתשלום
    transcription_tier = db.Column(db.String(10), nullable=True)
    language = db.Column(db.String(10), nullable=True)
    output_language = db.Column(db.String(10), nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)  # תפוגה אחרי 72 שעות
    # גיבוי בייטים של קובץ אודיו שהתקבל באימייל, לתקופה שבה הוא ממתין
    # ב"מצב תחזוקה" (status='queued_maintenance'). rec_url של הקלטת אימייל
    # מצביע על קובץ בדיסק המקומי של השרת - שנמחק בכל דפלוי/הפעלה מחדש
    # ב-Railway (בניגוד לשיחות טלפון, שה-rec_url שלהן מצביע לשרת חיצוני
    # של ימות המשיח). בלי הגיבוי הזה, הקלטת אימייל שממתינה במצב תחזוקה
    # תאבד את הקובץ בדיוק כמו הבאג שתיקנו בעבר בכתבי היד. נשמר רק כשבאמת
    # נכנסים לתור (לא לכל הקלטה) כדי לא לנפח את הטבלה סתם.
    file_data = db.Column(db.LargeBinary, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Transaction(db.Model):
    __tablename__ = 'transactions'
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False)
    type = db.Column(db.String(20))
    description = db.Column(db.String(200))
    recording_id = db.Column(db.Integer, db.ForeignKey('recordings.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Settings(db.Model):
    __tablename__ = 'settings'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.String(500))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class AdminUser(db.Model, UserMixin):
    __tablename__ = 'admin_users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class CallSession(db.Model):
    __tablename__ = 'call_sessions'
    id = db.Column(db.Integer, primary_key=True)
    call_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    step = db.Column(db.String(50), default='')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class ManagerMessage(db.Model):
    __tablename__ = 'manager_messages'
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(100))
    email = db.Column(db.String(200))
    fax = db.Column(db.String(20))
    delivery_method = db.Column(db.String(10))
    call_id = db.Column(db.String(100))
    rec_url = db.Column(db.String(500))
    transcript = db.Column(db.Text)  # תמלול אוטומטי (בלי חשיבה) של ההודעה, מתמלא ברקע אחרי השמירה
    status = db.Column(db.String(30), default='new')
    admin_note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # 'ivr' (ברירת מחדל, הודעה קולית משלוחת "השארת הודעה למנהל") או
    # 'institution_contact' (טופס "צור קשר" בממשק ניהול מוסד - אין rec_url,
    # אז חייבים דגל נפרד כדי שלא ייפלו תחת הסינון שמסתיר "שריונים ריקים")
    source = db.Column(db.String(30), default='ivr')


class ManuscriptPage(db.Model):
    """דף כתב-יד שהתקבל במייל - עותק מקביל ובלתי-תלוי לגמרי בצנרת ה-OCR
    (OcrResult). המטרה: נציג צוות פותח את התמונה, מקריא את הטקסט בקול, ומסמן
    עיצוב (מודגש/קו תחתון/כותרת) תוך כדי ההקראה עם כפתורים - ראו routes/dictate.py.
    שלב א' (נוכחי): רץ בשקט לגמרי במקביל ל-OCR, הלקוח לא נחשף לזה בשום צורה.
    """
    __tablename__ = 'manuscript_pages'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False, index=True)
    original_filename = db.Column(db.String(255))
    file_path = db.Column(db.String(512))  # נתיב מקומי היסטורי - לא אמין לבדו (ראו file_data למטה)
    # תוכן הקובץ עצמו, שמור ב-DB (לא רק בדיסק המקומי) - קריטי ב-Railway (ובכל
    # מארח קונטיינרים אפמרי דומה): הדיסק המקומי מתאפס בכל דיפלוי/הפעלה מחדש
    # של הקונטיינר, אז file_path לבדו לא שורד דיפלוי. file_data הוא מקור
    # האמת לתצוגת הכתב-יד בסטודיו (ראה routes/dictate.py._manuscript_data_uri) -
    # file_path נשאר רק לתאימות לאחור/דיבוג.
    file_data = db.Column(db.LargeBinary, nullable=True)
    status = db.Column(db.String(20), default='pending', index=True)
    # pending -> ממתין להקלטה
    # recording -> נציג פתח את הדף ומקליט/עורך כרגע (claimed_by נעול)
    # processing -> אודיו הועלה, מחכה לתמלול (לפי engine) ולבניית התוכן מהקטעים
    # review -> תומלל בהצלחה, ממתין לאישור/שליחה של הנציג
    # error -> תמלול נכשל
    engine = db.Column(db.String(20), nullable=True)  # 'gemini' | 'openai' - איזה מנוע תימלל את ההקלטה הזו (לצורך השוואה)
    content = db.Column(db.JSON, nullable=True)  # [{heading: bool, runs: [{text, bold, underline}, ...]}, ...]
    docx_filename = db.Column(db.String(255), nullable=True)  # קובץ ה-Word המוכן, בתוך static/fax_tmp
    claimed_by = db.Column(db.String(100), nullable=True)  # username של הנציג שפתח את הדף (נעילה רכה)
    claimed_at = db.Column(db.DateTime, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    sent_at = db.Column(db.DateTime, nullable=True)
    sent_to = db.Column(db.String(255), nullable=True)
    # שלב ב' - תמחור: מחושבים ונשמרים רק בפועל בעת שליחה מוצלחת (routes/dictate.py:send),
    # לפי ההגדרות price_per_manuscript_char_unit / manuscript_char_unit_size (routes/admin.py).
    # לפני שליחה השדות האלה נשארים 0/ריק - התשלום לא יורד ללקוח לפני אישור סופי ושליחה בפועל.
    char_count = db.Column(db.Integer, default=0)
    cost = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # שלב ג' - "הגהה חוזרת" (גרסה ישנה, לא בשימוש יותר לכתיבה): אחרי שהדף
    # נשלח ללקוח, הלקוח יכול לתקן ולשלוח את הקובץ המתוקן בחזרה במייל (ראה
    # כפתור ב-_send_manuscript_email וההוק ב-routes/email_inbound.email_inbound).
    # השדות האלה הוחלפו במודל ProofingRound למטה (שתומך בכמה סבבי הגהה
    # נפרדים על אותו כתב-יד, כל אחד עם שעון/חיוב משלו) - נשארים כאן רק
    # מטעמי תאימות לאחור/היסטוריה (מיגרציה חד-פעמית מעבירה נתונים ישנים
    # מכאן ל-proofing_rounds, ראה app.py._migrate_db). קוד חדש לא צריך
    # לקרוא/לכתוב לשדות האלה - להשתמש ב-page.proofing_rounds במקום.
    proof_status = db.Column(db.String(20), nullable=True, index=True)
    proof_file_data = db.Column(db.LargeBinary, nullable=True)
    proof_original_filename = db.Column(db.String(255), nullable=True)
    proof_requested_at = db.Column(db.DateTime, nullable=True)
    proof_completed_at = db.Column(db.DateTime, nullable=True)
    proof_cost = db.Column(db.Float, default=0.0)
    proof_final_file_data = db.Column(db.LargeBinary, nullable=True)
    proof_final_filename = db.Column(db.String(255), nullable=True)
    proof_edited_content = db.Column(db.JSON, nullable=True)

    customer = db.relationship('Customer', backref='manuscript_pages')


class ProofingRound(db.Model):
    """סבב הגהה בודד על כתב-יד (ManuscriptPage) - אפשר כמה סבבים נפרדים על
    אותו כתב-יד לאורך זמן (למשל אם הלקוח שולח עוד תיקונים בהמשך אחרי סבב
    קודם שכבר הושלם), כל אחד עם שעון וחיוב נפרדים לגמרי - ראה
    routes/dictate.py (studio_proof וכל שאר ה-routes תחת /proof/<round_id>/...).
    נוצר אוטומטית מתוך routes/email_inbound.py כשמתקבלת תגובת הגהה
    (_is_proofing_reply). התיקון עצמו יכול להגיע כקובץ Word (אם ללקוח יש
    מחשב) או כתמונה/PDF (אם תיקן בעט על דף מודפס וצילם/סרק).
    חיוב: לפי דקות עבודה בפועל (שעון עצר שהנציג מפעיל/עוצר ב-proof_studio.html,
    ראה timer_started_at/timer_accumulated_seconds) כפול המחיר-לדקה בהגדרות
    (price_manuscript_proofing) - לא מחיר קבוע לסבב."""
    __tablename__ = 'proofing_rounds'

    id = db.Column(db.Integer, primary_key=True)
    manuscript_page_id = db.Column(db.Integer, db.ForeignKey('manuscript_pages.id'), nullable=False, index=True)
    status = db.Column(db.String(20), default='pending', index=True)  # pending -> done

    customer_file_data = db.Column(db.LargeBinary, nullable=True)  # מה שהלקוח שלח בחזרה (Word/תמונה/PDF)
    customer_file_filename = db.Column(db.String(255), nullable=True)
    requested_at = db.Column(db.DateTime, default=datetime.utcnow)  # מתי התקבל הסבב מהלקוח
    completed_at = db.Column(db.DateTime, nullable=True)  # מתי הנציג סיים וחויב הלקוח

    # שעון עצר החיוב-לפי-דקה (ראה routes/dictate.py proof_timer_start/stop):
    # timer_started_at != None כשהשעון רץ כרגע; timer_accumulated_seconds
    # צובר את סך הזמן מכל מקטעי הפעלה/עצירה קודמים (לא כולל מקטע רץ נוכחי).
    timer_started_at = db.Column(db.DateTime, nullable=True)
    timer_accumulated_seconds = db.Column(db.Float, default=0.0)

    edited_content = db.Column(db.JSON, nullable=True)  # תוכן ההגהה כפי שנערך בעורך המובנה בדפדפן
    final_file_data = db.Column(db.LargeBinary, nullable=True)  # קובץ ה-Word הסופי (נשלח ללקוח אם התבקש)
    final_filename = db.Column(db.String(255), nullable=True)
    cost = db.Column(db.Float, default=0.0)  # מחושב ונשמר רק בעת סיום מוצלח (proof_complete)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    manuscript_page = db.relationship('ManuscriptPage', backref=db.backref('proofing_rounds', order_by='ProofingRound.created_at'))


class IncomingFax(db.Model):
    """פקס נכנס גולמי שהתקבל דרך מודול 'קבלת פקסים' של ימות המשיח (מוגדר
    לשלוח את הפקס במייל לכתובת ייעודית - ראה routes/email_inbound.py.
    FAX_INBOUND_EMAIL/_handle_incoming_fax). בניגוד לתגובת הגהה במייל, אין
    כאן שום נושא/כתובת-שולח מזהה - לכן הפקס נשמר "לא משויך" (status=pending)
    ונציג משייך אותו ידנית (שיוך אנושי לגמרי, ללא OCR אוטומטי - ראה
    routes/dictate.py fax_inbox/fax_assign_*) לפי מה שכתוב בעמוד הראשון של
    הפקס עצמו (טלפון + קוד אישי - ראה Customer.fax_code): או ככתב יד חדש
    להקראה, או כסבב הגהה חדש על כתב יד קיים. שימושי רק בעולם הקראת כתבי היד
    - לא קשור לתמלול/OCR הרגיל."""
    __tablename__ = 'incoming_faxes'

    id = db.Column(db.Integer, primary_key=True)
    received_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    from_email = db.Column(db.String(255), nullable=True)  # כתובת ה"שולח" בפועל של המייל שימות שלחה - לרוב לא שימושי לזיהוי לקוח, רק לדיבוג
    raw_subject = db.Column(db.String(500), nullable=True)
    filename = db.Column(db.String(255), nullable=True)
    file_data = db.Column(db.LargeBinary, nullable=True)

    status = db.Column(db.String(20), default='pending', index=True)  # pending -> assigned / ignored
    assigned_customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True, index=True)
    assigned_manuscript_page_id = db.Column(db.Integer, db.ForeignKey('manuscript_pages.id'), nullable=True)
    assigned_proofing_round_id = db.Column(db.Integer, db.ForeignKey('proofing_rounds.id'), nullable=True)
    assigned_at = db.Column(db.DateTime, nullable=True)
    assigned_by = db.Column(db.String(100), nullable=True)  # username הנציג ששייך/התעלם
    note = db.Column(db.String(500), nullable=True)  # לדוגמה "עמוד ריק" / "לא קריא" בעת התעלמות

    customer = db.relationship('Customer', foreign_keys=[assigned_customer_id])
    manuscript_page = db.relationship('ManuscriptPage', foreign_keys=[assigned_manuscript_page_id])
    proofing_round = db.relationship('ProofingRound', foreign_keys=[assigned_proofing_round_id])


class OcrResult(db.Model):
    __tablename__ = 'ocr_results'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False, index=True)
    original_filename = db.Column(db.String(255))
    original_file_path = db.Column(db.String(512))  # נתיב לקובץ המקורי
    ocr_text = db.Column(db.Text)
    char_count = db.Column(db.Integer, default=0)
    cost = db.Column(db.Float, default=0.0)
    engine = db.Column(db.String(20), default='gemini')
    status = db.Column(db.String(20), default='completed')  # completed / error / pending_payment / queued_maintenance
    delivered_to = db.Column(db.String(255), nullable=True)  # כתובת מייל לשליחה כשתשלים תשלום
    expires_at = db.Column(db.DateTime, nullable=True)  # לתוצאות pending_payment - 72 שעות
    # גיבוי בייטים של קובץ התמונה/PDF, לתקופה שבה הוא ממתין ב"מצב תחזוקה"
    # (status='queued_maintenance') - אותה בעיה בדיוק כמו Recording.file_data:
    # original_file_path מצביע על דיסק מקומי שנמחק בכל דפלוי. נמחק אוטומטית
    # כשמשחררים את התור (ראה routes/email_inbound.resume_queued_ocr).
    file_data = db.Column(db.LargeBinary, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship('Customer', backref=db.backref('ocr_results', lazy=True))


class ActiveJob(db.Model):
    """שורה = עבודת רקע אחת שרצה ממש עכשיו (תמלול שיחה/OCR/הקראת כתב יד/
    תלמיד מוסד) - לצורך מסך "פעילות מערכת" (/admin/maintenance, ראה
    services/job_tracker.py). נוצרת כשעבודה מתחילה, נמחקת כשהיא מסתיימת.
    חייבת להיות ב-DB (לא רק זיכרון של תהליך אחד) כי יש כמה worker processes
    של gunicorn במקביל - זה המקור היחיד שרואה את כולם ביחד."""
    __tablename__ = 'active_jobs'

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(30), nullable=False, index=True)
    label = db.Column(db.String(255), nullable=True)
    started_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class ProcessedWebhook(db.Model):
    """מונע עיבוד כפול כאשר SendGrid שולח את אותו webhook יותר מפעם אחת (retry)"""
    __tablename__ = 'processed_webhooks'

    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.String(255), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ConversationThread(db.Model):
    """שיחה בודדת (thread) בהתכתבות מייל עם לקוח. ללקוח יכולות להיות כמה שיחות
    נפרדות במקביל - כל אחת מוצגת בנפרד בממשק, לא מעורבבת יחד."""
    __tablename__ = 'conversation_threads'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    customer = db.relationship('Customer', backref='threads')


class CustomerMessage(db.Model):
    """הודעה בודדת בתוך שיחה (ConversationThread). direction='out' - המנהל
    שלח, direction='in' - תגובת הלקוח שהתקבלה במייל. is_read מסמן הודעות
    נכנסות שהמנהל עוד לא צפה בהן (לצורך התראה בעמוד הודעות למנהל)."""
    __tablename__ = 'customer_messages'

    id = db.Column(db.Integer, primary_key=True)
    thread_id = db.Column(db.Integer, db.ForeignKey('conversation_threads.id'), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False, index=True)
    direction = db.Column(db.String(10), nullable=False)  # 'out' | 'in'
    body = db.Column(db.Text, nullable=False)
    # ברירת המחדל True מתאימה ל-direction='out' (הודעת מנהל - "נקראה" מטבעה).
    # כל יצירה של הודעה נכנסת (direction='in') חייבת להעביר is_read=False
    # במפורש, אחרת היא תיווצר כבר "נקראה" ולעולם לא תפעיל התראה - בדיוק הבאג
    # שנמצא ותוקן ב-general_inbox() (routes/admin.py).
    is_read = db.Column(db.Boolean, default=True)
    message_id = db.Column(db.String(255))  # Message-ID שקבענו לעצמנו, לשרשור אמיתי (In-Reply-To/References)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    customer = db.relationship('Customer', backref='messages')
    thread = db.relationship('ConversationThread', backref=db.backref('messages', order_by='CustomerMessage.created_at'))


class CallLog(db.Model):
    """רישום כל שיחה נכנסת (התחלה/סיום) - לצורך דוחות פילוח שיחות לפי תאריך.
    נכתב ע"י שירות ה-IVR (yemot-router2) דרך /api/call/start ו-/api/call/end.
    started_at/ended_at נשמרים ב-UTC; ההמרה לשעון ישראל נעשית בזמן קריאה בלבד."""
    __tablename__ = 'call_logs'

    id = db.Column(db.Integer, primary_key=True)
    call_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    phone = db.Column(db.String(20), nullable=False, index=True)
    started_at = db.Column(db.DateTime, nullable=False, index=True)
    ended_at = db.Column(db.DateTime, nullable=True)
    duration_seconds = db.Column(db.Integer, nullable=True)


class GeneralInboxMessage(db.Model):
    """שרשור פנייה מגורם שלא זוהה כלקוח רשום (לא נמצא מספר טלפון בנושא שתואם
    ללקוח קיים) - מקביל ל-ConversationThread, אבל מזוהה לפי כתובת מייל בלבד
    (אין customer_id) כדי שגם מי שעדיין לא לקוח רשום יוכל לקבל תשובה במייל
    עם שרשור אמיתי (Message-ID/In-Reply-To), בדיוק כמו שיחה עם לקוח. is_read
    מסמן אם יש בשרשור הזה תוכן חדש שהמנהל עוד לא צפה בו."""
    __tablename__ = 'general_inbox_messages'

    id = db.Column(db.Integer, primary_key=True)
    from_email = db.Column(db.String(255), nullable=False, index=True)
    subject = db.Column(db.String(500))
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)


class InboxMessage(db.Model):
    """הודעה בודדת בתוך שרשור GeneralInboxMessage. direction='in' - מהשולח
    החיצוני, direction='out' - תגובת המנהל. אותו עיקרון בדיוק כמו
    CustomerMessage, רק ששייכת לשרשור אנונימי (לפי מייל) ולא ללקוח רשום."""
    __tablename__ = 'inbox_messages'

    id = db.Column(db.Integer, primary_key=True)
    thread_id = db.Column(db.Integer, db.ForeignKey('general_inbox_messages.id'), nullable=False, index=True)
    direction = db.Column(db.String(10), nullable=False)  # 'out' | 'in'
    body = db.Column(db.Text, nullable=False)
    message_id = db.Column(db.String(255))  # רק להודעות 'out' - לשרשור אמיתי מול תגובות עתידיות
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    thread = db.relationship('GeneralInboxMessage', backref=db.backref('messages', order_by='InboxMessage.created_at'))
