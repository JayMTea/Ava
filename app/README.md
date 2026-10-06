# Application

`cli.py` owns the `ava` command; `__main__.py` launches the HTTP service with
`python -m app`; `server.py` composes the FastAPI application. `router.py` runs
the inference router and `voice.py` runs the desktop voice loop.

`frontend/` contains the React application, its tests, and its built bundle.
Run `npm ci`, `npm test`, and `npm run build` from that directory.
Application services live in `backend/`. Live agent execution is imported from
`ava_agent`, installed from `agent-platform/integrations/ava/ava_agent/` by the
root project metadata. Install with `pip install -e .` before running the app.
