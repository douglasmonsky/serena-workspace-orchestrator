# Conventions

- Immutable records for lifecycle observations/decisions; stable machine-readable refusal reason tokens.
- Safety decisions collect all refusal reasons; only fully known, inactive state is safe.
- HTTP lifecycle access is loopback-only and bearer-authenticated; exact canonical open-project roots only.
- Close requests must re-read safety immediately before normal `ProjectManager.closeAndDispose`; never use force-close APIs.
- Use Conventional Commits and explicit staging paths.