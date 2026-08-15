from contextlib import contextmanager
import threading

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings

settings = get_settings()
engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
_local_bootstrap_lock = threading.Lock()
_POSTGRES_BOOTSTRAP_LOCK_ID = 7_307_226_281


class Base(DeclarativeBase):
    pass


@contextmanager
def bootstrap_lock():
    """Serialize schema/bootstrap work across API workers and the scheduler."""
    with _local_bootstrap_lock:
        if engine.dialect.name != "postgresql":
            yield
            return
        with engine.connect() as connection:
            connection.execute(text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": _POSTGRES_BOOTSTRAP_LOCK_ID})
            try:
                yield
            finally:
                connection.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": _POSTGRES_BOOTSTRAP_LOCK_ID})


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
