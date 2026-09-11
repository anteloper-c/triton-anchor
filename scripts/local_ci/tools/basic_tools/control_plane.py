"""Changed-file contracts and actual candidate control-plane regressions."""

from __future__ import annotations

from pathlib import Path

from actions import candidate_python, run, test_results, write_json
from contract_checks import check


def execute(payload: dict) -> None:
    context = payload["context"]
    root = Path(context["source_dir"])
    out = Path(context["artifact_dir"]) / "control_plane"
    result = check(root, context["base_sha"], context["target_sha"])
    changed = [line.split("\t")[-1] for line in result["changed_files"]]
    regression_required = any(
        path.startswith(("scripts/", ".github/", "dashboard/"))
        and not path.endswith((".md", ".rst", ".txt"))
        for path in changed
    )
    selected = payload["parameters"].get("paths")
    if selected is None and regression_required:
        selected = [
            name
            for name in (
                "scripts/local_ci",
                "scripts/ci/tests",
                "scripts/api_contract/tests",
            )
            if (root / name).is_dir() and any((root / name).rglob("test_*.py"))
        ]
    if selected:
        for value in selected:
            path = (root / value.split("::", 1)[0]).resolve(strict=True)
            if not path.is_relative_to(root.resolve()):
                raise ValueError(
                    "Control regression paths must stay inside the checkout"
                )
        command = [
            candidate_python(context),
            "-m",
            "pytest",
            "-q",
            "--import-mode=importlib",
            "-o",
            "addopts=",
            "--junitxml",
            str(out / "tests.xml"),
        ]
        if payload["parameters"].get("keyword"):
            command += ["-k", payload["parameters"]["keyword"]]
        command += [str(root / path) for path in selected]
        run(command, cwd=out)
        test_results({**payload, "test_source": str(root), "test_paths": selected})
    elif regression_required:
        raise ValueError("Control change has no runnable regression suite")
    result.update(
        task_id=context["task_id"],
        target_sha=context["target_sha"],
        regression_required=regression_required,
        regression_paths=selected or [],
    )
    write_json(out / "control_plane.json", result)
