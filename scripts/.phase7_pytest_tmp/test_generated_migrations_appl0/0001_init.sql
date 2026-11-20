-- pyzzel migration 0001_init

CREATE TABLE IF NOT EXISTS "pyzzel_test_users" ("id" INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "name" CHARACTER VARYING(50) NOT NULL, "version" INTEGER NOT NULL DEFAULT 1);

CREATE TABLE IF NOT EXISTS "pyzzel_test_orders" ("id" INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "user_id" INTEGER NOT NULL REFERENCES "pyzzel_test_users" ("id") ON DELETE CASCADE, "total" NUMERIC(10,2) DEFAULT 0, "note" TEXT, "code" CHARACTER VARYING(20) UNIQUE, "version" INTEGER NOT NULL DEFAULT 1);

CREATE INDEX IF NOT EXISTS "ix_pyzzel_test_orders_user_id" ON "pyzzel_test_orders" ("user_id");

CREATE INDEX IF NOT EXISTS "ix_pyzzel_test_orders_note" ON "pyzzel_test_orders" ("note");
