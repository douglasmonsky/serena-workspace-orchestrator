# Workspace Harbor Public Identity Design

## Decision

Use **Workspace Harbor** as the public product and JetBrains Marketplace name.

The harbor metaphor fits the product: workspaces are admitted, observed, protected while active, and released only when every safety condition is satisfied. “Workspace” distinguishes it from CNCF Harbor and avoids product-vendor trademarks in the plugin identity.

## Identity

- Display name: `Workspace Harbor`
- Plugin ID: `com.monsky.workspaceharbor`
- Artifact/root project name: `workspace-harbor`
- Initial version: `0.1.0`
- Short description: `Safely manages idle IDE project windows using fail-closed lifecycle checks.`

The implementation package remains unchanged during this metadata fix. A package refactor is unnecessary for marketplace identity and would widen a verifier-driven change without affecting users.

## Naming Constraints

- Do not use `PyCharm`, `JetBrains`, `Serena`, or `Codex` in the plugin ID or display name.
- Mention compatible IDEs and integrations only in descriptive documentation.
- Do not imply endorsement by JetBrains, Oraios, OpenAI, or CNCF Harbor.
- Preserve the existing safety behavior and API contracts; this change is identity metadata only.

## Verification

- JetBrains plugin verification accepts the ID, name, version, and description.
- The distribution filename uses `workspace-harbor`.
- Repository documentation consistently presents Workspace Harbor as a standalone lifecycle-safety companion.
- No exact `Workspace Harbor` collision currently appears in the JetBrains Marketplace search or prominent GitHub repository search performed on 2026-07-12.
