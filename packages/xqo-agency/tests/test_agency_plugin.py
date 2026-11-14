"""xqo-agency is found through its entry point and adds the `xqo agency` command."""
import subprocess
import sys


def test_agency_command_is_added():
    r = subprocess.run([sys.executable, "-m", "xqo", "agency", "--help"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "agency" in r.stdout


def test_backups_keep_the_agency_files():
    from xqo import plugin
    from xqo_agency import AgencyPlugin
    assert "agency" in plugin.load_classes()
    assert {"agency.json", "agency.lock.json", "AGENCY_AGENTS_LICENSE"} <= plugin.CONFIG_FILES
    assert AgencyPlugin.api == 1
