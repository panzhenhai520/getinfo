"""源码指纹：本机与生产机代码是否一致，用同一个数字回答。

算法必须与部署排除项一致（见 deploy-to-prod.ps1 的 $Excludes），否则两边算的不是同一集合。
做法：对每个纳入部署的文件算 sha256，按路径排序后拼成 "路径:哈希" 行，再整体取 sha256。
这样指纹只取决于文件内容，与打包时间、gzip 时间戳无关，两边可逐字比较。

用法：
    python tools/source_fingerprint.py            # 打印本机指纹
    python tools/source_fingerprint.py --json     # 供接口调用，输出 JSON
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 与 deploy-to-prod.ps1 的 $Excludes 对应：这些目录/文件不进镜像，也不参与指纹。
# 额外排除 .pyc：容器运行时会自己生成 __pycache__，若纳入指纹，生产端每次算出的值都会漂移。
EXCLUDED_DIRS = {
    ".git", "__pycache__", "data", "deploy-data", "crawl_results", "crawl_logs",
    "auth_storage", ".postgres_deps", "industry_pack_backups", "vendor",
    "node_modules", ".venv", "venv",
    # 本机测试/检查工具产生的缓存：不参与部署（构建时被 .dockerignore 排除），
    # 纳入指纹只会让本机凭空多出文件、把"代码一致"误报成不一致。
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache",
}
EXCLUDED_SUFFIXES = (
    ".db", ".db-shm", ".db-wal", ".log", ".zip", ".tgz", ".tar", ".tar.gz", ".pyc",
    ".bak", ".bak_", ".err", ".orig", ".rej",
)
# 构建产物：Dockerfile.deploy 由部署脚本生成，不是源码，不应参与指纹
EXCLUDED_NAMES = {"Dockerfile.deploy"}


def _included(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    if any(part in EXCLUDED_DIRS or part.startswith(".tmp-") for part in relative.parts):
        return False
    name = path.name
    if name in EXCLUDED_NAMES:
        return False
    if name.startswith("_askpass") or name.startswith("_prod_") or name.startswith("_host_compose"):
        return False
    if name == ".env" or name.startswith(".env."):
        return False
    if name.endswith(EXCLUDED_SUFFIXES):
        return False
    return path.is_file()


def source_fingerprint(root: Path = ROOT) -> dict:
    """返回 {fingerprint, file_count, entries}；entries 用于差异定位。"""
    global ROOT
    original, ROOT = ROOT, root
    try:
        entries = []
        for path in root.rglob("*"):
            if not _included(path):
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append("%s:%s" % (path.relative_to(root).as_posix(), digest))
    finally:
        ROOT = original
    entries.sort()
    overall = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
    return {"fingerprint": overall, "file_count": len(entries), "entries": entries}


def file_hashes(root: Path = ROOT) -> dict:
    """路径 → sha256，供两端逐文件比对（定位到底是哪几个文件不一致）。"""
    data = source_fingerprint(root)
    return dict(item.split(":", 1) for item in data["entries"])


if __name__ == "__main__":
    data = source_fingerprint()
    if "--json" in sys.argv:
        print(json.dumps({k: v for k, v in data.items() if k != "entries"},
                         ensure_ascii=False))
    else:
        print("源码指纹 = %s" % data["fingerprint"])
        print("纳入文件 = %d 个" % data["file_count"])
