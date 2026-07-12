# Workspace Harbor

Safely manages idle IDE project windows using fail-closed lifecycle checks.

Run tests with `JAVA_HOME=/Applications/PyCharm.app/Contents/jbr/Contents/Home ./gradlew test`.

## Idle-project reaper

`bin/pycharm-project-reaper` owns the local managed-project registry and only
closes projects after its broker and plugin safety checks succeed. Its Python
implementation supports the macOS system Python 3.9 used by LaunchAgents.
Run its focused checks with `python3 -m unittest tests/python/test_pycharm_project_reaper.py`.

## Managed PyCharm project trust

New projects opened through the managed opener are trusted only after their
exact Git root is validated beneath one of these approved parents:

- `/Users/Monsky/Documents/Codex`
- `/Users/Monsky/.codex/src`

The trust helper is intentionally narrow:

```sh
pycharm-project-trust allow /absolute/path/to/git-root
pycharm-project-trust status /absolute/path/to/git-root
pycharm-project-trust audit
```

`allow` rejects nested paths, non-Git directories, symlink escapes, and roots
outside those parents. Managed new opens perform this exact-root check before
launching PyCharm. Roots that are already open remain usable if the trust state
is temporarily unavailable; the opener does not make a trust write for them.

`audit` reports exact entries, broad entries, entries outside the approved
parents, and malformed state. It never removes broad entries. In particular,
removing `$USER_HOME$/Documents` is a separate, reviewed migration gate rather
than normal `allow` behavior.
