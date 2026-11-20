-- pyzzel migration 0002_changes

ALTER TABLE "pyzzel_test_orders" DROP CONSTRAINT IF EXISTS "pyzzel_test_orders_code_key";

-- pyzzel:destructive
ALTER TABLE "pyzzel_test_orders" DROP COLUMN IF EXISTS "note";

CREATE TABLE IF NOT EXISTS "pyzzel_test_coupons" ("id" INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "code" CHARACTER VARYING(20) UNIQUE, "version" INTEGER NOT NULL DEFAULT 1);

ALTER TABLE "pyzzel_test_orders" ADD COLUMN IF NOT EXISTS "status" CHARACTER VARYING(10) NOT NULL DEFAULT 'new';

-- pyzzel:destructive
ALTER TABLE "pyzzel_test_orders" ALTER COLUMN "total" TYPE NUMERIC(12,2);
