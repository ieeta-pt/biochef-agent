"""BIOCHEF_APPTAINER_BINARY, and the singularity runner (SPEC.md, section 14.3).

Snakemake has no setting for the binary: it runs `apptainer` if one is on PATH
and `singularity` otherwise. The agent names the binary absolutely, checks what
it says it is, and puts a link to it first on snakemake's PATH.
"""

import os
import shutil

import pytest

from runner import (ENFORCED, PROVIDERS, ApptainerRunner, SingularityRunner,
                    get_runner)


def fake(folder, name, says):
    path = folder / name
    path.write_text(f'#!/bin/sh\necho "{says}"\n')
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def singularity(tmp_path):
    return fake(tmp_path, "singularity", "singularity-ce version 4.6.0-noble")


def test_singularity_is_a_runner(monkeypatch, singularity):
    monkeypatch.setenv("BIOCHEF_APPTAINER_BINARY", singularity)
    assert "singularity" in PROVIDERS
    found = get_runner("singularity")
    assert isinstance(found, SingularityRunner) and found.binary == singularity


@pytest.mark.parametrize("value", [None, "", "apptainer", "bin/apptainer"])
def test_the_binary_has_no_default(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("BIOCHEF_APPTAINER_BINARY")
    else:
        monkeypatch.setenv("BIOCHEF_APPTAINER_BINARY", value)
    with pytest.raises(ValueError, match="absolute path.*no default"):
        ApptainerRunner()


def test_a_file_that_cannot_run_is_refused(tmp_path):
    path = tmp_path / "apptainer"
    path.write_text("#!/bin/sh\n")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="not an executable file"):
        ApptainerRunner(binary=str(path))
    with pytest.raises(ValueError, match="not an executable file"):
        ApptainerRunner(binary=str(tmp_path / "missing"))


def test_singularity_is_not_apptainer(singularity):
    with pytest.raises(ValueError, match="which is not apptainer"):
        ApptainerRunner(binary=singularity)


def test_apptainer_is_not_singularity(tmp_path):
    with pytest.raises(ValueError, match="which is not singularity"):
        SingularityRunner(binary=fake(tmp_path, "apptainer", "apptainer version 1.3.6"))


def test_apptainers_own_singularity_link_is_not_singularity(tmp_path):
    """apptainer's packages install a `singularity` that is apptainer."""
    link = fake(tmp_path, "singularity", "apptainer version 1.3.6")
    with pytest.raises(ValueError, match="which is not singularity"):
        SingularityRunner(binary=link)


def test_old_singularity_is_not_singularity_ce(tmp_path):
    with pytest.raises(ValueError, match="which is not singularity"):
        SingularityRunner(binary=fake(tmp_path, "singularity", "singularity version 3.7.4"))


def test_the_version_is_in_the_description(singularity):
    assert "singularity-ce version 4.6.0" in SingularityRunner(binary=singularity).describe()


def test_the_configured_binary_is_the_apptainer_snakemake_finds(tmp_path, monkeypatch,
                                                                 singularity):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    fake(elsewhere, "apptainer", "apptainer version 1.0.0")
    monkeypatch.setenv("PATH", f"{elsewhere}{os.pathsep}{os.environ['PATH']}")
    runner = SingularityRunner(binary=singularity)
    path = runner.environment()["PATH"]
    found = shutil.which("apptainer", path=path)
    assert os.path.realpath(found) == os.path.realpath(singularity)
    assert path.endswith(os.environ["PATH"])
    assert os.stat(os.path.dirname(found)).st_mode & 0o777 == 0o700


def test_the_link_is_made_once(singularity):
    runner = SingularityRunner(binary=singularity)
    assert runner.environment()["PATH"] == runner.environment()["PATH"]


def test_snakemake_gets_that_path(tmp_path, singularity):
    class Probe(SingularityRunner):
        def command(self, ws):
            return ["sh", "-c", "command -v apptainer; apptainer --version"]

    class Ws:
        path = str(tmp_path)

    out = Probe(binary=singularity).run(Ws(), timeout_s=10).stdout.split("\n")
    assert out[1] == "singularity-ce version 4.6.0-noble"
    assert os.path.realpath(out[0]) == os.path.realpath(singularity)


def test_singularity_guarantees_what_its_flags_say(singularity):
    assert set(SingularityRunner(binary=singularity).guarantees.values()) == {ENFORCED}
    bare = SingularityRunner(binary=singularity, apptainer_args="")
    assert not bare.isolates_network


def test_the_rule_accepts_singularity(monkeypatch, singularity):
    monkeypatch.setenv("BIOCHEF_APPTAINER_BINARY", singularity)
    monkeypatch.setenv("BIOCHEF_REQUIRE_NETWORK_ISOLATION", "true")
    assert get_runner("singularity").isolates_network


def test_other_runners_get_the_agents_environment():
    from runner import SubprocessRunner
    assert SubprocessRunner().environment() is None
