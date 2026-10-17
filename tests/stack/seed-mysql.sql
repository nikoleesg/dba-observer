-- Application data and an application account, both carrying planted secrets.
CREATE DATABASE app_one;
CREATE TABLE app_one.customers (id INT PRIMARY KEY, email VARCHAR(100) UNIQUE, note VARCHAR(100));
INSERT INTO app_one.customers VALUES (1, 'a@example.com', 'SEKRIT-ROW-VALUE');
CREATE TABLE app_one.orders (id INT PRIMARY KEY, customer_id INT, total DECIMAL(10,2),
  FOREIGN KEY (customer_id) REFERENCES app_one.customers(id));
CREATE TABLE app_one.audit_log (msg VARCHAR(100));  -- no primary or unique key
CREATE DATABASE app_hidden;  -- observer gets no privilege here
CREATE TABLE app_hidden.t (id INT PRIMARY KEY);
CREATE USER 'app_user'@'%' IDENTIFIED BY 'SEKRIT-MYSQL-PASS';
GRANT SELECT, INSERT, UPDATE, DELETE ON app_one.* TO 'app_user'@'%';
GRANT SELECT ON app_one.customers TO 'app_user'@'%';
-- Used only by the lock-wait test, so the seeded tables above stay untouched.
CREATE TABLE app_one.lockme (id INT PRIMARY KEY, v INT);
INSERT INTO app_one.lockme VALUES (1, 0);
