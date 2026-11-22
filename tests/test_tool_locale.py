"""Every tool step runs in C.UTF-8, whatever locale the Agent was started with."""

import pytest

from runner import TOOL_LOCALE, ApptainerRunner, Runner


class _Workspace:
    def __init__(self, path):
        self.path = str(path)


class _ReportEnv(Runner):
    name = "report-env-for-test"

    def command(self, ws):
        return ["sh", "-c", 'printf "%s|%s\\n" "$LC_ALL" "$PROBE"']


@pytest.mark.parametrize("host", [{"LANG": "en_US.UTF-8"}, {"LC_ALL": "C"}, {}])
def test_a_step_gets_the_tool_locale(monkeypatch, tmp_path, host):
    for name in ("LANG", "LC_ALL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in host.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PROBE", "kept")

    result = _ReportEnv().run(_Workspace(tmp_path), timeout_s=10)

    assert result.stdout.strip() == "C.UTF-8|kept"


def test_the_container_runner_passes_the_locale_through_cleanenv():
    env = ApptainerRunner(image="docker://debian:stable-slim",
                          cache_dir="/nonexistent").environment()
    assert env["APPTAINERENV_LC_ALL"] == env["SINGULARITYENV_LC_ALL"] == TOOL_LOCALE
