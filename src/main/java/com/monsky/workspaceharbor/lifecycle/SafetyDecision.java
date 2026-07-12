package com.monsky.workspaceharbor.lifecycle;

import java.util.ArrayList;
import java.util.List;

/** A fail-closed decision: it is safe only when every observed condition is known and inactive. */
public record SafetyDecision(boolean safeToClose, List<String> reasons) {
    public static SafetyDecision evaluate(SafetySnapshot snapshot) {
        List<String> reasons = new ArrayList<>();
        addKnownZero(reasons, "unsaved-documents", snapshot.unsavedKnown(), snapshot.unsavedCount());
        addFalse(reasons, "indexing", snapshot.indexingKnown(), snapshot.indexing());
        addKnownZero(reasons, "run-active", snapshot.runKnown(), snapshot.runCount());
        addKnownZero(reasons, "terminal-active", snapshot.terminalKnown(), snapshot.terminalCount());
        addKnownZero(reasons, "debugger-active", snapshot.debuggerKnown(), snapshot.debuggerCount());
        addFalse(reasons, "modal-active", snapshot.modalKnown(), snapshot.modalActive());
        addFalse(reasons, "closing", snapshot.closingKnown(), snapshot.closing());
        return new SafetyDecision(reasons.isEmpty(), List.copyOf(reasons));
    }

    private static void addKnownZero(List<String> reasons, String field, boolean known, int count) {
        if (!known) {
            reasons.add(field + "-unknown");
        } else if (count != 0) {
            reasons.add(field);
        }
    }

    private static void addFalse(List<String> reasons, String field, boolean known, boolean active) {
        if (!known) {
            reasons.add(field + "-unknown");
        } else if (active) {
            reasons.add(field);
        }
    }
}
