# Backend Status GitHub Pages

## Purpose

The dashboard publishes two independent result views:

1. The latest manually triggered full operator test. This is the primary view
   and supports operator search, status and failure-stage filters, exception-only
   display, pagination, and CSV download.
2. The latest backend health and performance summaries, including delivery
   smoke, compile-time regression, Pass profiling, and IR serialization.

GitHub Pages only serves static files. GitHub Actions is responsible for reading
Gitee results, normalizing them into the contracts below, and deploying the
static site. Browser JavaScript never receives a Gitee or GitHub token.

## Static layout

```text
dashboard/
├── index.html
├── styles.css
├── app.js
├── assets/
│   └── triton-anchor-logo.png
└── data/
    ├── manifest.json
    ├── full-test.json
    ├── full-test.csv
    ├── backend-status.json
    └── performance.json
```

`manifest.json` is the only fixed browser entry point. The remaining file names
can change without modifying the page as long as the manifest is updated.

## Data contracts

### Full operator test

Schema: `triton-anchor-full-test/v1`.

```json
{
  "schema": "triton-anchor-full-test/v1",
  "run": {
    "id": "20260720T020000Z-a1b2c3d4",
    "trigger": "manual",
    "state": "completed",
    "backend": "Sophgo CModel",
    "profile": "sophgo-cmodel",
    "sha": "a1b2c3d4...",
    "branch": "main",
    "started_at": "2026-07-20T02:00:00Z",
    "finished_at": "2026-07-20T03:12:00Z",
    "result_url": "https://gitee.com/..."
  },
  "operators": [
    {
      "index": 1,
      "name": "add",
      "status": "passed",
      "failure_stage": null,
      "duration_ms": 803.2,
      "tested_at": "2026-07-20T02:01:00Z"
    }
  ]
}
```

Allowed operator states are `passed`, `failed`, `timeout`, and `unknown`.
The downloadable CSV must use UTF-8 with BOM so Chinese column names open
correctly in Excel.

### Backend status

Schema: `triton-anchor-backend-status-list/v1`. Each row contains one backend's
latest qualifying `main` result. PR runs must not replace this pointer.

Allowed overall states are `success`, `warning`, `failure`, `pending`, `stale`,
and `unknown`.

### Performance summary

Schema: `triton-anchor-performance-summary/v1`. The three required sections are:

- `compile_time.kernels`
- `pass_profile.hotspots`
- `ir_serialization.metrics`

The page displays values from this file without embedding benchmark-specific
logic. Future metric fields can be added without changing existing fields.

## Data modes

The committed data is marked with `mode: mock` so pull requests can validate
the page without depending on Gitee. A production Pages deployment changes the
manifest to `mode: mixed`:

- Full operator rows remain the committed demonstration data until the manual
  full-test publisher provides a stable result pointer.
- Backend health is generated from the newest
  `runs/ci_push_jiwang-delivery-ci/<sha>/<run-id>` result.
- Performance is generated from the newest run containing each metric. A newer
  failed delivery therefore changes backend health immediately without erasing
  the last valid compile-time or Pass measurements.
- Missing metrics are shown as unavailable. Mock values are never relabeled as
  live results.

Regenerate the demo conversion with:

```bash
python scripts/dashboard/build_mock_full_test.py \
  --input-csv /path/to/batch_test_results_flaggems.csv \
  --output-json dashboard/data/full-test.json \
  --output-csv dashboard/data/full-test.csv
```

## Gitee result synchronization

For a deployment, `.github/workflows/backend-status-pages.yml` shallow-clones
the public Gitee `local-ci-results` branch and runs:

```bash
python scripts/dashboard/sync_gitee_results.py \
  --results-dir "$RUNNER_TEMP/gitee-results" \
  --output-dir dashboard/data \
  --source-branch ci/push/jiwang-delivery-ci
```

The script rewrites only `backend-status.json`, `performance.json`, and the
manifest metadata. It does not modify the full operator files.

Recommended stable Gitee inputs:

```text
dashboard-data/full-test/latest.json
dashboard-data/full-test/latest.csv
backend-status/<backend-profile>/latest.json
compile-time/by-sha/<sha>/<backend-profile>/latest.json
pass-profile/by-sha/<sha>/<backend-profile>/latest.json
ir-serialization/by-sha/<sha>/<backend-profile>/latest.json
```

The result publisher should update `dashboard-data/full-test/latest.*` only for
a completed manually triggered full test. It should update backend status only
for an approved `main` result. Per-PR data remains available in run history and
must not overwrite the dashboard's stable status pointers.

Repository variables can override the defaults:

| Variable | Default |
| --- | --- |
| `GITEE_RESULTS_REPO_URL` | `https://gitee.com/likehupochuan/triton-anchor-local-ci-results.git` |
| `GITEE_RESULTS_BRANCH` | `local-ci-results` |
| `GITEE_RESULTS_WEB_URL` | `https://gitee.com/likehupochuan/triton-anchor-local-ci-results` |
| `DASHBOARD_SOURCE_BRANCH` | `ci/push/jiwang-delivery-ci` |
| `LOCAL_CI_BACKEND_PROFILE` | `sophgo-cmodel` |

## Deployment

The repository workflow `.github/workflows/backend-status-pages.yml` validates
the contracts and publishes `dashboard/` through GitHub Pages. In repository
settings, select:

```text
Settings -> Pages -> Build and deployment -> Source -> GitHub Actions
```

Pull requests validate the committed fallback data but do not deploy it.
Pushes to `main`, pushes to `jiwang-delivery-ci`, and manual workflow dispatches
deploy data freshly read from Gitee. When the GitHub result receiver observes a
completed local-CI result, it also dispatches the Pages workflow immediately.
The last successful Pages deployment remains online if Gitee cannot be fetched
or normalized.
