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
    # X.Y and latest move to later releases; the notes must not claim them.
    assert "latest" not in body
    assert "`2.0`" not in body


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


FENCED = """## [2.0.0] — 2026-09-01

### Changed

- The heading format is now:

  ```markdown
  ## [1.2.3] — 2026-01-01
  [1.2.3]: https://example.org
  ```

~~~~text
## not a heading either
~~~
still inside the tilde fence
~~~~

- And this line is still part of 2.0.0.

## [1.0.0] — 2026-08-30

First release.

```bash
## [2.0.0] — a fenced copy that must not count as a second section
```
"""


def test_a_heading_inside_a_code_fence_does_not_end_the_section() -> None:
    body = rn.extract_section(FENCED, "2.0.0")
    assert "## [1.2.3] — 2026-01-01" in body
    assert "[1.2.3]: https://example.org" in body
    assert "## not a heading either" in body
    assert "still inside the tilde fence" in body
    assert body.rstrip().endswith("And this line is still part of 2.0.0.")
    assert "First release" not in body


def test_a_heading_inside_a_code_fence_is_not_a_section() -> None:
    assert "First release." in rn.extract_section(FENCED, "1.0.0")
    with pytest.raises(rn.ReleaseError, match="no '## \\[1.2.3\\]' section"):
        rn.extract_section(FENCED, "1.2.3")


def test_an_unclosed_code_fence_is_refused() -> None:
    text = "## [1.0.0] — 2026-01-01\n\n```\n## [0.9.0]\n\n## [0.9.0] — 2025-01-01\n\nOld.\n"
    with pytest.raises(rn.ReleaseError, match="unclosed code fence opened at line 3"):
        rn.extract_section(text, "1.0.0")


TAGS = ["v1.3.0", "v1.4.0", "v1.4.1", "v1.2.9", "v2.0.0-rc.1", "not-a-version", "refs/tags/v1.3.2"]


@pytest.mark.parametrize(
    ("tag", "latest", "minor"),
    [
        ("v1.4.1", True, True),
        ("v1.4.0", False, False),  # re-run or delayed behind 1.4.1
        ("v1.3.2", False, True),  # a patch to an older line keeps its own X.Y
        ("v1.3.0", False, False),
        ("v1.5.0", True, True),  # not yet in the list: the tag being published
        ("v1.4.2", True, True),
    ],
)
def test_latest_and_the_minor_tag_only_move_forward(tag: str, latest: bool, minor: bool) -> None:
    assert rn.channels(tag, TAGS) == (latest, minor)


def test_a_pre_release_tag_never_counts_as_newer() -> None:
    assert rn.channels("v1.9.9", ["v2.0.0-rc.1", "v10.0.0.1"]) == (True, True)


def test_the_channels_cli_prints_github_outputs() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "release_notes.py"), "channels", "v1.4.0"],
        input="refs/tags/v1.4.1\nrefs/tags/v1.4.0\n",
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "latest=false\nminor=false\n"


def test_backticks_in_an_info_string_are_inline_code_not_a_fence() -> None:
    text = (
        "## [2.0.0] — 2026-09-01\n\n"
        "- A wrapped list item whose next line starts with code:\n"
        "  ```old_option``` is now deprecated.\n"
        "```old_option``` again, unindented.\n\n"
        "## [1.0.0] — 2026-08-30\n\nFirst release.\n"
    )
    body = rn.extract_section(text, "2.0.0")
    assert body.endswith("```old_option``` again, unindented.\n")
    assert "First release" not in body
    assert rn.extract_section(text, "1.0.0") == "First release.\n"


def test_a_tilde_fence_info_string_may_contain_backticks() -> None:
    text = "## [1.0.0] — 2026-01-01\n\n~~~ `x`\n## inside\n~~~\n\nAfter.\n\n## [0.9.0]\n\nOld.\n"
    body = rn.extract_section(text, "1.0.0")
    assert "## inside" in body
    assert body.rstrip().endswith("After.")


NOT_FOUND = "ERROR: ghcr.io/o/r:1.4.0: not found"
DENIED = "ERROR: failed to authorize: ... 403 Forbidden"


@pytest.mark.parametrize(
    ("succeeded", "output", "first", "state"),
    [
        (True, '{"digest": "sha256:abc"}', False, "exists"),
        (True, '{"digest": "sha256:abc"}', True, "exists"),
        (False, NOT_FOUND, False, "absent"),
        (False, "manifest unknown", False, "absent"),
        (False, "name unknown: repository name not known to registry", False, "absent"),
        (False, DENIED, True, "absent-first-publish"),
        (False, "denied: requested access to the resource is denied", True, "absent-first-publish"),
    ],
)
def test_an_image_lookup_is_classified(
    succeeded: bool, output: str, first: bool, state: str
) -> None:
    assert rn.image_state(succeeded, output, first) == state


@pytest.mark.parametrize(
    "output",
    [DENIED, "denied: requested access to the resource is denied", "502 Bad Gateway", ""],
)
def test_a_denial_or_an_error_is_never_read_as_absent(output: str) -> None:
    # A lookup refused while the push is allowed would overwrite an existing X.Y.Z.
    with pytest.raises(rn.ReleaseError, match="could not tell"):
        rn.image_state(False, output, first_publish=False)


def test_first_publish_does_not_excuse_a_registry_error() -> None:
    with pytest.raises(rn.ReleaseError):
        rn.image_state(False, "502 Bad Gateway", first_publish=True)


@pytest.mark.parametrize(
    ("args", "stdin", "code", "stdout"),
    [
        (["--exit-code", "1"], NOT_FOUND, 0, "state=absent\n"),
        (["--exit-code", "1", "--first-publish", ""], DENIED, 1, ""),
        (
            ["--exit-code", "1", "--first-publish", "true"],
            DENIED,
            0,
            "state=absent-first-publish\n",
        ),
        (["--exit-code", "0"], '{"digest": "sha256:abc"}', 0, "state=exists\n"),
    ],
)
def test_the_image_state_cli(args: list[str], stdin: str, code: int, stdout: str) -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "release_notes.py"), "image-state", *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode, result.stdout) == (code, stdout)
