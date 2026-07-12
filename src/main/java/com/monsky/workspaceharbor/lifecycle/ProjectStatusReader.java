package com.monsky.workspaceharbor.lifecycle;

import com.intellij.execution.ExecutionManager;
import com.intellij.openapi.fileEditor.FileDocumentManager;
import com.intellij.openapi.project.DumbService;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.roots.ProjectFileIndex;
import com.intellij.xdebugger.XDebuggerManager;
import java.io.IOException;
import java.nio.file.Path;
import java.util.Arrays;
import java.util.Objects;
import com.intellij.openapi.application.ApplicationManager;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import org.jetbrains.plugins.terminal.TerminalToolWindowManager;

/** Reads IDE state only on the EDT. A failed observation is deliberately marked unknown. */
public final class ProjectStatusReader {
    private ProjectStatusReader() { }

    public static SafetySnapshot read(Project project) {
        String root = canonicalRoot(project);
        if (root == null || project.isDisposed()) return unknown(root == null ? "" : root);
        try {
            int unsaved = (int) Arrays.stream(FileDocumentManager.getInstance().getUnsavedDocuments())
                    .map(FileDocumentManager.getInstance()::getFile).filter(Objects::nonNull)
                    .filter(file -> ProjectFileIndex.getInstance(project).isInContent(file)).count();
            boolean indexing = DumbService.isDumb(project);
            int runs = ExecutionManager.getInstance(project).getRunningProcesses().length;
            int terminals = TerminalToolWindowManager.getInstance(project).getTerminalWidgets().size();
            int debuggers = XDebuggerManager.getInstance(project).getDebugSessions().length;
            boolean modal = !com.intellij.openapi.application.ModalityState.current().equals(com.intellij.openapi.application.ModalityState.nonModal());
            return new SafetySnapshot(root, unsaved, true, indexing, true, runs, true, terminals, true, debuggers, true, modal, true, false, true);
        } catch (RuntimeException exception) { return unknown(root); }
    }

    /** Gives an HTTP request at most two seconds to obtain an EDT safety observation. */
    public static SafetySnapshot readWithDeadline(Project project) {
        CompletableFuture<SafetySnapshot> result = CompletableFuture.supplyAsync(() -> {
            CompletableFuture<SafetySnapshot> edtResult = new CompletableFuture<>();
            try {
                ApplicationManager.getApplication().invokeAndWait(() -> edtResult.complete(read(project)));
                return edtResult.getNow(unknown(""));
            } catch (RuntimeException exception) { return unknown(""); }
        });
        try { return result.get(2, TimeUnit.SECONDS); }
        catch (Exception exception) { return unknown(""); }
    }

    private static String canonicalRoot(Project project) {
        String basePath = project.getBasePath();
        if (basePath == null) return null;
        try { return Path.of(basePath).toRealPath().toString(); } catch (IOException | RuntimeException exception) { return null; }
    }
    static SafetySnapshot unknown(String root) { return new SafetySnapshot(root, 0, false, false, false, 0, false, 0, false, 0, false, false, false, false, false); }
}
