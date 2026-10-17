"""Structured, non-sensitive tool errors.

Driver exceptions can carry hostnames, usernames, statement text and, for some
drivers, connection strings. None of that is passed through: every exception is
mapped to a fixed status and code, with a fixed message. The original exception
type is logged locally (never its message) so an operator can still debug.
"""

from __future__ import annotations

import psycopg
import redis


class ToolError(Exception):
    """An expected failure with a status the caller can act on."""

    def __init__(self, status: str, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def not_configured(what: str) -> ToolError:
    return ToolError("not_configured", "not_configured", f"{what} is not configured on this server")


def invalid(message: str) -> ToolError:
    return ToolError("invalid_argument", "invalid_argument", message)


def classify(exc: BaseException) -> ToolError:
    if isinstance(exc, ToolError):
        return exc

    # --- Redis ---------------------------------------------------------------
    if isinstance(exc, redis.AuthenticationError):
        return ToolError("permission_denied", "auth_failed", "Redis rejected the observer credential")
    if isinstance(exc, redis.TimeoutError):
        return ToolError("unavailable", "timeout", "Redis did not answer within the time limit")
    if isinstance(exc, redis.ConnectionError):
        return ToolError("unavailable", "connection_failed", "Redis is unreachable")
    if isinstance(exc, redis.ResponseError):
        text = str(exc)
        if text.startswith("NOPERM"):
            return ToolError("permission_denied", "noperm", "The observer ACL user is not allowed this command")
        if "unknown command" in text.lower() or "unknown subcommand" in text.lower():
            return ToolError("unsupported", "unsupported_command", "This Redis version does not support the command")
        return ToolError("error", "redis_error", "Redis returned an error")

    # --- PostgreSQL ----------------------------------------------------------
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return ToolError("error", "timeout", "The query exceeded the statement timeout")
    if isinstance(exc, psycopg.errors.LockNotAvailable):
        return ToolError("error", "lock_timeout", "The query could not get a lock within the lock timeout")
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return ToolError("permission_denied", "insufficient_privilege", "The observer role lacks a privilege this tool needs")
    if isinstance(exc, psycopg.OperationalError):
        text = str(exc).lower()
        if "password authentication failed" in text:
            return ToolError("permission_denied", "auth_failed", "PostgreSQL rejected the observer credential")
        if "no pg_hba.conf entry" in text:
            return ToolError("permission_denied", "hba_rejected", "pg_hba.conf does not allow the observer into this database")
        if "does not exist" in text:
            return ToolError("not_found", "database_not_found", "The database does not exist")
        if "timeout" in text:
            return ToolError("unavailable", "timeout", "PostgreSQL did not answer within the time limit")
        return ToolError("unavailable", "connection_failed", "PostgreSQL is unreachable")
    if isinstance(exc, psycopg.Error):
        return ToolError("error", "postgresql_error", "PostgreSQL returned an error")

    return ToolError("error", "internal_error", "Internal adapter error")
