import json
import shlex
import subprocess
import sys

import pytest

from ava_bridge.runtime.session_files import resolve_transcript


@pytest.mark.parametrize('entry', ['path', 'id', 'missing', 'outside'])
def test_session_index_resolves_only_this_chat(tmp_path, entry):
    root = tmp_path.as_posix()
    original = tmp_path / 'chat.jsonl'
    original.write_text('stale')
    current = tmp_path / 'rotated.jsonl'
    current.write_text('current')
    record = {'sessionFile': current.as_posix(), 'sessionId': 'rotated'}
    if entry == 'id':
        record.pop('sessionFile')
    if entry == 'outside':
        record['sessionFile'] = (tmp_path.parent / 'other.jsonl').as_posix()
    index = {'agent:main:explicit:other': {'sessionFile': current.as_posix()}}
    if entry != 'missing':
        index['agent:main:explicit:chat'] = record
    (tmp_path / 'sessions.json').write_text(json.dumps(index))

    def run(command):
        script = shlex.split(command)[2]
        return subprocess.check_output([sys.executable, '-c', script], text=True)

    expected = current if entry in ('path', 'id') else original
    assert resolve_transcript(run, root + '/chat.jsonl', 'main', 'chat') == expected.as_posix()


def test_missing_session_index_keeps_legacy_path():
    def unavailable(command):
        raise OSError('no index')
    assert resolve_transcript(unavailable, '/sessions/chat.jsonl', 'main', 'chat') == '/sessions/chat.jsonl'
