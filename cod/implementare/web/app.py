from __future__ import annotations

from flask import Flask

from web.config import HOST, PORT, STATIC_DIR, TEMPLATES_DIR
from web.routes.api import api_bp
from web.routes.pages import pages_bp


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(TEMPLATES_DIR),
        static_folder=str(STATIC_DIR),
    )
    app.register_blueprint(pages_bp)
    app.register_blueprint(api_bp)
    return app


def run() -> None:
    app = create_app()
    print(f"vibe-cli web panel  →  http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    run()