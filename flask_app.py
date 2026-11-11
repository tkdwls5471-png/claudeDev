"""앱 진입점: Flask 앱 생성, Blueprint 등록, DB 준비, 백그라운드 작업 시작.
라우트는 routes/ 아래 기능별 파일에 있다.  (gunicorn flask_app:app)"""
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from flask import Flask  # noqa: E402

import routes  # noqa: E402
import scheduler  # noqa: E402
from db import init_db  # noqa: E402

logging.basicConfig(level=logging.INFO)

ROOT = os.path.dirname(os.path.abspath(__file__))


def _static_version():
    """정적 파일 캐시 무효화용 버전: static/ 아래 파일의 가장 최근 수정 시각 (배포하면 바뀜)."""
    latest = 0
    for dirpath, _, files in os.walk(os.path.join(ROOT, 'static')):
        for f in files:
            latest = max(latest, os.path.getmtime(os.path.join(dirpath, f)))
    return str(int(latest))


app = Flask(__name__)
app.config.update(
    MAX_CONTENT_LENGTH=100 * 1024 * 1024,
    SECRET_KEY=os.environ.get('SECRET_KEY', 'eml-secret-2026'),
    TEMPLATES_AUTO_RELOAD=True,
)
STATIC_VERSION = os.environ.get('STATIC_VERSION') or _static_version()
app.context_processor(lambda: {'static_v': STATIC_VERSION})
routes.register(app)

with app.app_context():
    init_db()
    scheduler.start_background()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=False)
