"""Check fixture boundaries without running Docker or exposing credentials."""
import importlib.util
from pathlib import Path
import unittest
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


class RuntimeSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('runtime_fixture', ROOT / 'scripts/verify-forgejo-runtime.py')
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_container_has_only_disposable_named_mounts(self):
        args = self.module.container_args('/usr/bin/docker', 'fixture-unique', 'local/image:immutable',
                                          ['fixture-data'], Path('/tmp/fixture.env'), 12345)
        for flag in ['--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                     '--user=1000:1000', '--cpus=1', '--memory=1g', '--publish=127.0.0.1:12345:3000']:
            self.assertIn(flag, args)
        self.assertNotIn('--privileged', args)
        self.assertNotIn('--volume', args)
        mounts = [args[index + 1] for index, value in enumerate(args) if value == '--mount']
        self.assertTrue(all(mount.startswith('type=volume,src=fixture-') for mount in mounts))
        self.assertEqual(len(mounts), 1)
        self.assertIn('dst=/var/lib/gitea', mounts[0])
        self.assertNotIn('/etc/gitea', str(args))

    def test_form_parser_ignores_unchecked_defaults_and_preserves_csrf(self):
        parser = self.module.Inputs()
        parser.feed('<input name="_csrf" value="fixture"><input type="checkbox" name="public">'
                    '<input type="checkbox" name="private" checked><input name="admin_name" value="">')
        self.assertEqual(parser.fields['_csrf'], 'fixture')
        self.assertEqual(parser.fields['private'], 'on')
        self.assertNotIn('public', parser.fields)

    def test_redirect_cannot_send_fixture_to_another_host(self):
        redirect = self.module.LocalRedirect('http://127.0.0.1:12345')
        with self.assertRaisesRegex(ValueError, 'escaped'):
            redirect.redirect_request(None, None, 302, '', {}, 'https://example.invalid/')

    def test_source_never_passes_password_in_docker_or_git_arguments(self):
        source = (ROOT / 'scripts/verify-forgejo-runtime.py').read_text()
        self.assertIn("GIT_CONFIG_VALUE_0='Authorization: ' + auth", source)
        self.assertIn("GIT_CONFIG_GLOBAL='/dev/null'", source)
        self.assertIn("'--pull=never'", source)
        self.assertIn("actual != expected", source)
        self.assertNotIn("print(password", source)
        self.assertNotIn('kubectl', source)
        self.assertNotIn("'--password'", source)


if __name__ == '__main__':
    unittest.main()
