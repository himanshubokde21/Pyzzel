-- pyzzel migration 0001_initial

CREATE TABLE IF NOT EXISTS "pyzzel_test_pending" ("id" INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "value" TEXT, "version" INTEGER NOT NULL DEFAULT 1);
