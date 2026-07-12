package com.monsky.codex.pycharm.lifecycle;

import com.intellij.openapi.project.Project;
import com.intellij.openapi.startup.StartupActivity;

/** Deliberately inert startup hook; it must not alter IDE lifecycle state. */
public final class LifecycleStartupActivity implements StartupActivity.DumbAware {
    @Override
    public void runActivity(Project project) {
        // The safety model is intentionally pure; IDE observation is added by a later adapter.
    }
}
