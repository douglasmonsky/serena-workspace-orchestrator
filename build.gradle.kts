plugins {
    java
    id("org.jetbrains.intellij.platform") version "2.18.1"
}

java {
    sourceCompatibility = JavaVersion.VERSION_21
    targetCompatibility = JavaVersion.VERSION_21
}

tasks.withType<JavaCompile>().configureEach {
    options.release.set(21)
    options.isFork = true
    options.forkOptions.executable = "/Applications/PyCharm.app/Contents/jbr/Contents/Home/bin/javac"
}

repositories {
    mavenCentral()
    intellijPlatform { defaultRepositories() }
}

dependencies {
    intellijPlatform {
        local("/Applications/PyCharm.app")
        bundledPlugin("org.jetbrains.plugins.terminal")
    }
    testImplementation("org.junit.jupiter:junit-jupiter:5.13.4")
    testRuntimeOnly("org.junit.platform:junit-platform-launcher:1.13.4")
}

tasks.test {
    useJUnitPlatform()
}
