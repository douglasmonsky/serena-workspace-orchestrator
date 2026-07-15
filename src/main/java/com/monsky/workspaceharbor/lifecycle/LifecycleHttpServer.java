package com.monsky.workspaceharbor.lifecycle;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Authenticated loopback API. All project operations are delegated to the IDE adapter. */
public final class LifecycleHttpServer implements AutoCloseable {
    public interface Adapter { List<SafetySnapshot> openProjects(); SafetyDecision freshDecision(String root, boolean ownedRecovery); boolean close(String root, boolean ownedRecovery); boolean trust(String root); boolean modelReady(String root); boolean responsive(); }
    private final Adapter adapter; private final String token; private final Map<String, CompletableFuture<Boolean>> closingRoots = new ConcurrentHashMap<>();
    private final HttpServer server; private final ExecutorService executor = Executors.newCachedThreadPool();
    public LifecycleHttpServer(Adapter adapter, String token) throws IOException { this.adapter = adapter; this.token = token; server = HttpServer.create(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), 0), 0); server.createContext("/v1/projects", this::handle); server.createContext("/v1/health", this::handle); server.setExecutor(executor); }
    public void start() { server.start(); }
    public URI uri(String path) { return URI.create("http://127.0.0.1:" + server.getAddress().getPort() + path); }
    private void handle(HttpExchange x) throws IOException { try { if (!("Bearer " + token).equals(x.getRequestHeaders().getFirst("Authorization"))) { reply(x, 401, "{\"error\":\"unauthorized\"}"); return; } if ("GET".equals(x.getRequestMethod()) && "/v1/health".equals(x.getRequestURI().getPath())) { health(x); return; } if ("GET".equals(x.getRequestMethod()) && "/v1/projects".equals(x.getRequestURI().getPath())) { list(x); return; } if ("GET".equals(x.getRequestMethod()) && "/v1/projects/status".equals(x.getRequestURI().getPath())) { status(x); return; } if ("GET".equals(x.getRequestMethod()) && "/v1/projects/model".equals(x.getRequestURI().getPath())) { modelStatus(x); return; } if ("POST".equals(x.getRequestMethod()) && "/v1/projects/close".equals(x.getRequestURI().getPath())) { closeProject(x); return; } if ("POST".equals(x.getRequestMethod()) && "/v1/projects/trust".equals(x.getRequestURI().getPath())) { trustProject(x); return; } reply(x, 404, "{\"error\":\"not-found\"}"); } catch (RuntimeException ignored) { reply(x, 409, "{\"error\":\"protected\"}"); } finally { x.close(); } }
    private void health(HttpExchange x) throws IOException { boolean responsive = adapter.responsive(); reply(x, responsive ? 200 : 202, "{\"responsive\":" + responsive + "}"); }
    private void list(HttpExchange x) throws IOException { String roots = adapter.openProjects().stream().map(SafetySnapshot::canonicalRoot).map(v -> "\"" + json(v) + "\"").reduce((a,b) -> a + "," + b).orElse(""); reply(x, 200, "{\"projects\":[" + roots + "]}"); }
    private void status(HttpExchange x) throws IOException { String projects = adapter.openProjects().stream().map(this::statusJson).reduce((a,b) -> a + "," + b).orElse(""); reply(x, 200, "{\"projects\":[" + projects + "]}"); }
    private String statusJson(SafetySnapshot s) { SafetyDecision d = SafetyDecision.evaluate(s); String reasons = d.reasons().stream().map(v -> "\"" + json(v) + "\"").reduce((a,b) -> a + "," + b).orElse(""); return "{\"root\":\"" + json(s.canonicalRoot()) + "\",\"safeToClose\":" + d.safeToClose() + ",\"reasons\":[" + reasons + "],\"known\":{\"unsavedDocuments\":" + s.unsavedKnown() + ",\"indexing\":" + s.indexingKnown() + ",\"run\":" + s.runKnown() + ",\"terminal\":" + s.terminalKnown() + ",\"debugger\":" + s.debuggerKnown() + ",\"modal\":" + s.modalKnown() + ",\"closing\":" + s.closingKnown() + "},\"counts\":{\"unsavedDocuments\":" + s.unsavedCount() + ",\"run\":" + s.runCount() + ",\"terminal\":" + s.terminalCount() + ",\"debugger\":" + s.debuggerCount() + "},\"active\":{\"indexing\":" + s.indexing() + ",\"modal\":" + s.modalActive() + ",\"closing\":" + s.closing() + "}}"; }
    private void closeProject(HttpExchange x) throws IOException {
        String root = query(x.getRequestURI().getRawQuery(), "root");
        if (root == null) { reply(x, 404, "{\"error\":\"not-found\"}"); return; }
        String mode = query(x.getRequestURI().getRawQuery(), "mode");
        if (mode != null && !"owned-recovery".equals(mode)) { reply(x, 409, "{\"error\":\"protected\"}"); return; }
        boolean ownedRecovery = "owned-recovery".equals(mode);
        CompletableFuture<Boolean> attempt = new CompletableFuture<>();
        CompletableFuture<Boolean> existing = closingRoots.putIfAbsent(root, attempt);
        if (existing != null) { replyCloseResult(x, existing.join()); return; }
        try {
            if (adapter.openProjects().stream().map(SafetySnapshot::canonicalRoot).noneMatch(root::equals)) {
                attempt.complete(false); closingRoots.remove(root, attempt); reply(x, 404, "{\"error\":\"not-found\"}"); return;
            }
            SafetyDecision decision = adapter.freshDecision(root, ownedRecovery);
            if (!decision.safeToClose()) {
                attempt.complete(false); closingRoots.remove(root, attempt);
                String reasons = decision.reasons().stream().map(v -> "\"" + json(v) + "\"").reduce((a,b) -> a + "," + b).orElse("");
                reply(x, 409, "{\"reasons\":[" + reasons + "]}"); return;
            }
            boolean accepted = adapter.close(root, ownedRecovery);
            attempt.complete(accepted);
            if (!accepted) closingRoots.remove(root, attempt);
            replyCloseResult(x, accepted);
        } catch (RuntimeException exception) {
            attempt.complete(false);
            closingRoots.remove(root, attempt);
            throw exception;
        }
    }
    private void trustProject(HttpExchange x) throws IOException {
        String root = query(x.getRequestURI().getRawQuery(), "root");
        if (root == null) { reply(x, 404, "{\"error\":\"not-found\"}"); return; }
        boolean trusted = adapter.trust(root);
        reply(x, trusted ? 200 : 409, trusted ? "{\"status\":\"trusted\"}" : "{\"error\":\"protected\"}");
    }
    private void modelStatus(HttpExchange x) throws IOException {
        String root = query(x.getRequestURI().getRawQuery(), "root");
        if (root == null || adapter.openProjects().stream().map(SafetySnapshot::canonicalRoot).noneMatch(root::equals)) {
            reply(x, 404, "{\"error\":\"not-found\"}"); return;
        }
        boolean ready = adapter.modelReady(root);
        reply(x, ready ? 200 : 202, "{\"ready\":" + ready + "}");
    }
    private static void replyCloseResult(HttpExchange x, boolean accepted) throws IOException { reply(x, accepted ? 202 : 409, accepted ? "{\"status\":\"closing\"}" : "{\"error\":\"protected\"}"); }
    private static String query(String q, String key) { if (q == null) return null; for (String pair : q.split("&")) { int i = pair.indexOf('='); if (i > 0 && key.equals(URLDecoder.decode(pair.substring(0, i), StandardCharsets.UTF_8))) return URLDecoder.decode(pair.substring(i + 1), StandardCharsets.UTF_8); } return null; }
    private static String json(String v) { return v.replace("\\", "\\\\").replace("\"", "\\\""); }
    private static void reply(HttpExchange x, int status, String body) throws IOException { byte[] bytes = body.getBytes(StandardCharsets.UTF_8); x.getResponseHeaders().set("Content-Type", "application/json"); x.sendResponseHeaders(status, bytes.length); x.getResponseBody().write(bytes); }
    @Override public void close() { server.stop(0); executor.shutdownNow(); }
}
