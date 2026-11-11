"""페이지(/, /login)와 로그인·회원가입 API."""
from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user

from db import User, hash_password, check_password
from routes.common import db_session, json_body

bp = Blueprint('auth', __name__)
login_manager = LoginManager()
login_manager.login_view = 'auth.login_page'


class AuthUser(UserMixin):
    def __init__(self, user: User):
        self.id = user.id
        self.username = user.username


@login_manager.user_loader
def load_user(user_id):
    with db_session() as db:
        user = db.get(User, int(user_id))
        return AuthUser(user) if user else None


@login_manager.unauthorized_handler
def unauthorized():
    """API는 302 대신 401 JSON → 프론트가 로그인 화면으로 보낸다."""
    if request.path.startswith(('/api/', '/download')):
        return jsonify(success=False, message='로그인이 필요합니다', login_required=True), 401
    return redirect(url_for('auth.login_page'))


@bp.route('/')
@login_required
def index():
    return render_template('index.html')


@bp.route('/login')
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for('auth.index'))
    return render_template('login.html')


def _credentials():
    data = json_body()
    return (data.get('username') or '').strip(), (data.get('password') or '').strip()


@bp.route('/api/auth/register', methods=['POST'])
def register():
    username, password = _credentials()
    if not username or not password:
        return jsonify(success=False, message='아이디와 비밀번호를 입력하세요'), 400
    with db_session() as db:
        if db.query(User).filter_by(username=username).first():
            return jsonify(success=False, message='이미 사용 중인 아이디입니다'), 409
        user = User(username=username, password_hash=hash_password(password))
        db.add(user)
        db.commit()
        db.refresh(user)
        login_user(AuthUser(user))
        return jsonify(success=True, username=user.username)


@bp.route('/api/auth/login', methods=['POST'])
def login():
    username, password = _credentials()
    with db_session() as db:
        user = db.query(User).filter_by(username=username).first()
        if not user or not check_password(password, user.password_hash):
            return jsonify(success=False, message='아이디 또는 비밀번호가 틀렸습니다'), 401
        login_user(AuthUser(user), remember=True)
        return jsonify(success=True, username=user.username)


@bp.route('/api/auth/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return jsonify(success=True)


@bp.route('/api/me')
@login_required
def me():
    return jsonify(id=current_user.id, username=current_user.username)
