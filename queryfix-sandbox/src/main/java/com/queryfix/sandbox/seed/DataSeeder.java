package com.queryfix.sandbox.seed;

import com.github.javafaker.Faker;
import com.queryfix.sandbox.domain.OrderStatus;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.sql.Timestamp;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Random;
import java.util.Set;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.core.annotation.Order;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

/**
 * Seeds the sandbox with enough realistically-shaped data that {@code EXPLAIN
 * ANALYZE} produces meaningful plans.
 *
 * <p>Distributions are deliberately skewed rather than uniform, because uniform
 * data hides exactly the problems QueryFix looks for: with even spread, the
 * planner's row estimates are always right and a missing index costs little.
 * Specifically:
 *
 * <ul>
 *   <li><b>Orders per customer</b> — heavy tail. ~28% of customers never order;
 *       most of the rest place 1–4; a small VIP cohort places up to ~40. Row
 *       estimates for a customer join are therefore wrong for the tail.
 *   <li><b>Country</b> — weighted, US-dominant. A predicate on {@code country}
 *       is highly selective for {@code JP} and barely selective for {@code US},
 *       so a single missing index shows very different costs per bind value.
 *   <li><b>Order status</b> — weighted toward DELIVERED, matching a real backlog.
 *   <li><b>placed_at</b> — recency-weighted over 18 months, so date-range
 *       predicates behave like production ones.
 *   <li><b>Line totals</b> — long-tailed prices, a few high-value outliers.
 * </ul>
 *
 * <p>Runs on a fixed random seed so plan fixtures stay reproducible across runs.
 * Inserts go through {@link JdbcTemplate} batches with explicit ids, then the
 * identity sequences are restarted past the seeded range.
 */
@Component
@Order(DataSeeder.RUN_ORDER)
public class DataSeeder implements ApplicationRunner {

    /**
     * Seeding must precede {@link FixtureExerciser}. Both are runners, and with
     * neither annotated the relative order is unspecified — which would leave the
     * exerciser querying empty tables.
     */
    static final int RUN_ORDER = 0;

    private static final Logger log = LoggerFactory.getLogger(DataSeeder.class);

    private static final int BATCH_SIZE = 500;

    /** ISO country codes with cumulative weights; US-dominant on purpose. */
    private static final String[] COUNTRIES = {
        "US", "GB", "DE", "IN", "CA", "AU", "FR", "BR", "JP", "NL"
    };
    private static final double[] COUNTRY_CUMULATIVE = {
        0.40, 0.52, 0.62, 0.71, 0.78, 0.83, 0.88, 0.92, 0.96, 1.00
    };

    private static final OrderStatus[] STATUSES = {
        OrderStatus.DELIVERED,
        OrderStatus.SHIPPED,
        OrderStatus.PAID,
        OrderStatus.PENDING,
        OrderStatus.CANCELLED,
        OrderStatus.REFUNDED
    };
    private static final double[] STATUS_CUMULATIVE = {0.62, 0.76, 0.86, 0.94, 0.99, 1.00};

    private static final String[] LOYALTY_TIERS = {"STANDARD", "SILVER", "GOLD", "PLATINUM"};
    private static final double[] TIER_CUMULATIVE = {0.70, 0.88, 0.97, 1.00};

    private static final int HISTORY_DAYS = 540;

    private final JdbcTemplate jdbcTemplate;
    private final boolean seedOnStartup;
    private final int customerCount;
    private final long randomSeed;

    public DataSeeder(
            JdbcTemplate jdbcTemplate,
            @Value("${sandbox.seed.seed-on-startup:true}") boolean seedOnStartup,
            @Value("${sandbox.seed.customers:5000}") int customerCount,
            @Value("${sandbox.seed.random-seed:20260805}") long randomSeed) {
        this.jdbcTemplate = jdbcTemplate;
        this.seedOnStartup = seedOnStartup;
        this.customerCount = customerCount;
        this.randomSeed = randomSeed;
    }

    /**
     * The transaction is declared here rather than on {@link #seed()} because
     * Spring calls {@code run} through the bean proxy; a {@code this.seed()}
     * self-invocation would bypass the proxy and run unmanaged.
     */
    @Override
    @Transactional
    public void run(ApplicationArguments args) {
        if (!seedOnStartup) {
            log.info("Seeding disabled (sandbox.seed.seed-on-startup=false)");
            return;
        }
        Integer existing =
                jdbcTemplate.queryForObject("SELECT count(*) FROM customers", Integer.class);
        if (existing != null && existing > 0) {
            log.info("Customers table already has {} rows; skipping seed", existing);
            return;
        }
        seed();
    }

    void seed() {
        Random random = new Random(randomSeed);
        Faker faker = new Faker(Locale.US, random);
        OffsetDateTime now = OffsetDateTime.now(ZoneOffset.UTC);

        log.info("Seeding {} customers (seed={})", customerCount, randomSeed);

        List<Object[]> customerRows = new ArrayList<>(customerCount);
        Set<String> emails = new HashSet<>(customerCount * 2);

        long nextOrderId = 1L;
        long nextItemId = 1L;
        List<Object[]> orderRows = new ArrayList<>();
        List<Object[]> itemRows = new ArrayList<>();

        for (long customerId = 1; customerId <= customerCount; customerId++) {
            String fullName = faker.name().fullName();
            String email = uniqueEmail(faker, emails, customerId);
            String country = pick(COUNTRIES, COUNTRY_CUMULATIVE, random);
            OffsetDateTime signedUpAt = now.minusDays(random.nextInt(HISTORY_DAYS + 180) + 1);

            int orderCount = ordersForCustomer(random);
            BigDecimal lifetimeValue = BigDecimal.ZERO;

            for (int o = 0; o < orderCount; o++) {
                long orderId = nextOrderId++;
                // Orders never predate signup, and recent dates are more likely.
                OffsetDateTime placedAt = recencyWeightedDate(now, signedUpAt, random);
                OrderStatus status = pick(STATUSES, STATUS_CUMULATIVE, random);

                int itemCount = itemsForOrder(random);
                BigDecimal orderTotal = BigDecimal.ZERO;

                for (int i = 0; i < itemCount; i++) {
                    int quantity = quantityForItem(random);
                    BigDecimal unitPrice = priceForItem(random);
                    orderTotal =
                            orderTotal.add(unitPrice.multiply(BigDecimal.valueOf(quantity)));

                    itemRows.add(
                            new Object[] {
                                nextItemId++,
                                orderId,
                                faker.bothify("SKU-####-????").toUpperCase(Locale.ROOT),
                                faker.commerce().productName(),
                                quantity,
                                unitPrice
                            });
                }

                orderRows.add(
                        new Object[] {
                            orderId,
                            customerId,
                            String.format("ORD-%08d", orderId),
                            status.name(),
                            orderTotal,
                            Timestamp.from(placedAt.toInstant())
                        });

                // Cancelled and refunded orders do not count toward lifetime value.
                if (status != OrderStatus.CANCELLED && status != OrderStatus.REFUNDED) {
                    lifetimeValue = lifetimeValue.add(orderTotal);
                }
            }

            customerRows.add(
                    new Object[] {
                        customerId,
                        email,
                        fullName,
                        country,
                        pick(LOYALTY_TIERS, TIER_CUMULATIVE, random),
                        lifetimeValue.setScale(2, RoundingMode.HALF_UP),
                        Timestamp.from(signedUpAt.toInstant())
                    });
        }

        // Insert in FK dependency order: customers, then orders, then items.
        // Everything is buffered until here rather than flushed mid-loop, because
        // an order batch written before its customer row would fail the FK.
        flushCustomers(customerRows);
        flushOrders(orderRows);
        flushItems(itemRows);

        restartIdentity("customers", customerCount + 1);
        restartIdentity("orders", nextOrderId);
        restartIdentity("order_items", nextItemId);

        log.info(
                "Seeded {} customers, {} orders, {} order items",
                customerCount,
                nextOrderId - 1,
                nextItemId - 1);
    }

    // -- insert helpers -----------------------------------------------------

    private void flushCustomers(List<Object[]> rows) {
        flushChunked(
                "INSERT INTO customers "
                        + "(id, email, full_name, country, loyalty_tier, lifetime_value, signed_up_at) "
                        + "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows);
    }

    private void flushOrders(List<Object[]> rows) {
        flushChunked(
                "INSERT INTO orders "
                        + "(id, customer_id, order_number, status, total_amount, placed_at) "
                        + "VALUES (?, ?, ?, ?, ?, ?)",
                rows);
    }

    private void flushItems(List<Object[]> rows) {
        flushChunked(
                "INSERT INTO order_items "
                        + "(id, order_id, sku, product_name, quantity, unit_price) "
                        + "VALUES (?, ?, ?, ?, ?, ?)",
                rows);
    }

    /** Batches in fixed-size chunks so one insert does not build a 60k-row batch. */
    private void flushChunked(String sql, List<Object[]> rows) {
        for (int from = 0; from < rows.size(); from += BATCH_SIZE) {
            int to = Math.min(from + BATCH_SIZE, rows.size());
            jdbcTemplate.batchUpdate(sql, rows.subList(from, to));
        }
        rows.clear();
    }

    private void restartIdentity(String table, long nextValue) {
        jdbcTemplate.execute(
                "ALTER TABLE " + table + " ALTER COLUMN id RESTART WITH " + nextValue);
    }

    // -- distributions ------------------------------------------------------

    /**
     * Heavy-tailed order count: ~28% of customers never order, most place a
     * handful, a small VIP cohort places dozens.
     */
    private static int ordersForCustomer(Random random) {
        double roll = random.nextDouble();
        if (roll < 0.28) {
            return 0;
        }
        if (roll < 0.80) {
            return 1 + random.nextInt(4);
        }
        if (roll < 0.97) {
            return 5 + random.nextInt(8);
        }
        return 13 + random.nextInt(28);
    }

    /** Most baskets are small; a few are large. */
    private static int itemsForOrder(Random random) {
        double roll = random.nextDouble();
        if (roll < 0.55) {
            return 1 + random.nextInt(2);
        }
        if (roll < 0.90) {
            return 3 + random.nextInt(3);
        }
        return 6 + random.nextInt(5);
    }

    private static int quantityForItem(Random random) {
        double roll = random.nextDouble();
        if (roll < 0.72) {
            return 1;
        }
        if (roll < 0.94) {
            return 2 + random.nextInt(2);
        }
        return 4 + random.nextInt(9);
    }

    /** Log-normal-ish prices: cheap median, a thin tail of expensive items. */
    private static BigDecimal priceForItem(Random random) {
        double value = Math.exp(2.6 + random.nextGaussian() * 0.85);
        double clamped = Math.min(Math.max(value, 1.99), 4999.0);
        return BigDecimal.valueOf(clamped).setScale(2, RoundingMode.HALF_UP);
    }

    /**
     * Dates skewed toward the present — squaring a uniform roll biases the offset
     * toward zero days ago — and never earlier than the customer's signup.
     */
    private static OffsetDateTime recencyWeightedDate(
            OffsetDateTime now, OffsetDateTime signedUpAt, Random random) {
        double roll = random.nextDouble();
        int daysAgo = (int) (HISTORY_DAYS * roll * roll);
        OffsetDateTime candidate = now.minusDays(daysAgo).minusMinutes(random.nextInt(1440));
        return candidate.isBefore(signedUpAt) ? signedUpAt.plusHours(random.nextInt(72) + 1) : candidate;
    }

    private static <T> T pick(T[] values, double[] cumulative, Random random) {
        double roll = random.nextDouble();
        for (int i = 0; i < cumulative.length; i++) {
            if (roll < cumulative[i]) {
                return values[i];
            }
        }
        return values[values.length - 1];
    }

    /** Faker recycles names, and email is uniquely indexed — suffix on collision. */
    private static String uniqueEmail(Faker faker, Set<String> seen, long customerId) {
        String candidate = faker.internet().emailAddress();
        if (seen.add(candidate)) {
            return candidate;
        }
        int at = candidate.indexOf('@');
        String unique = candidate.substring(0, at) + "+" + customerId + candidate.substring(at);
        seen.add(unique);
        return unique;
    }
}
