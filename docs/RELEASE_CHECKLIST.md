# Release Checklist

The runbook for cutting a new k8s-mcp-server release. The first release
(v0.1.0) follows the full checklist; later releases can skip TestPyPI
validation once you trust the metadata.

---

## 0. Sanity checks (do this first)

- [ ] **Verify the GitHub username** in `pyproject.toml` and
  `CHANGELOG.md` links matches your actual GitHub handle. The current
  placeholder/inferred value is **`Sarah-Salah`** (in `[project.urls]`
  and the bottom-of-file link references). Fix here before publishing.
- [ ] All four-command quality gates pass:
  ```bash
  uv run ruff format --check .
  uv run ruff check .
  uv run mypy src/
  uv run pytest
  ```
- [ ] **Run the kind integration test locally.** CI runs it but a local
  pass is the final stack-end check before publishing:
  ```bash
  kind create cluster --name k8s-mcp-test
  export KUBECONFIG="$HOME/.kube/config"
  uv run pytest -m integration -v
  kind delete cluster --name k8s-mcp-test
  ```

---

## 1. Build the distribution

```bash
rm -rf dist/
uv build
ls -la dist/   # expect both .tar.gz (sdist) and .whl (wheel)
```

Inspect the wheel:

```bash
unzip -l dist/k8s_mcp_server-VERSION-py3-none-any.whl
unzip -p dist/k8s_mcp_server-VERSION-py3-none-any.whl '*/METADATA' | head -50
```

Verify:

- [ ] All `src/k8s_mcp_server/**/*.py` files present in wheel
- [ ] `LICENSE` and `py.typed` present
- [ ] `METADATA` reflects: version, all 4 `Project-URL` entries, all
      keywords, all classifiers (including `Typing :: Typed`)
- [ ] `Description-Content-Type: text/markdown` (so PyPI renders README
      correctly)

Smoke-install the wheel locally in a clean venv:

```bash
python3.13 -m venv /tmp/k8s-mcp-publish-test
/tmp/k8s-mcp-publish-test/bin/pip install dist/k8s_mcp_server-VERSION-py3-none-any.whl
/tmp/k8s-mcp-publish-test/bin/k8s-mcp-server --version    # → "k8s-mcp-server VERSION"
/tmp/k8s-mcp-publish-test/bin/k8s-mcp-server --help       # → usage block with all 5 flags
rm -rf /tmp/k8s-mcp-publish-test
```

---

## Phase B — Publish to TestPyPI

**TestPyPI is a hard requirement before real PyPI.** First publishes have
a knack for surfacing weird metadata issues (broken Markdown rendering,
missing classifiers, etc.). TestPyPI is free insurance.

### B.1 — Set up TestPyPI account

- [ ] Create account at <https://test.pypi.org>
- [ ] Generate an API token: account settings → "API tokens" → "Add API
      token". Scope to "Entire account" for the first publish; switch to
      "Project: k8s-mcp-server" for later publishes.
- [ ] Save the token to an env var:
      ```bash
      export UV_PUBLISH_TOKEN="pypi-..."
      ```

### B.2 — Publish to TestPyPI

```bash
uv publish --publish-url https://test.pypi.org/legacy/
```

If `uv publish` fails for any reason (auth issues, network errors),
`twine upload dist/*` is the universal fallback — install via
`pip install twine` first:

```bash
pip install twine
twine upload --repository testpypi dist/*
```

### B.3 — Verify on TestPyPI

- [ ] Visit <https://test.pypi.org/project/kubernetes-mcp/>. Verify:
  - README renders correctly (badges, headings, two feature tables)
  - All 4 sidebar URLs work (Homepage, Repository, Issues, **Changelog**)
  - All 8 keywords appear
  - All 10 classifiers appear, including `Typing :: Typed` and
    `Environment :: Console`
- [ ] Smoke install from TestPyPI in a fresh venv (note the
      `--extra-index-url` so dependencies install from real PyPI —
      TestPyPI doesn't mirror them):
  ```bash
  python3.13 -m venv /tmp/k8s-mcp-testpypi
  /tmp/k8s-mcp-testpypi/bin/pip install \
      --index-url https://test.pypi.org/simple/ \
      --extra-index-url https://pypi.org/simple/ \
      kubernetes-mcp
  /tmp/k8s-mcp-testpypi/bin/k8s-mcp-server --version
  /tmp/k8s-mcp-testpypi/bin/k8s-mcp-server --help
  rm -rf /tmp/k8s-mcp-testpypi
  ```

---

## Phase C — Publish to real PyPI

**Only after TestPyPI verifies cleanly.**

### C.1 — Set up PyPI account

- [ ] Create account at <https://pypi.org>
- [ ] **Enable 2FA** — now mandatory for PyPI accounts publishing new
      packages
- [ ] Generate an API token: account settings → "API tokens" → "Add API
      token". Scope to "Entire account" for the first publish; switch to
      "Project: kubernetes-mcp" once the project exists.
- [ ] Save the token to an env var:
      ```bash
      export UV_PUBLISH_TOKEN="pypi-..."
      ```

### C.2 — Publish

```bash
uv publish
```

Same fallback as Phase B if `uv publish` fails:

```bash
twine upload dist/*
```

### C.3 — Verify on PyPI

- [ ] Visit <https://pypi.org/project/kubernetes-mcp/>. Same verifications
      as B.3 (README, URLs, keywords, classifiers).
- [ ] Smoke install in a fresh venv (no `--extra-index-url` needed —
      everything is on real PyPI):
  ```bash
  python3.13 -m venv /tmp/k8s-mcp-pypi
  /tmp/k8s-mcp-pypi/bin/pip install kubernetes-mcp
  /tmp/k8s-mcp-pypi/bin/k8s-mcp-server --version
  /tmp/k8s-mcp-pypi/bin/k8s-mcp-server --help
  rm -rf /tmp/k8s-mcp-pypi
  ```
- [ ] Test the `uvx` no-install path:
  ```bash
  uvx --from kubernetes-mcp k8s-mcp-server --version
  ```

---

## Phase D — Git tag + GitHub release

### D.1 — Decide on CHANGELOG fold

**Before tagging v0.1.0**, decide whether to fold any current
`## [Unreleased]` entries into the `## [0.1.0] - 2026-05-13` block.

**Recommended: yes** — entries describing publish-enabling changes
(`py.typed` marker, `pyproject.toml` metadata, `RELEASE_CHECKLIST.md`)
are part of the v0.1.0 release scope, not separate work. Move them
under the `[0.1.0]` block's `### Added — Quality` (or similar) section
before tagging.

For subsequent releases, the `[Unreleased]` block accumulates over time
and gets folded into the new version's block at tag time.

### D.2 — Tag and push

```bash
git tag -a v0.1.0 -m "Release v0.1.0"
git push origin v0.1.0
```

The leading `v` matches the link references at the bottom of
`CHANGELOG.md` (`https://github.com/Sarah-Salah/k8s-mcp-server/releases/tag/v0.1.0`).

### D.3 — Create the GitHub release

```bash
gh release create v0.1.0 \
    --title "v0.1.0" \
    --notes-file docs/RELEASE_NOTES_v0.1.0.md
```

Or via the web UI:
<https://github.com/Sarah-Salah/k8s-mcp-server/releases/new>

- [ ] Verify the release page renders `docs/RELEASE_NOTES_v0.1.0.md`
- [ ] Verify the auto-attached source archives (`.tar.gz`, `.zip`) are
      present
- [ ] Optionally attach `dist/k8s_mcp_server-0.1.0-py3-none-any.whl`
      and `dist/k8s_mcp_server-0.1.0.tar.gz` to the release for
      out-of-PyPI download

---

## Post-publish

- [ ] The README's CI badge auto-updates as new commits run; verify it
      reflects the latest run after the tag push triggers a CI build
- [ ] Check the PyPI download stats start incrementing
      (<https://pepy.tech/project/kubernetes-mcp>)
- [ ] Bookmark the project page for future reference

---

## Rollback

**PyPI does NOT allow re-uploading a yanked version.** Once `0.1.0` is
yanked, the next attempt MUST be `0.1.1` or higher. You **cannot** fix
and re-publish the same version number.

If a critical bug is discovered post-publish:

1. **Yank** the bad version: PyPI project page → Manage → Releases →
   Yank. Yanked versions can't be installed by `pip install pkg` (only
   by exact pin: `pip install pkg==0.1.0`). Existing installs are
   unaffected.
2. **Fix**, bump to `0.1.1`, full checklist again from Phase 0.
3. Update `CHANGELOG.md` with a new `## [0.1.1] - DATE` block. The
   yanked `[0.1.0]` block stays — it's history.

For critical security issues, also consider:

- Posting an advisory at
  <https://github.com/Sarah-Salah/k8s-mcp-server/security/advisories/new>
- Updating `docs/SECURITY.md` if the fix changes the threat model
