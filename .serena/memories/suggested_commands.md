# Suggested Commands

- Set `JAVA_HOME=/Applications/PyCharm.app/Contents/jbr/Contents/Home` for Gradle commands.
- Unit tests: `./gradlew test --console=plain`.
- Distribution: `./gradlew buildPlugin --console=plain`.
- Compatibility: `./gradlew verifyPlugin --console=plain`; verifier is configured for the installed local IDE and JBR 25 launcher.
- Combined gate: `./gradlew test buildPlugin verifyPlugin --console=plain`.