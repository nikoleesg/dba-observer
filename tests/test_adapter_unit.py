"""Adapter behaviour that needs no database."""

import psycopg
import pytest
import redis

from dba_observer import redis_tools
from dba_observer.config import Settings
from dba_observer.errors import ToolError, classify
from dba_observer.server import ROUTES, Adapter, parse_args


def test_parse_args_rejects_unknown_and_bounds():
    r = ROUTES["pg_list_tables"]
    with pytest.raises(ToolError) as e:
        parse_args(r, {"database": ["x"], "sql": ["DROP"]})
    assert e.value.status == "invalid_argument"
    with pytest.raises(ToolError):
        parse_args(r, {"database": ["x"], "limit": ["501"]})
    with pytest.raises(ToolError):
        parse_args(r, {"limit": ["5"]})  # database required
    assert parse_args(r, {"database": ["x"]}) == {"database": "x", "schema": "", "limit": 25}


def test_unconfigured_targets_report_not_configured():
    a = Adapter(Settings())
    for tool, q in (("redis_health", {}), ("pg_list_schemas", {"database": ["x"]})):
        out = a.call(tool, q)
        assert out["status"] == "not_configured", out


def test_pg_database_must_be_allowlisted():
    s = Settings(pg_host="h", pg_user="u", pg_home_database="home", pg_databases=["app"])
    out = Adapter(s).call("pg_list_tables", {"database": ["other"]})
    assert out["status"] == "not_configured"
    assert out["error"]["code"] == "database_not_allowed"


def test_identifier_validation():
    s = Settings(pg_host="h", pg_user="u", pg_home_database="home", pg_databases=["app"])
    out = Adapter(s).call("pg_describe_table", {"database": ["app"], "schema": ["public"], "table": ["x; DROP TABLE y"]})
    assert out["status"] == "invalid_argument"


def test_error_messages_never_carry_driver_text():
    leaky = [
        redis.ConnectionError("Error connecting to 10.0.0.1:6379 user=secret"),
        redis.ResponseError("WRONGPASS secret-detail"),
        psycopg.OperationalError('connection to server at "10.0.0.1" failed: password authentication failed for user "x"'),
        psycopg.Error("ERROR: something with 'SEKRIT' literal"),
        RuntimeError("SEKRIT"),
    ]
    for exc in leaky:
        e = classify(exc)
        assert "SEKRIT" not in e.message and "secret" not in e.message and "10.0.0.1" not in e.message


def test_acl_hashes_never_returned(monkeypatch):
    class FakeRedis:
        def acl_getuser(self, name):
            return {"flags": ["on"], "passwords": ["5e88489...hash"], "commands": "+@all",
                    "keys": ["~*"], "channels": ["&*"], "selectors": []}

        def acl_users(self):
            return ["u1"]

    monkeypatch.setattr(redis_tools, "client", lambda s: FakeRedis())
    out = redis_tools.acl_permissions(Settings(redis_host="r"), {"user": "u1"})
    assert out["password_count"] == 1
    assert "5e88489" not in repr(out)
    out = redis_tools.acl_users(Settings(redis_host="r"), {})
    assert "5e88489" not in repr(out)


def test_config_allowlists_exclude_secrets():
    for name in redis_tools.CONFIG_FIELDS + redis_tools.PERSISTENCE_CONFIG_FIELDS:
        assert name not in ("requirepass", "masterauth", "masteruser", "aclfile", "dir", "dbfilename")
