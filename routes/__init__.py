"""기능별 Flask Blueprint 모음."""
from routes import accounts, auth, emails, export, rules, settings, upload

BLUEPRINTS = [auth.bp, accounts.bp, emails.bp, upload.bp, rules.bp, settings.bp, export.bp]


def register(app):
    auth.login_manager.init_app(app)
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)
