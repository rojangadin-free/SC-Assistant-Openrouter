"""Capture an untouched baseline, preserve EOLs, and verify a portable patch."""
import argparse
import difflib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = ['config.py', 'rag/chain.py', 'store_index.py', 'sc_assistant/admin.py',
         'requirements.txt', 'Dockerfile', 'README.md', 'tests/test_docx_ingestion.py', 'tests/test_activity.py']
NEW = ['rag/local_index.py', 'tests/test_local_index.py',
       'tools/compare_vector_backends.py', 'tools/export_local_index.py', 'tools/verify_local_backend.py']


def run(args, cwd, path, env=None):
    result = subprocess.run(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    path.write_bytes(result.stdout)
    print(f'{path.name}: exit {result.returncode}', flush=True)
    return result.returncode


def tests(root, output):
    env = dict(os.environ, PYTHONIOENCODING='utf-8', STORE_BACKEND='file')
    required = ['test_query_intent.py', 'test_source_policy.py', 'test_query_optimizer.py',
                'test_deans_list.py', 'test_unit_identity.py', 'test_newer_retrieval.py']
    run([sys.executable, '-m', 'pytest', *['tests/' + x for x in required], '-q'],
        root, output / 'required.txt', env)
    # Many historical suites exit during import; isolate them as the repo runner does.
    failures = []
    for file in sorted((root / 'tests').glob('test_*.py')):
        code = run([sys.executable, str(file)], root, output / (file.stem + '.txt'), env)
        if code:
            failures.append(file.name)
    (output / 'failures.json').write_text(json.dumps(failures, indent=2))
    for i in range(5):
        run([sys.executable, '-m', 'pytest', 'tests/test_newer_retrieval.py', '-q',
             '-k', 'concurrent_requests_do_not_leak_filters_or_mutate_settings'],
            root, output / f'concurrency-{i + 1}.txt', env)
        if (root / 'tests/test_local_index.py').exists():
            run([sys.executable, '-m', 'pytest', 'tests/test_local_index.py', '-q',
                 '-k', 'concurrent_search_reload_observes_only_complete_generations'],
                root, output / f'local-reload-{i + 1}.txt', env)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['baseline', 'after', 'eol', 'patch', 'probes', 'summary'])
    parser.add_argument('--workspace', required=True, type=Path)
    args = parser.parse_args()
    workspace = args.workspace
    workspace.mkdir(parents=True, exist_ok=True)
    pristine = workspace / 'pristine'
    if args.action == 'baseline':
        if pristine.exists():
            raise SystemExit('Baseline already exists; refusing to overwrite.')
        shutil.copytree(ROOT, pristine, ignore=shutil.ignore_patterns(
            '.git', 'node_modules', '__pycache__', '.pytest_cache', '.env', '*.log',
            '*.sqlite3*', '.kilo', '.playwright-mcp'))
        # Preserve operator secrets outside the deliverable, for offline baseline imports.
        if (ROOT / '.env').exists():
            shutil.copyfile(ROOT / '.env', pristine / '.env')
        output = workspace / 'baseline'
        output.mkdir()
        tests(pristine, output)
    elif args.action == 'after':
        output = workspace / 'after'
        output.mkdir(exist_ok=True)
        tests(ROOT, output)
    elif args.action == 'eol':
        for name in FILES:
            old = (pristine / name).read_bytes().splitlines(keepends=True)
            new = (ROOT / name).read_bytes().splitlines(keepends=True)
            matcher = difflib.SequenceMatcher(a=[x.rstrip(b'\r\n') for x in old],
                                             b=[x.rstrip(b'\r\n') for x in new], autojunk=False)
            restored = []
            for tag, i, j, k, l in matcher.get_opcodes():
                if tag == 'equal':
                    restored.extend(old[i:j])
                else:
                    nearby = old[min(i, len(old) - 1)]
                    ending = b'\r\n' if nearby.endswith(b'\r\n') else b'\n'
                    restored.extend(x.rstrip(b'\r\n') + ending for x in new[k:l])
            (ROOT / name).write_bytes(b''.join(restored))
            data = (ROOT / name).read_bytes()
            print(name, 'CRLF', data.count(b'\r\n'), 'LF', data.count(b'\n') - data.count(b'\r\n'))
    elif args.action == 'summary':
        comparison = json.loads((workspace / 'comparison.json').read_text(encoding='utf-8'))
        lines = [f"Quality comparison: {comparison['parity']}"]
        for name, operation in comparison['operations'].items():
            rankings = operation['rankings']
            equal = sum(row['ordered_ids_equal'] and row['text_equal'] and
                        row['metadata_without_scores_equal'] for row in rankings)
            if equal != len(rankings) or name.endswith('/retrieve_documents') or name == 'multi_query/count10':
                lines.append(f'{name}: {equal}/{len(rankings)} rank/text/metadata matches; '
                             f'min overlap {min(row["overlap_count"] for row in rankings)}')
            if name.endswith('/retrieve_documents') or name == 'multi_query/count10':
                for mode in ('cold', 'warm'):
                    a = operation['left_timings'][mode]['successful']
                    b = operation['right_timings'][mode]['successful']
                    lines.append(f'  {mode}: Pinecone p50/p95 {a["p50_ms"]:.2f}/{a["p95_ms"]:.2f} ms; '
                                 f'local {b["p50_ms"]:.2f}/{b["p95_ms"]:.2f} ms')
        baseline = json.loads((workspace / 'baseline/failures.json').read_text())
        after = json.loads((workspace / 'after/failures.json').read_text())
        lines.extend([f'Baseline failing scripts: {baseline}', f'After failing scripts: {after}',
                      f'New failing scripts: {sorted(set(after) - set(baseline))}'])
        text = '\n'.join(lines) + '\n'
        (workspace / 'verification-summary.txt').write_text(text, encoding='utf-8')
        print(text)
    elif args.action == 'probes':
        code = (
            "import runpy,sys; sys.path.insert(0,'tools'); import aws.dynamodb; "
            "aws.dynamodb.get_file_upload_dates=lambda:{}; "
            "sys.argv=['tools/probe_retrieval.py','--full',"
            "'does samar college allow partial payment?',"
            "'what do i do if i got dropped?','classroom location']; "
            "runpy.run_path('tools/probe_retrieval.py',run_name='__main__')"
        )
        for backend, root in [('pinecone', pristine), ('local', ROOT)]:
            env = dict(os.environ, VECTOR_BACKEND=backend, STORE_BACKEND='file',
                       RERANKER_ENABLED='false', AWS_EC2_METADATA_DISABLED='true',
                       PYTHONIOENCODING='utf-8')
            run([sys.executable, '-c', code], root, workspace / f'probe-{backend}.txt', env)
    else:
        patch = bytearray()
        for name in FILES + NEW:
            old_path = pristine / name
            old = old_path.read_bytes().splitlines(keepends=True) if old_path.exists() and name not in NEW else []
            new = (ROOT / name).read_bytes().splitlines(keepends=True)
            patch.extend(b''.join(difflib.diff_bytes(difflib.unified_diff, old, new,
                fromfile=('a/' + name).encode() if old else b'/dev/null',
                tofile=('b/' + name).encode())))
        patch_path = workspace / 'local-backend.patch'
        patch_path.write_bytes(patch)
        fresh = workspace / 'patched-copy'
        if fresh.exists():
            shutil.rmtree(fresh)
        shutil.copytree(pristine, fresh)
        for name in NEW:
            (fresh / name).unlink(missing_ok=True)
        patch_exe = shutil.which('patch')
        if not patch_exe:
            candidate = Path('C:/Program Files/Git/usr/bin/patch.exe')
            patch_exe = str(candidate) if candidate.exists() else None
        if not patch_exe:
            raise SystemExit('GNU patch unavailable; patch written, verification NOT performed.')
        for extra, log in [(['--dry-run'], 'patch-dry-run.txt'), ([], 'patch-apply.txt')]:
            if run([patch_exe, '-p1', '--binary', *extra, '-i', str(patch_path)],
                   fresh, workspace / log):
                raise SystemExit('Patch verification failed.')
        for name in FILES + NEW:
            if (fresh / name).read_bytes() != (ROOT / name).read_bytes():
                raise SystemExit(f'Patched bytes differ from the working copy: {name}')
        print('Patched files match the working copy byte-for-byte, including line endings.')
        output = workspace / 'patched-tests'
        output.mkdir(exist_ok=True)
        tests(fresh, output)
        artifact_root = ROOT / 'local_index'
        if (artifact_root / 'manifest.json').exists():
            manifest = json.loads((artifact_root / 'manifest.json').read_text(encoding='utf-8'))
            names = ['manifest.json'] + [f"generations/{manifest['generation']}/{name}"
                                        for name in manifest['files']]
            bundle = workspace / 'local-index-artifacts.zip'
            with zipfile.ZipFile(bundle, 'w', zipfile.ZIP_DEFLATED) as archive:
                for name in names:
                    archive.write(artifact_root / name, 'local_index/' + name)
            print(f'Runtime binary artifacts: {bundle}')


if __name__ == '__main__':
    main()
