package com.monsky.workspaceharbor.lifecycle;

import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.startup.StartupActivity;

/** Starts the application-owned endpoint after an IDE project is ready. */
public final class LifecycleStartupActivity implements StartupActivity.DumbAware {
    @Override public void runActivity(Project project) {
        IntelliJRuntimeProvisioner.configure(project);
        GradleModelProvisioner.configure(project);
        ApplicationManager.getApplication().getService(LifecycleService.class).start();
    }
}
