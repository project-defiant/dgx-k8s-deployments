# Private application chart

One non-root HTTP container, internal ClusterIP service, HTTP probes, resources,
optional ConfigMap files and optional persistent volume. No chart dependencies.
Supply an immutable image digest; default values deliberately cannot deploy.

```sh
helm upgrade --install docs charts/private-app --namespace docs-dev --create-namespace -f apps/docs/values.yaml
helm upgrade --install docs charts/private-app --namespace docs-prod --create-namespace -f apps/docs/values.yaml -f apps/docs/values-prod.yaml
```

Use the platform's explicit kubeconfig/context guard for live deployments.
Namespaces are supplied by the caller. Quotas apply to the entire namespace;
enable them for only one owning release per namespace. Same release names are
safe in separate namespaces. Service port is always 80; containerPort is configurable.

Configuration keys become files under config.mountPath. Keep content below the
Kubernetes ConfigMap 1 MiB limit. The content checksum triggers a rollout on change
and Helm rollback restores the previous configuration. No secrets belong here.
For larger sites, build content into a pinned image.

Persistence uses Recreate to prevent competing writers, fsGroup for non-root
volume access and Helm's keep annotation to retain the claim on uninstall. A
retained claim is not a backup; do not delete it during rollback or recovery.
Never disable persistence on a release containing useful data. Test storage
recovery with a distinct fixture release and separate restored claim.

The docs example uses a digest-pinned ARM64 nginx-unprivileged Alpine image.
A minimal nginx.conf is mounted alongside the site content under /www. The
nginx binary runs directly as UID/GID 65532 (bypassing entrypoint mutation), with
read-only root filesystem and a bounded writable /tmp for pid and temporary files.
The ARM64-specific digest does not support x86 workers; choose a verified
multiarchitecture index digest when adding those workers. Image package coverage
and High/Critical vulnerability checks are part of the development security gate.

For an application needing /tmp, set writableTmp: true (64Mi bounded emptyDir).
For storage fixture testing, apply apps/docs/values-fixture.yaml after the docs
values and create /data/fixture.txt via kubectl exec. It remains unready until
that file exists. Example payload: printf 'recovery-fixture-v1\n' > /data/fixture.txt.
The fixture serves port 8081 to exercise a second application configuration.

Image reference: https://github.com/nginx/docker-nginx-unprivileged.

The docs example enables a NetworkPolicy: deny pod ingress and allow only DNS
egress to kube-system CoreDNS. Node probes and kubectl port-forward remain
available. This requires an enforcing cluster network plugin. Other applications
leave this off until their required ingress/egress paths are designed and tested.
