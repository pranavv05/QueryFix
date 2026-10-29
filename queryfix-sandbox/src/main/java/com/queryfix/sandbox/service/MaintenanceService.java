package com.queryfix.sandbox.service;

import com.queryfix.sandbox.repository.CustomerRepository;
import java.math.BigDecimal;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * Call sites for the write-path fixtures.
 *
 * <p>The destructive one is guarded. QueryFix analyzes source, so the fixture
 * does its job without ever being executed.
 */
@Service
public class MaintenanceService {

    private final CustomerRepository customerRepository;
    private final boolean destructiveFixturesAllowed;

    public MaintenanceService(
            CustomerRepository customerRepository,
            @Value("${sandbox.allow-destructive-fixtures:false}")
                    boolean destructiveFixturesAllowed) {
        this.customerRepository = customerRepository;
        this.destructiveFixturesAllowed = destructiveFixturesAllowed;
    }

    /**
     * Call site for {@code CustomerRepository.promoteAllToTier}, the
     * UPDATE-without-WHERE fixture.
     *
     * <p>Refuses to run unless {@code sandbox.allow-destructive-fixtures=true}.
     * Enabling that against anything other than a disposable database will
     * rewrite every customer's loyalty tier and hold a row lock on the whole
     * table until the transaction ends.
     */
    @Transactional
    public int promoteLoyaltyTier(String tier) {
        if (!destructiveFixturesAllowed) {
            throw new IllegalStateException(
                    "Refusing to run the UPDATE-without-WHERE fixture: it rewrites every row "
                            + "in `customers`. Set sandbox.allow-destructive-fixtures=true only "
                            + "against a throwaway database.");
        }
        return customerRepository.promoteAllToTier(tier);
    }

    /** The scoped write the fixture above should have been. Safe to call. */
    @Transactional
    public int promoteHighValueCustomers(String tier, BigDecimal threshold) {
        return customerRepository.promoteHighValueCustomers(tier, threshold);
    }
}
