#!/usr/bin/env python3
"""First-run setup: create .env (with fresh random secrets) and config/risk.yaml.

Never overwrites an existing file. Run via ``make init``.
"""

from __future__ import annotations

import argparse
import re
import secrets
import shutil
import stat
import sys
from pathlib import Path


def init(root: Path) -> list[str]:
    messages: list[str] = []

    env_example = root / ".env.example"
    env_file = root / ".env"
    if env_file.exists():
        messages.append(f"kept existing {env_file.name}")
    else:
        text = env_example.read_text(encoding="utf-8")
        for name in ("WEBHOOK_SECRET", "ADMIN_SECRET"):
            text = re.sub(
                rf"^{name}=.*$", f"{name}={secrets.token_urlsafe(32)}", text, flags=re.MULTILINE
            )
        env_file.write_text(text, encoding="utf-8")
        env_file.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600: it holds secrets
        messages.append(f"created {env_file.name} with random WEBHOOK_SECRET and ADMIN_SECRET")

    risk_example = root / "config" / "risk.example.yaml"
    risk_file = root / "config" / "risk.yaml"
    if risk_file.exists():
        messages.append(f"kept existing {risk_file.relative_to(root)}")
    else:
        shutil.copyfile(risk_example, risk_file)
        messages.append(f"created {risk_file.relative_to(root)} (edit the limits to taste)")
    return messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parent.parent, help="project root"
    )
    args = parser.parse_args(argv)
    try:
        for line in init(args.root):
            print(line)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
