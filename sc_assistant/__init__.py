from flask import Flask
from config import FLASK_SECRET_KEY

def create_app():
    """
    Application factory to create and configure the Flask app.
    """
    app = Flask(__name__, template_folder='templates', static_folder='static')
    app.secret_key = FLASK_SECRET_KEY

    @app.route('/health')
    def health_check():
        return "OK", 200

    with app.app_context():
        from . import auth
        app.register_blueprint(auth.bp)

        from . import chat
        app.register_blueprint(chat.bp)

        from . import admin
        app.register_blueprint(admin.bp)

        from . import settings
        app.register_blueprint(settings.bp)

        from .admin_reports import bp_reports
        app.register_blueprint(bp_reports)

    return app