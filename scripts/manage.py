#!/usr/bin/env python3
"""Validate public records and deterministically build the two data indexes."""

import argparse
import json
import re
import sys
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from markdown_it import MarkdownIt
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_BASE = "https://schemas.example.invalid/awards/"
ROLES = {"recipients": "recipient", "curators": "curator", "verifiers": "verifier"}


class InvalidRecord(ValueError):
    pass


class RecordLoader(yaml.SafeLoader):
    """Preserve ISO dates as strings and reject duplicate mapping keys."""


RecordLoader.yaml_implicit_resolvers = {
    key: [(tag, regex) for tag, regex in values if tag != "tag:yaml.org,2002:timestamp"]
    for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise InvalidRecord("YAML keys must be unique strings")
        result[key] = loader.construct_object(value_node)
    return result


RecordLoader.add_constructor("tag:yaml.org,2002:map", unique_mapping)


def require(condition, message):
    if not condition:
        raise InvalidRecord(message)


def read_yaml(path):
    content = path.read_text(encoding="utf-8")
    # Aliases and custom tags are unnecessary in public records.
    require(not any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(content)),
            f"{path}: YAML aliases are not allowed")
    try:
        return yaml.load(content, Loader=RecordLoader)
    except (yaml.YAMLError, InvalidRecord) as error:
        raise InvalidRecord(f"{path}: {error}") from error


def schema_validators(root):
    schemas = {}
    for path in sorted((root / "data/schema").glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        require(schema.get("$id") == SCHEMA_BASE + path.name,
                f"{path}: unexpected schema ID")
        schemas[path.stem.removesuffix(".schema")] = schema
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()
    )
    return {name: Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
            for name, schema in schemas.items()}


def validate_schema(validators, kind, value, path):
    require(kind in validators, f"Missing schema: {kind}")
    errors = sorted(validators[kind].iter_errors(value), key=lambda error: str(error.path))
    if errors:
        error = errors[0]
        field = ".".join(map(str, error.absolute_path)) or "<root>"
        raise InvalidRecord(f"{path}: {field}: {error.message}")


def english_markdown(path):
    content = path.read_text(encoding="utf-8")
    match = re.search(r"^## English[^\S\n]*\n(.*?)(?=^## |\Z)", content, re.M | re.S)
    require(match and match.group(1).strip(), f"{path}: missing nonempty '## English' section")
    return content


def git_text(root, revision, path):
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "Expected full Git commit SHA")
    command = ["git", "-C", str(root), "show", f"{revision}:{path}"]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
    require(result.returncode == 0, f"Cannot read {path} at {revision}; fetch full Git history")
    return result.stdout


def check_recipients(entry, profiles, awarded):
    recipients = entry["recipients"]
    ids = [person["id"] for person in recipients]
    require(len(ids) == len(set(ids)), "Duplicate recipient IDs")
    for person in recipients:
        placeholder = person["id"].startswith("RECIPIENT-")
        if person["confirmation"] is None:
            require(placeholder, "Unconfirmed recipients must use RECIPIENT-* placeholders")
        if not placeholder:
            require(person["id"] in profiles["recipient"], f"Missing recipient profile: {person['id']}")
        if awarded:
            require(not placeholder and person["confirmation"] is not None, "Awards require written recipient confirmation")


def check_statement(statement, entry, profiles):
    require(statement["id"].startswith(f"STMT-{entry['id']}-v"), "Statement ID must belong to this entry")
    for person in statement["authorship"]["signatories"]:
        require(person in profiles["curator"], f"Unknown statement signatory: {person}")
    for definition in statement["statement"]["definitions_reviewed"]:
        if not definition["from_library"]:
            require(definition["review"].strip(), "Non-library definitions require a published review")


def check_verification(record, statement, entry, profiles, awarded):
    formal = entry["verification"]["formal"]
    confirmed = awarded or entry["status"] == "pending-recipient-confirmation"
    require(not confirmed or formal is not None, "Confirmed records require formal verification evidence")
    require(not formal or statement is not None, "Formal claims require verification/statement.yaml even for candidates")
    require(record is None or formal is not None, "Verification record requires a formal evidence reference")
    if statement:
        check_statement(statement, entry, profiles)
        require(statement["status"] == "active", "statement.yaml must be the active version; archive superseded versions")
    require(not confirmed or record is not None, "Confirmed records require a completed verification record")
    if record is None:
        return
    require(record["performed_by"] in profiles["verifier"], "Unknown verifier in record")
    require(len({checker["name"] for checker in record["checkers"]}) >= 2, "At least two distinct checkers are required")
    comparison = record["statement_comparison"]
    if comparison:
        require(statement and comparison["statement_id"] == statement["id"], "Statement comparison must reference the active statement")
        require(comparison["performed_by"] in profiles["verifier"], "Unknown statement comparison verifier")
        require(comparison["theorem_name"] in {theorem["name"] for theorem in record["theorems"]}, "Compared theorem is not in the axiom audit")
    if confirmed:
        require(comparison is not None, "Confirmed records require statement comparison")
        require(comparison["result"] == "equivalent", "Non-equivalent statement cannot support a confirmed record")
        require(record["toolchain"]["meets_minimum_safe_version"]
                and statement["statement"]["library"]["meets_minimum_safe_version"], "Minimum safe versions must be met")
        require(record["toolchain"]["library_commit"] == statement["statement"]["library"]["commit"], "Statement and proof library commits differ")
        require(all(theorem["clean"] for theorem in record["theorems"]), "Axiom audit must pass")
        require(all(checker["current_release"] and checker["result"] == "pass" for checker in record["checkers"]), "Independent current checkers must pass")
        require(all(record["sandbox"][key] for key in ("network_disabled", "prebuilt_artifacts_ignored", "unprivileged_user")), "All sandbox isolation controls must hold")
    if record["attribution"]["third_party"]:
        require(record["attribution"]["note_en"].strip(), "Third-party attribution requires an English explanation")


def collect(root):
    root = root.resolve()
    managed = [root / name for name in ("awards", "candidates", "people", "data")]
    for directory in managed:
        require(directory.is_dir() and not directory.is_symlink(), f"Missing or symlinked directory: {directory}")
        for path in directory.rglob("*"):
            require(not path.is_symlink(), f"Symlinks are not allowed in records: {path}")
    validators = schema_validators(root)
    consumed = set()

    def load(path, kind):
        require(path.is_file(), f"Missing required file: {path}")
        value = read_yaml(path)
        validate_schema(validators, kind, value, path)
        consumed.add(path)
        return value

    profiles = {role: {} for role in ROLES.values()}
    names = {}
    for directory, role in ROLES.items():
        require((root / "people" / directory).is_dir(), f"Missing people/{directory}")
        for path in sorted((root / "people" / directory).glob("*.yaml")):
            person = load(path, "person")
            require(path.stem == person["id"] and person["role"] == role, f"{path}: profile path/role mismatch")
            require(person["id"] not in names or names[person["id"]] == person["name"], f"{path}: inconsistent public name")
            if role == "recipient":
                require(person["publication_consent"], f"{path}: recipient name publication needs written confirmation")
            names[person["id"]] = person["name"]
            profiles[role][person["id"]] = person
    entries, batches = [], {}
    for directory in sorted((root / "awards").iterdir()):
        if not directory.is_dir():
            continue
        batch = load(directory / "batch.yaml", "batch")
        require(directory.name == batch["id"], f"{directory}: batch ID mismatch")
        batches[batch["id"]] = batch
        entries.extend((path, "awards", batch["id"]) for path in sorted(directory.iterdir()) if path.is_dir())
    for state in ("observation", "verified-pending"):
        directory = root / "candidates" / state
        require(directory.is_dir(), f"Missing directory: {directory}")
        entries.extend((path, state, None) for path in sorted(directory.iterdir()) if path.is_dir())
    actual_batches = {batch_id: [] for batch_id in batches}
    result = {name: {"schema_version": 1, "items": []} for name in ("awards", "candidates")}
    seen = set()
    for directory, pool, batch_id in entries:
        entry = load(directory / "award.yaml", "award")
        require(directory.name == entry["id"], f"{directory}: ID does not match directory")
        require(entry["id"] not in seen, f"Duplicate entry ID: {entry['id']}")
        seen.add(entry["id"])
        awarded = pool == "awards"
        statuses = ("announced", "disputed", "paid", "revoked") if awarded else ("draft", "under-verification", "pending-recipient-confirmation")
        require(entry["status"] in statuses and entry["batch"] == batch_id, f"{directory}: status/batch does not match location")
        if pool == "verified-pending":
            require(entry["status"] != "draft", f"{directory}: formal candidates must have entered verification")
        record = load(directory / "verification/record.yaml", "verification-record")
        statement_path = directory / "verification/statement.yaml"
        statement = load(statement_path, "statement") if statement_path.exists() else None
        try:
            check_recipients(entry, profiles, awarded)
            check_verification(record, statement, entry, profiles, awarded)
        except InvalidRecord as error:
            raise InvalidRecord(f"{directory}: {error}") from error
        if awarded:
            actual_batches[batch_id].append(entry["id"])
            if record:
                require(record["performed_at"] <= batches[batch_id]["announced_at"], "Verification must precede announcement")
        history = directory / "verification/statements"
        historical_ids = set()
        for path in sorted(history.glob("*.yaml")):
            old = load(path, "statement")
            require(old["status"] == "superseded" and path.stem == old["id"], f"{path}: archived statement must be superseded and named by ID")
            require(old["id"].startswith(f"STMT-{entry['id']}-v"), f"{path}: historical statement belongs to another entry")
            historical_ids.add(old["id"])
        if historical_ids:
            require(statement is not None, f"{directory}: superseded statements need an active replacement")
        if statement and statement["supersedes"]:
            require(statement["supersedes"] in historical_ids, f"{directory}: superseded statement is missing")
        output = {"entry": entry, "source_path": directory.relative_to(root).as_posix(),
                  "citation": english_markdown(directory / "citation.md"),
                  "recipients": english_markdown(directory / "recipients.md"),
                  "verification_record": record, "statement": statement}
        result["awards" if awarded else "candidates"]["items"].append(output)
    for batch_id, batch in batches.items():
        require(sorted(batch["award_ids"]) == sorted(actual_batches[batch_id]), f"awards/{batch_id}: batch membership mismatch")
    for directory in managed[:3]:
        for path in directory.rglob("*"):
            if path.suffix in (".yaml", ".yml"):
                require(path in consumed, f"Unexpected or misplaced YAML record: {path}")
    for name, output in result.items():
        output["items"].sort(key=lambda record: record["entry"]["id"])
        validate_schema(validators, name, output, f"generated {name}.json")
    return result


def check_history(root, base):
    outputs = collect(root)
    current = {item["entry"]["id"]: item for output in outputs.values() for item in output["items"]}
    require(re.fullmatch(r"[0-9a-f]{40}", base), "History base must be a full commit SHA")
    listed = subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "--name-only", base, "--", "awards", "candidates"],
                            capture_output=True, text=True, encoding="utf-8", check=False)
    require(listed.returncode == 0, "Unable to read comparison commit; fetch full history")
    old_paths = set(listed.stdout.splitlines())
    for path in sorted(old_paths):
        if not path.endswith("/award.yaml"):
            continue
        old = yaml.load(git_text(root, base, path), Loader=RecordLoader)
        new = current.get(old["id"])
        if old["status"] in ("announced", "disputed", "paid", "revoked"):
            require(new and new["source_path"].startswith("awards/"), "Historical awards must not be deleted or moved to candidates")
        if not new:
            continue
        if old["status"] in ("announced", "disputed", "paid", "revoked"):
            require(new["entry"]["decision"] == old["decision"], "Published decisions are immutable; record a reviewed correction separately")
        if new["entry"]["status"] == "revoked":
            for key in ("decision", "recipients", "verification"):
                require(old[key] == new["entry"][key], f"Revocation must preserve original {key}")
            old_record = yaml.load(git_text(root, base, (Path(path).parent / "verification/record.yaml").as_posix()), Loader=RecordLoader)
            require(old_record == new["verification_record"], "Revocation must preserve the original verification record")
        # Git tree paths always use '/', even when the checkout is on Windows.
        old_statement_path = (Path(path).parent / "verification/statement.yaml").as_posix()
        # Previously archived statements remain immutable after further replacements.
        prefix = (Path(path).parent / "verification/statements").as_posix() + "/"
        for archived_old_path in sorted(p for p in old_paths if p.startswith(prefix) and p.endswith(".yaml")):
            prior = yaml.load(git_text(root, base, archived_old_path), Loader=RecordLoader)
            current_archive = root / new["source_path"] / "verification/statements" / Path(archived_old_path).name
            require(current_archive.is_file() and read_yaml(current_archive) == prior,
                    "Previously archived statements must not be changed or deleted")
        if old_statement_path not in old_paths:
            continue
        old_statement = yaml.load(git_text(root, base, old_statement_path), Loader=RecordLoader)
        active = new["statement"]
        require(active, "Published statement cannot be deleted")
        if old_statement["id"] == active["id"]:
            require(old_statement == active, "Published statements are immutable; supersede with a new ID")
        else:
            archived_path = root / new["source_path"] / "verification/statements" / (old_statement["id"] + ".yaml")
            require(archived_path.is_file(), "Superseded statement must be retained")
            archived = read_yaml(archived_path)
            require(archived == {**old_statement, "status": "superseded"}, "Archived statement content must be preserved")
            require(active["supersedes"] == old_statement["id"], "Replacement must link the previous statement")


def serialize(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def generate(root, check=False):
    outputs = collect(root)
    for name, value in outputs.items():
        path = root / "data" / f"{name}.json"
        expected = serialize(value)
        if check:
            require(path.is_file() and path.read_text(encoding="utf-8") == expected,
                    f"{path}: stale or missing; run python scripts/manage.py build")
        else:
            path.write_text(expected, encoding="utf-8")
    return outputs


def markdown_tokens(path):
    return MarkdownIt("commonmark").parse(path.read_text(encoding="utf-8"))


class AnchorParser(HTMLParser):
    """Read explicit anchor IDs without interpreting or executing HTML."""

    def __init__(self):
        super().__init__()
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if value and (name == "id" or (tag == "a" and name == "name")):
                self.ids.add(value)


def heading_ids(path):
    tokens = markdown_tokens(path)
    used = set()
    explicit = AnchorParser()
    for index, token in enumerate(tokens):
        for item in (token, *(token.children or [])):
            if item.type in ("html_block", "html_inline"):
                explicit.feed(item.content)
        if token.type != "heading_open":
            continue
        children = tokens[index + 1].children or []
        title = "".join(child.content for child in children if child.type in ("text", "code_inline", "image"))
        slug = re.sub(r"[^\w\-\s]", "", title.lower()).replace(" ", "-")
        candidate, suffix = slug, 0
        while candidate in used:
            suffix += 1
            candidate = f"{slug}-{suffix}"
        used.add(candidate)
    return used | explicit.ids


def check_links(root):
    """Check Markdown local paths and heading anchors; never fetch submitted URLs."""
    root = root.resolve()
    files = [root / name for name in ("README.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md")]
    for directory in ("docs", "problems", "awards", "candidates", "people", "data", ".github"):
        files.extend(sorted((root / directory).rglob("*.md")))
    anchors = {}
    for path in files:
        require(not path.is_symlink(), f"Symlinked Markdown file: {path}")
        for token in markdown_tokens(path):
            for child in token.children or []:
                target = child.attrGet("href") if child.type == "link_open" else child.attrGet("src") if child.type == "image" else None
                if target is None:
                    continue
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc:
                    require(parsed.scheme in ("https", "http", "mailto"), f"{path}: unsupported link {target}")
                    continue
                local = unquote(parsed.path)
                dest = ((root / local.lstrip("/")) if local.startswith("/") else (path.parent / local)).resolve() if local else path.resolve()
                require(dest.is_relative_to(root), f"{path}: link escapes repository: {target}")
                require(dest.exists(), f"{path}: broken local link: {target}")
                if parsed.fragment and dest.suffix == ".md":
                    if dest not in anchors:
                        anchors[dest] = heading_ids(dest)
                    require(unquote(parsed.fragment) in anchors[dest], f"{path}: broken heading anchor: {target}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "build", "check", "links", "history"))
    parser.add_argument("--base", help="Full comparison commit for history checks")
    args = parser.parse_args()
    try:
        if args.command == "history":
            require(args.base, "history requires --base")
            check_history(ROOT, args.base)
        elif args.command == "links":
            check_links(ROOT)
        elif args.command == "validate":
            collect(ROOT)
        else:
            generate(ROOT, check=args.command == "check")
    except (InvalidRecord, OSError, yaml.YAMLError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"{args.command}: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
