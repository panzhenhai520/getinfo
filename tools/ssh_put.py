#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 SFTP 把本地文件上传到 VPN 服务器。用法: python tools/ssh_put.py <src> <dst>"""
from __future__ import annotations

import os
import sys
import warnings
warnings.filterwarnings("ignore")
import paramiko

# 凭据一律从环境变量读取（不把服务器密码写进仓库）：
#   SSH_HOST / SSH_USER / SSH_PASSWORD
HOST = os.environ.get("SSH_HOST", "")
USER = os.environ.get("SSH_USER", "")
PASSWORD = os.environ.get("SSH_PASSWORD", "")
PORT = int(os.environ.get("SSH_PORT", "22"))


def main(src: str, dst: str) -> int:
    if not (HOST and USER and PASSWORD):
        print("缺少 SSH 凭据：请设置 SSH_HOST / SSH_USER / SSH_PASSWORD 环境变量", file=sys.stderr)
        return 2
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, port=PORT, username=USER, password=PASSWORD, timeout=15,
                   allow_agent=False, look_for_keys=False)
    sftp = client.open_sftp()
    print(f"uploading {src} -> {dst}", flush=True)
    sftp.put(src, dst)
    sftp.chmod(dst, 0o644)
    sftp.close()
    client.close()
    print("OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
