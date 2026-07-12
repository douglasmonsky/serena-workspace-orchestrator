package com.monsky.workspaceharbor.lifecycle;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.List;
import java.util.stream.Stream;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;

class SafetyDecisionTest {
    private static final SafetySnapshot SAFE = new SafetySnapshot(
            "/workspace", 0, true, false, true, 0, true, 0, true, 0, true, false, true, false, true);

    @ParameterizedTest
    @MethodSource("unsafeSnapshots")
    void failsClosedForEachUnknownOrUnsafeField(SafetySnapshot snapshot, String reason) {
        SafetyDecision decision = SafetyDecision.evaluate(snapshot);

        assertFalse(decision.safeToClose());
        assertEquals(List.of(reason), decision.reasons());
    }

    static Stream<Arguments> unsafeSnapshots() {
        return Stream.of(
                Arguments.of(new SafetySnapshot("/workspace", 1, true, false, true, 0, true, 0, true, 0, true, false, true, false, true), "unsaved-documents"),
                Arguments.of(new SafetySnapshot("/workspace", 0, false, false, true, 0, true, 0, true, 0, true, false, true, false, true), "unsaved-documents-unknown"),
                Arguments.of(new SafetySnapshot("/workspace", 0, true, true, true, 0, true, 0, true, 0, true, false, true, false, true), "indexing"),
                Arguments.of(new SafetySnapshot("/workspace", 0, true, false, false, 0, true, 0, true, 0, true, false, true, false, true), "indexing-unknown"),
                Arguments.of(new SafetySnapshot("/workspace", 0, true, false, true, 1, true, 0, true, 0, true, false, true, false, true), "run-active"),
                Arguments.of(new SafetySnapshot("/workspace", 0, true, false, true, 0, false, 0, true, 0, true, false, true, false, true), "run-active-unknown"),
                Arguments.of(new SafetySnapshot("/workspace", 0, true, false, true, 0, true, 1, true, 0, true, false, true, false, true), "terminal-active"),
                Arguments.of(new SafetySnapshot("/workspace", 0, true, false, true, 0, true, 0, false, 0, true, false, true, false, true), "terminal-active-unknown"),
                Arguments.of(new SafetySnapshot("/workspace", 0, true, false, true, 0, true, 0, true, 1, true, false, true, false, true), "debugger-active"),
                Arguments.of(new SafetySnapshot("/workspace", 0, true, false, true, 0, true, 0, true, 0, false, false, true, false, true), "debugger-active-unknown"),
                Arguments.of(new SafetySnapshot("/workspace", 0, true, false, true, 0, true, 0, true, 0, true, true, true, false, true), "modal-active"),
                Arguments.of(new SafetySnapshot("/workspace", 0, true, false, true, 0, true, 0, true, 0, true, false, false, false, true), "modal-active-unknown"),
                Arguments.of(new SafetySnapshot("/workspace", 0, true, false, true, 0, true, 0, true, 0, true, false, true, true, true), "closing"),
                Arguments.of(new SafetySnapshot("/workspace", 0, true, false, true, 0, true, 0, true, 0, true, false, true, false, false), "closing-unknown"));
    }

    @org.junit.jupiter.api.Test
    void allowsOnlyTheKnownAllZeroSnapshot() {
        SafetyDecision decision = SafetyDecision.evaluate(SAFE);

        assertTrue(decision.safeToClose());
        assertTrue(decision.reasons().isEmpty());
    }

    @org.junit.jupiter.api.Test
    void collectsAllReasons() {
        SafetyDecision decision = SafetyDecision.evaluate(new SafetySnapshot(
                "/workspace", 2, true, true, true, 1, true, 3, true, 4, true, true, true, true, true));

        assertEquals(List.of("unsaved-documents", "indexing", "run-active", "terminal-active", "debugger-active", "modal-active", "closing"), decision.reasons());
    }
}
