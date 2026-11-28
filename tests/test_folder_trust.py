"""Folder trust rules (owner, 2026-11-27); the same rules as agent-bus trust_folder in agent-harness."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from polar.tasks import TRUST_ROOTS, Tasks


@pytest.fixture
def home(monkeypatch, tmp_path):
    h = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("POLAR_CLAUDE_JSON", str(h / ".claude.json"))
    for k in ("POLAR_WORK_DIR", "POLAR_PERSONAL_DIR"):
        monkeypatch.delenv(k, raising=False)
    return h


def trusted(home):
    p = home / ".claude.json"
    if not p.exists():
        return {}
    return {k: v.get("hasTrustDialogAccepted") for k, v in json.loads(p.read_text()).get("projects", {}).items()}


def trust(path):
    Tasks.ensure_trust(None, Path(path))


def git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.email=a@b", "-c", "user.name=a", *a], check=True,
                   capture_output=True)


def test_default_roots_are_the_two_area_folders():
    assert TRUST_ROOTS == ("work", "personal")


def test_folder_made_again_with_no_entry_is_trusted_and_keeps_other_keys(home):
    (home / ".claude.json").write_text(json.dumps({"keep": 1, "projects": {"/other": {"a": 1}}}))
    d = home / "work" / "x"
    d.mkdir(parents=True)
    trust(d)
    data = json.loads((home / ".claude.json").read_text())
    assert data["keep"] == 1 and data["projects"]["/other"] == {"a": 1}
    assert data["projects"][str(d)]["hasTrustDialogAccepted"] is True


def test_home_is_not_trusted_and_work_dir_redirects(home, monkeypatch):
    trust(home)
    trust("/")
    assert trusted(home) == {}
    monkeypatch.setenv("POLAR_WORK_DIR", str(home))
    assert Tasks.work_dir(None) == home / "work"


def test_new_repo_is_trusted_by_its_git_root(home):
    repo = home / "work" / "newrepo"
    (repo / "src").mkdir(parents=True)
    git(repo, "init", "-q")
    trust(repo / "src")
    k = trusted(home)
    assert k[str(repo)] and k[str(repo / "src")]


@pytest.mark.parametrize("name", ["work", "personal"])
def test_other_roots_are_trusted(home, name):
    d = home / name
    d.mkdir()
    trust(d)
    assert trusted(home)[str(d)]


def test_oss_is_never_trusted(home):
    d = home / "oss" / "x"
    d.mkdir(parents=True)
    trust(d)
    trust(home / "oss")
    assert trusted(home) == {}


def test_worktree_writes_both_keys(home):
    main = home / "work" / "repo"
    main.mkdir(parents=True)
    git(main, "init", "-q")
    git(main, "commit", "-q", "--allow-empty", "-m", "x")
    wt = main / ".worktrees" / "feat"
    git(main, "worktree", "add", "-q", "-b", "feat", str(wt))
    trust(wt)
    k = trusted(home)
    assert k[str(wt)] and k[str(main)]


def test_missing_folder_is_made_by_launch_in_the_area(home):
    # launch() makes the area folder (mkdir) before it trusts it: a folder that was deleted comes back, trusted
    d = home / "personal"
    trust(d)
    assert trusted(home)[str(d)]
