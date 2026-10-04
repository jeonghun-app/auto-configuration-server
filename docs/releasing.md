# Releasing

A release is an annotated tag `vX.Y.Z` on a commit that is already on `main`.
Pushing the tag runs [`.github/workflows/release.yml`](../.github/workflows/release.yml),
which publishes the container image and the GitHub Release. Nothing is published
by hand.

## Versioning

Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). For
this service the public interface is what handsets, operators and deployments
depend on:

| Bump | When |
| --- | --- |
| MAJOR | An incompatible change to wire behaviour a deployed handset relies on, to the admin API, to an `ACS_*` setting, or to the stored data layout |
| MINOR | New behaviour, endpoints, parameters, management objects or settings, with the previous behaviour still the default |
| PATCH | Fixes, including security fixes, with no new interface |

Only plain `X.Y.Z` is released. Pre-release tags (`v1.4.0-rc.1`) are refused by
the workflow, so that a candidate can never move the `X.Y` or `latest` image tags.

The version is declared in two places, and both must change together:

- `pyproject.toml` — `[project] version`
- `src/acs/__init__.py` — `__version__`

## Procedure

### 1. Release pull request

On a branch from `main`:

1. Set the new version in `pyproject.toml` and `src/acs/__init__.py`.
2. In `CHANGELOG.md`, rename the content under `## [Unreleased]` to
   `## [X.Y.Z] — YYYY-MM-DD` (an em dash, as in the existing headings), and leave
   a fresh, empty `## [Unreleased]` above it.
3. Update the link references at the bottom of `CHANGELOG.md`:

   ```text
   [Unreleased]: https://github.com/jeonghun-app/auto-configuration-server/compare/vX.Y.Z...HEAD
   [X.Y.Z]: https://github.com/jeonghun-app/auto-configuration-server/compare/vPREVIOUS...vX.Y.Z
   ```

4. Check locally:

   ```bash
   make check
   .venv/bin/python scripts/release_notes.py verify vX.Y.Z
   .venv/bin/python scripts/release_notes.py notes X.Y.Z   # the Release body, as it will appear
   ```

   `verify` is the same check the workflow runs: the tag must be `vX.Y.Z`, equal
   both declared versions, and have a non-empty CHANGELOG section.
   `tests/test_release_notes.py` also asserts that the declared version has a
   CHANGELOG section and that every section has a link reference, so a release
   pull request that forgets either fails `make check`.

5. Open the pull request and merge it once CI is green.

### 2. Tag

Tag the merge commit on `main`, not a local branch:

```bash
git fetch origin
git log -1 --format='%h %s' origin/main        # the release pull request's merge
git tag -a vX.Y.Z -m "vX.Y.Z" origin/main
git push origin vX.Y.Z
```

### 3. What the workflow does

| Job | Permissions | Does |
| --- | --- | --- |
| `verify` | `contents: read` | `release_notes.py verify` on the tag; refuses a tag whose commit is not on `main`; `make install` and `make check` |
| `image` | `contents: read`, `packages: write` | Decides `latest` and `X.Y` from the tags that exist now (`release_notes.py channels`); refuses to rebuild an `X.Y.Z` that already exists; otherwise builds `linux/amd64` and `linux/arm64` with QEMU and Buildx and pushes `ghcr.io/jeonghun-app/auto-configuration-server` tagged `X.Y.Z`, plus `X.Y` and `latest` when they apply, with OCI labels and annotations, an SBOM and `mode=max` provenance |
| `release` | `contents: write` | Decides `latest` again; `release_notes.py notes` renders the CHANGELOG section, with relative links pointed at the tag and the pull commands (tag and digest) appended, and creates the GitHub Release, or updates it on a re-run |

The jobs run in that order and each one needs the previous one, so a failed check
publishes nothing, and a failed image push creates no Release.

#### Floating tags only move forward

| Tag | Moves to this release when |
| --- | --- |
| `X.Y.Z` | Always, once; it is never overwritten |
| `X.Y` | It is the highest patch of its `X.Y` line among the `vX.Y.Z` tags |
| `latest` (and GitHub "Latest") | It is the highest `vX.Y.Z` tag of all |

So `v1.3.2` after `v1.4.0` is published as `1.3.2` and `1.3`, and `latest` stays
on `1.4.0`; re-running `v1.4.0` after `v1.4.1` moves neither `1.4` nor `latest`.

The decision is made in the publishing job, not when the tag is verified, and
release runs are serialised in one concurrency group (`release`) across all tags.
A release that waited behind a newer one, or is re-run after it, therefore sees
the newer tag. GitHub keeps only one *pending* run per group: if a third tag is
pushed while one release runs and another waits, the waiting run is cancelled.
Re-run it with `gh run rerun <run-id>` once the queue is clear.

#### X.Y.Z is immutable

If `ghcr.io/jeonghun-app/auto-configuration-server:X.Y.Z` already exists, the
`image` job builds and pushes nothing and logs a warning with the existing digest;
the `release` job still runs, so re-running a release only rewrites its notes from
the CHANGELOG. To ship different contents, release `X.Y.(Z+1)`.

Every action is pinned to a full commit SHA. Dependabot (`github-actions`, monthly)
proposes updates to those pins.

### 4. Confirm

```bash
gh run list --workflow Release --limit 1
gh run watch <run-id> --exit-status

gh release view vX.Y.Z

# Both platforms, and the attestations.
docker buildx imagetools inspect ghcr.io/jeonghun-app/auto-configuration-server:X.Y.Z
docker buildx imagetools inspect ghcr.io/jeonghun-app/auto-configuration-server:X.Y.Z \
  --format '{{ json .SBOM }}' | head -c 400; echo
docker buildx imagetools inspect ghcr.io/jeonghun-app/auto-configuration-server:X.Y.Z \
  --format '{{ json .Provenance }}' | head -c 400; echo

# The image runs and reports the expected version label.
docker pull ghcr.io/jeonghun-app/auto-configuration-server:X.Y.Z
docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' \
  ghcr.io/jeonghun-app/auto-configuration-server:X.Y.Z
```

On the first release, check the package's visibility under the repository's
**Packages**. If it is not public, `docker pull` works only for the maintainers;
change it in the package settings.

## When the workflow fails

| Failed in | State | Do |
| --- | --- | --- |
| `verify` | Nothing published | Fix on `main` through a pull request, then move the tag: `git push --delete origin vX.Y.Z`, `git tag -d vX.Y.Z`, and tag again. Moving a tag is acceptable only because nothing was published from it |
| `image` | Usually nothing tagged: Buildx tags the image only after every platform is pushed | Re-run: `gh run rerun <run-id> --failed`. If `X.Y.Z` did get pushed, the re-run does not rebuild it; if `X.Y` or `latest` should have moved and did not, move them by hand as under [The image tags](#the-image-tags) |
| `release` | Image published, no Release | Re-run: `gh run rerun <run-id> --failed`. The job edits the Release if one already exists |

Once an image has been published from a tag, do not move the tag. Publish
`X.Y.(Z+1)` instead.

## Rollback

Prefer rolling forward with a patch release. When that is not fast enough:

### The image tags

GHCR tags are mutable; `X.Y.Z` must never be repointed, but `latest` and `X.Y`
can be moved back to the previous good release without rebuilding, after
`docker login ghcr.io` with a token that has `write:packages`:

```bash
GOOD=1.3.0      # the previous good release
docker buildx imagetools create \
  --tag ghcr.io/jeonghun-app/auto-configuration-server:latest \
  ghcr.io/jeonghun-app/auto-configuration-server:$GOOD
```

Move `X.Y` the same way only to an earlier patch of the same line (`1.4` to
`1.4.0` after a bad `1.4.1`). If the bad release is `X.Y.0`, there is no earlier
`X.Y` image to point at; publish `X.Y.1` instead.

### The GitHub Release

```bash
gh release edit vGOOD --latest                 # make the good release "Latest" again
gh release edit vX.Y.Z --notes-file notes.md   # add a warning to the bad one's notes
gh release delete vX.Y.Z --yes                 # or withdraw it; the tag is kept
```

Do not delete the tag of a release whose image was published: deployments may
reference it, and the CHANGELOG compare links do.

### An AWS deployment

`scripts/deploy.sh` does not pull from GHCR. It builds the checked-out tree and
pushes to the ECR repository it manages, whose tags are immutable. To go back to
an image that is still in ECR:

```bash
scripts/deploy.sh --allowed-cidr <cidr> --certificate-arn <arn> \
  --image-tag <previous-tag> --skip-build
```

If the previous image has expired from ECR (the lifecycle policy keeps
`MaxTaggedImages`, default 20), build it from its tag:

```bash
git checkout vGOOD
scripts/deploy.sh --allowed-cidr <cidr> --certificate-arn <arn> --image-tag GOOD
```

Two cautions:

- `deploy.sh` passes every application-stack parameter it knows on every run
  (`ImageUri`, `AllowedCidr`, `Environment`, `DesiredCount`, `SmsProvider`,
  `SmsOriginationIdentity`, `CertificateArn`), so a parameter left out of a
  rollback command reverts to the script's default, and a missing
  `--certificate-arn` turns HTTPS off. Read the current values first, and repeat
  them:

  ```bash
  aws cloudformation describe-stacks --region <region> --stack-name rcs-acs-app \
    --query 'Stacks[0].Parameters' --output table
  ```

- A rollback replaces the tasks, not the data. The DynamoDB table is untouched.
  Before rolling back across a release, read its CHANGELOG entry for any change
  to stored records.

The ECS service runs with `MinimumHealthyPercent: 100` and the deployment circuit
breaker, so a rollout whose tasks never become healthy reverts by itself; see
[runbook.md](runbook.md#rollback).
