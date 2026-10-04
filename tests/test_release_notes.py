from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest
from scripts import release_notes as rn

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

CHANGELOG = """# Changelog

Intro paragraph.

## [Unreleased]

## [2.0.0] — 2026-09-01

### Added

- Second thing, see [docs/a.md](docs/a.md) and [the spec](https://example.org/x).

## [1.0.0] — 2026-08-30

First release.

- See [limits](docs/limitations.md#gaps) and [above](#added).

[Unreleased]: https://github.com/o/r/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/o/r/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/o/r/releases/tag/v1.0.0
"""


def _repo(tmp_path: pathlib.Path, version: str = "2.0.0", init_version: str | None = None) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "x"\nversion = "{version}"\n', encoding="utf-8"
    )
    pkg = tmp_path / "src" / "acs"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        f'__version__ = "{init_version or version}"\n', encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")


def test_a_section_stops_at_the_next_version_heading() -> None:
    body = rn.extract_section(CHANGELOG, "2.0.0")
    assert body.startswith("### Added")
    assert "Second thing" in body
    assert "1.0.0" not in body
    assert "First release" not in body


def test_the_last_section_stops_before_the_link_references() -> None:
    body = rn.extract_section(CHANGELOG, "1.0.0")
    assert body.startswith("First release.")
    assert "[Unreleased]:" not in body
    assert "releases/tag" not in body


def test_the_heading_is_not_part_of_the_notes() -> None:
    assert "## [2.0.0]" not in rn.extract_section(CHANGELOG, "2.0.0")


def test_a_missing_section_is_refused() -> None:
    with pytest.raises(rn.ReleaseError, match=r"no '## \[3.0.0\]' section"):
        rn.extract_section(CHANGELOG, "3.0.0")


def test_an_empty_section_is_refused() -> None:
    with pytest.raises(rn.ReleaseError, match="empty"):
        rn.extract_section(CHANGELOG, "Unreleased")


def test_a_version_prefix_does_not_match_a_longer_version() -> None:
    text = "## [1.0.10] — 2026-01-01\n\nTen.\n"
    with pytest.raises(rn.ReleaseError):
        rn.extract_section(text, "1.0.1")


def test_a_duplicated_section_is_refused() -> None:
    text = "## [1.0.0] — 2026-01-01\n\nA.\n\n## [1.0.0] — 2026-01-02\n\nB.\n"
    with pytest.raises(rn.ReleaseError, match="more than one"):
        rn.extract_section(text, "1.0.0")


def test_an_ascii_hyphen_before_the_date_is_accepted() -> None:
    assert rn.extract_section("## [1.0.0] - 2026-01-01\n\nA.\n", "1.0.0") == "A.\n"


@pytest.mark.parametrize("tag", ["1.2.3", "v1.2", "v1.2.3-rc.1", "v01.2.3", "v1.2.3.4", "vX.Y.Z"])
def test_a_tag_that_is_not_plain_semver_is_refused(tag: str) -> None:
    with pytest.raises(rn.ReleaseError, match="vMAJOR.MINOR.PATCH"):
        rn.version_from_tag(tag)


def test_verify_accepts_a_tag_that_matches_everything(tmp_path: pathlib.Path) -> None:
    _repo(tmp_path)
    assert rn.verify("v2.0.0", tmp_path) == "2.0.0"


def test_verify_refuses_a_tag_that_differs_from_pyproject(tmp_path: pathlib.Path) -> None:
    _repo(tmp_path, version="1.0.0")
    with pytest.raises(rn.ReleaseError, match="pyproject.toml version 1.0.0"):
        rn.verify("v2.0.0", tmp_path)


def test_verify_refuses_a_package_version_left_behind(tmp_path: pathlib.Path) -> None:
    _repo(tmp_path, version="2.0.0", init_version="1.0.0")
    with pytest.raises(rn.ReleaseError, match="__init__.py version 1.0.0"):
        rn.verify("v2.0.0", tmp_path)


def test_verify_refuses_a_version_with_no_changelog_section(tmp_path: pathlib.Path) -> None:
    _repo(tmp_path, version="3.0.0")
    with pytest.raises(rn.ReleaseError, match="no '## \\[3.0.0\\]' section"):
        rn.verify("v3.0.0", tmp_path)


def test_relative_links_point_at_the_tagged_tree() -> None:
    body = rn.absolutise_links(
        rn.extract_section(CHANGELOG, "2.0.0"), "https://github.com/o/r/", "v2.0.0"
    )
    assert "](https://github.com/o/r/blob/v2.0.0/docs/a.md)" in body
    assert "](https://example.org/x)" in body


def test_anchors_are_left_alone_and_fragments_survive() -> None:
    body = rn.absolutise_links(
        rn.extract_section(CHANGELOG, "1.0.0"), "https://github.com/o/r", "v1.0.0"
    )
    assert "](https://github.com/o/r/blob/v1.0.0/docs/limitations.md#gaps)" in body
    assert "](#added)" in body


def test_the_notes_carry_the_pull_command_for_the_exact_version(tmp_path: pathlib.Path) -> None:
    _repo(tmp_path)
    body = rn.notes("2.0.0", tmp_path, image="ghcr.io/o/r", digest="sha256:abc")
    assert "docker pull ghcr.io/o/r:2.0.0" in body
    assert "docker pull ghcr.io/o/r@sha256:abc" in body
    assert "`2.0`" in body


def test_the_cli_exits_non_zero_with_a_reason(tmp_path: pathlib.Path) -> None:
    _repo(tmp_path, version="1.0.0")
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "release_notes.py"),
            "--root",
            str(tmp_path),
            "verify",
            "v2.0.0",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "does not match pyproject.toml" in result.stderr


def test_the_repository_itself_is_releasable_at_its_declared_version() -> None:
    version = rn.project_version((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert rn.verify(f"v{version}", REPO_ROOT) == version


def test_every_released_version_has_a_compare_link() -> None:
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    versions = [
        m.group("name")
        for line in text.splitlines()
        if (m := rn._HEADING_RE.match(line)) is not None
    ]
    assert versions[0] == "Unreleased"
    for name in versions:
        assert any(line.startswith(f"[{name}]: https://") for line in text.splitlines()), name
