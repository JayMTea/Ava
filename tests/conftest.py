"""Point every unit test at a throwaway AVA_HOME before anything is imported.

tests/ deliberately had no conftest: the tier is meant to be fast and isolated,
and isolation was achieved by convention — nothing in it wrote to disk.

That convention broke silently the moment the chat store gained a backing file.
A distiller test that used to seed fixtures by assigning into an in-memory dict
was rewritten to build them through chat_store's public API (which is the right
thing — it exercises the real path), and chat_store now persists. Running
`pytest tests/` from a checkout therefore wrote data/chats.db into the OWNER'S
live data directory.

The pollution was not the dangerous part. The one-time migration from chats.json
is guarded on "has this store been used", so a test-created database with one
fixture chat in it looks exactly like a store already in service: the migration
would mark itself done and never import the real corpus. A stray test artefact
could have cost someone their chat history on next restart.

So isolation stops being a convention here. AVA_HOME is redirected to a temp
directory at import time — before ava_bridge.settings is first touched, since it
freezes the value on first access — and the whole tree is removed afterwards.
"""
import os
import shutil
import tempfile

import pytest

_TMP_HOME = tempfile.mkdtemp(prefix="ava-unit-home-")
os.environ["AVA_HOME"] = _TMP_HOME
os.makedirs(os.path.join(_TMP_HOME, "data"), exist_ok=True)


@pytest.fixture(scope="session", autouse=True)
def _isolated_home():
    yield
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


def test_unit_tests_never_touch_the_real_ava_home():
    """The guard for the guard: prove the redirect actually took effect."""
    from ava_bridge import settings
    assert str(settings.AVA_HOME).startswith(tempfile.gettempdir()), (
        f"unit tests are running against AVA_HOME={settings.AVA_HOME!r}, which is "
        "not a temp directory. Anything that persists — the chat store above all "
        "— would write into a real install's data dir, and a test-created "
        "chats.db is indistinguishable from a store already in service, which "
        "would suppress the one-time import of the owner's real corpus."
    )
