# Tech Stack

- Java sources target Java 21 bytecode.
- Build: Gradle wrapper 9.6.1, IntelliJ Platform Gradle Plugin 2.18.1.
- Target IDE: local `/Applications/PyCharm.app`, build family 261; bundled Terminal plugin is a compile dependency.
- PyCharm JBR 25 supplies `javac`; Gradle toolchain discovery misclassifies it, so `JavaCompile` explicitly forks its compiler with `--release 21`.
- Tests: JUnit Jupiter 5.13.4 plus explicit JUnit Platform launcher 1.13.4.