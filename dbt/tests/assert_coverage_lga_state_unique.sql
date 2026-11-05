-- The coverage mart's key is (state, canonical_lga), not canonical_lga alone.
-- Several LGA names recur across states (Obi in Benue and Nasarawa, Surulere in
-- Oyo and Lagos, Bassa in Kogi and Plateau), so a bare name is not unique and
-- must never be used as the join key. This returns any (state, canonical_lga)
-- pair that appears more than once, which should be none.
select state, canonical_lga, count(*) as n
from {{ ref('lga_coverage_burden') }}
group by state, canonical_lga
having count(*) > 1
