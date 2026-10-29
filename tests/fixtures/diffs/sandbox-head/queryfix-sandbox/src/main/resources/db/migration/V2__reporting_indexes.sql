-- V2: reporting support for the nightly customer summary.
--
-- Recorded as the "added file" case in QueryFix's diff fixture.

CREATE INDEX idx_order_items_sku ON order_items (sku);

-- Deliberately unbounded: the statement this fixture expects a finding on.
SELECT * FROM orders;

DELETE FROM order_items;
