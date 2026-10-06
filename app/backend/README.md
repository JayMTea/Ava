# Application backend

This package owns HTTP-facing application services: authentication, instance
configuration, UI APIs, chat persistence, uploads, branding, hardware monitoring,
performance history, and voice I/O. `app/server.py` composes those services.

Live agent behavior belongs to the installed `ava_agent` package in
`agent-platform/integrations/ava/ava_agent/`: turns, adapters, inference, tools,
skills, memory, consent, and provisioning. Import it by package name, never by
editing `sys.path` or adding a compatibility `runtime/` directory.

`app/paths.py` owns source locations. `settings.py` owns instance locations
under `AVA_HOME`; moving source must never relocate user data implicitly.
`agent_platform.py` launches the template CLI with its separate interpreter.
