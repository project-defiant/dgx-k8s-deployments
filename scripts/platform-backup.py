#!/usr/bin/env python3
"""Encrypt a consistent directory export, optionally copy it, or restore safely."""
import argparse
import os
from pathlib import Path, PurePosixPath
import re
import shutil

# External tools use fixed executables and argument lists; no shell is invoked.
import subprocess  # nosec B404
import tarfile
import tempfile


def destination(value):
    # Deliberately exclude shell syntax, options, relative paths and IPv6 syntax.
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9.-]*:/[A-Za-z0-9_./-]+", value):
        raise ValueError("destination must be user@host:/absolute/path (simple characters only)")
    if ".." in value.split(":", 1)[1].split("/"):
        raise ValueError("destination cannot contain parent traversal")
    return value


def gpg(*args, **kwargs):
    # Arguments are constructed internally; filenames remain distinct argv elements.
    return subprocess.run(["/usr/bin/gpg", "--batch", *args], check=True, **kwargs)  # nosec B603


def backup(source, output, recipient, remote=None):
    if not re.fullmatch(r"(?:[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64})", recipient):
        raise ValueError("recipient must be a full 40- or 64-character public key fingerprint")
    source, output = Path(source).resolve(), Path(output)
    if not source.is_dir():
        raise ValueError("source must be a consistent staging directory")
    if output.resolve().is_relative_to(source):
        raise ValueError("output must be outside the source directory")
    if remote:
        destination(remote)
    # Create exclusively and restrict permissions even when the user's umask is permissive.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as encrypted, tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "export.tar"
            with tarfile.open(archive, "w") as tar:
                for item in sorted(source.rglob("*")):
                    if item.is_symlink() or not (item.is_file() or item.is_dir()):
                        raise ValueError("source may contain only regular files and directories")
                    tar.add(item, arcname=item.relative_to(source), recursive=False)
            gpg("--trust-model", "always", "--recipient", recipient, "--output", "-", "--encrypt", str(archive), stdout=encrypted)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    if remote:
        # Legacy scp can use a remote shell; destination validation also protects that mode.
        # Destination excludes shell metacharacters; options and executable are fixed.
        command = ["/usr/bin/scp", "-oBatchMode=yes", "-oStrictHostKeyChecking=yes", "--", str(output.resolve()), remote]
        subprocess.run(command, check=True)  # nosec B603


def restore(archive, target):
    target = Path(target).absolute()
    if target.is_symlink() or (target.exists() and (not target.is_dir() or any(target.iterdir()))):
        raise ValueError("restore target must be absent or an empty directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temp:
        plain = Path(temp) / "verified.tar"
        # Complete authenticated decryption before interpreting any archive content.
        status = gpg("--status-fd", "1", "--output", str(plain), "--decrypt",
                     str(Path(archive).resolve()), stdout=subprocess.PIPE)
        if b"[GNUPG:] GOODMDC" not in status.stdout:
            raise ValueError("archive lacks verified integrity protection")
        staging = Path(temp) / "restore"
        staging.mkdir()
        with tarfile.open(plain, "r:") as tar:
            members = tar.getmembers()
            seen = set()
            for member in members:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or not path.parts or member.name in seen:
                    raise ValueError("unsafe or duplicate archive path")
                if not (member.isfile() or member.isdir()):
                    raise ValueError("archive links and special files are forbidden")
                seen.add(member.name)
            for member in members:
                path = staging.joinpath(*PurePosixPath(member.name).parts)
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(member) as src, path.open("xb") as dst:
                        shutil.copyfileobj(src, dst)
                    path.chmod(0o600)
        if target.exists():
            target.rmdir()  # Fails safely if another process populated it meanwhile.
        staging.rename(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("backup")
    create.add_argument("source")
    create.add_argument("output")
    create.add_argument("--recipient", required=True, help="existing public GPG key fingerprint")
    create.add_argument("--destination", type=destination)
    recover = commands.add_parser("restore")
    recover.add_argument("archive")
    recover.add_argument("target")
    args = parser.parse_args()
    try:
        if args.command == "backup":
            backup(args.source, args.output, args.recipient, args.destination)
        else:
            restore(args.archive, args.target)
    except (ValueError, OSError, subprocess.CalledProcessError, tarfile.TarError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
