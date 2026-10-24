# Development workflow

Start every update session with:

```sh
make dev
```

This bootstraps/verifies the pinned local tools, synchronizes the locked Python
development environment, installs worktree-local **prek** hooks and runs the full
security/configuration/test gate. Linux ARM64/x86_64, Python 3.12+, uv, curl,
Git, Bash and GnuPG are bootstrap prerequisites;
Docker with Buildx and access to its daemon are also needed for the native patched
Forgejo build. The first run is longer; verified cached builds are reused.
the command reports missing prerequisites rather than running an unchecked
remote installer or using sudo. Initial setup needs network access to official
release/package sources. Repository-local tool/cache/report directories are ignored.
Advisory checks also need network access: pip-audit sends public package names and
versions to its advisory service, and Trivy downloads advisory/image metadata.
Secret scanning stays local; neither check uploads repository source or credentials.

Do not start unrelated edits while the baseline is failing. Fix the finding or
document a narrowly justified exception and have that change reviewed. `make check`
repeats the gate after edits; hooks run automatically before committing. Hooks
cannot prevent arbitrary editor changes and can be bypassed locally, so hosted CI
must independently require the same checks before merge.

## Change and review

1. Run `make dev`, then use a small feature branch/worktree.
2. Write/test the behavior change; review new files and the full diff.
3. Run `make check` and the relevant integration checks. Stage explicit files,
   not a blanket `git add .` from a directory containing private host data.
4. Run prek on all candidate files before the first publication; include the
   new files in the staged candidate so hooks can see them.
5. Open a PR. Root-executed scripts, authentication, credential renewal, encryption
   and network policy changes need independent security review.
6. Deploy the reviewed commit separately, using the documented host commands.

The development gate never authorizes or performs a host deployment. Never run
root bootstrap, apply cluster changes, or expose the Spark to public PR runners.
Use disposable hosted runners with read-only repository permission and no secrets.
Do not use `pull_request_target` to execute untrusted checked-out code.
`make dev` executes repository automation and tests; it is not a sandbox. Review
bootstrap, hook, workflow and lockfile changes before running an untrusted
contribution locally. Use hosted CI or a disposable isolated environment first.

## Credentials and private configuration

Keep kubeconfigs, tokens, SSH/GPG keys, local environment files, scanner reports,
host inventories and recovery artifacts outside public source. Commit sanitized
examples with placeholders. `.gitignore` prevents accidents; it does not scrub
tracked files or history. Use redacted scanner output and never post real findings
containing credential values to a public issue.

If a real credential leaks, revoke or rotate it first. Removing a file or rewriting
Git history does not revoke it. Coordinate any history rewrite separately.

## Updating tools and exceptions

Tool versions, downloaded artifact hashes, Python development dependencies and
image digests are reviewed inputs. Update them in a dedicated PR, verify the
official source and rerun all checks. A digest pin prevents silent replacement;
it is not an automatic security update.

Do not disable a scanner globally to pass CI. Any exception must name the exact
rule and affected resource/code, explain the effective controls and remaining
risk, and have an owner and a review/expiry condition. Scheduled scans check for
new advisories even when source has not changed. A scan with no detected package
inventory must be treated as incomplete coverage.

See [SECURITY.md](SECURITY.md) for the threat boundaries and publication checks.
