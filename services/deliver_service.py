import os
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger(__name__)


def deliver(call_data):
    dest = call_data.get('destination', 'email')
    if dest == 'email':
        _send_email(call_data)
    elif dest == 'fax':
        log.info("fax not configured yet")


def _build_html(call_data):
    transcript = call_data.get('transcript', '')
    summary = call_data.get('summary', '')
    caller = call_data.get('caller', 'unknown')
    created = call_data.get('created_at', '')[:16].replace('T', ' ')
    summary_block = ''
    if summary:
        lines = ''.join(f'<li>{l.strip()}</li>' for l in summary.split('\n') if l.strip())
        summary_block = f'<div style="background:#f0f7ff;border-right:4px solid #2563eb;padding:16px;margin:20px 0;border-radius:4px;"><strong>סיכום:</strong><ul style="margin:8px 0 0;padding-right:20px">{lines}</ul></div>'
    return f'''<div dir="rtl" style="font-family:Arial,sans-serif;max-width:600px;margin:auto;color:#1f2937">
<h2 style="color:#1d4ed8;border-bottom:2px solid #e5e7eb;padding-bottom:8px">תמלול שיחה</h2>
<p style="color:#6b7280;font-size:14px">מתקשר: <b>{caller}</b> | תאריך: <b>{created}</b></p>
{summary_block}
<h3>תמלול מלא:</h3>
<div style="background:#f9fafb;border:1px solid #e5e7eb;padding:16px;border-radius:8px;line-height:1.7;white-space:pre-wrap">{transcript}</div>
<p style="color:#9ca3af;font-size:12px;margin-top:24px">נשלח ע"י מערכת התמלול האוטומטית</p>
</div>'''


def _send_email(call_data):
    try:
        gmail_user = os.environ.get('GMAIL_USER', '')
        gmail_password = os.environ.get('GMAIL_APP_PASSWORD', '')
        target = call_data.get('target_address') or os.environ.get('DEFAULT_EMAIL', gmail_user)
        caller = call_data.get('caller', 'unknown')

        msg = MIMEMultipart('alternative')
        msg['Subject'] = f'תמלול שיחה מ-{caller}'
        msg['From'] = gmail_user
        msg['To'] = target

        html = _build_html(call_data)
        msg.attach(MIMEText(html, 'html', 'utf-8'))

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(gmail_user, gmail_password)
            server.sendmail(gmail_user, target, msg.as_string())

        log.info(f"email sent to {target}")

    except Exception as e:
        log.error(f"email error: {e}")
