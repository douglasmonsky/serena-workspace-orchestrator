# Workspace Harbor

Safely manages idle IDE project windows using fail-closed lifecycle checks.

Run tests with `JAVA_HOME=/Applications/PyCharm.app/Contents/jbr/Contents/Home ./gradlew test`.

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
