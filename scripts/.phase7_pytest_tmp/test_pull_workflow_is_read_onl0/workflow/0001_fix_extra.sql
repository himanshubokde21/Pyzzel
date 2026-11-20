-- pyzzel migration 0001_fix_extra

-- pyzzel:destructive
ALTER TABLE "pyzzel_test_compare" DROP COLUMN IF EXISTS "manual_extra";
