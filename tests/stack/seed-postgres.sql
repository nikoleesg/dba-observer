CREATE ROLE app_pg_user LOGIN PASSWORD 'SEKRIT-PG-PASS';
CREATE DATABASE app_pg OWNER app_pg_user;
\c app_pg
CREATE SCHEMA billing AUTHORIZATION app_pg_user;
CREATE TABLE billing.invoices (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, customer text NOT NULL, note text);
CREATE INDEX invoices_customer_idx ON billing.invoices (customer);
INSERT INTO billing.invoices (customer, note) VALUES ('acme', 'SEKRIT-ROW-VALUE');
ALTER TABLE billing.invoices OWNER TO app_pg_user;
CREATE TABLE billing.lockme (id int PRIMARY KEY, v int);
INSERT INTO billing.lockme VALUES (1, 0);
