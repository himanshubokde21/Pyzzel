-- pyzzel migration 0002_add_age

ALTER TABLE "pyzzel_test_compare" ADD COLUMN IF NOT EXISTS "age" INTEGER;
