package com.queryfix.sandbox.repository;

import com.queryfix.sandbox.domain.Order;
import com.queryfix.sandbox.domain.OrderStatus;
import java.time.OffsetDateTime;
import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

public interface OrderRepository extends JpaRepository<Order, Long> {

    /**
     * Derived query used by the N+1 fixture in
     * {@code ReportingService.buildCustomerOrderSummary}. The method itself is
     * fine — indexed on {@code customer_id}, bounded result set. The bug is the
     * call site.
     */
    List<Order> findByCustomerId(Long customerId);

    /** Healthy comparison query: indexed column, bounded range. */
    List<Order> findByStatusAndPlacedAtAfter(OrderStatus status, OffsetDateTime placedAfter);

    /** Added in the fixture PR: the first of two hunks in this file. */
    List<Order> findByStatusAndCustomerId(OrderStatus status, Long customerId);

    /*
     * PLANTED BUG: native query using SELECT * with no WHERE clause. Reads every
     * column of every row in `orders` and materialises the whole table into the
     * heap, with no bound on result size. Cost grows without limit as the table
     * does, and `SELECT *` also defeats any index-only scan and breaks silently
     * when a column is added or reordered.
     *
     * Expected QueryFix findings: SELECT * (static), missing WHERE clause
     * (static), unbounded result set (static), sequential scan on a large table
     * (EXPLAIN ANALYZE).
     */
    @Query(value = "SELECT * FROM orders", nativeQuery = true)
    List<Object[]> exportAllOrders();

    /**
     * Healthy comparison query: same shape as the fixture above, but projects
     * named columns and bounds the scan with an indexed predicate.
     */
    @Query(
            value =
                    "SELECT id, order_number, status, total_amount "
                            + "FROM orders WHERE placed_at >= :since",
            nativeQuery = true)
    List<Object[]> exportRecentOrders(@Param("since") OffsetDateTime since);
    /**
     * Added in the fixture PR: the second hunk, far enough from the first that
     * git emits two separate hunks rather than one.
     */
    @Query(value = "SELECT * FROM order_items WHERE sku LIKE '%-REFURB'", nativeQuery = true)
    List<Object[]> findRefurbishedItems();
}
