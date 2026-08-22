"""Running every step in a container (#15).

The seam made the strategy replaceable. This is the second provider, and what
E2 is actually for: a step no longer runs on the host as the agent's own user.

Apptainer rather than docker. Snakemake has no docker-per-rule mode at all --
its per-rule container support goes through apptainer, which pulls docker://
images, needs no daemon and no root, and is what the HPC systems this is meant
to reach actually have (F4). One mechanism for the server and the cluster.
"""

import os
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

if "oras" not in sys.modules:
    oras = types.ModuleType("oras")
    client_mod = types.ModuleType("oras.client")

    class _Client:
        def __init__(self, *a, **k):
            pass

        def login(self, *a, **k):
            pass

        def pull(self, *a, **k):
            raise AssertionError("a test reached the registry")

    client_mod.OrasClient = _Client
    oras.client = client_mod
    sys.modules["oras"] = oras
    sys.modules["oras.client"] = client_mod

import pytest

from runner import PROVIDERS, ApptainerRunner, SubprocessRunner, get_runner


class _Workspace:
    def __init__(self, path):
        self.path = str(path)


# --------------------------------------------------------------------------
# the provider exists and is selectable


def test_both_providers_are_available():
    assert sorted(PROVIDERS) == ["apptainer", "subprocess"]
    assert isinstance(get_runner("apptainer"), ApptainerRunner)


def test_the_container_provider_asks_snakemake_to_deploy_with_apptainer(tmp_path):
    argv = ApptainerRunner(image="docker://alpine:3.20",
                           cache_dir="/cache").command(_Workspace(tmp_path))
    assert "--software-deployment-method" in argv
    assert argv[argv.index("--software-deployment-method") + 1] == "apptainer"
    assert argv[argv.index("--apptainer-prefix") + 1] == "/cache"


def test_the_two_providers_run_the_same_workflow_the_same_way(tmp_path):
    """E2's acceptance is that both produce identical outputs.

    That can only hold if they are running the same thing, so the parts that
    decide WHAT runs -- the Snakefile and the working directory -- must be
    identical, and only the deployment flags may differ.
    """
    ws = _Workspace(tmp_path)
    plain = SubprocessRunner().command(ws)
    boxed = ApptainerRunner(image="docker://alpine:3.20", cache_dir="/cache",
                            apptainer_args="--contain").command(ws)

    assert boxed[:len(plain)] == plain, (
        "the container provider must run the same snakemake invocation, plus "
        "deployment flags -- not a different one"
    )
    assert boxed[len(plain):] == [
        "--software-deployment-method", "apptainer", "--apptainer-prefix", "/cache",
        "--apptainer-args=--contain",
    ]


def test_the_container_is_contained_by_default():
    """--contain, and the default is the security property, not a preference.

    Apptainer binds the host's /tmp unless told not to, and make_workspace puts
    a run in the system temp directory whenever BIOCHEF_RUN_ROOT is unset --
    which is the default. Without this a containerised tool is walled off from
    /usr and /etc while still able to read every other run's workspace, and the
    container runs as the same user so the 0700 mode on a workspace does not
    help. That is the wrong half to isolate for a service whose reason to exist
    is keeping one dataset away from another.

    Proved end to end by the CI parity check, which fails if a step inside the
    container can read a file standing in for another run.
    """
    import runner as runner_module

    assert runner_module.APPTAINER_ARGS == "--contain"
    argv = ApptainerRunner(cache_dir="/cache").command(_Workspace("/ws"))
    assert "--apptainer-args=--contain" in argv


def test_the_apptainer_args_are_one_argv_element():
    """"--apptainer-args=X", not "--apptainer-args", "X".

    The value starts with a dash. As a separate element argparse reads it as
    another option and snakemake exits 2 with "expected one argument" and a
    page of usage, which reads like a snakemake problem rather than a quoting
    one. Confirmed against snakemake 9.21 both ways: the two-element form dies
    at argparse, the "=" form parses and reaches the apptainer check.
    """
    argv = ApptainerRunner(cache_dir="/cache",
                           apptainer_args="--contain --cleanenv").command(
        _Workspace("/ws"))
    assert "--apptainer-args" not in argv, (
        "a bare --apptainer-args element means the value was passed separately"
    )
    assert "--apptainer-args=--contain --cleanenv" in argv


def test_snakemake_accepts_the_argv_this_provider_builds(tmp_path):
    """The check that was missing, and the reason a broken argv reached CI.

    Every other test here inspects the list we build. None of them handed it to
    snakemake, so "--apptainer-args", "--contain" as two elements looked
    perfectly correct and died at argparse on the runner.

    Exit 2 is argparse rejecting the command line. Exit 1 with the apptainer
    message means the argv was understood and snakemake got as far as looking
    for a container runtime, which is as far as it can get here.
    """
    import shutil
    import subprocess

    snakemake = shutil.which("snakemake")
    if snakemake is None:
        pytest.skip("snakemake is not on PATH")

    runner = ApptainerRunner(image="docker://debian:stable-slim",
                             cache_dir=str(tmp_path / "cache"))
    (tmp_path / "Snakefile").write_text(
        runner.snakefile_preamble()
        + '\nrule all:\n    input: "o.txt"\n\nrule m:\n'
          '    output: o_0="o.txt"\n    shell: "echo hi > {output.o_0}"\n'
    )

    done = subprocess.run(runner.command(_Workspace(tmp_path)) + ["--dry-run"],
                          capture_output=True, text=True, cwd=str(tmp_path),
                          timeout=120)
    assert done.returncode != 2, (
        f"snakemake rejected the command line this provider builds:\n"
        f"{done.stderr[-1200:]}"
    )


def test_containment_can_be_turned_off_deliberately():
    """An operator who needs the host /tmp visible can say so, and gets no flag
    rather than an empty one that apptainer would reject."""
    argv = ApptainerRunner(cache_dir="/cache",
                           apptainer_args="").command(_Workspace("/ws"))
    assert "--apptainer-args" not in argv


# --------------------------------------------------------------------------
# the Snakefile preamble, which is how a provider asks for a container
# without the emitter knowing anything about runners


def test_the_container_is_requested_globally_in_the_snakefile():
    preamble = ApptainerRunner(image="docker://alpine:3.20").snakefile_preamble()
    assert preamble == 'container: "docker://alpine:3.20"\n'


def test_the_subprocess_provider_contributes_nothing():
    """So the Snakefile it runs is byte-for-byte the emitter's, as before."""
    assert SubprocessRunner().snakefile_preamble() == ""


def test_snakemake_itself_parses_the_preamble_plus_the_emitted_snakefile(tmp_path):
    """Checked with snakemake's parser, not Python's.

    A Snakefile is not plain Python -- `rule all:` is a SyntaxError to
    compile() -- so the only honest check is snakemake reading the file it will
    actually be given. A dry run parses and plans without needing apptainer to
    be installed, which is what makes this runnable here at all.
    """
    import shutil
    import subprocess

    import convert

    text = (ApptainerRunner(image="docker://alpine:3.20").snakefile_preamble()
            + convert.convert_to_snakemake(convert.Workflow(nodes=[])))
    (tmp_path / "Snakefile").write_text(text)

    snakemake = shutil.which("snakemake")
    if snakemake is None:
        pytest.skip("snakemake is not on PATH")

    done = subprocess.run(
        [snakemake, "-s", str(tmp_path / "Snakefile"), "-d", str(tmp_path),
         "--cores", "1", "--dry-run"],
        capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stderr[-1500:]
    assert "SyntaxError" not in done.stderr


# --------------------------------------------------------------------------
# the image is configuration that reaches the interpreter as code


@pytest.mark.parametrize("hostile", [
    'docker://x"\nimport os\nos.system("id")\n#',
    "docker://x'; import os; os.system('id')",
    'docker://x"',
    "docker://x\nrule evil:\n    shell: 'id'",
    "docker://x\\",
    "",
    "../../etc/passwd\nimport os",
])
def test_an_image_that_is_not_a_container_reference_is_refused(hostile):
    """Shape check and json.dumps, both.

    Either would do on its own. Both, because this repository has already
    shipped one injection that survived a quoting function: shlex.quote was
    correct for a shell, and the Snakefile turned out to be Python (#43).
    """
    with pytest.raises(ValueError, match="BIOCHEF_CONTAINER_IMAGE"):
        ApptainerRunner(image=hostile)


@pytest.mark.parametrize("no_scheme", [
    "debian:stable-slim",
    "ubuntu",
    "some.sif",
    "biocontainers/samtools:1.19",
])
def test_an_image_with_no_scheme_is_refused(no_scheme):
    """Because snakemake reads it as a local FILE, not as a registry reference.

    deployment/singularity.py decides with is_local_file(url): anything without
    a scheme is local, and its path is resolved against the working directory --
    which for us is the run's own workspace, where the client's uploads are
    written. So `BIOCHEF_CONTAINER_IMAGE=ubuntu` does not fail, it quietly means
    "use a file out of the run directory as the container image".

    An operator's typo should say so rather than becoming a different feature.
    """
    with pytest.raises(ValueError, match="no scheme"):
        ApptainerRunner(image=no_scheme)


@pytest.mark.parametrize("not_an_image", ["/etc/passwd", "/etc/shadow", "/tmp/x"])
def test_an_absolute_path_that_is_not_an_image_is_refused(not_an_image):
    """A local image is allowed, but it has to look like one.

    Absolute paths are permitted so an operator can point at a .sif built ahead
    of time -- which is the normal thing to do on an HPC system with no outbound
    network -- but only with an image extension.
    """
    with pytest.raises(ValueError):
        ApptainerRunner(image=not_an_image)


@pytest.mark.parametrize("legitimate", [
    "docker://debian:stable-slim",
    "oras://registry.biochef.app/image:1",
    "library://user/collection/image:tag",
    "/opt/images/samtools.sif",
    "/opt/images/samtools.simg",
    "docker://biocontainers/samtools:1.19--h50ea8bc_0",
    "docker://ghcr.io/ieeta-pt/biochef@sha256:" + "a" * 64,
    "docker://quay.io/biocontainers/seqtk:1.4--he4a0461_2",
])
def test_a_real_image_reference_is_accepted(legitimate):
    """The shape check must not be so tight it refuses the registry's own names."""
    runner = ApptainerRunner(image=legitimate)
    assert legitimate in runner.snakefile_preamble()
    compile(runner.snakefile_preamble(), "Snakefile", "exec")


def test_a_bad_image_stops_the_process_rather_than_every_request(monkeypatch):
    """The refusal has to happen where the provider is resolved, which is at
    import, not when a Snakefile is first written.

    Otherwise the service starts, accepts work, and fails every submission with
    a 500 that says nothing about the misconfigured image.
    """
    import runner as runner_module

    monkeypatch.setattr(runner_module, "CONTAINER_IMAGE", "not a reference")
    with pytest.raises(ValueError, match="BIOCHEF_CONTAINER_IMAGE"):
        get_runner("apptainer")

    monkeypatch.setattr(runner_module, "CONTAINER_IMAGE", "docker://alpine:3.20")
    assert isinstance(get_runner("apptainer"), ApptainerRunner)


# --------------------------------------------------------------------------
# the service actually uses it


def test_the_service_writes_the_preamble_into_the_snakefile():
    """The wiring, not just the pieces.

    Read from main.py's own source: the preamble has to be applied where the
    Snakefile is built, or the provider asks for a container that never appears.
    """
    source = Path(REPO_ROOT / "main.py").read_text()
    assert "RUNNER.snakefile_preamble()" in source
    assert "snakefile_preamble() + convert_to_snakemake" in source
