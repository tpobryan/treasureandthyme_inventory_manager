from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate

limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")
db = SQLAlchemy()
migrate = Migrate()
