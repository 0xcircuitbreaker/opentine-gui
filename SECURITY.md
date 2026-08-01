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

Two properties matter most, and both have regression tests:

- **Trust verdicts cannot be forged by the artifact describing itself.** The
  inspector shows integrity, signature and fork-provenance verdicts. Artifact text
  is collapsed to a single line before it is interpolated, so a newline in a model
  name or a prompt cannot open a row that impersonates one of those verdicts.
- **Artifact content cannot escape the runs directory.** Run ids are validated
  before they become filenames, for reads, writes and exports alike.

### What is in scope

- Anything that makes the console display a false trust verdict — an artifact that
  appears verified, signed, or correctly forked when it is not.
- Reading or writing outside the runs directory.
- A crash, hang, or unbounded resource use triggered by a `.tine` file that
  opentine itself accepts.
- Anything that causes recorded content to be executed.

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
  which is refused before parsing.

## Supported versions

The latest release on PyPI receives security fixes. Given the pre-1.0 pace, please
upgrade before reporting.

| Version | Supported |
| --- | --- |
| 0.2.x | Yes |
| < 0.2 | No |
