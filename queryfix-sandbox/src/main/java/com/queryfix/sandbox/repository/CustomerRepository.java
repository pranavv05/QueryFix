package com.queryfix.sandbox.repository;

import com.queryfix.sandbox.domain.Customer;
import java.math.BigDecimal;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

public interface CustomerRepository extends JpaRepository<Customer, Long> {

    /** Healthy: unique index on email. */
    Optional<Customer> findByEmail(String email);

    /** Healthy: indexed on signed_up_at, bounded range. */
    List<Customer> findBySignedUpAtAfter(OffsetDateTime signedUpAfter);

    /*
     * PLANTED BUG: JPQL @Query filtering on a column with no index.
     * `customers.country` is intentionally left unindexed in
     * V1__create_tables.sql, so this predicate forces a full sequential scan and
     * a filter on every row. The scan cost is proportional to the whole table
     * regardless of how selective the country happens to be, and it worsens
     * linearly as customers grow.
     *
     * Expected QueryFix findings: sequential scan on a large table (EXPLAIN
     * ANALYZE), plus an index suggestion measured via HypoPG — a hypothetical
     * btree on customers(country) should show a clear before/after cost drop.
     */
    @Query("SELECT c FROM Customer c WHERE c.country = :country ORDER BY c.signedUpAt DESC")
    List<Customer> findByCountryCode(@Param("country") String country);

    /*
     * PLANTED BUG: UPDATE query missing a WHERE clause. This rewrites
     * loyalty_tier for every row in `customers` — an unbounded write, with a row
     * lock taken on the entire table for the duration of the transaction. The
     * intent was almost certainly to promote one cohort, not the whole table.
     *
     * DESTRUCTIVE. This method is never called on any normal code path. The one
     * call site, MaintenanceService.promoteLoyaltyTier, is hard-guarded behind
     * `sandbox.allow-destructive-fixtures`, which defaults to false. Run it only
     * against a throwaway sandbox database you are willing to lose.
     *
     * Expected QueryFix findings: UPDATE without WHERE (static, critical),
     * unbounded write / full-table lock (static).
     */
    @Modifying
    @Query("UPDATE Customer c SET c.loyaltyTier = :tier")
    int promoteAllToTier(@Param("tier") String tier);

    /**
     * Healthy comparison: the write the fixture above was probably meant to be —
     * scoped to a cohort rather than the whole table.
     */
    @Modifying
    @Query("UPDATE Customer c SET c.loyaltyTier = :tier WHERE c.lifetimeValue >= :threshold")
    int promoteHighValueCustomers(
            @Param("tier") String tier, @Param("threshold") BigDecimal threshold);
}
