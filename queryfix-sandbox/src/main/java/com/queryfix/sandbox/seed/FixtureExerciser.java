package com.queryfix.sandbox.seed;

import com.queryfix.sandbox.service.ReportingService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;

/**
 * Executes the read-path fixtures so they appear in the p6spy statement log.
 *
 * <p>QueryFix's N+1 stage corroborates a source-level finding against the
 * statements a real run actually issued. That log is empty unless something calls
 * the fixtures — analysing the source proves the loop exists, but only running it
 * proves the query count. This runner is that call site.
 *
 * <p>Off by default: seeding is the normal reason to boot this app, and a full
 * exercise pass issues one query per seeded customer. Enable with
 * {@code sandbox.exercise-fixtures=true}.
 *
 * <p>Read-path only. The {@code UPDATE}-without-{@code WHERE} fixture is
 * deliberately excluded — it stays behind {@code MaintenanceService}'s own guard
 * and is never executed by any automated path.
 */
@Component
@Order(FixtureExerciser.RUN_ORDER)
public class FixtureExerciser implements ApplicationRunner {

    /** Must run after {@link DataSeeder}, or there is no data to query. */
    static final int RUN_ORDER = DataSeeder.RUN_ORDER + 1;

    private static final Logger log = LoggerFactory.getLogger(FixtureExerciser.class);

    private final ReportingService reportingService;
    private final boolean enabled;

    public FixtureExerciser(
            ReportingService reportingService,
            @Value("${sandbox.exercise-fixtures:false}") boolean enabled) {
        this.reportingService = reportingService;
        this.enabled = enabled;
    }

    @Override
    public void run(ApplicationArguments args) {
        if (!enabled) {
            log.info("Fixture exercise disabled (sandbox.exercise-fixtures=false)");
            return;
        }

        // The N+1 itself: one findAll plus one findByCustomerId per customer.
        long before = System.nanoTime();
        int rows = reportingService.buildCustomerOrderSummary().size();
        log.info(
                "N+1 fixture: {} summary rows in {} ms — expect {} statements in the p6spy log",
                rows,
                (System.nanoTime() - before) / 1_000_000,
                rows + 1);

        // The batched counterpart, for the same result in two statements. The
        // ratio between these two timings is the fixture's whole point.
        before = System.nanoTime();
        int batchedRows = reportingService.buildCustomerOrderSummaryBatched().size();
        log.info(
                "Batched counterpart: {} summary rows in {} ms — expect 2 statements",
                batchedRows,
                (System.nanoTime() - before) / 1_000_000);

        if (rows != batchedRows) {
            // Not a fixture problem but a correctness one: the two methods are
            // meant to be interchangeable, so a mismatch means one is wrong.
            log.error(
                    "Fixture disagreement: N+1 produced {} rows, batched produced {}",
                    rows,
                    batchedRows);
        }

        // Sequential-scan fixture: `country` is unindexed on purpose.
        log.info("Unindexed-country fixture: {} matches", reportingService.customersByCountry("US").size());

        // SELECT *-with-no-WHERE fixture.
        log.info("Unbounded-export fixture: {} rows", reportingService.countExportedRows());
    }
}
