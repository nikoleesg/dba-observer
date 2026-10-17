"""Settings from the environment, and the committed tool catalog."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from importlib.metadata import PackageNotFoundError, version

import yaml


def _version() -> str:
    try:
        return version("dba-observer")
    except PackageNotFoundError:  # running from a source tree without install
        return "0.0.0+source"


VERSION = _version()


@lru_cache(maxsize=1)
def catalog() -> dict:
    text = resources.files("dba_observer").joinpath("catalog.yaml").read_text()
    return yaml.safe_load(text)


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080

    redis_host: str | None = None
    redis_port: int = 6379
    redis_username: str | None = None
    redis_password: str | None = None
    redis_tls: bool = False

    pg_host: str | None = None
    pg_port: int = 5432
    pg_user: str | None = None
    pg_password: str | None = None
    pg_sslmode: str = "require"
    pg_home_database: str | None = None
    # Databases the catalog tools may connect to. Deny by default: empty = none.
    pg_databases: list[str] = field(default_factory=list)

    # MySQL is reached by Toolbox, not here; the adapter only TCP-probes it for tool_health.
    mysql_host: str | None = None
    mysql_port: int = 3306

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        return cls(
            listen_host=e.get("ADAPTER_LISTEN_HOST", "0.0.0.0"),
            listen_port=int(e.get("ADAPTER_LISTEN_PORT", "8080")),
            redis_host=e.get("REDIS_HOST") or None,
            redis_port=int(e.get("REDIS_PORT") or 6379),
            redis_username=e.get("REDIS_USERNAME") or None,
            redis_password=e.get("REDIS_PASSWORD") or None,
            redis_tls=(e.get("REDIS_TLS", "false").lower() == "true"),
            pg_host=e.get("PG_HOST") or None,
            pg_port=int(e.get("PG_PORT") or 5432),
            pg_user=e.get("PG_USER") or None,
            pg_password=e.get("PG_PASSWORD") or None,
            pg_sslmode=e.get("PG_SSLMODE") or "require",
            pg_home_database=e.get("PG_HOME_DATABASE") or None,
            pg_databases=_csv(e.get("PG_DATABASES")),
            mysql_host=e.get("MYSQL_HOST") or None,
            mysql_port=int(e.get("MYSQL_PORT") or 3306),
        )

    @property
    def redis_configured(self) -> bool:
        return bool(self.redis_host)

    @property
    def pg_configured(self) -> bool:
        return bool(self.pg_host and self.pg_user and self.pg_home_database)
