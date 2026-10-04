#!/usr/bin/env python3
"""Check a release tag against the repository, and extract its release notes.

The release workflow and a maintainer preparing a release by hand run the same
code, so "the tag matches the version" and "the notes are the CHANGELOG section"
mean the same thing in both places.

    python scripts/release_notes.py verify v1.3.0
    python scripts/release_notes.py notes 1.3.0 [--image ghcr.io/owner/repo]
        [--digest sha256:...] [--repo-url https://github.com/owner/repo]

``verify`` fails unless the tag is ``vMAJOR.MINOR.PATCH``, equals the version in
``pyproject.toml`` and ``src/acs/__init__.py``, and ``CHANGELOG.md`` has a non-empty
section for it. ``notes`` prints that section.
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
_HEADING_RE = re.compile(r"^## \[(?P<name>[^\]]+)\](?:\s+[—–-]\s+\S.*)?\s*$")
# Keep a Changelog ends with link reference definitions; they belong to no section.
_LINK_DEF_RE = re.compile(r"^\[[^\]]+\]:\s+\S+")
_INIT_VERSION_RE = re.compile(r'^__version__\s*=\s*"(?P<version>[^"]+)"', re.MULTILINE)
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


def extract_section(changelog_text: str, version: str) -> str:
    """Return the body of the ``## [version]`` section, without its heading."""
    lines = changelog_text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        heading = _HEADING_RE.match(line)
        if heading is not None and heading.group("name") == version:
            if start is not None:
                raise ReleaseError(f"CHANGELOG.md has more than one section for {version}")
            start = index + 1
    if start is None:
        raise ReleaseError(f"CHANGELOG.md has no '## [{version}]' section")

    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## ") or _LINK_DEF_RE.match(lines[index]):
            end = index
            break

    body = "\n".join(lines[start:end]).strip("\n")
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
        f"Also tagged `{version.rsplit('.', 1)[0]}`. Built for `linux/amd64` and "
        "`linux/arm64`, with SBOM and provenance attestations attached.\n"
    )


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

    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            version = verify(args.tag, args.root)
            print(
                f"{args.tag}: pyproject.toml, src/acs/__init__.py and CHANGELOG.md agree "
                f"on {version}"
            )
        else:
            sys.stdout.write(notes(args.version, args.root, args.image, args.repo_url, args.digest))
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
