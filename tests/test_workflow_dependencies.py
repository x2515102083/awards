"""Prevent validation jobs from relying on packages preinstalled on a runner."""

import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


class WorkflowDependencyTests(unittest.TestCase):
    def test_direct_runtime_dependencies_are_declared(self):
        requirements = {
            line.split("==", 1)[0].strip().lower()
            for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertIn("referencing", requirements)

    def test_validation_jobs_install_dependencies_before_use(self):
        checked = 0
        for path in sorted(WORKFLOWS.glob("*.yml")):
            # BaseLoader preserves the GitHub Actions key `on` as a string.
            workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
            for job_name, job in workflow.get("jobs", {}).items():
                steps = job.get("steps", [])
                consumers = [
                    index for index, step in enumerate(steps)
                    if "scripts/manage.py" in step.get("run", "")
                    or "-m unittest" in step.get("run", "")
                ]
                if not consumers:
                    continue
                checked += 1
                with self.subTest(workflow=path.name, job=job_name):
                    setups = [
                        index for index, step in enumerate(steps)
                        if step.get("uses", "").startswith("actions/setup-python@")
                    ]
                    installs = [
                        index for index, step in enumerate(steps)
                        if re.search(
                            r"\bpython(?:3)? -m pip install -r requirements\.txt\b",
                            step.get("run", ""),
                        )
                    ]
                    self.assertTrue(setups, "Set up Python explicitly in each validation job")
                    self.assertTrue(installs, "Install requirements.txt before running validation")
                    self.assertLess(setups[0], installs[0])
                    self.assertLess(installs[0], consumers[0])
                    for index in (setups[0], installs[0]):
                        self.assertNotIn("if", steps[index])
                        self.assertNotEqual(steps[index].get("continue-on-error"), "true")
        self.assertGreater(checked, 0, "No validation jobs were found")

    def test_embedded_python_scripts_compile(self):
        path = WORKFLOWS / "prepare-small-ramsey-catalog.yml"
        workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        scripts = [
            step["run"]
            for step in workflow["jobs"]["prepare"]["steps"]
            if step.get("shell") == "python"
        ]
        self.assertTrue(scripts)
        for source in scripts:
            # Only compile: the payload's GitHub upload must never run in tests.
            compile(source, str(path), "exec")


if __name__ == "__main__":
    unittest.main()
