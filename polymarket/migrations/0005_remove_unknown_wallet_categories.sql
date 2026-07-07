-- UNKNOWN is a missing-category sentinel, not a proven trading category.
DELETE FROM wallet_category_stats
 WHERE category IS NULL
    OR TRIM(category) = ''
    OR UPPER(TRIM(category)) = 'UNKNOWN';

UPDATE wallet_trades
   SET category = NULL
 WHERE category IS NOT NULL
   AND (TRIM(category) = '' OR UPPER(TRIM(category)) = 'UNKNOWN');

UPDATE markets
   SET category = NULL
 WHERE category IS NOT NULL
   AND (TRIM(category) = '' OR UPPER(TRIM(category)) = 'UNKNOWN');
