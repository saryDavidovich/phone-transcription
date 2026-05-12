@echo off
cd C:\transcription
call venv\Scripts\activate
set /p OPENAI_API_KEY=Enter OpenAI API Key:
set /p SENDGRID_API_KEY=Enter SendGrid API Key:
set /p SENDGRID_FROM=Enter sender email:
set /p DEFAULT_EMAIL=Enter recipient email:
python app.py
pause
