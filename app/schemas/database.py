"""app/schemas/database.py"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator


class DatabaseType(str, Enum):
    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    MONGODB = "mongodb"


class ConnectionStatus(str, Enum):
    CONNECTED = "connected"
    VALIDATING = "validating"
    ERROR = "error"
    DISCONNECTED = "disconnected"


class ConnectionOptions(BaseModel):
    pool_size: int = Field(default=5, ge=1, le=50)
    max_overflow: int = Field(default=10, ge=0, le=100)
    connect_timeout: int = Field(default=10, ge=1, le=120)
    query_timeout: int = Field(default=30, ge=1, le=300)
    pool_recycle: int = Field(default=1800, ge=60)
    ssl: bool = Field(default=False)
    server_selection_timeout_ms: int = Field(default=5000, ge=1000, le=60000)


class ConnectionRequest(BaseModel):
    db_type: DatabaseType
    host: str = Field(..., min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    database_name: str = Field(..., min_length=1, max_length=255)
    username: str = Field(..., min_length=1, max_length=255)
    password: SecretStr = Field(..., min_length=1)
    options: ConnectionOptions = Field(default_factory=ConnectionOptions)
    label: str | None = Field(default=None, max_length=100)

    @field_validator("host")
    @classmethod
    def sanitise_host(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("host must not be blank.")
        return v

    @model_validator(mode="after")
    def set_default_port(self) -> ConnectionRequest:
        default_ports = {
            DatabaseType.POSTGRESQL: 5432,
            DatabaseType.MYSQL: 3306,
            DatabaseType.MONGODB: 27017,
        }
        if self.port is None:
            self.port = default_ports[self.db_type]
        return self


class ConnectionMeta(BaseModel):
    connection_id: UUID
    db_type: DatabaseType
    host: str
    port: int
    database_name: str
    username: str
    label: str | None = None
    ssl: bool = False


class ConnectionResponse(BaseModel):
    connection_id: UUID
    status: ConnectionStatus = ConnectionStatus.CONNECTED
    message: str = "Connection established successfully."
    meta: ConnectionMeta
    connected_at: datetime


class ValidationResponse(BaseModel):
    connection_id: UUID
    status: ConnectionStatus
    message: str
    meta: ConnectionMeta
    last_validated_at: datetime
    latency_ms: float


class DisconnectResponse(BaseModel):
    connection_id: UUID
    status: ConnectionStatus = ConnectionStatus.DISCONNECTED
    message: str = "Connection closed and removed from the pool."
    disconnected_at: datetime


class ActiveConnectionSummary(BaseModel):
    connection_id: UUID
    db_type: DatabaseType
    host: str
    port: int
    database_name: str
    label: str | None = None
    status: ConnectionStatus
    connected_at: datetime
    last_used_at: datetime | None = None
