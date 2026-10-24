"""Regression checks for scanner policy and candidate file inclusion."""
import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import io
import tarfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('checks', ROOT / 'scripts/checks.py')
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


class ScannerPolicyTests(unittest.TestCase):
    def test_brotli_exception_is_exact_context_bound_and_expires(self):
        finding = {'VulnerabilityID': 'CVE-2026-111497', 'PkgName': 'brotli-libs',
                   'InstalledVersion': '1.2.0-r1', 'Severity': 'HIGH'}
        report = {'Results': [{'Vulnerabilities': [finding]}]}
        with patch.object(checks, 'DOCS_BROTLI_EXPIRES', checks.datetime.date(2099, 1, 1)):
            self.assertEqual(checks.blocking_image_findings(report, checks.DOCS_BROTLI_IMAGE, True), [])
            self.assertTrue(checks.blocking_image_findings(report, checks.DOCS_BROTLI_IMAGE, False))
            self.assertTrue(checks.blocking_image_findings(report, 'another-image@sha256:' + 'a' * 64, True))
            for field, value in [('Severity', 'CRITICAL'), ('PkgName', 'another-library'),
                                 ('InstalledVersion', 'another-version'), ('VulnerabilityID', 'another-CVE')]:
                altered = dict(finding, **{field: value})
                with self.subTest(field=field):
                    self.assertTrue(checks.blocking_image_findings({'Results': [{'Vulnerabilities': [altered]}]}, checks.DOCS_BROTLI_IMAGE, True))
        with patch.object(checks, 'DOCS_BROTLI_EXPIRES', checks.datetime.date(2000, 1, 1)):
            self.assertTrue(checks.blocking_image_findings(report, checks.DOCS_BROTLI_IMAGE, True))

    def test_brotli_profile_invalidates_execution_or_template_changes(self):
        result = subprocess.run([str(checks.BIN / 'helm'), 'template', 'docs', 'charts/private-app',
                                 '-n', 'docs-dev', '-f', 'apps/docs/values.yaml'], cwd=ROOT,
                                capture_output=True, text=True, check=True)
        self.assertTrue(checks.docs_brotli_profile(result.stdout))
        self.assertFalse(checks.docs_brotli_profile(result.stdout.replace('command: ["nginx"]', 'command: ["curl"]')))
        with patch.object(checks, 'DOCS_PROFILE_FILES', {'apps/docs/values.yaml': '0' * 64}):
            self.assertFalse(checks.docs_brotli_profile(result.stdout))

    def test_staged_secret_is_blocked_and_redacted(self):
        # Deliberately synthetic provider-shaped data; never contact a provider.
        value = 'ghp_' + 'A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'fixture.txt').write_text('github_token=' + value + '\n')
            subprocess.run(['git', '-C', str(root), 'add', 'fixture.txt'], check=True)
            reports = root / 'reports'
            with patch.object(checks, 'ROOT', root), patch.object(checks, 'REPORTS', reports):
                with self.assertRaises(RuntimeError):
                    checks.secrets(staged=True)
            self.assertNotIn(value, (reports / 'secrets-staged.log').read_text())

    def report(self, target, identifier, severity='HIGH'):
        return {'Results': [{'Target': target, 'Misconfigurations': [{'ID': identifier, 'Severity': severity, 'Message': "Container 'gemma4-31b-vllm' of Deployment 'gemma4-31b-vllm' should set 'securityContext.readOnlyRootFilesystem' to true"}]}]}

    def test_real_high_fails(self):
        self.assertTrue(checks.blocking_findings(self.report('docs.yaml', 'KSV-0014'), 'docs'))
        self.assertTrue(checks.blocking_findings(self.report('gemma4-31b.yaml', 'KSV-9999'), 'legacy'))

    def test_legacy_exception_does_not_cover_other_targets(self):
        self.assertTrue(checks.blocking_findings(self.report('another.yaml', 'KSV-0014'), 'legacy'))
        self.assertTrue(checks.blocking_findings(self.report('gemma4-31b.yaml', 'KSV-0014'), 'docs'))

    def test_only_named_legacy_writable_rootfs_is_temporarily_excepted(self):
        with patch.object(checks, 'EXPIRES', checks.datetime.date(2099, 1, 1)):
            report = self.report('gemma4-31b.yaml', 'KSV-0014')
            self.assertEqual(checks.blocking_findings(report, 'legacy'), [])
            report['Results'][0]['Misconfigurations'][0]['Message'] = "Container 'another' needs read-only rootfs"
            self.assertTrue(checks.blocking_findings(report, 'legacy'))
            self.assertTrue(checks.blocking_findings(self.report('gemma4-31b.yaml', 'KSV-0118'), 'legacy'))

    def test_expired_exception_fails_closed(self):
        with patch.object(checks, 'EXPIRES', checks.datetime.date(2000, 1, 1)):
            self.assertTrue(checks.blocking_findings(self.report('gemma4-31b.yaml', 'KSV-0014'), 'legacy'))

    def test_candidate_includes_new_untracked_script(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / '.gitignore').write_text('private/\n')
            (root / 'tracked.py').write_text('print("tracked")\n')
            subprocess.run(['git', '-C', str(root), 'add', '.gitignore', 'tracked.py'], check=True)
            (root / 'new.py').write_text('print("untracked")\n')
            (root / 'private').mkdir()
            (root / 'private/report.json').write_text('{}')
            with patch.object(checks, 'ROOT', root):
                files = checks.candidate_files()
            self.assertIn(Path('tracked.py'), files)
            self.assertIn(Path('new.py'), files)
            self.assertNotIn(Path('private/report.json'), files)

    def test_rendered_images_require_digest_and_are_deduplicated(self):
        ref = 'example.invalid/docs@sha256:' + 'a' * 64
        self.assertEqual(checks.image_references(f'    image: "{ref}"\n    image: {ref}\n'), {ref})
        for text in ['    image: example.invalid/docs:latest', 'kind: ConfigMap']:
            with self.assertRaises(RuntimeError):
                checks.image_references(text)

    def test_production_uses_base_values_and_scans_unique_docs_image(self):
        ref = 'example.invalid/docs@sha256:' + 'a' * 64
        with tempfile.TemporaryDirectory() as temporary:
            calls = []
            def fake_run(name, argv, output=None):
                calls.append(argv)
                if output:
                    output.write_text(f'    image: {ref}\n')
            with patch.object(checks, 'REPORTS', Path(temporary)), patch.object(checks, 'tool', side_effect=lambda name: name), patch.object(checks, 'run', side_effect=fake_run), patch.object(checks, 'misconfig'), patch.object(checks, 'forgejo_record'), patch.object(checks, 'image') as scan:
                checks.manifests()
                scan.assert_called_once_with(ref)
            prod = [args for args in calls if 'apps/docs/values-prod.yaml' in args]
            self.assertEqual(len(prod), 2)
            for args in prod:
                self.assertLess(args.index('apps/docs/values.yaml'), args.index('apps/docs/values-prod.yaml'))

    def test_subprocess_uses_pinned_tool_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(checks, 'REPORTS', Path(temporary)), patch.object(checks.subprocess, 'run') as invoke:
                invoke.return_value.returncode = 0
                checks.run('fixture', ['fixture-tool'])
                self.assertTrue(invoke.call_args.kwargs['env']['PATH'].startswith(str(checks.BIN) + checks.os.pathsep))

    def test_critical_is_never_covered_by_legacy_exception(self):
        self.assertTrue(checks.blocking_findings(self.report('gemma4-31b.yaml', 'KSV-0014', 'CRITICAL'), 'legacy'))

    def test_snapshot_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'secret').write_text('private')
            (root / 'link').symlink_to(root / 'secret')
            with patch.object(checks, 'ROOT', root), patch.object(checks, 'candidate_files', return_value=[Path('link')]):
                with self.assertRaises(RuntimeError):
                    checks.snapshot(root / 'snapshot')

    def test_image_scan_without_packages_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            reports = Path(directory)
            def empty_scan(*args, **kwargs):
                (reports / 'image-aaaaaaaaaaaa.json').write_text(json.dumps({'Results': [{'Class': 'os-pkgs', 'Packages': []}]}))
            with patch.object(checks, 'REPORTS', reports), patch.object(checks, 'tool', return_value='trivy'), patch.object(checks, 'run', side_effect=empty_scan):
                with self.assertRaises(RuntimeError):
                    checks.image('example.invalid/docs@sha256:' + 'a' * 64)

    def test_image_scan_rejects_unsafe_or_unpinned_references(self):
        for reference in ('example.invalid/docs:latest', 'example.invalid/docs@sha256:../../outside'):
            with self.subTest(reference=reference), self.assertRaises(RuntimeError):
                checks.image(reference)

    def test_dependency_scan_without_dev_inventory_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            reports = Path(directory)
            def incomplete_scan(*args, **kwargs):
                (reports / 'dependencies.json').write_text(json.dumps({'dependencies': [{'name': 'project-only', 'vulns': []}]}))
            with patch.object(checks, 'REPORTS', reports), patch.object(checks, 'run', side_effect=incomplete_scan):
                with self.assertRaises(RuntimeError):
                    checks.dependencies()

    def test_misconfiguration_scan_requires_actual_policy_coverage(self):
        reports_to_check = [{}, {'Results': []}, {'Results': [{'Type': 'kubernetes', 'MisconfSummary': {'Successes': 0, 'Failures': 0}}]}]
        with tempfile.TemporaryDirectory() as directory:
            reports = Path(directory)
            for content in reports_to_check:
                with self.subTest(report=content):
                    def empty_scan(*args, **kwargs):
                        (reports / 'fixture.json').write_text(json.dumps(content))
                    with patch.object(checks, 'REPORTS', reports), patch.object(checks, 'tool', return_value='trivy'), patch.object(checks, 'run', side_effect=empty_scan):
                        with self.assertRaises(RuntimeError):
                            checks.misconfig('fixture', reports, 'docs')

    def test_oci_scan_layout_rejects_paths_and_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / 'layout'
            destination.mkdir()
            for name, link in [('../outside', False), ('index.json', True)]:
                archive = root / 'malformed.tar'
                with tarfile.open(archive, 'w') as bundle:
                    item = tarfile.TarInfo(name)
                    if link:
                        item.type = tarfile.SYMTYPE
                        item.linkname = '../outside'
                        bundle.addfile(item)
                    else:
                        item.size = 1
                        bundle.addfile(item, io.BytesIO(b'x'))
                with self.subTest(name=name), self.assertRaises(RuntimeError):
                    checks.prepare_oci_layout(archive, destination)
            self.assertFalse((root / 'outside').exists())
