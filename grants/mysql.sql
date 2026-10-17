-- dba-observer — MySQL observer account. Proposed grants; apply only after the
-- operator approves. Placeholders: <DOCKER_VM_IP>, <PASSWORD>, <APP_SCHEMA>.
--
-- No SELECT on any application schema: the account reads metadata
-- (information_schema, performance_schema, sys) and the grant tables below,
-- never rows. mysql.user is granted by COLUMN so authentication_string (the
-- password hash) is unreachable.

CREATE USER 'dba_observer'@'<DOCKER_VM_IP>' IDENTIFIED BY '<PASSWORD>'
  WITH MAX_USER_CONNECTIONS 5;

-- Sessions, InnoDB transactions/metrics, replication status.
GRANT PROCESS, REPLICATION CLIENT ON *.* TO 'dba_observer'@'<DOCKER_VM_IP>';
GRANT SELECT ON performance_schema.* TO 'dba_observer'@'<DOCKER_VM_IP>';
-- sys views are SQL SECURITY INVOKER and call sys functions.
GRANT SELECT, EXECUTE ON sys.* TO 'dba_observer'@'<DOCKER_VM_IP>';

-- Accounts and grants, without the password hash column.
GRANT SELECT (User, Host, plugin, account_locked, password_expired, password_last_changed,
              max_connections, max_user_connections,
              Select_priv, Insert_priv, Update_priv, Delete_priv, Create_priv, Drop_priv, Reload_priv,
              Shutdown_priv, Process_priv, File_priv, Grant_priv, References_priv, Index_priv, Alter_priv,
              Show_db_priv, Super_priv, Create_tmp_table_priv, Lock_tables_priv, Execute_priv,
              Repl_slave_priv, Repl_client_priv, Create_view_priv, Show_view_priv, Create_routine_priv,
              Alter_routine_priv, Create_user_priv, Event_priv, Trigger_priv, Create_tablespace_priv,
              Create_role_priv, Drop_role_priv)
  ON mysql.user TO 'dba_observer'@'<DOCKER_VM_IP>';
GRANT SELECT ON mysql.db TO 'dba_observer'@'<DOCKER_VM_IP>';
GRANT SELECT ON mysql.tables_priv TO 'dba_observer'@'<DOCKER_VM_IP>';
GRANT SELECT ON mysql.global_grants TO 'dba_observer'@'<DOCKER_VM_IP>';
GRANT SELECT ON mysql.role_edges TO 'dba_observer'@'<DOCKER_VM_IP>';

-- Metadata visibility. MySQL shows a schema's tables, columns, indexes and keys
-- in information_schema only to accounts holding some privilege on them.
-- REFERENCES is the one that reveals all four without granting row access
-- (SHOW VIEW hides columns and keys). It would let an account declare a
-- foreign key into the schema, but that needs CREATE/ALTER on a table of its
-- own, which this account has nowhere. Verified by
-- tests/test_integration.py::test_mysql_metadata_visible_rows_not.
-- Repeat per application schema.
GRANT REFERENCES ON `<APP_SCHEMA>`.* TO 'dba_observer'@'<DOCKER_VM_IP>';
