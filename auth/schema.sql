-- Схема БД для авторизации site-checker (Supabase / Postgres).
-- Прогнать ЦЕЛИКОМ один раз в новом Supabase-проекте: SQL Editor → New query → Run.
-- Остальные таблицы (sessions) создаст сам код при первом обращении.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE user_role AS ENUM ('admin', 'manager', 'specialist');
CREATE TYPE user_status AS ENUM ('pending', 'active', 'disabled');

CREATE TABLE users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email         text UNIQUE NOT NULL,
    password_hash text NOT NULL,
    first_name    text NOT NULL,
    last_name     text NOT NULL,
    role          user_role NOT NULL,
    status        user_status NOT NULL DEFAULT 'pending',
    manager_id    uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE user_projects (
    user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_key text NOT NULL,
    PRIMARY KEY (user_id, project_key)
);

CREATE TABLE invite_codes (
    code       text PRIMARY KEY,
    manager_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       user_role,
    expires_at timestamptz NOT NULL,
    used_by    uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE password_resets (
    token      text PRIMARY KEY,
    user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL,
    used       boolean NOT NULL DEFAULT false
);
