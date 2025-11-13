from flask import Flask
from config import FLASK_SECRET_KEY

def create_app():
    """
    Application factory to create and configure the Flask app.
    """
    # Note: We specify template_folder and static_folder
    # to match the new package structure.
    app = Flask(__name__, template_folder='templates', static_folder='static')
    app.secret_key = FLASK_SECRET_KEY

    with app.app_context():
        # Import and register the blueprints
        
        from . import auth
        app.register_blueprint(auth.bp)

        from . import chat
        app.register_blueprint(chat.bp)

        from . import admin
        app.register_blueprint(admin.bp)

        from . import settings
        app.register_blueprint(settings.bp)

    return app