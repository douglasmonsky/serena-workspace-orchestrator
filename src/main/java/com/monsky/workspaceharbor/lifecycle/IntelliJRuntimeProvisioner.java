package com.monsky.workspaceharbor.lifecycle;

import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.projectRoots.JavaSdk;
import com.intellij.openapi.projectRoots.ProjectJdkTable;
import com.intellij.openapi.projectRoots.Sdk;
import com.intellij.openapi.roots.ProjectRootManager;

/** Registers IntelliJ's bundled JBR as the default project SDK when none is configured. */
final class IntelliJRuntimeProvisioner {
    private static final String SDK_NAME =
            "Workspace Harbor IntelliJ Runtime " + Runtime.version().feature();

    private IntelliJRuntimeProvisioner() { }

    static void configure(Project project) {
        if (ProjectRootManager.getInstance(project).getProjectSdk() != null) return;
        String javaHome = System.getProperty("java.home");
        if (javaHome == null || javaHome.isBlank()) return;

        ApplicationManager.getApplication().runWriteAction(() -> {
            ProjectRootManager roots = ProjectRootManager.getInstance(project);
            if (roots.getProjectSdk() != null) return;
            ProjectJdkTable table = ProjectJdkTable.getInstance();
            Sdk sdk = table.findJdk(SDK_NAME);
            if (sdk == null) {
                sdk = JavaSdk.getInstance().createJdk(SDK_NAME, javaHome, false);
                table.addJdk(sdk);
            }
            roots.setProjectSdk(sdk);
        });
    }
}
