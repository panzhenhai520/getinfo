"""Create server and client environment files without printing the API token."""

from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', default='.')
    parser.add_argument('--client-url', default='http://10.88.0.1:11236')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    example = directory / '.env.example'
    server = directory / '.env'
    client = directory.parent / 'remote_pipeline.client.env'
    if not example.is_file():
        raise SystemExit('.env.example is missing')
    if server.exists() and not args.force:
        print(f'kept existing {server}')
        return 0
    token = secrets.token_urlsafe(48)
    server_text = example.read_text(encoding='utf-8').replace(
        'replace-with-at-least-32-random-characters', token
    )
    server.write_text(server_text, encoding='utf-8')
    client.write_text(
        f'PIPELINE_MODE=auto\nREMOTE_PIPELINE_URL={args.client_url}\nREMOTE_PIPELINE_TOKEN={token}\nLOCAL_BROWSER_ENABLED=false\n',
        encoding='utf-8',
    )
    try:
        os.chmod(server, 0o600)
        os.chmod(client, 0o600)
    except OSError:
        pass
    print(f'created {server} and {client}; token was not printed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

