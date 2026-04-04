from __future__ import annotations

import os
import sys

from common import build_cleaner_command, load_config


def main() -> None:
    config = load_config()
    cmd = build_cleaner_command(config)
    os.execv(cmd[0], cmd)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'CLIProxyAPI-cleaner launcher failed: {exc}', file=sys.stderr)
        raise
