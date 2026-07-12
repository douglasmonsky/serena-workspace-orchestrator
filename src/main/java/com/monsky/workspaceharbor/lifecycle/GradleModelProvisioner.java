package com.monsky.workspaceharbor.lifecycle;

import com.intellij.openapi.externalSystem.service.execution.ExternalSystemJdkUtil;
import com.intellij.openapi.project.Project;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import org.jetbrains.plugins.gradle.settings.GradleProjectSettings;
import org.jetbrains.plugins.gradle.settings.GradleSettings;

/** Ensures native Gradle import uses IntelliJ's bundled runtime without a download prompt. */
final class GradleModelProvisioner {
    private static final List<String> MARKERS = List.of(
            "settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts");

    private GradleModelProvisioner() { }

    static void configure(Project project) {
        String basePath = project.getBasePath();
        if (basePath == null) return;
        Path root = Path.of(basePath).toAbsolutePath().normalize();
        if (MARKERS.stream().noneMatch(marker -> Files.isRegularFile(root.resolve(marker)))) return;

        GradleSettings settings = GradleSettings.getInstance(project);
        GradleProjectSettings linked = settings.getLinkedProjectSettings(root.toString());
        if (linked == null) {
            linked = new GradleProjectSettings();
            linked.setExternalProjectPath(root.toString());
            linked.setGradleJvm(ExternalSystemJdkUtil.USE_INTERNAL_JAVA);
            settings.linkProject(linked);
            return;
        }
        linked.setGradleJvm(ExternalSystemJdkUtil.USE_INTERNAL_JAVA);
    }
}
