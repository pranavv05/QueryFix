package com.queryfix.sandbox;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * Entry point for the QueryFix sandbox.
 *
 * <p>This application exists to be analyzed, not to be imitated. It contains
 * four deliberately planted performance bugs, each marked with a
 * {@code PLANTED BUG:} comment.
 */
@SpringBootApplication
public class SandboxApplication {

    public static void main(String[] args) {
        SpringApplication.run(SandboxApplication.class, args);
    }
}
