"""Read-only SQL. The reference implementation supports SQLite only
(DATABASE_URL=sqlite:///path/to.db); add your driver behind the same function signature.

Read-only is enforced by the connection (`mode=ro`), not by inspecting the SQL, and only
one statement runs per call.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from runtime.types import ToolContext, ToolError


def query(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("sqlite:///"):
        raise ToolError("DATABASE_URL must be sqlite:///<path> for the reference database tool")
    path = ctx.policy.resolve_path(url.removeprefix("sqlite:///"), "read")
    max_rows = max(1, min(int(args.get("max_rows", 200)), 5000))
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        raise ToolError(f"cannot open database: {exc}") from exc
    try:
        cur = conn.execute(args["sql"], args.get("params", []))
        columns = [c[0] for c in cur.description or []]
        rows = cur.fetchmany(max_rows + 1)
    except sqlite3.Error as exc:
        raise ToolError(f"query failed: {exc}") from exc
    finally:
        conn.close()
    return {"columns": columns, "rows": [list(r) for r in rows[:max_rows]], "truncated": len(rows) > max_rows}
