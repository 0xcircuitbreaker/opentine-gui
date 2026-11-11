"""`Run > Verify offline...`: opentine's own offline verification report.

opentine 0.25 added `tine verify --offline`, which chains every check that
needs no network - digest, signature, upgrade provenance, in-toto attestation,
the action manifest and an action policy, RFC 3161 timestamps - into one
versioned `tine-verify-report/1`. The trust panel only ever showed the first
two. This module runs opentine's report builder rather than re-implementing any
of it, so the console and the CLI cannot disagree about a check.

One substitution: the signature row. The CLI takes keys as flags; the console
already has a configured key (environment or preferences) and its own verdict
for it. Rather than hand a secret to a second loader that might read it
differently - and report a correctly signed file as INVALID - the console's
verdict is mapped into the report's vocabulary and replaces that row, with a
trust basis that says where the key came from.

Everything opentine writes into a check is file-derived and rendered through
`_oneline`, like every other artifact string.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opentine_gui.text import _oneline, _truncate

try:
    from opentine._cli_parser import _build_parser
    from opentine._verify_chain import build_report as _build_report
    from opentine._verify_report import Check as _Check
except Exception:  # pragma: no cover - only reachable against a foreign opentine
    _build_parser = None
    _build_report = None
    _Check = None
try:
    from opentine._assurance import assess as _assess
except Exception:  # pragma: no cover - opentine < 0.39
    _assess = None

#: Outcome words, in the order a reader should worry about them.
_RANK = {"fail": 0, "unavailable-offline": 1, "pass": 2, "not-applicable": 3}


def available() -> bool:
    """Whether the installed opentine carries the offline report builder."""
    return _build_report is not None and _build_parser is not None and _Check is not None


@dataclass(frozen=True)
class VerifyOptions:
    """The optional inputs `tine verify --offline` accepts, as paths."""

    action_policy: str = ""
    tsa_roots: str = ""
    provenance_source: str = ""
    strict: bool = False


@dataclass
class OfflineReport:
    ok: bool
    lines: list[str] = field(default_factory=list)
    document: dict[str, Any] = field(default_factory=dict)
    error: str = ""


def _field(verdict: Any, name: str) -> Any:
    """One verdict field from a SignatureResult or the console's cached dict."""
    try:
        return verdict.get(name) if isinstance(verdict, dict) else getattr(verdict, name, None)
    except Exception:
        return None


def _signature_check(verdict: Any, source: str) -> Any:
    """The console's own keyed verdict, in the report's vocabulary."""
    state = str(_field(verdict, "state") or "error")
    reason = _truncate(_oneline(_field(verdict, "reason") or state), 200)
    algorithm = _oneline(_field(verdict, "algorithm") or "-")
    signer = _oneline(_field(verdict, "signer") or "-")
    basis = f"key configured in the console ({_oneline(source)})"
    if state == "verified":
        return _Check(
            "signature", "pass", "signature.verified",
            f"alg={algorithm} signer={signer}", basis, required=True, authenticates=True,
        )
    if state == "verified-tofu":
        return _Check(
            "signature", "pass", "signature.verified-tofu",
            f"alg={algorithm} signer={signer} (self-asserted key: consistency, not authorship)",
            "trust on first use: the key came from the file", required=True,
        )
    if state in ("unsigned", "no-key"):
        detail = "the run carries no signature" if state == "unsigned" else reason
        return _Check(
            "signature", "unavailable-offline", f"signature.{state}", detail,
            "none: no trusted key supplied" if state == "no-key" else basis, required=True,
        )
    return _Check(
        "signature", "fail", f"signature.{state}", reason, basis, required=True,
    )


def _argv(path: Path, options: VerifyOptions) -> list[str]:
    argv = ["verify", str(path), "--offline"]
    if options.strict:
        argv.append("--strict")
    for flag, value in (
        ("--action-policy", options.action_policy),
        ("--tsa-roots", options.tsa_roots),
        ("--source", options.provenance_source),
    ):
        if value.strip():
            argv.extend([flag, value.strip()])
    return argv


def _report_assurance(report: Any) -> Any:
    try:
        return report.as_json().get("assurance")
    except Exception:
        return None


def assurance_line(assurance: Any) -> str:
    """opentine's assurance tier (0.39), as one flat row, or "".

    T0 imported, T1 hook-captured, T2 witnessed by a proxy key, T3 witnessed
    and timestamped. `verified` is computed from signatures and timestamps at
    check time; `claimed` is what the capture says about itself.
    """
    if not isinstance(assurance, dict):
        return ""
    verified = _oneline(assurance.get("verified") or "?")[:4]
    claimed = _oneline(assurance.get("claimed") or "?")[:4]
    reasons = assurance.get("reasons")
    why = "; ".join(_oneline(r) for r in reasons[:3]) if isinstance(reasons, list) else ""
    text = f"Assurance: {verified} verified (claimed {claimed})"
    return text + (f" - {_truncate(why, 160)}" if why else "")


def assess(path: Path) -> str:
    """The assurance row for one file, without the rest of the report."""
    if _assess is None:
        return ""
    try:
        return assurance_line(_assess(path))
    except Exception:
        return ""


def render(report: Any) -> list[str]:
    """Plain rows for a flat text panel, worst outcome first within the order."""
    lines: list[str] = []
    for check in report.checks:
        need = " (required)" if check.required else ""
        lines.append(
            f"{check.outcome:<19} {_oneline(check.name)}{need}: "
            f"{_truncate(_oneline(check.detail), 160)} [{_oneline(check.reason)}]"
        )
        if check.outcome != "not-applicable":
            lines.append(f"{'':<19}   trust basis: {_truncate(_oneline(check.trust_basis), 120)}")
    for stamp in getattr(report, "timestamps", None) or []:
        if isinstance(stamp, dict):
            shown = ", ".join(
                f"{key}={_truncate(_oneline(stamp[key]), 60)}"
                for key in ("gen_time", "tsa", "policy", "hash_algorithm")
                if stamp.get(key)
            )
            lines.append(f"timestamp token: {shown or '(no details)'}")
    assurance = assurance_line(_report_assurance(report))
    basis = report.authorship if report.ok else None
    lines.append("")
    if assurance:
        lines.append(assurance)
    lines.append(
        f"Authorship: {_oneline(basis)}" if basis else
        "Authorship: NOT established (internal consistency only)"
    )
    if report.strict and report.unavailable_required:
        missing = ", ".join(_oneline(name) for name in report.unavailable_required)
        lines.append(f"Strict: required check(s) unavailable offline: {missing}")
    lines.append("VERIFIED (offline)" if report.ok else "FAILED (offline)")
    return lines


def verify_offline(
    path: Path,
    *,
    signature: Any = None,
    key_source: str = "",
    options: VerifyOptions | None = None,
) -> OfflineReport:
    """Run opentine's offline report over one artifact.

    `signature` is the console's own verdict for this file under its configured
    key, or None to keep opentine's keyless row. Never raises: a report that
    could not be built is an `OfflineReport` with `error` set, which the panel
    shows instead of a verdict.
    """
    if not available():
        return OfflineReport(False, error="this opentine has no offline verifier")
    options = options or VerifyOptions()
    try:
        args = _build_parser().parse_args(_argv(path, options))
        report = _build_report(args, path, str(path))
    except SystemExit:
        return OfflineReport(False, error="opentine refused these verify options")
    except Exception as exc:
        return OfflineReport(False, error=_truncate(_oneline(f"{type(exc).__name__}: {exc}"), 300))
    if signature is not None:
        for index, check in enumerate(report.checks):
            # A digest failure already made opentine skip the signature; keep
            # its "not checked" row rather than verifying a broken file.
            if check.name == "signature" and check.outcome != "not-applicable":
                report.checks[index] = _signature_check(signature, key_source)
    try:
        document = report.as_json()
    except Exception:
        document = {}
    return OfflineReport(bool(report.ok), lines=render(report), document=document)


__all__ = ["OfflineReport", "VerifyOptions", "available", "render", "verify_offline"]
