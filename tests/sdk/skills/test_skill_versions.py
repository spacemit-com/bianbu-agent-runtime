from pathlib import Path
import subprocess
import sys

from bianbu_agent.skills.versions import SkillVersions


def test_orphan_cleanup_preserves_live_worker_and_reclaims_killed_worker(tmp_path):
    store = SkillVersions(tmp_path / "versions")
    own = store._reserve()
    child = subprocess.Popen(
        [sys.executable, "-c", """
import sys
from pathlib import Path
from bianbu_agent.skills.versions import SkillVersions
store = SkillVersions(Path(sys.argv[1]))
directory = store._reserve()
(directory / 'resource').write_text('in use')
print(directory, flush=True)
sys.stdin.readline()
""", str(store.root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        child_path = Path(child.stdout.readline().strip())
        assert child_path.parent == store.root
        store.collect_orphans()
        assert own.exists()
        assert (child_path / "resource").read_text() == "in use"
        child.kill()
        child.wait(timeout=10)
        restarted = SkillVersions(store.root)
        assert not child_path.exists()
        assert own.exists()
        restarted.collect_orphans()
        assert own.exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        child.stdin.close()
        child.stdout.close()
        store._remove(own)
