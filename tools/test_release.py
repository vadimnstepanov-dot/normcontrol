"""Run the mandatory release checks with fresh private-data-free portal storage."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='normcontrol-release-') as temporary:
        env = dict(os.environ, PYTHONPATH=str(ROOT/'sto_rag'), PYTHONUTF8='1',
                   APP_DEBUG='1', APP_SECRET='synthetic-release-test-secret',
                   APP_DATA=temporary, NORMCONTROL_KNOWLEDGE_V2='1')
        # A developer's opt-in integration flags must not turn CI into a live run.
        env.pop('KNOWLEDGE_LIVE_SEMANTIC_TEST', None)
        commands = [
            ([sys.executable, '-m', 'unittest', 'discover', '-s', 'sto_rag/nc5/tests'], ROOT),
            ([sys.executable, '-m', 'unittest', 'discover', '-s', 'sto_rag/knowledge_v2/tests', '-t', 'sto_rag'], ROOT),
            ([sys.executable, '-m', 'unittest', 'discover', '-s', 'sto_rag/tests'], ROOT),
            ([sys.executable, 'manage.py', 'test', 'portal', 'knowledge', '--noinput'], ROOT/'normcontrol-web'),
        ]
        for command, directory in commands:
            subprocess.run(command, cwd=directory, env=env, check=True)


if __name__ == '__main__':
    main()
