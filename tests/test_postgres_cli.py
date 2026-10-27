"""Credential and deployment guards without cluster changes."""
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('postgres_cli',ROOT/'scripts/postgres.py')
pg=importlib.util.module_from_spec(spec)
spec.loader.exec_module(pg)

class PostgreSQLCLI(unittest.TestCase):
    def test_foreign_namespace_refuses_changes(self):
        with patch.object(pg,'image_record',return_value=(Path('/fixture'),{'imageRef':'local.thedefiant/postgres@sha256:'+'a'*64})), patch.object(pg,'run'), patch.object(pg,'get',return_value={'metadata':{'name':'postgres-dev'}}), patch.object(pg,'k') as mutate:
            with self.assertRaisesRegex(pg.Refusal,'unmanaged PostgreSQL namespace'):pg.deploy()
            mutate.assert_not_called()

    def test_existing_data_without_credentials_is_never_reset(self):
        namespace={'metadata':pg.metadata('postgres-dev')}
        claim={'metadata':pg.metadata('postgres-data'),'spec':{'storageClassName':'local-path','resources':{'requests':{'storage':'20Gi'}}}}
        def lookup(kind,name):return namespace if kind=='namespace' else claim if kind=='pvc' else {}
        with patch.object(pg,'image_record',return_value=(Path('/fixture'),{'imageRef':'local.thedefiant/postgres@sha256:'+'a'*64})), patch.object(pg,'run'), patch.object(pg,'get',side_effect=lookup), patch.object(pg,'k') as mutate:
            with self.assertRaisesRegex(pg.Refusal,'no credentials'):pg.deploy()
            mutate.assert_not_called()

    def test_new_secret_uses_stdin_and_does_not_print_passwords(self):
        calls=[]
        with patch.object(pg,'image_record',return_value=(Path('/fixture'),{'imageRef':'local.thedefiant/postgres@sha256:'+'a'*64})), patch.object(pg,'run'), patch.object(pg,'get',return_value={}), patch.object(pg,'KUBE',['k3s','kubectl','--kubeconfig','/private/config']), patch.object(pg,'k',side_effect=lambda *a,**kw: 'dev|dev' if a[0]=='exec' else calls.append((a,kw))), contextlib.redirect_stdout(io.StringIO()) as output:
            pg.deploy()
        manifests=[json.loads(kw['data']) for _,kw in calls]
        namespace=next(m for m in manifests if m['kind']=='Namespace')
        self.assertNotIn('namespace',namespace['metadata'])
        secret=next(m for m in manifests if m['kind']=='Secret')
        for password in secret['stringData'].values():
            self.assertGreaterEqual(len(password),32)
            self.assertNotIn(password,output.getvalue())
            self.assertNotIn(password,str([args for args,_ in calls]))

    def test_private_pgpass_supports_colons_and_never_prints_password(self):
        password='fixture:password-with-enough-length-to-validate'
        with tempfile.TemporaryDirectory() as directory, patch.object(pg,'ROOT',Path(directory)), patch.object(pg,'credentials',return_value={'dev-password':password}), contextlib.redirect_stdout(io.StringIO()) as output:
            pg.connection(5432)
            path=Path(directory)/'.dev-tools/postgres/client/pgpass'
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertIn('fixture\\:password',path.read_text())
            self.assertNotIn(password,output.getvalue())

    def test_weak_or_malformed_existing_password_is_refused(self):
        secret={'metadata':pg.metadata('postgres-credentials'),'data':{'postgres-password':base64.b64encode(b'short').decode(),'dev-password':base64.b64encode(b'short').decode()}}
        with patch.object(pg,'get',return_value=secret):
            with self.assertRaisesRegex(pg.Refusal,'Invalid stored'):pg.credentials()

    def test_invalid_port_never_contacts_cluster(self):
        with patch.object(pg.sys,'argv',['postgres.py','access','--port','80']), patch.object(pg,'client') as client, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(pg.main(),1)
            client.assert_not_called()

if __name__=='__main__':unittest.main()
