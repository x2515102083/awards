"""Synthetic public-record fixtures; no assessment policy or live awards."""

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import manage  # noqa: E402


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ("awards", "candidates/observation", "candidates/verified-pending",
                          "people/recipients", "people/curators", "people/verifiers", "docs"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        shutil.copytree(ROOT / "data/schema", self.root / "data/schema")
        self.entry_dir = self.root / "candidates/observation/example-problem"
        (self.entry_dir / "verification").mkdir(parents=True)
        for source, target in (("award.yaml.example", "award.yaml"), ("record.yaml.example", "verification/record.yaml"),
                               ("citation.md", "citation.md"), ("recipients.md", "recipients.md")):
            shutil.copy(ROOT / "docs/templates" / source, self.entry_dir / target)
        self.entry = manage.read_yaml(self.entry_dir / "award.yaml")
        self.profiles = {"recipient": {}, "curator": {"reviewer-a": {}, "reviewer-b": {}}, "verifier": {"verifier-a": {}}}

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(value, sort_keys=False, allow_unicode=True), encoding="utf-8")

    def save_entry(self):
        self.write(self.entry_dir.relative_to(self.root) / "award.yaml", self.entry)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, text=True, encoding="utf-8", capture_output=True).stdout.strip()

    def commit(self):
        self.git("add", ".")
        self.git("-c", "user.name=Synthetic Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "Synthetic fixture")
        return self.git("rev-parse", "HEAD")

    def profile(self, person_id, role):
        self.write(f"people/{role}s/{person_id}.yaml", {
            "id": person_id, "name": f"Synthetic {person_id}", "role": role, "affiliation": "",
            "links": [], "publication_consent": "https://example.invalid/confirmed" if role == "recipient" else None,
            "recusals": [],
        })

    def test_empty_indexes(self):
        shutil.rmtree(self.entry_dir)
        manage.generate(self.root)
        self.assertEqual(json.loads((self.root / "data/awards.json").read_text()), {"schema_version": 1, "items": []})
        manage.generate(self.root, check=True)

    def test_award_cannot_be_deleted(self):
        self.prepare_award()
        base = self.commit()
        shutil.rmtree(self.root / "awards/2026-01")
        with self.assertRaisesRegex(manage.InvalidRecord, "must not be deleted"):
            manage.check_history(self.root, base)

    def test_draft_roundtrip_and_drift(self):
        result = manage.generate(self.root)
        self.assertEqual(result["candidates"]["items"][0]["entry"]["id"], "example-problem")
        before = (self.root / "data/candidates.json").read_bytes()
        manage.generate(self.root)
        self.assertEqual(before, (self.root / "data/candidates.json").read_bytes())
        (self.root / "data/candidates.json").write_text("{}\n")
        with self.assertRaisesRegex(manage.InvalidRecord, "stale"):
            manage.generate(self.root, check=True)

    def test_unconfirmed_award_rejected(self):
        self.entry["status"] = "announced"
        self.save_entry()
        with self.assertRaises(manage.InvalidRecord):
            manage.collect(self.root)

    def test_nonempty_english_sections(self):
        for name in ("citation.md", "recipients.md"):
            path = self.entry_dir / name
            original = path.read_text(encoding="utf-8")
            for content in ("# Missing section\n", "## English\n\n", "## English\n\n## Other\nUnrelated text.\n"):
                with self.subTest(name=name, content=content):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaisesRegex(manage.InvalidRecord, "English"):
                        manage.collect(self.root)
            path.write_text(original, encoding="utf-8")
        manage.collect(self.root)

    def test_duplicate_yaml_keys_rejected(self):
        (self.entry_dir / "award.yaml").write_text("id: one\nid: two\n")
        with self.assertRaisesRegex(manage.InvalidRecord, "unique"):
            manage.collect(self.root)

    def test_alias_and_custom_tag_rejected(self):
        for content in ("one: &one 1\ntwo: *one\n", "!!python/object/apply:os.system ['false']\n"):
            with self.subTest(content=content):
                (self.entry_dir / "award.yaml").write_text(content)
                with self.assertRaises(manage.InvalidRecord):
                    manage.collect(self.root)

    def test_unknown_payment_field_rejected(self):
        self.entry["bank_account"] = "should not be public"
        self.save_entry()
        with self.assertRaisesRegex(manage.InvalidRecord, "Additional properties"):
            manage.collect(self.root)

    def test_symlinks_and_misplaced_records_rejected(self):
        link = self.entry_dir / "outside.yaml"
        link.symlink_to(ROOT / "README.md")
        with self.assertRaisesRegex(manage.InvalidRecord, "Symlinks"):
            manage.collect(self.root)
        link.unlink()
        link.write_text("{}")
        with self.assertRaisesRegex(manage.InvalidRecord, "misplaced"):
            manage.collect(self.root)

    def test_duplicate_entry_ids_rejected(self):
        destination = self.root / "candidates/verified-pending/example-problem"
        shutil.copytree(self.entry_dir, destination)
        with self.assertRaisesRegex(manage.InvalidRecord, "Duplicate entry"):
            manage.collect(self.root)

    def test_published_statement_immutable_and_superseded(self):
        self.git("init", "-q")
        self.profile("reviewer-a", "curator")
        self.profile("reviewer-b", "curator")
        relative = self.entry_dir.relative_to(self.root) / "verification/statement.yaml"
        statement = self.statement()
        self.write(relative, statement)
        base = self.commit()
        changed = copy.deepcopy(statement)
        changed["statement"]["text"] = "Changed assertion"
        self.write(relative, changed)
        with self.assertRaisesRegex(manage.InvalidRecord, "immutable"):
            manage.check_history(self.root, base)
        changed["id"] = "STMT-example-problem-v2"
        changed["supersedes"] = statement["id"]
        self.write(relative, changed)
        self.write(relative.parent / "statements" / (statement["id"] + ".yaml"), {**statement, "status": "superseded"})
        manage.check_history(self.root, base)

        # A later revision must not silently bypass the archived-statement check.
        base = self.commit()
        archived = {**statement, "status": "superseded"}
        archived["statement"] = {**statement["statement"], "text": "Changed archive"}
        self.write(relative.parent / "statements" / (statement["id"] + ".yaml"), archived)
        with self.assertRaisesRegex(manage.InvalidRecord, "Previously archived statements"):
            manage.check_history(self.root, base)

    def test_git_history_reads_utf8_records(self):
        self.git("init", "-q")
        relative = "candidates/observation/example-problem/verification/statement.yaml"
        statement = self.statement()
        statement["statement"]["text"] = "Caf\u00e9 \u6570\u5b66"
        self.write(relative, statement)
        base = self.commit()
        self.assertEqual(yaml.load(manage.git_text(self.root, base, relative), Loader=manage.RecordLoader), statement)

    def test_local_links_and_anchors(self):
        for name in ("README.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md"):
            (self.root / name).write_text("# Title\n")
        # Replace copied templates, whose links are not needed for this isolated test.
        for name in ("citation.md", "recipients.md"):
            (self.entry_dir / name).write_text("# Entry\n")
        target = self.root / "docs/page.md"
        target.write_text("# Café\n\n# Same\n\n# Same\n", encoding="utf-8")
        (self.root / "README.md").write_text("[Unicode heading](docs/page.md#café)\n\n[repeat][ref]\n\n[ref]: docs/page.md#same-1\n", encoding="utf-8")
        manage.check_links(self.root)
        (self.root / "README.md").write_text("[broken](docs/page.md#missing)\n")
        with self.assertRaisesRegex(manage.InvalidRecord, "anchor"):
            manage.check_links(self.root)
        (self.root / "README.md").write_text("[escape](../outside.md)\n")
        with self.assertRaisesRegex(manage.InvalidRecord, "escapes"):
            manage.check_links(self.root)
        (self.root / "README.md").write_text("# Home\n")
        (self.root / "problems").mkdir()
        (self.root / "problems/README.md").write_text("[removed audit](removed-audit.md)\n")
        with self.assertRaisesRegex(manage.InvalidRecord, "broken local link"):
            manage.check_links(self.root)
        (self.root / "problems/README.md").write_text('[custom](catalog.md#JSP-000002)\n')
        catalog = self.root / "problems/catalog.md"
        catalog.write_text('<a id="JSP-000002"></a>\n\n# Problem\n')
        manage.check_links(self.root)
        catalog.write_text('```html\n<a id="JSP-000002"></a>\n```\n')
        with self.assertRaisesRegex(manage.InvalidRecord, "broken heading anchor"):
            manage.check_links(self.root)

    def statement(self):
        return {"id": "STMT-example-problem-v1", "status": "active", "supersedes": None,
                "statement": {"text": "Synthetic statement", "natural_language_source": "Synthetic source",
                              "definitions_reviewed": [], "library": {"name": "test", "commit": "a" * 40, "meets_minimum_safe_version": True}},
                "authorship": {"signatories": ["reviewer-a", "reviewer-b"], "issued_at": "2026-01-01"}}

    def record(self):
        return {
            "repository": "https://example.invalid/proof", "commit": "b" * 40,
            "toolchain": {"language": "Lean", "language_version": "test", "library": "test", "library_commit": "a" * 40, "meets_minimum_safe_version": True},
            "theorems": [{"name": "Synthetic.theorem", "axioms": [], "clean": True}],
            "statement_comparison": {"statement_id": self.statement()["id"], "theorem_name": "Synthetic.theorem", "result": "equivalent",
                                     "basis": "Synthetic comparison", "performed_by": "verifier-a", "performed_at": "2026-01-01"},
            "checkers": [{"name": "checker-a", "version": "test", "current_release": True, "result": "pass"},
                         {"name": "checker-b", "version": "test", "current_release": True, "result": "pass"}],
            "environment": {"arch": "test", "image": "synthetic-image@sha256:" + "a" * 64},
            "sandbox": {"network_disabled": True, "prebuilt_artifacts_ignored": True, "limits": {"time": "60s", "memory": "1GiB"}, "unprivileged_user": True},
            "attribution": {"proof_route": "Synthetic route", "third_party": False, "note_en": ""},
            "artifacts": {"build_log": {"archive": "https://example.invalid/archive", "sha256": "a" * 64, "bytes": 1}},
            "performed_by": "verifier-a", "performed_at": "2026-01-01",
        }

    def prepare_evidence(self):
        for person, role in (("reviewer-a", "curator"), ("reviewer-b", "curator"), ("verifier-a", "verifier")):
            self.profile(person, role)
        self.entry["verification"]["formal"] = {"record": "verification/record.yaml", "statement": "verification/statement.yaml"}
        self.write(self.entry_dir.relative_to(self.root) / "verification/statement.yaml", self.statement())
        self.write(self.entry_dir.relative_to(self.root) / "verification/record.yaml", self.record())

    def prepare_award(self):
        self.git("init", "-q")
        self.prepare_evidence()
        self.profile("recipient-a", "recipient")
        self.entry.update(status="announced", batch="2026-01",
                          decision={"level": 3, "announcement": "https://example.invalid/announcement"},
                          recipients=[{"id": "recipient-a", "affiliation": "", "contribution_en": "Synthetic mathematical contribution",
                                       "confirmation": "https://example.invalid/confirmed"}])
        self.save_entry()
        destination = self.root / "awards/2026-01/example-problem"
        destination.parent.mkdir(parents=True)
        shutil.move(str(self.entry_dir), destination)
        self.entry_dir = destination
        self.write("awards/2026-01/batch.yaml", {"id": "2026-01", "announced_at": "2026-01-02",
                                                "announcement": "https://example.invalid/announcement", "award_ids": ["example-problem"]})

    def test_public_award_and_batch_membership(self):
        self.prepare_award()
        result = manage.generate(self.root)
        self.assertEqual(result["awards"]["items"][0]["entry"]["decision"]["level"], 3)
        self.assertEqual(result["candidates"]["items"], [])
        manage.generate(self.root, check=True)
        batch = manage.read_yaml(self.root / "awards/2026-01/batch.yaml")
        batch["award_ids"] = ["missing-entry"]
        self.write("awards/2026-01/batch.yaml", batch)
        with self.assertRaisesRegex(manage.InvalidRecord, "membership"):
            manage.collect(self.root)

    def test_candidate_cannot_publish_award_decision(self):
        self.entry["decision"] = {"level": 1, "announcement": "https://example.invalid/announcement"}
        self.save_entry()
        with self.assertRaises(manage.InvalidRecord):
            manage.collect(self.root)

    def test_internal_record_fields_rejected(self):
        for key in ("score", "tier", "rules", "payout", "award_structure"):
            with self.subTest(key=key):
                self.entry[key] = {}
                self.save_entry()
                with self.assertRaisesRegex(manage.InvalidRecord, "Additional properties"):
                    manage.collect(self.root)
                del self.entry[key]

    def test_unconfirmed_names_and_duplicate_recipients_rejected(self):
        person = {"id": "unconfirmed-person", "affiliation": "", "contribution_en": "Synthetic contribution", "confirmation": None}
        self.entry["recipients"] = [person]
        with self.assertRaisesRegex(manage.InvalidRecord, "placeholders"):
            manage.check_recipients(self.entry, self.profiles, False)
        person["id"] = "RECIPIENT-example-A"
        self.entry["recipients"] = [person, person.copy()]
        with self.assertRaisesRegex(manage.InvalidRecord, "Duplicate recipient"):
            manage.check_recipients(self.entry, self.profiles, False)

    def test_formal_candidate_needs_statement(self):
        self.entry["verification"]["formal"] = {"record": "verification/record.yaml", "statement": "verification/statement.yaml"}
        self.save_entry()
        with self.assertRaisesRegex(manage.InvalidRecord, "require verification/statement.yaml"):
            manage.collect(self.root)

    def test_confirmed_states_need_verification(self):
        self.entry["status"] = "pending-recipient-confirmation"
        self.save_entry()
        with self.assertRaisesRegex(manage.InvalidRecord, "formal verification evidence"):
            manage.collect(self.root)
        self.prepare_award()
        self.entry["verification"]["formal"] = None
        self.write(self.entry_dir.relative_to(self.root) / "verification/record.yaml", None)
        self.save_entry()
        with self.assertRaisesRegex(manage.InvalidRecord, "formal verification evidence"):
            manage.collect(self.root)

    def test_pending_candidate_accepts_incomplete_evidence(self):
        self.prepare_evidence()
        record = self.record()
        record["statement_comparison"]["result"] = "not-equivalent"
        self.write(self.entry_dir.relative_to(self.root) / "verification/record.yaml", record)
        self.entry["status"] = "under-verification"
        self.save_entry()
        manage.collect(self.root)
        self.entry["status"] = "pending-recipient-confirmation"
        self.save_entry()
        with self.assertRaisesRegex(manage.InvalidRecord, "Non-equivalent"):
            manage.collect(self.root)

    def test_verification_requires_consistent_successful_evidence(self):
        self.prepare_award()
        relative = self.entry_dir.relative_to(self.root) / "verification/record.yaml"
        for section, field, value, message in (
            ("statement_comparison", "result", "not-equivalent", "Non-equivalent"),
            ("statement_comparison", "statement_id", "STMT-other-v1", "reference"),
            ("sandbox", "prebuilt_artifacts_ignored", False, "isolation"),
            ("toolchain", "meets_minimum_safe_version", False, "safe versions"),
            ("toolchain", "library_commit", "c" * 40, "commits differ"),
        ):
            with self.subTest(field=field):
                record = self.record()
                record[section][field] = value
                self.write(relative, record)
                with self.assertRaisesRegex(manage.InvalidRecord, message):
                    manage.collect(self.root)
        record = self.record()
        record["checkers"][1]["name"] = "checker-a"
        record["checkers"][1]["version"] = "another-test-version"
        self.write(relative, record)
        with self.assertRaisesRegex(manage.InvalidRecord, "distinct checkers"):
            manage.collect(self.root)
        self.write(relative, None)
        with self.assertRaisesRegex(manage.InvalidRecord, "completed verification record"):
            manage.collect(self.root)

    def test_published_decision_immutable(self):
        self.prepare_award()
        base = self.commit()
        self.entry["decision"]["level"] = 1
        self.save_entry()
        with self.assertRaisesRegex(manage.InvalidRecord, "Published decisions are immutable"):
            manage.check_history(self.root, base)

    def test_revocation_keeps_original_evidence(self):
        self.prepare_award()
        base = self.commit()
        self.entry.update(status="revoked", revocation={"date": "2026-01-03", "trigger": "Synthetic trigger",
                          "reason_en": "Synthetic test", "evidence": ["https://example.invalid/evidence"], "failure_point": "Synthetic failure"})
        self.save_entry()
        manage.check_history(self.root, base)
        record = self.record()
        record["artifacts"]["build_log"]["bytes"] = 2
        self.write(self.entry_dir.relative_to(self.root) / "verification/record.yaml", record)
        with self.assertRaisesRegex(manage.InvalidRecord, "original verification record"):
            manage.check_history(self.root, base)


if __name__ == "__main__":
    unittest.main()
