"""Whether this console can vouch for an artifact, and how far that actually goes.

The trust panel used to answer "no" by construction: it checked signatures with
no key material at all, so even a correctly signed artifact could be reported
only as "present, not verified here (no key)". opentine ships key loaders for
exactly this, and this module is where the console picks a key up, from the
environment or from saved preferences, and binds it into a per-file verifier the
panels can call.

The second job is scope. Both verdicts the panel prints cover less than their
names suggest, and a reader who takes "Integrity: ok / Signature: verified" at
face value over an artifact whose tags were rewritten after signing has been told
something untrue:

* the integrity digest is a SHA-256 over the artifact body with the whole of
  `metadata` excluded, so tags, fork reason, replay and budget state can all be
  rewritten and still match;
* a `tine-sig/1` signature, which is everything opentine 0.3.0 through 0.7.0
  wrote, covers an eleven-key metadata allowlist that leaves out tags and fork
  reason, while `tine-sig/2` (0.7.1+) covers every metadata key but `integrity`.

`SignatureResult` carries no scheme field, so which of the two is on a file has
to be read back out of the artifact's own JSON. That makes the scheme itself
untrusted input, and only the two literal scheme strings this build knows are
ever returned from here or rendered.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opentine.core import Run

from opentine_gui.sources import MAX_TINE_BYTES
from opentine_gui.text import _oneline, _truncate

# opentine's key loaders live in the private `opentine._signing_keys` and reach
# us only through `opentine.signing`'s re-export, so they are imported
# defensively: a build that moved or dropped them must leave the console with
# "no key configured" rather than failing to import at all.
try:
    from opentine.signing import (
        HAS_ED25519,
        MIN_HMAC_KEY_BYTES,
        ed25519_public_from_file,
        hmac_key_from_file,
    )
except Exception:  # pragma: no cover - only reachable against a foreign opentine
    HAS_ED25519 = False
    MIN_HMAC_KEY_BYTES = 16
    ed25519_public_from_file = None
    hmac_key_from_file = None

# Scheme names are compared against, never taken from, an artifact. The fallback
# literals cannot drift: a scheme's name is part of the header older signatures
# were computed over, so it is frozen for as long as those files exist.
try:
    from opentine.signing import SCHEME_V1, SCHEME_V2
except Exception:  # pragma: no cover - only reachable against opentine < 0.7.1
    SCHEME_V1, SCHEME_V2 = "tine-sig/1", "tine-sig/2"

_SCHEMES = (SCHEME_V1, SCHEME_V2)

#: Names a file holding the HMAC secret, or holds the secret itself.
HMAC_KEY_ENV = "OPENTINE_GUI_HMAC_KEY"

#: Path to an Ed25519 public key, raw or hex, as `tine keygen` writes it.
PUBLIC_KEY_ENV = "OPENTINE_GUI_PUBLIC_KEY"

#: Preference keys the settings dialog writes. Paths only: a preferences file is
#: world-readable JSON in the user's config directory and is no place for a
#: shared secret.
HMAC_KEY_PREF = "trust_hmac_key_path"
PUBLIC_KEY_PREF = "trust_public_key_path"
TRUST_EMBEDDED_PREF = "trust_embedded"

#: Largest key file this console will read. An HMAC secret is tens of bytes and
#: an Ed25519 public key is 32 or 64; opentine's own loader caps at 1 MiB, which
#: is generous enough that a mistyped path (a log, a core file) gets read in full
#: before anything rejects it. Past this the file is refused on its stat, unread.
MAX_KEY_FILE_BYTES = 4096

#: Preference values that mean "on". Anything else, including an empty string, is
#: off: trust-on-first-use has to be asked for, never inferred.
_TRUTHY = frozenset({"1", "true", "yes", "on"})

_SHORT_KEY = f"HMAC key is shorter than the {MIN_HMAC_KEY_BYTES} bytes opentine requires"


class _KeyProblem(Exception):
    """A key could not be loaded, carrying a message that is safe to render.

    Nothing raised through here may quote key bytes. A `UnicodeDecodeError` from
    a binary key file names the byte it choked on, and `bytes.fromhex` names an
    offset into the secret, so loader exceptions are translated to fixed text
    rather than passed through as `str(exc)`.
    """


def _shown(value: object) -> str:
    """A path or origin, bounded and flattened for a one-line trust row."""
    return _truncate(_oneline(value), 120)


def _os_reason(exc: Exception) -> str:
    """Why a key file could not be opened, named by failure and not by message."""
    if isinstance(exc, ValueError):  # embedded NUL, from stat rather than the OS
        return "not a usable file path"
    if isinstance(exc, FileNotFoundError):
        return "no such file"
    if isinstance(exc, IsADirectoryError):
        return "is a directory"
    if isinstance(exc, PermissionError):
        return "permission denied"
    return f"unreadable ({type(exc).__name__})"


def _key_path(value: str, shown: str) -> Path:
    """A configured value as a path, or a problem if it cannot be one.

    `Path.expanduser` raises RuntimeError for `~someone` with no home directory
    (and on any machine that has none at all), which would otherwise escape
    `load_trust_config` and stop the console opening over a stale preference.
    """
    try:
        return Path(value).expanduser()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _KeyProblem(f"{shown}: not a usable file path") from exc


def _read_key_file(path: Path, shown: str) -> bytes:
    """Read a key file, or say why not, bounding size before reading anything.

    A configured path can be missing, a directory, a FIFO, or a gigabyte of
    something else entirely, so the size and file type are settled on the stat
    and the read is capped again in case the file grew in between.

    `shown` names the file in any message rather than being derived from `path`:
    see `_load_hmac` for why the caller sometimes has to withhold the value.
    ValueError joins OSError because a path carrying a NUL byte - which a
    hand-edited preferences file can hold as a JSON "\\u0000" escape - fails in
    `stat` itself rather than in the OS.
    """
    try:
        info = path.stat()
    except (OSError, ValueError) as exc:
        raise _KeyProblem(f"{shown}: {_os_reason(exc)}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise _KeyProblem(f"{shown}: not a regular file")
    if info.st_size > MAX_KEY_FILE_BYTES:
        raise _KeyProblem(f"{shown}: larger than the {MAX_KEY_FILE_BYTES}-byte key limit")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_KEY_FILE_BYTES + 1)
    except (OSError, ValueError) as exc:
        raise _KeyProblem(f"{shown}: {_os_reason(exc)}") from exc
    if len(raw) > MAX_KEY_FILE_BYTES:
        raise _KeyProblem(f"{shown}: larger than the {MAX_KEY_FILE_BYTES}-byte key limit")
    if not raw.strip():
        raise _KeyProblem(f"{shown}: key file is empty")
    return raw


def _checked_hmac(key: bytes, where: str = "") -> bytes:
    """Reject a secret opentine would refuse, at load time rather than per file.

    `verify_artifact` turns a short key into a per-artifact `error` verdict,
    which reads as a fault in the file being inspected. The fault is in the
    configuration, so it is caught here and reported as one.
    """
    if len(key) < MIN_HMAC_KEY_BYTES:
        raise _KeyProblem(f"{where}: {_SHORT_KEY}" if where else _SHORT_KEY)
    return key


def _secret_bytes(value: str) -> bytes:
    """The exact bytes the environment holds, for a secret given inline.

    `os.environ` decodes with surrogateescape on POSIX, so a key generated the
    obvious way (`export ...=$(head -c 32 /dev/urandom)`) arrives as text that
    plain `str.encode` refuses outright. Encoding back through surrogateescape
    round-trips those bytes, so the key that signed the artifact is the key used
    to check it; anything that still will not encode is a configuration problem
    rather than a traceback out of `load_trust_config`.
    """
    try:
        return value.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError as exc:
        raise _KeyProblem("value cannot be read as key bytes") from exc


def _looks_like_path(value: str) -> bool:
    """Whether a configured value means a file name rather than the key itself.

    Decided by the shape of the value, not by whether the file happens to exist:
    guessing from existence turns a typo in a path into key *material*, and every
    correctly signed artifact would then be reported as INVALID (an alarm) rather
    than as unverifiable here.
    """
    return value.startswith("~") or "/" in value or os.sep in value


def _hmac_from_path(path: Path, shown: str) -> bytes:
    """Load an HMAC secret from a file, this module's bounds first."""
    _read_key_file(path, shown)  # size and type, before opentine's 1 MiB reader sees it
    if hmac_key_from_file is None:  # pragma: no cover - foreign opentine
        raise _KeyProblem(f"{shown}: this opentine build exposes no key loader")
    try:
        # opentine's own loader owns the byte semantics (it drops one trailing
        # newline), so a key file that signed through the CLI verifies here.
        key = bytes(hmac_key_from_file(path))
    except Exception as exc:
        raise _KeyProblem(f"{shown}: key file could not be read") from exc
    return _checked_hmac(key, shown)


#: How a failing `$OPENTINE_GUI_HMAC_KEY` is named once it has been read as a
#: path. The variable is documented as holding *either* a file name or the secret
#: itself, and the two are told apart by shape, so a secret can land on this
#: branch: `openssl rand -base64 32` produces a "/" about half the time. The
#: value therefore never reaches a message, which is rendered in the trust panel
#: and pasted into bug reports. Naming the variable and what was done with it is
#: the more useful half anyway - the user can read their own environment.
_ENV_AS_PATH = "value read as a file path"


def _load_hmac(value: str, origin: str) -> tuple[bytes, str]:
    """(secret, where it came from) for one configured HMAC key.

    `origin` is a description, not a value: only a path that successfully loaded
    a key is echoed back, because by then it is known to be a file and not a
    secret that merely looked like one.
    """
    candidate = value.strip()
    if _looks_like_path(candidate):
        path = _key_path(candidate, _ENV_AS_PATH)
        return _hmac_from_path(path, _ENV_AS_PATH), f"HMAC key from {_shown(path)} ({origin})"
    # Unstripped: opentine's environment loader encodes the value verbatim, and a
    # secret whose surrounding bytes we trimmed would agree with nothing.
    return _checked_hmac(_secret_bytes(value)), f"HMAC key from {origin}"


def _load_public(value: str, origin: str) -> tuple[Any, bytes, str]:
    """(key object, file bytes, where it came from) for one Ed25519 public key.

    The key is coerced now rather than at verify time so a malformed file is one
    configuration problem instead of an `error` verdict on every artifact.
    """
    # A public key is not a secret, so the configured value is safe to echo.
    path = _key_path(value, _shown(value))
    raw = _read_key_file(path, _shown(path))
    if not HAS_ED25519 or ed25519_public_from_file is None:
        raise _KeyProblem(f"{_shown(path)}: ed25519 needs the cryptography package")
    try:
        key = ed25519_public_from_file(path)
    except Exception as exc:
        raise _KeyProblem(f"{_shown(path)}: not an ed25519 public key") from exc
    return key, raw, f"Ed25519 public key from {_shown(path)} ({origin})"


def _fingerprint(material: list[bytes], trust_embedded: bool) -> str:
    """A stable id for one trust configuration that cannot be run back to the key.

    The verdict cache is keyed by this, so it must change whenever the key does,
    and it is rendered nowhere but must survive being seen. Hence a purpose-salted
    digest truncated to 64 bits: never the bare digest of the secret, which would
    confirm a guessed key offline. Each part is length-prefixed so two different
    configurations cannot concatenate to the same bytes.
    """
    if not material and not trust_embedded:
        return ""
    digest = hashlib.sha256(b"opentine-gui-trust-fp\0")
    for item in material:
        digest.update(len(item).to_bytes(8, "big"))
        digest.update(item)
    digest.update(b"tofu" if trust_embedded else b"-")
    return digest.hexdigest()[:16]


def _pref(preferences: Any, name: str) -> str:
    """One preference as trimmed text, whatever the preferences file held.

    Preferences are JSON a user can hand-edit, and `trust_embedded: true` is the
    obvious way to write a boolean: that arrives as a `bool`, and `.strip()` on
    it took the console down before it drew a window. A non-string is treated as
    absent, which for the trust-on-first-use switch is also the safe reading -
    it has to be asked for, never inferred.
    """
    try:
        value = preferences.get(name)
    except AttributeError:  # not a mapping at all
        return ""
    return value.strip() if isinstance(value, str) else ""


@dataclass(frozen=True)
class TrustConfig:
    """The key material the trust panel verifies with, and where it came from."""

    # repr=False: a TrustConfig lands in tracebacks, logs and debugger frames,
    # and the default dataclass repr would print the shared secret in all three.
    hmac_key: bytes | None = field(default=None, repr=False)
    public_key: Any | None = None
    trust_embedded: bool = False
    source: str = "no key configured"
    fingerprint: str = ""
    problem: str = ""

    @property
    def configured(self) -> bool:
        """Whether any key material was successfully loaded."""
        return bool(self.hmac_key) or self.public_key is not None or self.trust_embedded

    @property
    def verifier(self) -> Callable[[Any], Any]:
        """This configuration's bound check, for callers holding only the config."""
        return verifier(self)


def load_trust_config(preferences: dict[str, str] | None = None) -> TrustConfig:
    """Assemble the trust configuration from the environment and preferences.

    The environment is read first and wins: a machine that exports a key means it
    now, and a preference saved on another day must not quietly outrank it.

    No failure raises. A bad key path must not stop the console from opening, and
    it must never be mistaken for a verdict about an artifact, so every failure
    lands in `problem` and leaves the corresponding key unset. Unset means the
    panel says "no key", which is the honest answer and the safe one.
    """
    prefs = preferences if preferences is not None else {}
    problems: list[str] = []
    sources: list[str] = []
    material: list[bytes] = []

    hmac_key: bytes | None = None
    env_hmac = os.environ.get(HMAC_KEY_ENV) or ""
    pref_hmac = _pref(prefs, HMAC_KEY_PREF)
    if env_hmac.strip():
        try:
            hmac_key, described = _load_hmac(env_hmac, f"${HMAC_KEY_ENV}")
            sources.append(described)
        except _KeyProblem as exc:
            problems.append(f"{HMAC_KEY_ENV}: {exc}")
    elif pref_hmac:
        # A preference is declared to be a path, never the secret, so unlike the
        # environment value it is safe to name in a problem message.
        try:
            shown = _shown(pref_hmac)
            hmac_key = _hmac_from_path(_key_path(pref_hmac, shown), shown)
            sources.append(f"HMAC key from {shown} (preferences)")
        except _KeyProblem as exc:
            problems.append(f"{HMAC_KEY_PREF}: {exc}")
    if hmac_key is not None:
        material.append(b"hmac\0" + hmac_key)

    public_key: Any | None = None
    env_public = (os.environ.get(PUBLIC_KEY_ENV) or "").strip()
    pref_public = _pref(prefs, PUBLIC_KEY_PREF)
    chosen, origin, label = (
        (env_public, f"${PUBLIC_KEY_ENV}", PUBLIC_KEY_ENV)
        if env_public
        else (pref_public, "preferences", PUBLIC_KEY_PREF)
    )
    if chosen:
        try:
            public_key, raw, described = _load_public(chosen, origin)
            sources.append(described)
            material.append(b"ed25519\0" + raw)
        except _KeyProblem as exc:
            problems.append(f"{label}: {exc}")

    trust_embedded = _pref(prefs, TRUST_EMBEDDED_PREF).lower() in _TRUTHY
    if trust_embedded:
        sources.append("embedded keys trusted on first use")

    return TrustConfig(
        hmac_key=hmac_key,
        public_key=public_key,
        trust_embedded=trust_embedded,
        source="; ".join(sources) if sources else "no key configured",
        fingerprint=_fingerprint(material, trust_embedded),
        problem="; ".join(problems),
    )


def verifier(config: TrustConfig) -> Callable[[Any], Any]:
    """A path -> SignatureResult check with this configuration's keys bound.

    Returned even when nothing is configured, so a caller has one code path; the
    keyless closure then answers exactly what `Run.verify_signature` alone does.
    """
    hmac_key = config.hmac_key
    public_key = config.public_key
    trust_embedded = config.trust_embedded

    def check(path: Any) -> Any:
        try:
            return Run.verify_signature(
                path,
                hmac_key=hmac_key,
                public_key=public_key,
                trust_embedded=trust_embedded,
            )
        except TypeError:
            # An opentine that no longer accepts key material. The honest answer
            # is then the keyless one, not an error per artifact about our call.
            return Run.verify_signature(path)

    return check


def _read_artifact(path_or_data: Any) -> Any:
    """Bounded JSON read of one artifact, or None if it is not readable as one."""
    if isinstance(path_or_data, dict):
        return path_or_data
    try:
        source = Path(path_or_data)
        info = source.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TINE_BYTES:
            return None
        return json.loads(source.read_bytes())
    except Exception:
        # An artifact is untrusted input of any shape and size: a missing file, a
        # directory, a truncated read, deep nesting (RecursionError) and invalid
        # UTF-8 all mean the same thing here, which is "no scheme is readable".
        return None


def signature_scheme(path_or_data: Any) -> str:
    """The scheme an artifact's own signature block names: v1, v2, or "".

    `SignatureResult` does not carry the scheme, and the two schemes sign
    different parts of the file, so the panel has to read it back off the
    artifact. Only the two known scheme strings are ever returned: this value is
    attacker-controlled, and echoing it verbatim would let a file write its own
    coverage claim into a trust row.
    """
    data = _read_artifact(path_or_data)
    if not isinstance(data, dict):
        return ""
    metadata = data.get("metadata")
    integrity = metadata.get("integrity") if isinstance(metadata, dict) else None
    block = integrity.get("signature") if isinstance(integrity, dict) else None
    scheme = block.get("scheme") if isinstance(block, dict) else None
    return scheme if scheme in _SCHEMES else ""


def _text(verdict: Any, name: str) -> str:
    """One verdict field as flat, bounded text, whatever it turns out to be.

    These three renderers are the console's last word on whether a file can be
    trusted, so nothing they are handed may make them the thing that raises: a
    verdict that is not a mapping, and a value whose `str()` fails (a JSON
    integer past CPython's 4300-digit conversion limit does), both read as
    absent rather than propagating out of a public function.
    """
    try:
        value = verdict.get(name)
        return _oneline(value) if value else ""
    except Exception:
        return ""


def _flag(verdict: Any, name: str) -> bool:
    """One verdict field as a plain bool, on the same terms as `_text`."""
    try:
        return bool(verdict.get(name))
    except Exception:
        return False


def _parens(*parts: str) -> str:
    shown = [part for part in parts if part]
    return f" ({', '.join(shown)})" if shown else ""


def signature_line(verdict: Any, *, scheme: str = "") -> str:
    """Render a SignatureResult by its state, with the scheme it was written at.

    ok=False is the normal case for both an unsigned run and a validly signed one
    this console holds no key for, so only a real mismatch or a malformed block
    may read as an alarm.

    `verified-tofu` deliberately never uses the word "verified": the key came out
    of the file being checked, which proves the file is internally consistent and
    nothing whatever about who wrote it. Every field below is written by whoever
    wrote the artifact, so all of them are flattened and bounded first.
    """
    state = _text(verdict, "state")
    reason = _truncate(_text(verdict, "reason"), 200)
    signer = _truncate(_text(verdict, "signer"), 120)
    algorithm = _truncate(_text(verdict, "algorithm"), 40)
    scheme = scheme if scheme in _SCHEMES else ""
    who = f" by {signer}" if signer else ""
    if state == "verified":
        return f"Signature: verified{who}{_parens(algorithm, scheme)}"
    if state == "verified-tofu":
        return (
            f"Signature: self-attested{who}{_parens(algorithm, scheme)}"
            " - trust on first use, not a key you configured"
        )
    if state == "unsigned":
        return "Signature: unsigned"
    if state == "no-key":
        return f"Signature: present{who}{_parens(scheme)}, not verified here (no key)"
    if state == "mismatch":
        return f"Signature: INVALID{who}{_parens(scheme)} - {reason}"
    return f"Signature: {reason or state or 'unknown'}"


_INTEGRITY_SCOPE = (
    "Integrity covers the run body only: tags, fork reason, replay and budget"
    " are outside the digest."
)
#: Said instead when a signature this build actually verified covers what the
#: digest leaves out. Without this the two lines read as contradicting each
#: other: one says the metadata is signed, the next that it is unprotected.
_INTEGRITY_SCOPE_SIGNED = (
    "Integrity covers the run body only; the metadata it leaves out"
    " (tags, fork reason, replay, budget) is covered by the signature above."
)
_V1_SCOPE = "tine-sig/1 signs a frozen metadata allowlist: tags and fork reason are NOT signed."
_V2_SCOPE = (
    "tine-sig/2 signs every metadata key but integrity, so tags and fork reason are covered."
)
_UNKNOWN_SCOPE = (
    "This signature names no scheme this build knows; assume the narrower tine-sig/1 coverage."
)
_UNSIGNED = "Unsigned: the digest is unkeyed, so anyone who edits the file can recompute it."
_NO_KEY = "No key is configured here, so nothing in this file has been checked against one."
_TOFU = "Trust on first use: the key came from the file, which shows consistency, not authorship."
_MISMATCH = (
    "The signature does not match: the file changed after signing, or another key signed it."
)
_ERROR = "The signature block is malformed, so it attests to nothing."
_DRAFT = "Draft checkpoint: a partial autosave, which opentine refuses to sign."


def coverage_lines(verdict: Any, *, scheme: str = "", draft: bool = False) -> list[str]:
    """What the two rendered verdicts actually cover, in at most three lines.

    `verdict` is the *signature* verdict. `draft` is passed separately because it
    is the integrity check that reports it.

    Three lines is roughly what a trust panel can carry before it stops being
    read, so the caveats are ranked and clipped rather than listed in full. The
    last line is always the integrity scope: it is true of every artifact,
    whatever the signature says, and it is the one most often assumed away.
    """
    state = _text(verdict, "state")
    scheme = scheme if scheme in _SCHEMES else ""
    detail: list[str] = []
    if state == "mismatch":
        detail.append(_MISMATCH)
    elif state == "error":
        detail.append(_ERROR)
    elif state == "unsigned":
        detail.append(_UNSIGNED)
    elif state == "no-key":
        detail.append(_NO_KEY)
    if state == "verified-tofu":
        detail.append(_TOFU)
    if draft:
        detail.append(_DRAFT)
    if state in ("verified", "verified-tofu", "no-key"):
        if scheme == SCHEME_V2:
            detail.append(_V2_SCOPE)
        elif scheme == SCHEME_V1:
            detail.append(_V1_SCOPE)
        else:
            detail.append(_UNKNOWN_SCOPE)
    covered = state == "verified" and scheme == SCHEME_V2
    return [*detail[:2], _INTEGRITY_SCOPE_SIGNED if covered else _INTEGRITY_SCOPE]


def integrity_line(verdict: Any) -> str:
    """Render an IntegrityResult, stating what was checked and not what it means.

    "ok" here is "the body digest matched", not "this file is genuine": the digest
    is unkeyed, so anyone who edits the artifact can recompute it. That caveat is
    `coverage_lines`' job; this line only reports the check.
    """
    reason = _truncate(_text(verdict, "reason"), 200)
    if _flag(verdict, "ok"):
        return "Integrity: ok" + (" (draft)" if _flag(verdict, "draft") else "")
    if reason.startswith("check failed"):
        return f"Integrity: {reason}"
    return f"Integrity: FAILED - {reason or 'unknown'}"
