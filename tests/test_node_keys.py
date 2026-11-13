"""The node's keys, and everything that makes one unusable (agent UI, part 2)."""

import os
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from tests.store_support import REPO_ROOT

from node import keys as node_keys
from node.keys import NodeKeyError


@pytest.fixture
def key_dir(tmp_path):
    directory = tmp_path / "keys"
    node_keys.init(directory)
    return directory


def paths(directory):
    return (directory / node_keys.SIGNING_FILE,
            directory / node_keys.ENCRYPTION_FILE)


def test_init_makes_two_owner_only_files(key_dir):
    for path in paths(key_dir):
        assert path.stat().st_mode & 0o777 == 0o600
    assert key_dir.stat().st_mode & 0o777 == 0o700


def test_what_init_wrote_loads_back(tmp_path):
    directory = tmp_path / "keys"
    made = node_keys.init(directory)
    loaded = node_keys.load(*paths(directory))
    assert loaded.signing_fingerprint == made.signing_fingerprint
    assert loaded.encryption_fingerprint == made.encryption_fingerprint


def test_keys_load_from_the_settings(key_dir, monkeypatch):
    signing, encryption = paths(key_dir)
    monkeypatch.setenv("BIOCHEF_NODE_SIGNING_KEY", str(signing))
    monkeypatch.setenv("BIOCHEF_NODE_ENCRYPTION_KEY", str(encryption))
    assert node_keys.load().signing_fingerprint.startswith("ed25519:")


@pytest.mark.parametrize("setting", ["BIOCHEF_NODE_SIGNING_KEY",
                                     "BIOCHEF_NODE_ENCRYPTION_KEY"])
def test_an_unset_setting_is_named(key_dir, monkeypatch, setting):
    signing, encryption = paths(key_dir)
    monkeypatch.setenv("BIOCHEF_NODE_SIGNING_KEY", str(signing))
    monkeypatch.setenv("BIOCHEF_NODE_ENCRYPTION_KEY", str(encryption))
    monkeypatch.delenv(setting)
    with pytest.raises(NodeKeyError, match=setting):
        node_keys.load()


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o606, 0o610])
def test_a_key_others_can_reach_is_refused(key_dir, mode):
    signing, encryption = paths(key_dir)
    signing.chmod(mode)
    with pytest.raises(NodeKeyError, match="chmod 600"):
        node_keys.load(signing, encryption)


def test_the_encryption_key_is_held_to_the_same_rule(key_dir):
    signing, encryption = paths(key_dir)
    encryption.chmod(0o644)
    with pytest.raises(NodeKeyError, match="chmod 600"):
        node_keys.load(signing, encryption)


def test_read_only_for_its_owner_is_fine(key_dir):
    signing, encryption = paths(key_dir)
    signing.chmod(0o400)
    node_keys.load(signing, encryption)


def test_a_missing_file_is_refused(key_dir):
    signing, encryption = paths(key_dir)
    signing.unlink()
    with pytest.raises(NodeKeyError, match="cannot be read"):
        node_keys.load(signing, encryption)


def test_an_unreadable_file_is_refused(key_dir):
    signing, encryption = paths(key_dir)
    signing.chmod(0o000)
    with pytest.raises(NodeKeyError, match="cannot be read"):
        node_keys.load(signing, encryption)


def test_a_directory_is_refused(key_dir, tmp_path):
    (tmp_path / "dir").mkdir(mode=0o700)
    with pytest.raises(NodeKeyError, match="not a file"):
        node_keys.load(tmp_path / "dir", paths(key_dir)[1])


def test_keys_swapped_are_refused(key_dir):
    signing, encryption = paths(key_dir)
    with pytest.raises(NodeKeyError, match="wrong type"):
        node_keys.load(encryption, signing)


def test_a_key_of_another_algorithm_is_refused(key_dir):
    other = ec.generate_private_key(ec.SECP256R1())
    signing, encryption = paths(key_dir)
    signing.write_bytes(other.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    with pytest.raises(NodeKeyError, match="wrong type"):
        node_keys.load(signing, encryption)


def test_garbage_is_refused_without_quoting_it(key_dir):
    signing, encryption = paths(key_dir)
    signing.write_bytes(b"SECRET-CONTENT")
    with pytest.raises(NodeKeyError) as refused:
        node_keys.load(signing, encryption)
    assert "SECRET-CONTENT" not in str(refused.value)
    assert refused.value.__suppress_context__


def test_a_passphrase_protected_key_is_refused(key_dir):
    key = node_keys.generate().signing
    signing, encryption = paths(key_dir)
    signing.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"passphrase")))
    with pytest.raises(NodeKeyError, match="unencrypted"):
        node_keys.load(signing, encryption)


def test_a_key_inside_the_repository_is_refused(key_dir, tmp_path):
    inside = REPO_ROOT / "tests" / "not-a-real-key"
    with pytest.raises(NodeKeyError, match="inside the repository"):
        node_keys.load(inside, paths(key_dir)[1])


def test_a_link_into_the_repository_is_refused_too(key_dir, tmp_path):
    link = tmp_path / "link.pem"
    link.symlink_to(REPO_ROOT / "README.md")
    with pytest.raises(NodeKeyError, match="inside the repository"):
        node_keys.load(link, paths(key_dir)[1])


def test_init_refuses_to_write_inside_the_repository():
    with pytest.raises(NodeKeyError, match="inside the repository"):
        node_keys.init(REPO_ROOT / "keys")
    assert not (REPO_ROOT / "keys").exists()


@pytest.mark.parametrize("existing", [node_keys.SIGNING_FILE,
                                      node_keys.ENCRYPTION_FILE])
def test_init_never_replaces_a_key(tmp_path, existing):
    directory = tmp_path / "keys"
    directory.mkdir()
    (directory / existing).write_bytes(b"precious")
    with pytest.raises(NodeKeyError, match="not replacing"):
        node_keys.init(directory)
    assert (directory / existing).read_bytes() == b"precious"
    assert len(os.listdir(directory)) == 1


def test_a_narrow_umask_does_not_matter_and_a_wide_one_cannot_widen(tmp_path):
    previous = os.umask(0)
    try:
        node_keys.init(tmp_path / "keys")
    finally:
        os.umask(previous)
    for path in paths(tmp_path / "keys"):
        assert path.stat().st_mode & 0o777 == 0o600


def test_fingerprints_have_the_spec_format():
    made = node_keys.generate()
    kind, _, digest = made.signing_fingerprint.partition(":")
    assert kind == "ed25519" and len(digest) == 64
    short = node_keys.short_fingerprint(made.encryption_fingerprint)
    digest = made.encryption_fingerprint.partition(":")[2]
    assert short == f"x25519:{digest[:4]}…{digest[-4:]}"


def test_keys_do_not_print_themselves():
    made = node_keys.generate()
    assert "PrivateKey" not in repr(made)
    assert repr(made).startswith("NodeKeys(ed25519:")


def test_the_command_line_prints_fingerprints_and_no_key(tmp_path):
    directory = tmp_path / "keys"
    result = subprocess.run(
        [sys.executable, "-m", "node.keys", "init", "--out", str(directory)],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    lines = result.stdout.split()
    assert [line.split(":")[0] for line in lines] == ["ed25519", "x25519"]
    assert "PRIVATE" not in result.stdout + result.stderr
    loaded = node_keys.load(*paths(directory))
    assert lines == [loaded.signing_fingerprint, loaded.encryption_fingerprint]


def test_the_command_line_says_why_it_refused(key_dir):
    result = subprocess.run(
        [sys.executable, "-m", "node.keys", "init", "--out", str(key_dir)],
        cwd=REPO_ROOT, capture_output=True, text=True)
    assert result.returncode == 1
    assert "not replacing" in result.stderr
    assert result.stdout == ""


def test_a_pem_file_cannot_be_added_to_the_repository():
    ignored = subprocess.run(["git", "check-ignore", "-q", "node-signing.pem"],
                             cwd=REPO_ROOT)
    assert ignored.returncode == 0
