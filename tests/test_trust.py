"""Trust configuration, and whether the verdicts it renders overstate themselves.

Two properties matter more than the rendering: a key the user configured must
actually change the answer (the console could not say "verified" at all before
it had one), and no verdict may claim coverage it does not have. The tests below
sign real artifacts rather than hand-rolling signature blocks, so the coverage
claims are checked against what opentine genuinely signs.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from opentine.core import Graph, Run, RunStatus, Step, StepKind
from opentine.signing import HAS_ED25519, generate_ed25519

from opentine_gui import trust
from opentine_gui.trust import (
    HMAC_KEY_ENV,
    HMAC_KEY_PREF,
    MAX_KEY_FILE_BYTES,
    PUBLIC_KEY_ENV,
    PUBLIC_KEY_PREF,
    TRUST_EMBEDDED_PREF,
    TrustConfig,
    coverage_lines,
    integrity_line,
    load_trust_config,
    signature_line,
    signature_scheme,
    verifier,
)

#: 32 bytes, comfortably over opentine's 16-byte floor, and hex so the same value
#: can be handed to the env var as inline material.
KEY = b"0123456789abcdef0123456789abcdef"
OTHER_KEY = b"fedcba9876543210fedcba9876543210"


def _run(run_id: str = "abc") -> Run:
    graph = Graph()
    graph.add(Step(id="s1", parent_ids=[], kind=StepKind.think, inputs={"text": "plan"}))
    graph.add(
        Step(id="s2", parent_ids=["s1"], kind=StepKind.done, inputs={}, outputs={"text": "ok"})
    )
    # Only a terminal run may be signed at all.
    return Run(id=run_id, graph=graph, status=RunStatus.completed, user_prompt="hi")


def _signed(tmp_path: Path, **save_kwargs) -> Path:
    path = tmp_path / "abc.tine"
    save_kwargs.setdefault("sign_key", KEY)
    _run().save(path, **save_kwargs)
    return path


def _verdict(result) -> dict:
    """The verdict dict shape `sources._verify_cached` hands the renderers."""
    return {
        "ok": bool(result.ok),
        "state": result.state,
        "reason": result.reason,
        "draft": False,
        "signer": result.signer,
        "algorithm": result.algorithm,
        "key_id": result.key_id,
    }


def _keyfile(tmp_path: Path, name: str, key: bytes) -> str:
    path = tmp_path / name
    path.write_bytes(key)
    return str(path)


# ---- key loading ----

def test_a_configured_key_verifies_what_the_keyless_check_cannot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _signed(tmp_path)
    monkeypatch.setenv(HMAC_KEY_ENV, _keyfile(tmp_path, "key", KEY))
    config = load_trust_config()
    assert config.problem == ""
    assert config.configured
    assert verifier(config)(path).state == "verified"
    # The state this console was stuck at before it could load key material.
    assert Run.verify_signature(path).state == "no-key"


def test_the_config_carries_its_own_bound_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _signed(tmp_path)
    monkeypatch.setenv(HMAC_KEY_ENV, _keyfile(tmp_path, "key", KEY))
    assert load_trust_config().verifier(path).state == "verified"


def test_the_env_var_also_accepts_the_secret_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HMAC_KEY_ENV, KEY.decode())
    config = load_trust_config()
    assert config.hmac_key == KEY
    assert config.problem == ""


def test_a_path_shaped_value_is_never_used_as_key_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A typo in a path must degrade to "no key", not become a secret that reports
    # every correctly signed artifact as INVALID.
    monkeypatch.setenv(HMAC_KEY_ENV, str(tmp_path / "typo" / "key"))
    config = load_trust_config()
    assert config.hmac_key is None
    assert not config.configured
    assert "no such file" in config.problem


def test_a_wrong_key_reports_a_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _signed(tmp_path)
    monkeypatch.setenv(HMAC_KEY_ENV, _keyfile(tmp_path, "wrong", OTHER_KEY))
    verdict = _verdict(verifier(load_trust_config())(path))
    assert verdict["state"] == "mismatch"
    assert "INVALID" in signature_line(verdict, scheme=signature_scheme(path))


def test_preferences_supply_a_key_and_the_environment_outranks_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefs = {HMAC_KEY_PREF: _keyfile(tmp_path, "pref", KEY)}
    monkeypatch.delenv(HMAC_KEY_ENV, raising=False)
    assert load_trust_config(prefs).hmac_key == KEY
    monkeypatch.setenv(HMAC_KEY_ENV, _keyfile(tmp_path, "env", OTHER_KEY))
    assert load_trust_config(prefs).hmac_key == OTHER_KEY


def test_trust_on_first_use_is_off_unless_it_is_asked_for() -> None:
    assert not load_trust_config().trust_embedded
    assert not load_trust_config({TRUST_EMBEDDED_PREF: ""}).trust_embedded
    assert not load_trust_config({TRUST_EMBEDDED_PREF: "maybe"}).trust_embedded
    assert load_trust_config({TRUST_EMBEDDED_PREF: "true"}).trust_embedded
    assert load_trust_config({TRUST_EMBEDDED_PREF: "1"}).configured


# ---- key files that are not keys ----

@pytest.mark.parametrize(
    ("name", "expected"),
    [("missing", "no such file"), ("directory", "not a regular file")],
)
def test_an_unusable_key_path_becomes_a_problem_not_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, expected: str
) -> None:
    target = tmp_path / name
    if name == "directory":
        target.mkdir()
    monkeypatch.setenv(HMAC_KEY_ENV, str(target))
    config = load_trust_config()
    assert config.hmac_key is None
    assert not config.configured
    assert expected in config.problem
    assert config.fingerprint == ""
    # Still usable: the panel falls back to the keyless answer.
    assert verifier(config)(tmp_path / "nothing.tine").state == "error"


def test_an_oversize_key_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HMAC_KEY_ENV, _keyfile(tmp_path, "big", b"k" * (MAX_KEY_FILE_BYTES + 1)))
    config = load_trust_config()
    assert config.hmac_key is None
    assert str(MAX_KEY_FILE_BYTES) in config.problem


def test_an_empty_key_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HMAC_KEY_ENV, _keyfile(tmp_path, "empty", b""))
    assert "empty" in load_trust_config().problem


def test_a_short_secret_is_refused_here_rather_than_per_artifact(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    # opentine turns a short key into an `error` verdict on every file, which
    # reads as a fault in the artifact rather than in the configuration.
    monkeypatch.setenv(HMAC_KEY_ENV, "tiny")
    config = load_trust_config()
    assert config.hmac_key is None
    assert "shorter than" in config.problem
    assert "tiny" not in config.problem


def test_a_problem_message_survives_a_hostile_path(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(HMAC_KEY_ENV, raising=False)
    # A preference is declared to be a path, so it is named in the message - and
    # so it has to be flattened, or it forges a second trust row.
    forgery = "/no/such\nSignature: verified by root/key"
    problem = load_trust_config({HMAC_KEY_PREF: forgery}).problem
    assert problem and "\n" not in problem
    monkeypatch.setenv(HMAC_KEY_ENV, forgery)
    problem = load_trust_config().problem
    assert problem and "\n" not in problem


def test_a_path_shaped_secret_is_never_echoed_back(monkeypatch: pytest.MonkeyPatch) -> None:
    # $OPENTINE_GUI_HMAC_KEY holds either a file name or the secret itself, told
    # apart by shape - and `openssl rand -base64 32` contains a "/" about half
    # the time, so a real secret lands on the path branch and fails there. The
    # message is rendered in the trust panel and pasted into bug reports, so it
    # must not carry the value that produced it.
    secret = "xK3/9aBqZmT1nP0sVwEuYr7dLgHjFcNbQaZxCvBnMk8="
    monkeypatch.setenv(HMAC_KEY_ENV, secret)
    config = load_trust_config()
    assert config.hmac_key is None  # fails closed, as a mistyped path must
    for rendered in (config.problem, config.source, repr(config)):
        assert secret not in rendered
        for fragment in (secret[:12], secret[10:24], secret[-12:]):
            assert fragment not in rendered
    # Still says which setting failed and what was done with it.
    assert HMAC_KEY_ENV in config.problem and "file path" in config.problem


def test_a_secret_that_is_not_valid_utf8_still_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `export OPENTINE_GUI_HMAC_KEY=$(head -c 32 /dev/urandom)` is how a key gets
    # made, and os.environ hands those bytes back as surrogateescaped text that
    # plain str.encode refuses. Encoding it back the same way is what makes the
    # key that signed the artifact the key that checks it.
    raw = bytes(range(200, 232))
    assert len(raw) == 32
    with pytest.raises(UnicodeDecodeError):
        raw.decode()
    path = _signed(tmp_path, sign_key=raw)
    monkeypatch.setenv(HMAC_KEY_ENV, raw.decode("utf-8", "surrogateescape"))
    config = load_trust_config()
    assert config.problem == ""
    assert config.hmac_key == raw
    assert verifier(config)(path).state == "verified"


@pytest.mark.parametrize(
    "value",
    [
        # Path.expanduser raises RuntimeError for a ~user with no home directory.
        "~nosuchuser67890/key",
        "~nosuchuser67890",
        # stat raises ValueError, not OSError, for a NUL byte - which a
        # hand-edited preferences file can hold as a "\\u0000" escape. (The OS
        # refuses one in an environment variable, so this reaches us only
        # through preferences.)
        "/tmp/embedded\x00nul/key",
    ],
)
def test_an_unexpandable_or_unrepresentable_path_is_a_problem_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    # Neither may stop the console opening over a setting saved on another day.
    monkeypatch.delenv(HMAC_KEY_ENV, raising=False)
    monkeypatch.delenv(PUBLIC_KEY_ENV, raising=False)
    for prefs in ({HMAC_KEY_PREF: value}, {PUBLIC_KEY_PREF: value}):
        config = load_trust_config(prefs)
        assert not config.configured
        assert config.problem and "\n" not in config.problem
    if "\x00" in value:
        return
    monkeypatch.setenv(HMAC_KEY_ENV, value)
    assert load_trust_config().problem
    monkeypatch.delenv(HMAC_KEY_ENV)
    monkeypatch.setenv(PUBLIC_KEY_ENV, value)
    assert load_trust_config().problem


@pytest.mark.parametrize(
    "prefs",
    [
        {TRUST_EMBEDDED_PREF: True},
        {HMAC_KEY_PREF: 123},
        {PUBLIC_KEY_PREF: ["a", "b"]},
        {HMAC_KEY_PREF: None},
        ["not a mapping at all"],
        "not a mapping either",
    ],
)
def test_a_preferences_file_of_any_shape_cannot_crash_the_load(
    monkeypatch: pytest.MonkeyPatch, prefs: object
) -> None:
    # _load_preferences drops non-string values today, but `trust_embedded: true`
    # is the obvious way to hand-edit the file and .strip() on a bool is a
    # startup crash. A non-string reads as absent, which for a trust switch is
    # also the safe reading.
    monkeypatch.delenv(HMAC_KEY_ENV, raising=False)
    monkeypatch.delenv(PUBLIC_KEY_ENV, raising=False)
    config = load_trust_config(prefs)
    assert not config.configured
    assert config.problem == ""


# ---- ed25519 ----

@pytest.mark.skipif(not HAS_ED25519, reason="ed25519 needs the cryptography package")
def test_an_ed25519_signature_verifies_against_a_configured_public_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private, public = generate_ed25519()
    path = _signed(tmp_path, sign_key=private, sign_algorithm="ed25519", signer="sec@example.com")
    pub = tmp_path / "pub.hex"
    pub.write_text(public)
    monkeypatch.setenv(PUBLIC_KEY_ENV, str(pub))
    config = load_trust_config()
    assert config.problem == ""
    assert config.public_key is not None
    assert verifier(config)(path).state == "verified"


@pytest.mark.skipif(not HAS_ED25519, reason="ed25519 needs the cryptography package")
def test_trust_on_first_use_never_renders_as_verified(tmp_path: Path) -> None:
    private, _ = generate_ed25519()
    path = _signed(tmp_path, sign_key=private, sign_algorithm="ed25519", signer="sec@example.com")
    result = verifier(load_trust_config({TRUST_EMBEDDED_PREF: "yes"}))(path)
    assert result.state == "verified-tofu"
    line = signature_line(_verdict(result), scheme=signature_scheme(path))
    # The key came out of the file being checked: it proves self-consistency and
    # nothing about authorship, so the word "verified" must not appear at all.
    assert "verified" not in line
    assert "trust on first use" in line
    assert any("not authorship" in x for x in coverage_lines(_verdict(result), scheme="tine-sig/2"))


def test_a_malformed_public_key_file_becomes_a_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    junk = tmp_path / "pub.hex"
    junk.write_bytes(b"not a key at all")
    monkeypatch.setenv(PUBLIC_KEY_ENV, str(junk))
    config = load_trust_config()
    assert config.public_key is None
    assert not config.configured
    assert PUBLIC_KEY_ENV in config.problem


def test_a_public_key_can_come_from_preferences(tmp_path: Path) -> None:
    junk = tmp_path / "pub.hex"
    junk.write_bytes(b"not a key at all")
    assert PUBLIC_KEY_PREF in load_trust_config({PUBLIC_KEY_PREF: str(junk)}).problem


# ---- fingerprint ----

def test_the_fingerprint_is_stable_and_does_not_leak_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HMAC_KEY_ENV, _keyfile(tmp_path, "key", KEY))
    first = load_trust_config().fingerprint
    assert first == load_trust_config().fingerprint
    assert len(first) == 16
    assert KEY.decode() not in first
    # Not the bare digest of the secret: that would confirm a guessed key offline.
    assert first != hashlib.sha256(KEY).hexdigest()[:16]
    assert first != hashlib.sha256(KEY).hexdigest()[:len(first)]


def test_a_different_key_gets_a_different_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The verdict cache is keyed by this, so a rotated key must not be served the
    # previous key's answer.
    monkeypatch.setenv(HMAC_KEY_ENV, _keyfile(tmp_path, "key", KEY))
    first = load_trust_config().fingerprint
    monkeypatch.setenv(HMAC_KEY_ENV, _keyfile(tmp_path, "other", OTHER_KEY))
    assert load_trust_config().fingerprint not in ("", first)
    assert load_trust_config({TRUST_EMBEDDED_PREF: "on"}).fingerprint != first


def test_a_config_never_renders_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HMAC_KEY_ENV, KEY.decode())
    config = load_trust_config()
    for rendered in (repr(config), str(config), config.source, config.fingerprint, config.problem):
        assert KEY.decode() not in rendered


# ---- scheme detection ----

def test_the_scheme_is_read_off_a_signed_artifact(tmp_path: Path) -> None:
    assert signature_scheme(_signed(tmp_path)) == "tine-sig/2"


def test_a_v1_signature_block_is_recognised(tmp_path: Path) -> None:
    # Hand-rolled on purpose: nothing this decade writes tine-sig/1, and the
    # console still has to state what an 0.3.0 artifact's signature covers.
    path = tmp_path / "v1.tine"
    path.write_text(
        json.dumps({"metadata": {"integrity": {"signature": {"scheme": "tine-sig/1"}}}})
    )
    assert signature_scheme(path) == "tine-sig/1"


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"metadata": "not an object"},
        {"metadata": {"integrity": ["not", "an", "object"]}},
        {"metadata": {"integrity": {"signature": 7}}},
        {"metadata": {"integrity": {"signature": {"scheme": "tine-sig/9000"}}}},
        {"metadata": {"integrity": {"signature": {"scheme": {"nested": True}}}}},
        {"metadata": {"integrity": {"signature": {"scheme": float("nan")}}}},
        {"metadata": {"integrity": {"signature": {"scheme": 10**400}}}},
    ],
)
def test_a_foreign_signature_shape_yields_no_scheme(data: dict) -> None:
    assert signature_scheme(data) == ""


def test_scheme_detection_fails_open_on_anything_unreadable(tmp_path: Path) -> None:
    unsigned = tmp_path / "plain.tine"
    _run().save(unsigned)
    assert signature_scheme(unsigned) == ""
    junk = tmp_path / "junk.tine"
    junk.write_bytes(b"\x00\xff not json at all")
    assert signature_scheme(junk) == ""
    assert signature_scheme(tmp_path / "missing.tine") == ""
    assert signature_scheme(tmp_path) == ""
    assert signature_scheme(None) == ""


# ---- rendering ----

@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ({"state": "verified", "signer": "alice", "algorithm": "ed25519"},
         "Signature: verified by alice (ed25519, tine-sig/2)"),
        ({"state": "unsigned", "reason": "no signature present"},
         "Signature: unsigned"),
        # ok=False but not an alarm: the file is signed, this console holds no key.
        ({"state": "no-key", "signer": "alice", "reason": "no key supplied"},
         "Signature: present by alice (tine-sig/2), not verified here (no key)"),
        ({"state": "mismatch", "signer": "mallory", "reason": "signature mismatch"},
         "Signature: INVALID by mallory (tine-sig/2) - signature mismatch"),
        ({"state": "error", "reason": "unsupported signature scheme"},
         "Signature: unsupported signature scheme"),
    ],
)
def test_signature_line_renders_each_state(verdict: dict, expected: str) -> None:
    assert signature_line(verdict, scheme="tine-sig/2") == expected


def test_only_a_real_mismatch_reads_as_an_alarm() -> None:
    for state in ("unsigned", "no-key", "verified", "verified-tofu"):
        line = signature_line({"state": state, "reason": "r"}, scheme="tine-sig/1")
        assert "INVALID" not in line, f"{state} must not alarm the user"


def test_a_signer_name_cannot_forge_a_second_trust_row(tmp_path: Path) -> None:
    forgery = "alice\nIntegrity: ok\nSignature: verified by root"
    path = _signed(tmp_path, signer=forgery)
    verdict = _verdict(Run.verify_signature(path, hmac_key=KEY))
    line = signature_line(verdict, scheme=signature_scheme(path))
    # The panel is one flat text widget, so the test is on rows, not substrings:
    # the forged text stays inert inside the row it was interpolated into.
    rows = "\n".join([integrity_line(_integrity(path)), line]).split("\n")
    assert rows == ["Integrity: ok", line]
    assert line.startswith("Signature: verified by alice Integrity: ok Signature: verified by root")


def test_an_artifact_cannot_write_its_own_scheme_into_the_panel() -> None:
    verdict = {"state": "verified", "signer": "alice"}
    assert signature_line(verdict, scheme="tine-sig/2 (all metadata signed)") == (
        "Signature: verified by alice"
    )


@pytest.mark.parametrize(
    "verdict",
    [
        {},
        {"state": None, "reason": None, "signer": None, "algorithm": None},
        {"state": ["weird"], "signer": {"a": 1}, "algorithm": 3, "reason": b"x"},
        {"state": "weird", "reason": ""},
    ],
)
def test_rendering_survives_a_hostile_verdict(verdict: dict) -> None:
    for line in (signature_line(verdict), integrity_line(verdict), *coverage_lines(verdict)):
        assert isinstance(line, str) and "\n" not in line and line


@pytest.mark.parametrize("verdict", ["not a mapping", None, ["state"], 7, object()])
def test_rendering_survives_a_verdict_that_is_not_a_mapping(verdict: object) -> None:
    # These three are the console's last word on whether a file can be trusted,
    # so nothing handed to them may make them the thing that raises.
    for line in (signature_line(verdict), integrity_line(verdict), *coverage_lines(verdict)):
        assert isinstance(line, str) and "\n" not in line and line


def test_rendering_survives_a_value_python_refuses_to_stringify() -> None:
    # CPython refuses str() on an integer past 4300 digits, and the renderers
    # stringify whatever a verdict carries.
    huge = 10**6000
    verdict = {"state": huge, "reason": huge, "signer": huge, "algorithm": huge, "ok": huge}
    with pytest.raises(ValueError):
        str(huge)
    for line in (signature_line(verdict), integrity_line(verdict), *coverage_lines(verdict)):
        assert isinstance(line, str) and "\n" not in line and line


def test_integrity_line_states_only_what_was_checked() -> None:
    assert integrity_line({"ok": True}) == "Integrity: ok"
    assert integrity_line({"ok": True, "draft": True}) == "Integrity: ok (draft)"
    assert integrity_line({"ok": False, "reason": "digest mismatch"}) == (
        "Integrity: FAILED - digest mismatch"
    )
    # A failure of the check itself is not a verdict about the artifact.
    assert integrity_line({"ok": False, "reason": "check failed: boom"}) == (
        "Integrity: check failed: boom"
    )
    assert integrity_line({"ok": False}) == "Integrity: FAILED - unknown"


# ---- coverage ----

def test_coverage_names_what_each_scheme_leaves_out() -> None:
    verdict = {"ok": True, "state": "verified"}
    v1 = coverage_lines(verdict, scheme="tine-sig/1")
    v2 = coverage_lines(verdict, scheme="tine-sig/2")
    assert v1 != v2
    assert any("tags and fork reason are NOT signed" in line for line in v1)
    assert any("tine-sig/2" in line and "covered" in line for line in v2)
    # The digest's blind spot is the same in both cases; what differs is whether
    # anything else covers it. Saying "outside the digest" beside "tine-sig/2
    # signs every metadata key" reads as a contradiction, so a verified v2
    # signature changes the closing line rather than leaving the reader to
    # reconcile the two.
    assert "outside the digest" in v1[-1]
    assert "covered by the signature above" in v2[-1]
    # An unverified signature cannot be the thing that covers it, whatever
    # scheme it names.
    unchecked = coverage_lines({"ok": False, "state": "no-key"}, scheme="tine-sig/2")
    assert "outside the digest" in unchecked[-1]
    assert "outside the digest" in v1[-1]


def test_an_unknown_scheme_is_reported_as_the_narrower_coverage() -> None:
    lines = coverage_lines({"state": "verified"})
    assert any("assume the narrower tine-sig/1" in line for line in lines)


@pytest.mark.parametrize(
    "state",
    ["verified", "verified-tofu", "unsigned", "no-key", "mismatch", "error", "weird", ""],
)
@pytest.mark.parametrize("scheme", ["", "tine-sig/1", "tine-sig/2"])
@pytest.mark.parametrize("draft", [False, True])
def test_coverage_never_outgrows_the_panel(state: str, scheme: str, draft: bool) -> None:
    lines = coverage_lines({"state": state}, scheme=scheme, draft=draft)
    assert 1 <= len(lines) <= 3
    assert lines[-1].startswith("Integrity covers the run body only")


# ---- end to end: what the two verdicts really cover ----

def test_a_tampered_body_reads_as_an_alarm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _signed(tmp_path)
    raw = json.loads(path.read_text())
    next(iter(raw["graph"]["steps"].values()))["inputs"]["text"] = "tampered"
    path.write_text(json.dumps(raw))
    monkeypatch.setenv(HMAC_KEY_ENV, KEY.decode())
    verdict = _verdict(verifier(load_trust_config())(path))
    assert verdict["state"] == "mismatch"
    assert "INVALID" in signature_line(verdict, scheme=signature_scheme(path))
    assert integrity_line(_integrity(path)).startswith("Integrity: FAILED")
    assert any("changed after signing" in line for line in coverage_lines(verdict))


def test_rewritten_metadata_passes_the_digest_and_the_panel_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _signed(tmp_path)
    raw = json.loads(path.read_text())
    raw["metadata"]["tags"] = ["reviewed", "approved"]
    path.write_text(json.dumps(raw))
    # The digest excludes all of metadata, so it still reads "ok" - which is the
    # exact overstatement the coverage lines exist to correct.
    assert integrity_line(_integrity(path)) == "Integrity: ok"
    monkeypatch.setenv(HMAC_KEY_ENV, KEY.decode())
    verdict = _verdict(verifier(load_trust_config())(path))
    assert verdict["state"] == "mismatch"  # tine-sig/2 does cover tags
    assert any("outside the digest" in line for line in coverage_lines(verdict))


def test_an_unsigned_run_is_normal_and_says_what_it_is_worth(tmp_path: Path) -> None:
    path = tmp_path / "plain.tine"
    _run().save(path)
    verdict = _verdict(verifier(load_trust_config())(path))
    assert signature_line(verdict) == "Signature: unsigned"
    assert integrity_line(_integrity(path)) == "Integrity: ok"
    assert any("unkeyed" in line for line in coverage_lines(verdict))


def test_the_verifier_degrades_when_opentine_takes_no_key_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def picky(path, **kwargs):
        if kwargs:
            raise TypeError("verify_signature() got an unexpected keyword argument")
        return SimpleNamespace(state="unsigned", ok=False, reason="", signer=None)

    monkeypatch.setattr(Run, "verify_signature", staticmethod(picky))
    config = TrustConfig(hmac_key=KEY, trust_embedded=True)
    assert verifier(config)(tmp_path / "any.tine").state == "unsigned"


def test_the_console_degrades_when_opentine_exposes_no_key_loaders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The key loaders are re-exports of the private opentine._signing_keys, so
    # they are imported defensively. This is that except branch, run: a build
    # without them must leave the console usable and keyless, not unimportable.
    monkeypatch.setattr(trust, "hmac_key_from_file", None)
    monkeypatch.setattr(trust, "ed25519_public_from_file", None)
    monkeypatch.setattr(trust, "HAS_ED25519", False)

    monkeypatch.setenv(HMAC_KEY_ENV, _keyfile(tmp_path, "key", KEY))
    config = load_trust_config()
    assert config.hmac_key is None
    assert "no key loader" in config.problem
    monkeypatch.delenv(HMAC_KEY_ENV)

    monkeypatch.setenv(PUBLIC_KEY_ENV, _keyfile(tmp_path, "pub", b"ab" * 32))
    assert load_trust_config().public_key is None
    assert "cryptography" in load_trust_config().problem
    monkeypatch.delenv(PUBLIC_KEY_ENV)

    # An inline secret needs no loader, so it still works, and rendering is
    # unaffected: the scheme names are frozen literals either way.
    monkeypatch.setenv(HMAC_KEY_ENV, KEY.decode())
    assert load_trust_config().hmac_key == KEY
    assert signature_scheme(_signed(tmp_path)) == "tine-sig/2"
    assert verifier(load_trust_config())(_signed(tmp_path)).state == "verified"


def test_the_scheme_literals_match_the_installed_opentine() -> None:
    # The fallback spellings are frozen (a scheme name is part of the header old
    # signatures were computed over), but they still have to be the right ones.
    from opentine.signing import SCHEME_V1, SCHEME_V2

    assert (SCHEME_V1, SCHEME_V2) == ("tine-sig/1", "tine-sig/2")
    assert (trust.SCHEME_V1, trust.SCHEME_V2) == (SCHEME_V1, SCHEME_V2)


def _integrity(path: Path) -> dict:
    result = Run.verify_integrity(path)
    return {"ok": result.ok, "reason": result.reason, "draft": result.draft}
