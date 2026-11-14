# Security

Do not publish connector tokens, relay configuration, signing keys, conversation
databases, logs or screenshots of private chats. Local runtime data belongs in
`.local/`; Android signing credentials belong in GitHub Actions secrets.

The connector binds to loopback. The relay must use HTTPS. The browser uses
HttpOnly cookies and CSRF protection; the agent has a separate credential.
Downloads are limited to explicitly linked files inside authorized directories.

Codex desktop IPC and DeepSeek Harness integration are version-dependent.
Unvalidated Codex versions retain read-only access when possible; writes remain
disabled until verified. Do not remove version or authorization checks to work
around a connection error.

Use the repository's private vulnerability reporting feature for security reports.
Avoid posting secrets or personal conversations in public issues.
