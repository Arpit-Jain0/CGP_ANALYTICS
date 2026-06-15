"""SQLAlchemy engine and session helpers."""

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.common.config import get_settings


def _build_engine():
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,         # recycle stale connections after a DB restart
        pool_size=5,
        max_overflow=10,
        echo=(settings.app_env == "development"),
    )


# Module-level singletons; re-used across the process lifetime.
engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Yield a transactional database session; rollback on any exception."""
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ping() -> bool:
    """Return True if the database is reachable."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
