# Private Forgejo

Personal rootless Forgejo with SQLite. Install into the dedicated forgejo namespace
using apps/forgejo/values.yaml plus the generated image overlay, after the
platform's explicit-context guard.
No public ingress, SSH server, Actions runner or package registry is enabled.
The docs gateway forwards git.spark.test:8088 to the internal HTTP service.

The forgejo-data claim (20Gi) survives Helm uninstall and contains repositories,
SQLite, application data and generated configuration. The rootless image stores
app.ini at /var/lib/gitea/custom/conf/app.ini (its GITEA_APP_INI default).
Back up this entire volume with Forgejo stopped for a consistent SQLite/files
snapshot. No separate /etc/gitea mount is used.
Never put generated config, database, tokens or administrator credentials in Git.
The read-only root filesystem has an explicit writable data mount and bounded
tmp storage. Pod and container processes run without root or Linux capabilities.

The initial installation wizard is intentionally available only over the private
gateway/tunnel. Create the first administrator in that wizard. The application
writes INSTALL_LOCK=true to persisted app.ini; no environment variable overrides
it back to false on restart. Registration and anonymous viewing remain disabled.
Check those settings and the external URL before completing setup. This does not
replace authentication: everyone allowed to reach the private initial wizard
could complete first setup, so finish it before sharing access.

Forgejo v15 LTS requires an explicit trusted proxy range (no wildcard). The initial
10.84.0.0/16 value matches this platform's pod CIDR; NetworkPolicy further limits
inbound connections to docs-dev/docs-prod pods with instance=docs. Reverse-proxy
user authentication and auto-registration are disabled, and auth header names are
cleared. No external Git imports, webhook deliveries or outbound mail work with
DNS-only egress; design explicit outbound rules before enabling those features.

Official references:
- https://forgejo.org/docs/v15.0/admin/installation/docker/
- https://forgejo.org/docs/v15.0/admin/config-cheat-sheet/
- https://forgejo.org/releases/

The upstream runtime images failed the High/Critical vulnerability gate. This
installation uses a locally patched Forgejo 15 LTS build, not an upstream image
with suppressed findings. Run `make dev` and `make build-forgejo-image`, then
import the resulting image into K3s using the documented sudo import command
before `make deploy-services`. The build creates
`.dev-tools/forgejo/<arch>/values.yaml` with the actual immutable OCI digest;
apply that overlay after apps/forgejo/values.yaml. Generated image artifacts and
values stay outside Git. No deployable digest is committed, so omitting the
overlay fails validation. The image repository is restricted to
local.thedefiant/forgejo and pullPolicy Never requires the verified local import.
Tests use an explicitly synthetic digest solely for offline render contracts.
