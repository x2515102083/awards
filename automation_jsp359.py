"""Prepare a single catalog entry, validate externally, then publish a clean branch.

This builder is deliberately NOT an ancestor of the submission commit. The final
commit's parent is the pinned official base and its only changed file is catalog.
No existing branch, award record, eligibility field or official CI gate is changed.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

REPO = 'x2515102083/awards'
BASE = 'ff33abd13163e789790eb1014e55f57c05f94432'
BASE_TREE = 'd873b8a2ddde88592a92d1392bf0d89ee0565b6e'
CATALOG = 'problems/catalog-0301-0400.md'
CATALOG_BLOB = '7a52e65bc7633cf21950153ccb3edcdc626f4ef6'
BRANCH = 'submission/jsp-000359-real-20260918'
PROOF_REPO = 'x2515102083/erdos339-upper-density'
PROOF = '096a9c5692817f5c10aeca2df04fa479e66eb718'
RUN = 35272506165
SOURCE = f'https://github.com/{PROOF_REPO}/blob/{PROOF}'
PRIOR = '8822f7ddef30fadbd92e1c6ab4ed897af356af5e'


def public_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'jsp359-catalog-reference'})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def check_evidence() -> None:
    run = public_json(f'https://api.github.com/repos/{PROOF_REPO}/actions/runs/{RUN}')
    if (run['head_sha'], run['status'], run['conclusion']) != (PROOF, 'completed', 'success'):
        raise RuntimeError('Pinned proof run is not completed and successful')
    for name, expected in {
        'Proof.lean': 'dee6537d95c2abbcdf0c4ff3c8ba080a65fcbdcbc500fe852dd9fcfd0cd613cc',
        'Cutoff.lean': '7c345331ef04800a1cc21bf75ee5bb7329990fca97d676ac6ac031fa53cb757e',
    }.items():
        with urllib.request.urlopen(f'https://raw.githubusercontent.com/{PROOF_REPO}/{PROOF}/{name}', timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError(f'Proof source hash mismatch: {name}')
        print('PROOF_HASH_VERIFIED', name, expected, flush=True)
    print('SUCCESSFUL_PROOF_RUN_VERIFIED', RUN, PROOF, flush=True)


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def patched_catalog(original: bytes) -> bytes:
    actual = hashlib.sha1(b'blob ' + str(len(original)).encode() + b'\0' + original).hexdigest()
    if actual != CATALOG_BLOB:
        raise RuntimeError('Catalog base blob does not match reviewed version')
    text = original.decode('utf-8')
    start = text.index('<a id="JSP-000359"></a>')
    end = text.index('<a id="JSP-000360"></a>', start)
    entry = text[start:end]
    if entry.count('| Lean proof | No |') != 1 or entry.count('| Current status | Solved |') != 1:
        raise RuntimeError('Unexpected catalog status or duplicate entry')
    lean = (
        f'| Lean proof | Yes. [Complete real-threshold proof]({SOURCE}/Proof.lean), theorem '
        '`Erdos440Real.complete_original_problem`: square-root counting bound, sharp universal limsup '
        'and an attaining sequence, and sharp universal liminf with an attaining sequence. '
        f'Branch `research/prize-lcm-20260918`; commit `{PROOF}`. '
        f'[Build and audit instructions]({SOURCE}/README.md): Lean 4.33.0, '
        'Mathlib `db584cd6d46c92f209a44c0f1c829460d327499d`, committed transitive lockfile. '
        'The counting set is proved finite and equal to the unrestricted qualifying-index set. '
        'Maintainer completeness and attribution review pending. |'
    )
    attribution = (
        '| Attribution basis | Historical mathematics: Erdős and Szemerédi (1980). '
        f'The natural-threshold sharp constants and sharp sequence are reused unchanged from '
        f'[plby/lean-proofs](https://github.com/plby/lean-proofs/blob/{PRIOR}/src/latest/ErdosProblems/Erdos440.lean), '
        f'commit `{PRIOR}`, whose source credits Codex and GPT-5.6 Sol. '
        'New real/natural threshold-transfer proofs, strict finite-cutoff proofs and integration: '
        f'x2515102083, with ChatGPT assistance; see [scope and attribution]({SOURCE}/ATTRIBUTION.md). '
        'Prior related intake: [issue #22](https://github.com/TheJustinSunPrize/awards/issues/22) '
        'and [PR #34](https://github.com/TheJustinSunPrize/awards/pull/34). '
        'This increment does not claim the first mathematical solution or first Lean solution, '
        'nor authorship of the reused core. |'
    )
    updated = entry.replace('| Lean proof | No |', lean + '\n' + attribution)
    lines = updated.splitlines(keepends=True)
    found = 0
    for i, line in enumerate(lines):
        if line.startswith('| Publication details |'):
            found += 1
            suffix = (
                f'<br>[Real-threshold integration and finite refinement]({SOURCE}/README.md): '
                '`Erdos440Real.real_limsup_eq_nat` and `Erdos440Real.real_liminf_eq_nat` preserve both extrema; '
                f'[Cutoff.lean]({SOURCE}/Cutoff.lean), `Erdos440Cutoff.strict_cutoff` and '
                '`Erdos440Cutoff.square_threshold`, provide strict finite estimates. '
                f'[Successful submitter-operated verification](https://github.com/{PROOF_REPO}/actions/runs/{RUN}) '
                'checks ten public theorems against the standard axiom allowlist and runs negative controls. '
                'This is not independent-operator or designated isolated verification, a priority certificate, or an award decision. '
            )
            lines[i] = line.rstrip('\n').removesuffix('|').rstrip() + suffix + '|\n'
    if found != 1:
        raise RuntimeError('Unexpected publication row count')
    updated = ''.join(lines)
    allowed = {'Lean proof', 'Attribution basis', 'Publication details'}
    def unchanged_rows(s: str) -> list[str]:
        return [line for line in s.splitlines() if not (line.startswith('| ') and line.split('|')[1].strip() in allowed)]
    if unchanged_rows(entry) != unchanged_rows(updated):
        raise RuntimeError('Attempt to change a non-permitted catalog field')
    return (text[:start] + updated + text[end:]).encode('utf-8')


def api(path: str, payload: dict | None = None) -> dict:
    if not path.startswith(f'/repos/{REPO}/git/'):
        raise ValueError('Writes are restricted to the submitting fork Git database')
    req = urllib.request.Request('https://api.github.com' + path,
        data=None if payload is None else json.dumps(payload).encode(),
        method='GET' if payload is None else 'POST', headers={
            'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
            'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
            'User-Agent': 'jsp359-catalog-reference', 'X-GitHub-Api-Version': '2022-11-28'})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in {'prepare', 'publish'}:
        raise SystemExit('Usage: automation_jsp359.py prepare|publish checkout-directory')
    mode, root = sys.argv[1], Path(sys.argv[2]).resolve()
    if os.environ.get('GITHUB_REPOSITORY') != REPO or git(root, 'rev-parse', 'HEAD') != BASE:
        raise RuntimeError('Wrong fork or checkout base')
    original = subprocess.check_output(['git', '-C', str(root), 'show', f'{BASE}:{CATALOG}'])
    expected = patched_catalog(original)
    target = root / CATALOG
    if mode == 'prepare':
        check_evidence()
        if target.read_bytes() != original:
            raise RuntimeError('Refuse to overwrite an unexpected working tree change')
        target.write_bytes(expected)
        print('CATALOG_PATCH_PREPARED', CATALOG, flush=True)
        return
    if (root.parent / 'catalog-validation.txt').read_text() != 'VALIDATION_PASS\n':
        raise RuntimeError('Validation marker is missing')
    if target.read_bytes() != expected or git(root, 'diff', '--name-only') != CATALOG:
        raise RuntimeError('Only the exact reviewed catalog patch can be published')
    check_evidence()
    base = api(f'/repos/{REPO}/git/commits/{BASE}')
    if base['tree']['sha'] != BASE_TREE:
        raise RuntimeError('Unexpected official base tree')
    blob = api(f'/repos/{REPO}/git/blobs', {'content': expected.decode('utf-8'), 'encoding': 'utf-8'})
    tree = api(f'/repos/{REPO}/git/trees', {'base_tree': BASE_TREE, 'tree': [
        {'path': CATALOG, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']}]})
    commit = api(f'/repos/{REPO}/git/commits', {
        'message': 'JSP-000359: reference audited real-threshold sharp limits and finite cutoff; retain prior attribution',
        'tree': tree['sha'], 'parents': [BASE]})
    # Creation only: an existing branch is an error, never force-updated.
    api(f'/repos/{REPO}/git/refs', {'ref': f'refs/heads/{BRANCH}', 'sha': commit['sha']})
    print('CATALOG_VALIDATION_PASS', flush=True)
    print('SUBMISSION_BRANCH=' + BRANCH, flush=True)
    print('SUBMISSION_COMMIT=' + commit['sha'], flush=True)
    print('SUBMISSION_TREE=' + tree['sha'], flush=True)
    print('CHANGED_FILE=' + CATALOG, flush=True)
    print('PARENT_IS_OFFICIAL_BASE=' + BASE, flush=True)
    print('No workflow, source proof, build file, private contact or eligibility change in submission commit.', flush=True)

if __name__ == '__main__':
    main()
