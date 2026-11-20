-- pyzzel migration 0001_initial

CREATE TABLE IF NOT EXISTS "pyzzel_test_compare_parent" ("id" INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "name" TEXT, "version" INTEGER NOT NULL DEFAULT 1);

CREATE TABLE IF NOT EXISTS "pyzzel_test_compare" ("id" INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "small" SMALLINT NOT NULL, "integer_value" INTEGER DEFAULT 0, "big" BIGINT, "floating" DOUBLE PRECISION, "amount" NUMERIC(10,2), "plain_text" TEXT, "bounded_text" CHARACTER VARYING(50), "flag" BOOLEAN, "day" DATE, "clock" TIME WITHOUT TIME ZONE, "aware" TIMESTAMP WITH TIME ZONE, "naive" TIMESTAMP WITHOUT TIME ZONE, "token" UUID, "payload" JSONB DEFAULT '{}', "binary" BYTEA, "parent_no_action" INTEGER REFERENCES "pyzzel_test_compare_parent" ("id"), "parent_restrict" INTEGER REFERENCES "pyzzel_test_compare_parent" ("id") ON DELETE RESTRICT, "parent_cascade" INTEGER REFERENCES "pyzzel_test_compare_parent" ("id") ON DELETE CASCADE, "parent_set_null" INTEGER REFERENCES "pyzzel_test_compare_parent" ("id") ON DELETE SET NULL, "self_id" INTEGER REFERENCES "pyzzel_test_compare" ("id") ON DELETE CASCADE, "unique_value" TEXT UNIQUE, "indexed_value" TEXT, "unindexed_fk" INTEGER REFERENCES "pyzzel_test_compare_parent" ("id"), "nullable_value" TEXT, "required_value" TEXT NOT NULL, "default_now" TIMESTAMP WITH TIME ZONE DEFAULT now(), "default_true" BOOLEAN DEFAULT TRUE, "default_active" TEXT DEFAULT 'active', "default_negative" INTEGER DEFAULT -1, "version" INTEGER NOT NULL DEFAULT 1);

CREATE INDEX IF NOT EXISTS "ix_pyzzel_test_compare_parent_no_action" ON "pyzzel_test_compare" ("parent_no_action");

CREATE INDEX IF NOT EXISTS "ix_pyzzel_test_compare_parent_restrict" ON "pyzzel_test_compare" ("parent_restrict");

CREATE INDEX IF NOT EXISTS "ix_pyzzel_test_compare_parent_cascade" ON "pyzzel_test_compare" ("parent_cascade");

CREATE INDEX IF NOT EXISTS "ix_pyzzel_test_compare_parent_set_null" ON "pyzzel_test_compare" ("parent_set_null");

CREATE INDEX IF NOT EXISTS "ix_pyzzel_test_compare_self_id" ON "pyzzel_test_compare" ("self_id");

CREATE INDEX IF NOT EXISTS "ix_pyzzel_test_compare_indexed_value" ON "pyzzel_test_compare" ("indexed_value");
