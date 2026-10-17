-- dba-observer — PostgreSQL observer role. Proposed; apply only after the
-- operator approves. Placeholders: <PASSWORD>, <CATALOG_DB> (repeat per database).
--
-- pg_monitor = pg_read_all_settings + pg_read_all_stats + pg_stat_scan_tables:
-- every session's activity, replication, settings and database sizes. It
-- grants no SELECT on any user table. Catalog tools read pg_catalog only.

CREATE ROLE dba_observer LOGIN PASSWORD '<PASSWORD>' CONNECTION LIMIT 6;
GRANT pg_monitor TO dba_observer;

-- Belt and braces: the clients also set these per connection.
ALTER ROLE dba_observer SET default_transaction_read_only = on;
ALTER ROLE dba_observer SET statement_timeout = '5s';
ALTER ROLE dba_observer SET lock_timeout = '1s';
ALTER ROLE dba_observer SET idle_in_transaction_session_timeout = '30s';

-- Home database for cluster-wide tools. The `postgres` database is rejected
-- over the network by pg_hba, so the observer gets an empty one of its own.
CREATE DATABASE dba_observer OWNER postgres;
REVOKE ALL ON DATABASE dba_observer FROM PUBLIC;
GRANT CONNECT ON DATABASE dba_observer TO dba_observer;

-- Databases the per-database catalog tools may open (adapter PG_DATABASES).
GRANT CONNECT ON DATABASE <CATALOG_DB> TO dba_observer;

-- pg_hba.conf — append AFTER the `reject` rules, one line naming every database:
--   hostssl dba_observer,<CATALOG_DB>,... dba_observer <DOCKER_VM_IP>/32 scram-sha-256
-- then: SELECT pg_reload_conf();  (a reload, not a restart)
