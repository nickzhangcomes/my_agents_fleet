# Security and data boundaries

This library runs local CLI programs with the user's permissions. Those programs may call remote model services, read files, and execute tools. Full mode may disable native approval or sandbox mechanisms; use the narrowest supported mode for the task.

Role instructions are not an isolation boundary. Read-only strength differs by backend, and untrusted input controls cover known tool categories, not all possible process or network behavior. The library is designed for single-user local workflows, not hostile multi-tenant hosting.

Persistent sessions contain prompt and response text. Run records contain metadata and truncated error summaries, which can quote sensitive text. File tasks append full events to a workspace log. Model caches can contain local CLI paths and account-visible models. These are runtime data, not publishable examples.

Do not post credentials or real conversation data in a public issue. Before the first public release, the repository owner should enable GitHub private vulnerability reporting or publish a private reporting contact. No private reporting endpoint is configured in this local preparation folder.
