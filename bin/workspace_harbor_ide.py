"""Strict IntelliJ IDEA discovery shared by Workspace Harbor helpers."""

from __future__ import annotations

import os
import plistlib
import pwd
import re
import subprocess
from pathlib import Path


VERSION_PATTERN = re.compile(r"(\d{4})\.(\d+)(?:\.\d+)*")


def account_home() -> Path:
    """Return the authenticated account home instead of trusting ``HOME``."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def configured_app() -> Path:
    """Return the configured IntelliJ application bundle."""
    configured = os.environ.get("INTELLIJ_APP_PATH")
    if configured:
        return Path(configured).expanduser()
    return account_home() / "Applications/IntelliJ IDEA.app"


def app_version(app: Path) -> str:
    """Read a validated IntelliJ version from the application plist."""
    try:
        with (app / "Contents/Info.plist").open("rb") as handle:
            version = plistlib.load(handle).get("CFBundleShortVersionString")
    except (OSError, plistlib.InvalidFileException) as error:
        raise ValueError("cannot identify configured IntelliJ application") from error
    if not isinstance(version, str) or VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError("invalid IntelliJ version")
    return version


def config_dir(app: Path) -> Path:
    """Return the version-matched IntelliJ settings directory."""
    match = VERSION_PATTERN.fullmatch(app_version(app))
    if match is None:  # app_version already validates; keep the boundary explicit.
        raise ValueError("invalid IntelliJ version")
    return (
        account_home()
        / "Library/Application Support/JetBrains"
        / ("IntelliJIdea" + match.group(1) + "." + match.group(2))
    )


def trusted_paths_file(app: Path) -> Path:
    """Return IntelliJ's exact trusted-path registry location."""
    return config_dir(app) / "options/trusted-paths.xml"


def is_intellij_command(command: str, app: Path) -> bool:
    """Whether a process command belongs to the configured IntelliJ bundle."""
    executables = {
        str(app / "Contents/MacOS/idea"),
        str((app / "Contents/MacOS/idea").resolve(strict=False)),
    }
    return any(
        command == executable or command.startswith(executable + " ")
        for executable in executables
    )


def intellij_owned_port(port: int, app: Path) -> bool:
    """Fail closed unless one IntelliJ process owns the listening port."""
    if not isinstance(port, int) or not 1 <= port <= 65535:
        return False
    try:
        listeners = subprocess.run(
            [
                "/usr/sbin/lsof",
                "-nP",
                f"-iTCP:{port}",
                "-sTCP:LISTEN",
                "-Fp",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        pids = {
            line[1:]
            for line in listeners.stdout.splitlines()
            if line.startswith("p") and line[1:].isdigit()
        }
        if listeners.returncode != 0 or len(pids) != 1:
            return False
        process = subprocess.run(
            ["/bin/ps", "-p", next(iter(pids)), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return process.returncode == 0 and is_intellij_command(
            process.stdout.strip(), app
        )
    except (OSError, subprocess.SubprocessError):
        return False
