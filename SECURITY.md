# Security Policy

## Reporting a vulnerability

Report privately through
[GitHub Security Advisories](https://github.com/0xcircuitbreaker/opentine-gui/security/advisories/new).
Please do not open a public issue for a security problem.

Include what you have: the affected version, a `.tine` artifact or the steps that
reproduce it, and what you observed versus what you expected. A minimal artifact
that triggers the behaviour is the single most useful thing you can attach.

Expect an acknowledgement within a week. If a fix is warranted it ships in a
patch release, and the advisory credits you unless you ask otherwise.

## What this project's threat model is

`opentine-gui` **renders artifacts it did not create**. A `.tine` file holds
recorded model output, tool arguments, prompts and file paths — none of which the
console controls, and any of which may be attacker-influenced if you open a run
you were sent. The console is a viewer: it never executes recorded content, never
shells out on it, and makes no network requests.

It reads two shapes of store, and writes to only one of them. A directory of
`.tine` files may be written (pause, resume, fork). An opentine v3 repository is
opened **read-only**: writing into one would append an object and move a branch,
so the write actions are disabled there, and the repository is attached in the
way that creates no files — `Repo(<.tine dir>)` rather than `Repo.open(path)`,
which would heal the layout and leave directories behind. A test asserts the
repository tree is byte-identical before and after a scan. An export you ask for
still writes its `.otel.json`, beside the store in the worktree and never inside
`.tine/`.

Two properties matter most, and both have regression tests:

- **Trust verdicts cannot be forged by the artifact describing itself.** The
  inspector shows integrity, signature and fork-provenance verdicts. Artifact text
  is collapsed to a single line before it is interpolated, so a newline in a model
  name or a prompt cannot open a row that impersonates one of those verdicts.
- **Artifact content cannot escape the runs directory.** Run ids are validated
  before they become filenames, for reads, writes, imports and exports alike.
- **A verdict states its own scope.** The integrity digest excludes `metadata` and
  a `tine-sig/1` signature excludes `tags` and `fork_reason`; the console says so
  beside each verdict rather than letting "ok" and "verified" be read as blanket
  claims. Trust-on-first-use renders differently from a real verification.

### What is in scope

- Anything that makes the console display a false trust verdict — an artifact that
  appears verified, signed, or correctly forked when it is not.
- Reading or writing outside the runs directory.
- A crash, hang, or unbounded resource use triggered by a `.tine` file that
  opentine itself accepts.
- Anything that causes recorded content to be executed.
- Key material handled by the trust settings being logged, rendered, written to
  preferences, or recoverable from the configuration fingerprint.
- An imported trace file escaping its temporary workspace, or an import writing
  anywhere other than the destination the console names.

### What is out of scope

- **Bugs in `opentine` itself**, including artifact parsing, integrity digests and
  signature verification. The console delegates all of those. Report them to
  [opentine](https://github.com/0xcircuitbreaker/opentine).
- **`metadata` is not covered by the integrity digest**, by opentine's design. The
  console re-derives what it can (a fork reason is checked against the signed fork
  intent, and a fork id against its recorded basis) and labels the rest as
  unverified rather than presenting it as attested. Metadata that is merely
  *editable* is expected, not a vulnerability.
- **Verification results are cached** per file revision, keyed on path, size,
  inode, mtime and ctime. On POSIX a rewrite always changes ctime, which no writer
  can backdate. On Windows `st_ctime` is creation time, so a same-size rewrite that
  also restores mtime can be served from cache until the file changes again. This
  is documented rather than fixed; a report that improves on it is welcome.
- Denial of service that requires a file larger than `MAX_TINE_BYTES` (10 MiB),
  which is refused before parsing, or an import file larger than the import cap.
- **A save destroys a signature.** `Run.save` recomputes the integrity block from
  scratch, which drops any signature and any draft marker; the console holds no
  signing key and cannot put either back. Pause and Resume state this and ask
  first. Performing it after confirming is the documented behaviour, not a flaw.
- **Post-hoc pricing is a computation, not a claim about the artifact.** A price
  produced with an assumed provider is labelled assumed, and nothing about a
  quote is ever written back to the run.

## Supported versions

The latest release on PyPI receives security fixes. Given the pre-1.0 pace, please
upgrade before reporting.

| Version | Supported |
| --- | --- |
| 0.3.x | Yes |
| < 0.3 | No |

`opentine-gui` 0.3.0 requires `opentine >= 0.7.2`. Below `opentine` 0.7.1 the
library cannot verify a `tine-sig/2` signature (a valid signature is reported as
an error) and cannot read a step's `causal_ids` (saving a run through it erases
them), so older combinations are not supported for reasons that are themselves
security-relevant.
