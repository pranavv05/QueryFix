package com.queryfix.sandbox.service;

import com.queryfix.sandbox.domain.Customer;
import com.queryfix.sandbox.domain.Order;
import com.queryfix.sandbox.repository.CustomerRepository;
import com.queryfix.sandbox.repository.OrderRepository;
import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class ReportingService {

    private final CustomerRepository customerRepository;
    private final OrderRepository orderRepository;

    public ReportingService(
            CustomerRepository customerRepository, OrderRepository orderRepository) {
        this.customerRepository = customerRepository;
        this.orderRepository = orderRepository;
    }

    /** One row of the summary report. */
    public record CustomerOrderSummary(
            Long customerId, String fullName, int orderCount, BigDecimal totalSpend) {}

    /*
     * PLANTED BUG: Spring Data derived method called inside a for loop (N+1).
     * findAll() issues one query, then orderRepository.findByCustomerId is
     * invoked once per customer — so a 5,000-customer report costs 5,001 round
     * trips instead of 1. Each individual query is index-backed and looks
     * healthy in isolation; the cost is entirely in the per-iteration latency,
     * which is why this is invisible to single-query analysis and has to be
     * caught by looking across the whole method.
     *
     * Expected QueryFix findings: N+1 access pattern (LLM stage, cross-query),
     * corroborated by the p6spy statement log showing findByCustomerId repeated
     * with only the bind parameter changing.
     */
    @Transactional(readOnly = true)
    public List<CustomerOrderSummary> buildCustomerOrderSummary() {
        List<Customer> customers = customerRepository.findAll();
        List<CustomerOrderSummary> summaries = new ArrayList<>(customers.size());

        for (Customer customer : customers) {
            List<Order> orders = orderRepository.findByCustomerId(customer.getId());

            BigDecimal totalSpend =
                    orders.stream()
                            .map(Order::getTotalAmount)
                            .reduce(BigDecimal.ZERO, BigDecimal::add);

            summaries.add(
                    new CustomerOrderSummary(
                            customer.getId(),
                            customer.getFullName(),
                            orders.size(),
                            totalSpend));
        }

        return summaries;
    }

    /**
     * Healthy comparison: the same report in two queries. Fetch every order once,
     * group in memory by customer id, then assemble. Two round trips regardless
     * of how many customers there are.
     *
     * <p>QueryFix should <em>not</em> flag this method. It is the false-positive
     * guard for the N+1 rule — a loop over customers that contains no query.
     */
    @Transactional(readOnly = true)
    public List<CustomerOrderSummary> buildCustomerOrderSummaryBatched() {
        List<Customer> customers = customerRepository.findAll();
        Map<Long, List<Order>> ordersByCustomer =
                orderRepository.findAll().stream()
                        .collect(Collectors.groupingBy(order -> order.getCustomer().getId()));

        return customers.stream()
                .map(
                        customer -> {
                            List<Order> orders =
                                    ordersByCustomer.getOrDefault(customer.getId(), List.of());
                            BigDecimal totalSpend =
                                    orders.stream()
                                            .map(Order::getTotalAmount)
                                            .reduce(BigDecimal.ZERO, BigDecimal::add);
                            return new CustomerOrderSummary(
                                    customer.getId(),
                                    customer.getFullName(),
                                    orders.size(),
                                    totalSpend);
                        })
                .collect(Collectors.toCollection(ArrayList::new));
    }

    /** Exercises the unindexed-country fixture so it appears in the statement log. */
    @Transactional(readOnly = true)
    public Map<String, String> customersByCountry(String countryCode) {
        return customerRepository.findByCountryCode(countryCode).stream()
                .collect(Collectors.toMap(Customer::getEmail, Customer::getFullName, (a, b) -> a));
    }

    /** Exercises the SELECT *-with-no-WHERE fixture. */
    @Transactional(readOnly = true)
    public int countExportedRows() {
        return orderRepository.exportAllOrders().size();
    }
}
