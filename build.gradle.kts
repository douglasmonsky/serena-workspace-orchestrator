import org.jetbrains.intellij.platform.gradle.tasks.VerifyPluginTask

plugins {
    java
    id("org.jetbrains.intellij.platform") version "2.18.1"
}

version = "0.1.9"

val intellijAppPath = providers.environmentVariable("INTELLIJ_APP_PATH")
    .orElse("${System.getProperty("user.home")}/Applications/IntelliJ IDEA.app")
    .get()

java {
    sourceCompatibility = JavaVersion.VERSION_21
    targetCompatibility = JavaVersion.VERSION_21
}

tasks.withType<JavaCompile>().configureEach {
    options.release.set(21)
    options.isFork = true
    options.forkOptions.executable = "$intellijAppPath/Contents/jbr/Contents/Home/bin/javac"
}

repositories {
    mavenCentral()
    intellijPlatform { defaultRepositories() }
}

dependencies {
    intellijPlatform {
        local(intellijAppPath)
        bundledPlugin("org.jetbrains.plugins.terminal")
        bundledPlugin("com.intellij.gradle")
        bundledPlugin("com.intellij.java")
    }
    testImplementation("org.junit.jupiter:junit-jupiter:5.13.4")
    testRuntimeOnly("org.junit.platform:junit-platform-launcher:1.13.4")
}

intellijPlatform {
    pluginVerification {
        ides {
            local(intellijAppPath)
        }
    }
}

tasks.test {
    useJUnitPlatform()
}

tasks.named<VerifyPluginTask>("verifyPlugin") {
    javaLauncher.set(javaToolchains.launcherFor {
        languageVersion.set(JavaLanguageVersion.of(25))
    })
}
