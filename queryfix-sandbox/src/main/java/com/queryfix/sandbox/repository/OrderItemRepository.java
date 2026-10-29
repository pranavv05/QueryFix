package com.queryfix.sandbox.repository;

import com.queryfix.sandbox.domain.OrderItem;
import java.util.Collection;
import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;

public interface OrderItemRepository extends JpaRepository<OrderItem, Long> {

    /**
     * Derived query, indexed on order_id. Fine on its own; becomes an N+1 when
     * called once per order in a loop.
     */
    List<OrderItem> findByOrderId(Long orderId);

    /**
     * Healthy: the batched form of the method above. One round trip for a whole
     * page of orders instead of one per order.
     */
    List<OrderItem> findByOrderIdIn(Collection<Long> orderIds);
}
