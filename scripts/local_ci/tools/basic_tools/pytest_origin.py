"""Pytest plugin recording imports from the actual test process."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path


def imports() -> dict:
    distribution = importlib.metadata.distribution("triton-anchor")
    members = {
        Path(distribution.locate_file(item)).resolve()
        for item in distribution.files or []
    }
    result = {}
    for name in ("triton", "triton_anchor"):
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        if path not in members:
            raise ValueError(
                "Test process imported outside the installed wheel: " + name
            )
        result[name] = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    return result


class ImportOrigin:
    def __init__(self, report: str | Path, installation: dict):
        self.path, self.installation = Path(report), installation
        self.error = ""
        self.observed = {}

    def check(self):
        self.observed = imports()
        expected = self.installation.get("imports", {})
        if not expected or any(
            self.observed.get(name) != expected.get(name)
            for name in ("triton", "triton_anchor")
        ):
            raise ValueError(
                "Test process import identity differs from the installed wheel record"
            )

    def pytest_sessionstart(self, session):
        try:
            self.check()
        except (ImportError, OSError, ValueError) as exc:
            self.error = str(exc)

    def pytest_sessionfinish(self, session, exitstatus):
        try:
            self.check()
        except (ImportError, OSError, ValueError) as exc:
            self.error = str(exc)
        value = {
            key: self.installation[key]
            for key in ("task_id", "target_sha", "environment_fingerprint", "sha256")
            if key in self.installation
        }
        value.update(
            status="fail" if self.error else "pass",
            imports=self.observed,
            error=self.error,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(value, indent=2) + "\n")
        if self.error and not session.exitstatus:
            session.exitstatus = 1


# `pytest -p pytest_origin` is available when this trusted directory is on the
# test process import path; the entry script below also works with Python -I.
def pytest_configure(config):
    report, installation = (
        os.environ.get("LOCAL_CI_IMPORT_REPORT"),
        os.environ.get("LOCAL_CI_INSTALLATION_MANIFEST"),
    )
    if report and installation:
        value = json.loads(Path(installation).read_text())
        config.pluginmanager.register(
            ImportOrigin(report, value), "local-ci-import-origin"
        )
