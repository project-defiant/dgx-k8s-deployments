"""The existing docs service remains the single private hostname entry point."""
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


def render(*extra, template=None):
    args = ['helm', 'template', 'docs', 'charts/private-app', '--namespace', 'docs-dev', '-f', 'apps/docs/values.yaml']
    if template:
        args += ['--show-only', f'templates/{template}.yaml']
    return subprocess.run(args + list(extra), cwd=ROOT, capture_output=True, text=True, check=False)


def nginx_config():
    text = (ROOT / 'apps/docs/values.yaml').read_text()
    return text.split('    nginx.conf: |\n', 1)[1].split('    index.html: |\n', 1)[0]


def servers(config):
    # Isolate server scopes so the default host cannot accidentally inherit proxying.
    blocks = []
    for match in re.finditer(r'\bserver\s*\{', config):
        start = match.end()
        depth = 1
        for index in range(start, len(config)):
            depth += (config[index] == '{') - (config[index] == '}')
            if depth == 0:
                blocks.append(config[start:index])
                break
    return blocks


class GatewayContract(unittest.TestCase):
    def test_stable_service_and_identity_do_not_change(self):
        result = render(template='service')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('name: docs', result.stdout)
        self.assertIn('app.kubernetes.io/instance: docs', result.stdout)
        self.assertIn('type: ClusterIP', result.stdout)
        for forbidden in ['NodePort', 'LoadBalancer', 'gateway']:
            self.assertNotIn(forbidden, result.stdout)

    def test_host_routing_rejects_unknown_hosts_and_keeps_local_docs(self):
        blocks = servers(nginx_config())
        self.assertEqual(len(blocks), 4)
        default, docs, forgejo, ollama = blocks
        self.assertIn('listen 8080 default_server;', default)
        self.assertIn('location / { return 404; }', default)
        self.assertIn('location = /healthz { return 200', default)
        self.assertNotIn('proxy_pass', default)
        self.assertIn('server_name docs.spark.test localhost 127.0.0.1;', docs)
        self.assertIn('root /www;', docs)
        self.assertIn('location / { try_files $uri $uri/ =404; }', docs)
        self.assertIn('server_name git.spark.test;', forgejo)
        self.assertIn('set $forgejo http://forgejo.forgejo.svc.cluster.local:3000;', forgejo)
        self.assertIn('proxy_pass $forgejo$request_uri;', forgejo)
        self.assertIn('resolver 10.85.0.10 valid=30s ipv6=off;', nginx_config())
        result = render(template='deployment')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('path: "/healthz"', result.stdout)

    def test_git_paths_streaming_and_headers_are_preserved_safely(self):
        config = nginx_config()
        for expected in ['merge_slashes off;', 'proxy_pass $forgejo$request_uri;',
                         'proxy_set_header Host $http_host;',
                         'proxy_set_header X-Forwarded-Host $http_host;',
                         'proxy_set_header X-Forwarded-For $remote_addr;',
                         'proxy_request_buffering off;', 'client_max_body_size 256m;',
                         'proxy_set_header X-WEBAUTH-USER "";',
                         'proxy_set_header Remote-User "";',
                         'add_header X-Content-Type-Options nosniff always;',
                         'server_tokens off;']:
            self.assertIn(expected, config)
        self.assertNotIn('$proxy_add_x_forwarded_for', config)
        self.assertNotIn('proxy_set_header Authorization', config)

    def test_large_git_uploads_have_a_bounded_host_specific_limit(self):
        default, docs, forgejo, ollama = servers(nginx_config())
        self.assertIn('client_max_body_size 40g;', forgejo)
        self.assertNotIn('client_max_body_size 40g;', default)
        self.assertNotIn('client_max_body_size 40g;', docs.split('location ^~ /ollama/', 1)[0])
        self.assertIn('client_max_body_size 256m;', nginx_config())
        self.assertIn('proxy_request_buffering off;', forgejo)
        self.assertIn('proxy_http_version 1.1;', forgejo)

    def test_backend_egress_requires_both_namespace_and_pod_and_port(self):
        result = render(template='networkpolicy')
        self.assertEqual(result.returncode, 0, result.stderr)
        policy = result.stdout
        self.assertIn('ingress: []', policy)
        self.assertIn('kubernetes.io/metadata.name: "forgejo"', policy)
        self.assertIn('app.kubernetes.io/instance: "forgejo"', policy)
        self.assertIn('port: 3000', policy)
        self.assertIn('k8s-app: kube-dns', policy)
        self.assertNotIn('ipBlock:', policy)
        self.assertNotIn('namespaceSelector: {}', policy)
        self.assertNotIn('podSelector: {}', policy)
        without_backend = render('--set-json', 'networkPolicy.egressBackends=[]', template='networkpolicy')
        self.assertEqual(without_backend.returncode, 0, without_backend.stderr)
        self.assertNotIn('forgejo', without_backend.stdout)
        self.assertNotIn('port: 3000', without_backend.stdout)

    def test_ollama_api_has_a_constant_private_upstream_and_streaming(self):
        matches = [block for block in servers(nginx_config()) if 'server_name ollama.spark.test;' in block]
        self.assertEqual(len(matches), 1)
        block = matches[0]
        for directive in ['set $ollama http://ollama.llm-backup.svc.cluster.local:11434;',
                          'proxy_pass $ollama$request_uri;', 'proxy_http_version 1.1;',
                          'proxy_request_buffering off;', 'proxy_buffering off;',
                          'proxy_read_timeout 600s;', 'proxy_set_header Host $http_host;']:
            self.assertIn(directive, block)
        self.assertNotIn('proxy_set_header Authorization', block)
        policy = render(template='networkpolicy').stdout
        self.assertIn('kubernetes.io/metadata.name: "llm-backup"', policy)
        self.assertIn('app.kubernetes.io/instance: "llm-backup"', policy)
        self.assertIn('port: 11434', policy)

    def test_local_cli_prefix_preserves_raw_api_path_and_keeps_docs_root(self):
        config=nginx_config()
        docs=next(block for block in servers(config) if 'server_name docs.spark.test localhost 127.0.0.1;' in block)
        for directive in ['location ^~ /ollama/ {','proxy_pass $ollama$ollama_path;',
                          'set $ollama http://ollama.llm-backup.svc.cluster.local:11434;',
                          'proxy_buffering off;', 'proxy_request_buffering off;']:
            self.assertIn(directive,docs)
        self.assertIn('map $request_uri $ollama_path {',config)
        self.assertIn('~^/ollama(/.*)$ $1;',config)
        self.assertIn('location / { try_files $uri $uri/ =404; }',docs)

    def test_malformed_backend_selectors_and_ports_fail_validation(self):
        for setting in ['networkPolicy.egressBackends[0].namespace=',
                        'networkPolicy.egressBackends[0].namespace=Bad_Namespace',
                        'networkPolicy.egressBackends[0].instance=',
                        'networkPolicy.egressBackends[0].port=0',
                        'networkPolicy.egressBackends[0].port=65536']:
            with self.subTest(setting=setting):
                result = render('--set', setting)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('schema', result.stderr.lower())


if __name__ == '__main__':
    unittest.main()
