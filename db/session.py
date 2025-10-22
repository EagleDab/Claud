"""Database engine and session helpers."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from pricing.config import get_settings
from db.models import Base

_SETTINGS = get_settings()
_ENGINE = create_engine(_SETTINGS.database_url, pool_pre_ping=True, future=True)
_SessionFactory = sessionmaker(bind=_ENGINE, expire_on_commit=False, autoflush=False, class_=Session)


def init_database() -> None:
    """Create database tables if they do not exist."""

    Base.metadata.create_all(_ENGINE)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional scope for a series of operations."""

    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


__all__ = ["session_scope", "init_database", "_ENGINE", "_SessionFactory"]
