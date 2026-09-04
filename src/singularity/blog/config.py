# Blog platform config
import os

SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
SQLALCHEMY_DATABASE_URI = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://blog:blog@localhost:5432/blog"
)
SQLALCHEMY_TRACK_MODIFICATIONS = False

# JWT settings
JWT_ACCESS_EXPIRES = 900          # 15 minutes
JWT_REFRESH_EXPIRES = 7 * 86400   # 7 days

# Argon2id params
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 65536  # 64 MB
ARGON2_PARALLELISM = 4
ARGON2_HASH_LEN = 32
