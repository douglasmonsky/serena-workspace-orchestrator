package com.monsky.codex.pycharm.lifecycle;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

class LifecycleHttpServerTest {
    private final FakeAdapter adapter = new FakeAdapter();
    private LifecycleHttpServer server;

    @AfterEach void stop() { if (server != null) server.close(); }

    @Test void rejects_missing_or_wrong_bearer_token() throws Exception {
        start();
        assertEquals(401, get("/v1/projects", null).statusCode());
        assertEquals(401, get("/v1/projects", "wrong").statusCode());
    }

    @Test void rejects_missing_root_and_reports_unsafe_reasons() throws Exception {
        start();
        assertEquals(404, post("/v1/projects/close?root=%2Fmissing").statusCode());
        adapter.safe = false;
        HttpResponse<String> response = post("/v1/projects/close?root=%2Fworkspace");
        assertEquals(409, response.statusCode());
        org.junit.jupiter.api.Assertions.assertTrue(response.body().contains("unsaved-documents"));
    }

    @Test void accepts_safe_close_and_is_idempotent_after_project_disappears() throws Exception {
        start();
        assertEquals(202, post("/v1/projects/close?root=%2Fworkspace").statusCode());
        adapter.open = false;
        assertEquals(202, post("/v1/projects/close?root=%2Fworkspace").statusCode());
        assertEquals(404, post("/v1/projects/close?root=%2Fother").statusCode());
    }

    private void start() throws Exception { server = new LifecycleHttpServer(adapter, "token"); server.start(); }
    private HttpResponse<String> get(String path, String token) throws Exception {
        HttpRequest.Builder builder = HttpRequest.newBuilder(server.uri(path)).GET();
        if (token != null) builder.header("Authorization", "Bearer " + token);
        return HttpClient.newHttpClient().send(builder.build(), HttpResponse.BodyHandlers.ofString());
    }
    private HttpResponse<String> post(String path) throws Exception {
        return HttpClient.newHttpClient().send(HttpRequest.newBuilder(server.uri(path)).header("Authorization", "Bearer token")
                .POST(HttpRequest.BodyPublishers.noBody()).build(), HttpResponse.BodyHandlers.ofString());
    }

    private static final class FakeAdapter implements LifecycleHttpServer.Adapter {
        boolean open = true;
        boolean safe = true;
        @Override public List<SafetySnapshot> openProjects() { return open ? List.of(snapshot()) : List.of(); }
        @Override public SafetyDecision freshDecision(String root) { return safe ? new SafetyDecision(true, List.of()) : new SafetyDecision(false, List.of("unsaved-documents")); }
        @Override public void close(String root) { open = false; }
        private SafetySnapshot snapshot() { return new SafetySnapshot("/workspace", 0, true, false, true, 0, true, 0, true, 0, true, false, true, false, true); }
    }
}
