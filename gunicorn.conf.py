def on_starting(server):
    from services.transcribe import start_soferai_scheduler
    start_soferai_scheduler()
