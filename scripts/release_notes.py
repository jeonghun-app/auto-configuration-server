#!/usr/bin/env python3
"""Check a release tag against the repository, and extract its release notes.

The release workflow and a maintainer preparing a release by hand run the same
code, so "the tag matches the version" and "the notes are the CHANGELOG section"
mean the same thing in both places.

    python scripts/release_notes.py verify v1.3.0
    python scripts/release_notes.py notes 1.3.0 [--image ghcr.io/owner/repo]
        [--digest sha256:...] [--repo-url https://github.com/owner/repo]
    git tag -l | python scripts/release_notes.py channels v1.3.0
    python scripts/release_notes.py image-state --exit-code N [--first-publish true] < out

``verify`` fails unless the tag is ``vMAJOR.MINOR.PATCH``, equals the version in
``pyproject.toml`` and ``src/acs/__init__.py``, and ``CHANGELOG.md`` has a non-empty
section for it. ``notes`` prints that section. ``channels`` reads every existing
tag on stdin and prints, in ``$GITHUB_OUTPUT`` form, whether the tag is the newest
release overall (``latest``) and the newest patch of its minor line (``minor``).
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Pre-releases are refused rather than half-supported: a pre-release image must
# not move the X.Y or latest tags, and nothing else in the pipeline expects one.
TAG_RE = re.compile(r"^v(?P<version>(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*))$")
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

# "## [1.3.0] — 2026-08-30". The date separator is an em dash in this repository;
# an ASCII hyphen is accepted so a hand-edited heading does not silently vanish.
# As in CommonMark, an ATX heading may be indented by up to three spaces and its
# marker may be followed by a tab.
_H2_RE = re.compile(r"^ {0,3}##(?:[ \t].*)?$")
_HEADING_RE = re.compile(r"^ {0,3}##[ \t]+\[(?P<name>[^\]]+)\](?:[ \t]+[—–-][ \t]+\S.*)?[ \t]*$")
# A link reference definition. Keep a Changelog ends the file with a block of
# them; inside a section they are part of the section.
_LINK_DEF_RE = re.compile(r"^ {0,3}\[(?P<label>[^\]]+)\]:[ \t]+\S+")
_INIT_VERSION_RE = re.compile(r'^__version__\s*=\s*"(?P<version>[^"]+)"', re.MULTILINE)
# A fenced code block opens with three or more backticks or tildes, indented by at
# most three spaces, and closes with at least as many of the same character. A
# backtick opener's info string cannot contain a backtick (CommonMark), so
# "```old_option``` is deprecated" is inline code, not a fence.
_FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
# A relative Markdown link: ](path) where path has no scheme and is not an anchor.
_RELATIVE_LINK_RE = re.compile(r"\]\((?![a-zA-Z][a-zA-Z0-9+.-]*:|#|/)(?P<path>[^)\s]+)\)")


class ReleaseError(Exception):
    """The repository is not in a releasable state for the requested version."""


def version_from_tag(tag: str) -> str:
    match = TAG_RE.match(tag)
    if match is None:
        raise ReleaseError(f"tag {tag!r} is not of the form vMAJOR.MINOR.PATCH")
    return match.group("version")


def project_version(pyproject_text: str) -> str:
    data = tomllib.loads(pyproject_text)
    try:
        version = data["project"]["version"]
    except KeyError as exc:
        raise ReleaseError("pyproject.toml has no [project] version") from exc
    if not isinstance(version, str):
        raise ReleaseError("pyproject.toml [project] version is not a string")
    return version


def package_version(init_text: str) -> str:
    match = _INIT_VERSION_RE.search(init_text)
    if match is None:
        raise ReleaseError("src/acs/__init__.py has no __version__")
    return match.group("version")


def _outside_fences(lines: list[str]) -> list[bool]:
    """For each line, whether it is Markdown structure rather than code.

    A ``## `` or ``[x]: url`` line inside a code block is example text; treating it
    as a heading would cut a section short and publish truncated release notes.
    """
    result: list[bool] = []
    open_fence: str | None = None
    opened_at = 0
    for number, line in enumerate(lines, start=1):
        match = _FENCE_RE.match(line)
        if open_fence is None:
            if match is not None and not (
                match.group("fence")[0] == "`" and "`" in match.group("info")
            ):
                open_fence = match.group("fence")
                opened_at = number
                result.append(False)
            else:
                result.append(True)
            continue
        result.append(False)
        if (
            match is not None
            and match.group("fence")[0] == open_fence[0]
            and len(match.group("fence")) >= len(open_fence)
            and not line.strip().lstrip(open_fence[0])
        ):
            open_fence = None
    if open_fence is not None:
        # Markdown would run the block to the end of the file, swallowing every
        # later section; refuse rather than publish that.
        raise ReleaseError(f"CHANGELOG.md has an unclosed code fence opened at line {opened_at}")
    return result


def _without_footer(section: list[str], structural: list[bool]) -> list[str]:
    """Drop the file's closing block of link definitions from the last section.

    That block holds every version's compare link, not this section's content.
    A definition in it that this section actually uses is kept, so the
    reference still resolves in the release notes.
    """
    cut = len(section)
    while (
        cut > 0
        and structural[cut - 1]
        and (not section[cut - 1].strip() or _LINK_DEF_RE.match(section[cut - 1]))
    ):
        cut -= 1
    footer = [line for line in section[cut:] if line.strip()]
    if not footer:
        return section
    body = "\n".join(section[:cut]).lower()
    used = [
        line
        for line in footer
        if (m := _LINK_DEF_RE.match(line)) is not None and f"[{m.group('label').lower()}]" in body
    ]
    return section[:cut] + ([""] + used if used else [])


def extract_section(changelog_text: str, version: str) -> str:
    """Return the body of the ``## [version]`` section, without its heading."""
    lines = changelog_text.splitlines()
    structural = _outside_fences(lines)
    start: int | None = None
    for index, line in enumerate(lines):
        if not structural[index]:
            continue
        heading = _HEADING_RE.match(line)
        if heading is not None and heading.group("name") == version:
            if start is not None:
                raise ReleaseError(f"CHANGELOG.md has more than one section for {version}")
            start = index + 1
    if start is None:
        raise ReleaseError(f"CHANGELOG.md has no '## [{version}]' section")

    # Only the next level-two heading ends a section. A link definition does not:
    # one in the middle of a section is followed by more of that section.
    end = len(lines)
    for index in range(start, len(lines)):
        if structural[index] and _H2_RE.match(lines[index]):
            end = index
            break

    section = lines[start:end]
    if end == len(lines):
        section = _without_footer(section, structural[start:end])
    body = "\n".join(section).strip("\n")
    if not body.strip():
        raise ReleaseError(f"CHANGELOG.md section for {version} is empty")
    return body + "\n"


def absolutise_links(markdown: str, repo_url: str, ref: str) -> str:
    """Point relative links at the tagged tree.

    A release body is rendered under /releases/tag/, where a link written relative
    to the repository root would resolve to a page that does not exist.
    """
    base = f"{repo_url.rstrip('/')}/blob/{ref}/"
    return _RELATIVE_LINK_RE.sub(lambda m: f"]({base}{m.group('path')})", markdown)


def image_block(image: str, version: str, digest: str | None = None) -> str:
    pull = f"docker pull {image}:{version}\n"
    if digest:
        # The digest is what a deployment should pin; a tag is only a pointer.
        pull += f"docker pull {image}@{digest}\n"
    return (
        "\n## Container image\n\n"
        f"```bash\n{pull}```\n\n"
        # No X.Y or latest here: they move on to later releases, the notes do not.
        "Built for `linux/amd64` and `linux/arm64`, with SBOM and provenance "
        "attestations attached.\n"
    )


def _key(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in version.split("."))
    return major, minor, patch


def channels(tag: str, existing: list[str]) -> tuple[bool, bool]:
    """Return (newest overall, newest patch in its minor line) for ``tag``.

    Decided from the tags that exist when the image is about to be published, not
    when the tag was verified: a release delayed behind a newer one, or re-run
    after it, must not move ``latest`` or ``X.Y`` backwards. Anything that is not a
    plain ``vX.Y.Z`` tag is ignored.
    """
    current = _key(version_from_tag(tag))
    released = [current]
    for name in existing:
        match = TAG_RE.match(name.strip().removeprefix("refs/tags/"))
        if match is not None:
            released.append(_key(match.group("version")))
    newest = max(released)
    newest_in_line = max(v for v in released if v[:2] == current[:2])
    return current == newest, current == newest_in_line


_ABSENT_RE = re.compile(r"not found|manifest unknown|name unknown", re.IGNORECASE)
# Checked before _ABSENT_RE: "denied: token not found for scope" is a denial.
_AUTH_FAILURE_RE = re.compile(r"denied|unauthori[sz]ed|forbidden|\b40[13]\b", re.IGNORECASE)
# The denial GHCR gives for a package that has never been published.
_DENIED_RE = re.compile(r"denied|403 forbidden", re.IGNORECASE)
_UNAUTHENTICATED_RE = re.compile(r"unauthori[sz]ed|\b401\b", re.IGNORECASE)
# The shell's "cannot execute" and "command not found": the lookup never ran.
_NOT_RUN = frozenset({126, 127})


def image_state(
    succeeded: bool, output: str, first_publish: bool, exit_code: int | None = None
) -> str:
    """Classify an authenticated ``imagetools inspect`` of ``X.Y.Z``.

    Returns ``exists``, ``absent`` or ``absent-first-publish``; anything else is an
    error. Only an explicit not-found counts as absent. A denial does not, because
    a lookup that is refused while the later push is allowed would overwrite an
    existing image. The one exception is the very first publish, when GHCR answers
    an authenticated lookup of a package that does not exist yet like a denial;
    a maintainer opts into that with GHCR_FIRST_PUBLISH for that one run.
    """
    if exit_code in _NOT_RUN:
        raise ReleaseError(f"the image lookup did not run (exit {exit_code}): {output.strip()}")
    if succeeded:
        return "exists"
    if _AUTH_FAILURE_RE.search(output):
        if first_publish and _DENIED_RE.search(output) and not _UNAUTHENTICATED_RE.search(output):
            return "absent-first-publish"
        raise ReleaseError(
            f"could not tell whether the image exists, the lookup was refused: {output.strip()}"
        )
    if _ABSENT_RE.search(output):
        return "absent"
    raise ReleaseError(f"could not tell whether the image exists: {output.strip()}")


def verify(tag: str, root: pathlib.Path) -> str:
    version = version_from_tag(tag)
    declared = project_version((root / "pyproject.toml").read_text(encoding="utf-8"))
    if declared != version:
        raise ReleaseError(f"tag {tag} does not match pyproject.toml version {declared}")
    in_package = package_version((root / "src" / "acs" / "__init__.py").read_text(encoding="utf-8"))
    if in_package != version:
        raise ReleaseError(f"tag {tag} does not match src/acs/__init__.py version {in_package}")
    extract_section((root / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    return version


def notes(
    version: str,
    root: pathlib.Path,
    image: str | None = None,
    repo_url: str | None = None,
    digest: str | None = None,
) -> str:
    if VERSION_RE.match(version) is None:
        raise ReleaseError(f"version {version!r} is not of the form MAJOR.MINOR.PATCH")
    body = extract_section((root / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    if repo_url:
        body = absolutise_links(body, repo_url, f"v{version}")
    if image:
        body += image_block(image, version, digest)
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--root", type=pathlib.Path, default=ROOT, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    p_verify = sub.add_parser("verify", help="check a tag against the repository")
    p_verify.add_argument("tag", help="for example v1.3.0")

    p_notes = sub.add_parser("notes", help="print the CHANGELOG section for a version")
    p_notes.add_argument("version", help="for example 1.3.0")
    p_notes.add_argument("--image", help="image repository to show a pull command for")
    p_notes.add_argument("--digest", help="image index digest to show a pinned pull for")
    p_notes.add_argument("--repo-url", help="rewrite relative links against this repository")

    p_channels = sub.add_parser(
        "channels", help="decide latest and X.Y from the existing tags on stdin"
    )
    p_channels.add_argument("tag", help="for example v1.3.0")

    p_state = sub.add_parser(
        "image-state", help="classify an imagetools inspect result read from stdin"
    )
    p_state.add_argument("--exit-code", type=int, required=True)
    p_state.add_argument("--first-publish", choices=["true", "false", ""], default="")

    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            version = verify(args.tag, args.root)
            print(
                f"{args.tag}: pyproject.toml, src/acs/__init__.py and CHANGELOG.md agree "
                f"on {version}"
            )
        elif args.command == "image-state":
            state = image_state(
                args.exit_code == 0, sys.stdin.read(), args.first_publish == "true", args.exit_code
            )
            print(f"state={state}")
        elif args.command == "channels":
            latest, minor = channels(args.tag, sys.stdin.read().splitlines())
            print(f"latest={str(latest).lower()}")
            print(f"minor={str(minor).lower()}")
        else:
            sys.stdout.write(notes(args.version, args.root, args.image, args.repo_url, args.digest))
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
