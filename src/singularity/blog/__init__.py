"""Blog platform — Flask application factory + extensions."""
from __future__ import annotations

import os

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate

db = SQLAlchemy()
migrate = Migrate()


def create_app(config_object: str | object | None = None) -> Flask:
    app = Flask(__name__)
    if config_object is None:
        app.config.from_object("singularity.blog.config")
    elif isinstance(config_object, str):
        app.config.from_object(config_object)
    else:
        app.config.from_object(config_object)

    # Allow env override
    db_uri = os.environ.get("DATABASE_URL")
    if db_uri:
        app.config["SQLALCHEMY_DATABASE_URI"] = db_uri

    db.init_app(app)
    migrate.init_app(app, db)

    from singularity.blog import models  # noqa: F401 — register models

    return app
