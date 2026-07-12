package com.monsky.codex.pycharm.lifecycle;

/** Immutable observations used to determine whether it is safe to close PyCharm. */
public record SafetySnapshot(
        String canonicalRoot,
        int unsavedCount,
        boolean unsavedKnown,
        boolean indexing,
        boolean indexingKnown,
        int runCount,
        boolean runKnown,
        int terminalCount,
        boolean terminalKnown,
        int debuggerCount,
        boolean debuggerKnown,
        boolean modalActive,
        boolean modalKnown,
        boolean closing,
        boolean closingKnown) {
}
