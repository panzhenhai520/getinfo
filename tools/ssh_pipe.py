#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSH 到 VPN 服务器（panython）并执行命令/N多行脚本，密码登录。

用法:
  python tools/ssh_pipe.py "whoami; hostname"
  python tools/ssh_pipe.py --stdin   # 从 stdin 读多行脚本执行
"""
from __future__ import annotations

import os
import sys
import warnings

warnings.filterwarnings("ignore")

import paramiko

# 凭据一律从环境变量读取（不把服务器密码写进仓库）：
#   SSH_HOST / SSH_USER / SSH_PASSWORD / SSH_PORT
HOST = os.environ.get("SSH_HOST", "")
USER = os.environ.get("SSH_USER", "")
PASSWORD = os.environ.get("SSH_PASSWORD", "")
PORT = int(os.environ.get("SSH_PORT", "22"))


def run(script: str, *, sudo: bool = False):
    if not (HOST and USER and PASSWORD):
        return 2, "", "缺少 SSH 凭据：请设置 SSH_HOST / SSH_USER / SSH_PASSWORD 环境变量"
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, port=PORT, username=USER, password=PASSWORD, timeout=15, allow_agent=False, look_for_keys=False)
    try:
        # 若需要 sudo，则用 sudo的密码(与登录一致)
        if sudo:
            cmd = "echo '%s' | sudo -S bash -c \"%s\"" % (PASSWORD, script.replace('"', '\\"'))
        else:
            cmd = script
        stdin, stdout, stderr = client.exec_command(cmd, timeout=120)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        return rc, out, err
    finally:
        client.close()


if __name__ == "__main__":
    sudo = "--sudo" in sys.argv
    if "--stdin" in sys.argv:
        script = sys.stdin.read()
    else:
        script = " ".join(a for a in sys.argv[1:] if a != "--sudo")
    rc, out, err = run(script, sudo=sudo)
    sys.stdout.write(out)
    if err.strip():
        sys.stderr.write("ERR: " + err.strip() + "\n")
    sys.exit(rc)
