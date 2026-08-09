"""
app/models/database_connection.py
===================================
SQLAlchemy ORM model for persisting connection metadata.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Integer, Boolean, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseModel


class DatabaseConnectionRecord(BaseModel):
    __tablename__ = "database_connections"

    connection_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
        index=True,
    )
    db_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    database_name: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ssl_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="connected", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    pool_size: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    connect_timeout: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def mark_validated(self, now: datetime | None = None) -> None:
        self.status = "connected"
        self.last_validated_at = now or datetime.now(timezone.utc)
        self.error_message = None

    def mark_error(self, message: str) -> None:
        self.status = "error"
        self.error_message = message

    def mark_disconnected(self) -> None:
        self.status = "disconnected"
        self.error_message = None
