ffmpeg.wasm - ffmpeg שמהודר לרוץ בתוך הדפדפן (כלי הכיווץ: static/js/compress.js).
@ffmpeg/ffmpeg 0.12.15 (MIT) ו-@ffmpeg/core 0.12.10 (GPL-2.0-or-later, כולל ffmpeg ו-LAME).
קוד המקור: https://github.com/ffmpegwasm/ffmpeg.wasm  ·  https://ffmpeg.org  ·  https://lame.sourceforge.io
הקבצים כאן הם בדיוק הקבצים מחבילות npm, בלי שינוי.
core-wasm-0..6.js: אותו ffmpeg-core.wasm בדיוק, אחרי gzip -9 ו-base64, מחולק ל-7 קבצים (נטפרי חוסמים קבצי wasm).
compress.js מחבר, מפענח ופורס אותם בדפדפן, ובודק שהתוצאה היא קובץ wasm תקין.
