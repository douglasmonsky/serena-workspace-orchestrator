package com.monsky.workspaceharbor.lifecycle;

import com.intellij.ide.plugins.PluginManagerCore;
import com.intellij.ide.trustedProjects.TrustedProjects;
import com.intellij.openapi.Disposable;
import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.extensions.PluginId;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.project.ProjectManager;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.attribute.PosixFilePermission;
import java.security.SecureRandom;
import java.time.Instant;
import java.util.Base64;
import java.util.EnumSet;
import java.util.List;
import java.util.Set;

/** Owns the authenticated loopback endpoint and its same-instance discovery record. */
public final class LifecycleService implements Disposable, LifecycleHttpServer.Adapter {
    private static final Path RUNTIME_FILE = Path.of(System.getProperty("user.home"), ".codex", "state", "intellij-projects", "plugin-runtime.json");
    private LifecycleHttpServer server;
    private String token;

    public synchronized void start() {
        if (server != null) return;
        try {
            token = newToken();
            server = new LifecycleHttpServer(this, token);
            server.start();
            writeRuntimeFile();
        } catch (IOException exception) {
            if (server != null) server.close();
            server = null;
            token = null;
        }
    }

    @Override public List<SafetySnapshot> openProjects() {
        return java.util.Arrays.stream(ProjectManager.getInstance().getOpenProjects())
                .map(ProjectStatusReader::readWithDeadline).toList();
    }

    @Override public SafetyDecision freshDecision(String root) {
        Project project = findOpenProject(root);
        return project == null ? SafetyDecision.evaluate(ProjectStatusReader.unknown(root))
                : SafetyDecision.evaluate(ProjectStatusReader.readWithDeadline(project));
    }

    @Override public boolean close(String root) {
        Project project = findOpenProject(root);
        if (project == null) return false;
        final boolean[] accepted = {false};
        ApplicationManager.getApplication().invokeAndWait(() -> {
            if (project.isDisposed()) return;
            SafetyDecision decision = SafetyDecision.evaluate(ProjectStatusReader.read(project));
            if (!decision.safeToClose()) return;
            ProjectManager.getInstance().closeAndDispose(project);
            accepted[0] = true;
        });
        return accepted[0];
    }

    @Override public boolean trust(String root) {
        Path requested;
        try {
            requested = Path.of(root);
            if (!requested.isAbsolute() || !requested.equals(requested.normalize())) return false;
            requested = requested.toRealPath();
            Path home = Path.of(System.getProperty("user.home")).toRealPath();
            if (!requested.startsWith(home.resolve("Documents/Codex"))
                    && !requested.startsWith(home.resolve(".codex/src"))) return false;
        } catch (IOException | RuntimeException exception) {
            return false;
        }
        Path exactRoot = requested;
        final boolean[] trusted = {false};
        ApplicationManager.getApplication().invokeAndWait(() -> {
            TrustedProjects.setProjectTrusted(exactRoot, true);
            trusted[0] = TrustedProjects.isProjectTrusted(exactRoot);
        });
        return trusted[0];
    }

    @Override public boolean modelReady(String root) {
        Project project = findOpenProject(root);
        return project != null && GradleModelProvisioner.isReady(project);
    }

    private Project findOpenProject(String root) {
        for (Project project : ProjectManager.getInstance().getOpenProjects()) {
            SafetySnapshot status = ProjectStatusReader.readWithDeadline(project);
            if (!status.canonicalRoot().isBlank() && root.equals(status.canonicalRoot())) return project;
        }
        return null;
    }

    private void writeRuntimeFile() throws IOException {
        Files.createDirectories(RUNTIME_FILE.getParent());
        Path temporary = Files.createTempFile(RUNTIME_FILE.getParent(), "plugin-runtime-", ".json");
        try {
            setOwnerOnly(temporary);
            String json = "{\"schemaVersion\":1,\"port\":" + server.uri("").getPort()
                    + ",\"token\":\"" + token + "\",\"pid\":" + ProcessHandle.current().pid()
                    + ",\"processStartInstant\":\"" + ProcessHandle.current().info().startInstant().orElse(Instant.EPOCH)
                    + "\",\"pluginVersion\":\"" + pluginVersion() + "\"}";
            Files.writeString(temporary, json, StandardCharsets.UTF_8);
            Files.move(temporary, RUNTIME_FILE, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
            setOwnerOnly(RUNTIME_FILE);
        } finally { Files.deleteIfExists(temporary); }
    }

    private static void setOwnerOnly(Path path) throws IOException {
        Files.setPosixFilePermissions(path, EnumSet.of(PosixFilePermission.OWNER_READ, PosixFilePermission.OWNER_WRITE));
    }
    private static String newToken() { byte[] bytes = new byte[32]; new SecureRandom().nextBytes(bytes); return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes); }
    private static String pluginVersion() {
        var descriptor = PluginManagerCore.getPlugin(PluginId.getId("com.monsky.workspaceharbor"));
        return descriptor == null ? "unknown" : descriptor.getVersion();
    }
    @Override public synchronized void dispose() {
        if (server != null) server.close();
        server = null;
        try { if (Files.exists(RUNTIME_FILE) && token != null && Files.readString(RUNTIME_FILE).contains("\"token\":\"" + token + "\"")) Files.deleteIfExists(RUNTIME_FILE); }
        catch (IOException ignored) { }
        token = null;
    }
}
