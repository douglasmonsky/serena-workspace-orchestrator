# Core

- Standalone JetBrains companion plugin; source under `src/main/java/com/monsky/codex/pycharm/lifecycle`, descriptor at `src/main/resources/META-INF/plugin.xml`.
- Fail-closed lifecycle invariant: unknown IDE state is unsafe; never force-close, save user documents, signal processes, or interact with installed/live IDE state from development tasks.
- Safety/build details: `mem:tech_stack`; commands: `mem:suggested_commands`; coding rules: `mem:conventions`; completion gate: `mem:task_completion`.