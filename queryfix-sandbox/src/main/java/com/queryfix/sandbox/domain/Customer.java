package com.queryfix.sandbox.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.FetchType;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.OneToMany;
import jakarta.persistence.Table;
import java.math.BigDecimal;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;

@Entity
@Table(name = "customers")
public class Customer {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(nullable = false, unique = true)
    private String email;

    @Column(name = "full_name", nullable = false, length = 150)
    private String fullName;

    /** ISO-3166 alpha-2. Deliberately not indexed — see V1__create_tables.sql. */
    @Column(nullable = false, length = 2)
    private String country;

    @Column(name = "loyalty_tier", nullable = false, length = 16)
    private String loyaltyTier = "STANDARD";

    @Column(name = "lifetime_value", nullable = false, precision = 12, scale = 2)
    private BigDecimal lifetimeValue = BigDecimal.ZERO;

    @Column(name = "signed_up_at", nullable = false)
    private OffsetDateTime signedUpAt;

    @OneToMany(mappedBy = "customer", fetch = FetchType.LAZY)
    private List<Order> orders = new ArrayList<>();

    protected Customer() {
        // for JPA
    }

    public Long getId() {
        return id;
    }

    public String getEmail() {
        return email;
    }

    public String getFullName() {
        return fullName;
    }

    public String getCountry() {
        return country;
    }

    public String getLoyaltyTier() {
        return loyaltyTier;
    }

    public BigDecimal getLifetimeValue() {
        return lifetimeValue;
    }

    public OffsetDateTime getSignedUpAt() {
        return signedUpAt;
    }

    /**
     * Lazy collection. Touching this per row inside a loop is another route to an
     * N+1; the explicit repository-call version is in
     * {@code ReportingService.buildCustomerOrderSummary}.
     */
    public List<Order> getOrders() {
        return orders;
    }
}
