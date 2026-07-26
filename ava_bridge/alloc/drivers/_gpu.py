"""Re-export of the per-process accelerator oracle for driver use.

Drivers import this rather than reaching up into the package, keeping their imports
shallow and making the one-oracle rule easy to see. See ``alloc/gpumem.py`` for why a
container runtime's or service manager's own memory figure is not trustworthy here.
"""
from ..gpumem import by_pid, for_pids, for_tree, reset_cache  # noqa: F401
