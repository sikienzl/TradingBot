-- Run once as an administrator in the existing analytics database.
-- The schema, tables and indexes must already exist. Passwords are set separately.
CREATE ROLE trading_analytics_writer LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT;
CREATE ROLE trading_dashboard_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT;

-- Avoid inheriting rights on these objects via PUBLIC.
REVOKE ALL ON SCHEMA trading_analytics FROM PUBLIC;
REVOKE ALL ON trading_analytics.trade_events, trading_analytics.portfolio_snapshots FROM PUBLIC;
REVOKE ALL ON SEQUENCE trading_analytics.trade_events_id_seq, trading_analytics.portfolio_snapshots_id_seq FROM PUBLIC;

-- The existing writer issues CREATE TABLE/INDEX IF NOT EXISTS on every connect.
ALTER TABLE trading_analytics.trade_events OWNER TO trading_analytics_writer;
ALTER TABLE trading_analytics.portfolio_snapshots OWNER TO trading_analytics_writer;
GRANT USAGE, CREATE ON SCHEMA trading_analytics TO trading_analytics_writer;
GRANT USAGE ON SCHEMA trading_analytics TO trading_dashboard_reader;
GRANT SELECT ON trading_analytics.trade_events, trading_analytics.portfolio_snapshots TO trading_dashboard_reader;
ALTER ROLE trading_dashboard_reader SET default_transaction_read_only = on;

-- CREATE SCHEMA IF NOT EXISTS is also issued by the writer.
DO $$
BEGIN
    EXECUTE format('GRANT CONNECT, CREATE ON DATABASE %I TO trading_analytics_writer', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO trading_dashboard_reader', current_database());
END
$$;
