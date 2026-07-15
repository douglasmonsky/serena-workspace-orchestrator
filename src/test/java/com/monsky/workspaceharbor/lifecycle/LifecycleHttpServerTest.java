package com.monsky.workspaceharbor.lifecycle;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
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

    @Test void status_returns_canonical_root_complete_safety_contract() throws Exception {
        start();
        HttpResponse<String> response = get("/v1/projects/status", "token");
        assertEquals(200, response.statusCode());
        assertEquals("{\"projects\":[{\"root\":\"/workspace\",\"safeToClose\":true,\"reasons\":[],\"known\":{\"unsavedDocuments\":true,\"indexing\":true,\"run\":true,\"terminal\":true,\"debugger\":true,\"modal\":true,\"closing\":true},\"counts\":{\"unsavedDocuments\":0,\"run\":0,\"terminal\":0,\"debugger\":0},\"active\":{\"indexing\":false,\"modal\":false,\"closing\":false}}]}", response.body());
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

    @Test void ownedRecoveryCloseUsesTheExplicitRecoveryPolicy() throws Exception {
        start();
        adapter.safe = false;
        adapter.ownedRecoverySafe = true;

        assertEquals(202, post("/v1/projects/close?root=%2Fworkspace&mode=owned-recovery").statusCode());
        assertEquals(true, adapter.lastOwnedRecovery);
    }

    @Test void accepts_authenticated_exact_trust_request() throws Exception {
        start();

        HttpResponse<String> response = post("/v1/projects/trust?root=%2Fworkspace");

        assertEquals(200, response.statusCode());
        assertEquals("/workspace", adapter.trustedRoot);
        assertEquals(404, post("/v1/projects/trust").statusCode());
    }

    @Test void reports_project_model_readiness_without_guessing() throws Exception {
        start();
        assertEquals(200, get("/v1/projects/model?root=%2Fworkspace", "token").statusCode());
        adapter.modelReady = false;
        assertEquals(202, get("/v1/projects/model?root=%2Fworkspace", "token").statusCode());
        assertEquals(404, get("/v1/projects/model?root=%2Fmissing", "token").statusCode());
    }

    @Test void health_requires_a_responsive_ide_event_thread() throws Exception {
        start();
        assertEquals(200, get("/v1/health", "token").statusCode());
        adapter.responsive = false;
        assertEquals(202, get("/v1/health", "token").statusCode());
    }

    @Test void refuses_when_final_close_safety_check_changes() throws Exception {
        start();
        adapter.closeAccepted = false;

        assertEquals(409, post("/v1/projects/close?root=%2Fworkspace").statusCode());
        assertEquals(1, adapter.closeCalls);
        assertEquals(409, post("/v1/projects/close?root=%2Fworkspace").statusCode());
    }

    @Test void concurrent_same_root_close_attempts_share_one_close_and_result() throws Exception {
        start();
        adapter.concurrentDecisionBarrier = new CountDownLatch(2);
        try (ExecutorService requests = Executors.newFixedThreadPool(2)) {
            Future<HttpResponse<String>> first = requests.submit(() -> post("/v1/projects/close?root=%2Fworkspace"));
            Future<HttpResponse<String>> second = requests.submit(() -> post("/v1/projects/close?root=%2Fworkspace"));

            assertEquals(202, first.get().statusCode());
            assertEquals(202, second.get().statusCode());
        }
        assertEquals(1, adapter.closeCalls);
    }

    @Test void releases_claim_when_close_throws_so_retry_does_not_block() throws Exception {
        start();
        adapter.throwOnClose = true;

        assertEquals(409, post("/v1/projects/close?root=%2Fworkspace").statusCode());
        adapter.throwOnClose = false;
        try (ExecutorService requests = Executors.newSingleThreadExecutor()) {
            Future<HttpResponse<String>> retry = requests.submit(() -> post("/v1/projects/close?root=%2Fworkspace"));
            assertEquals(202, retry.get(2, TimeUnit.SECONDS).statusCode());
        }
        assertEquals(2, adapter.closeCalls);
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
        boolean ownedRecoverySafe;
        boolean lastOwnedRecovery;
        boolean closeAccepted = true;
        boolean throwOnClose;
        int closeCalls;
        String trustedRoot;
        boolean modelReady = true;
        boolean responsive = true;
        CountDownLatch concurrentDecisionBarrier;
        @Override public List<SafetySnapshot> openProjects() { return open ? List.of(snapshot()) : List.of(); }
        @Override public SafetyDecision freshDecision(String root, boolean ownedRecovery) {
            lastOwnedRecovery = ownedRecovery;
            if (concurrentDecisionBarrier != null) {
                concurrentDecisionBarrier.countDown();
                try { concurrentDecisionBarrier.await(1, TimeUnit.SECONDS); } catch (InterruptedException exception) { Thread.currentThread().interrupt(); throw new RuntimeException(exception); }
            }
            boolean accepted = ownedRecovery ? ownedRecoverySafe : safe;
            return accepted ? new SafetyDecision(true, List.of()) : new SafetyDecision(false, List.of("unsaved-documents"));
        }
        @Override public boolean close(String root, boolean ownedRecovery) { lastOwnedRecovery = ownedRecovery; closeCalls++; if (throwOnClose) throw new RuntimeException("close failed"); if (closeAccepted) open = false; return closeAccepted; }
        @Override public boolean trust(String root) { trustedRoot = root; return true; }
        @Override public boolean modelReady(String root) { return modelReady; }
        @Override public boolean responsive() { return responsive; }
        private SafetySnapshot snapshot() { return new SafetySnapshot("/workspace", 0, true, false, true, 0, true, 0, true, 0, true, false, true, false, true); }
    }
}
