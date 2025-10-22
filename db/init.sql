-- Initial database bootstrap for the price monitoring system.
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Ensure the default schema exists
CREATE SCHEMA IF NOT EXISTS public;

-- Optional: set timezone for sessions
ALTER DATABASE price_monitor SET TIMEZONE TO 'Europe/Moscow';
