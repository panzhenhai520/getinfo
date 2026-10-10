#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 04（P04-01…P04-06）舰队验收用的**生产语料只读快照**导出工具。

为什么要有这个工具：舰队要在**真实语料**上留可复算的 before/after 证据，但生产机（A 机）不许
部署新代码、也不许写。所以做法与 Phase 03 一致：**只读导出一个小快照 → 本地离线回放**。

只读保证（与 baseline/qa-verifier-real-sample.json 的 source 段同口径）：
  · SSH 进 A 机 → `docker exec <容器> psql`，**会话内**先执行
    `SET statement_timeout='8s'; SET default_transaction_read_only=on;`；
  · 每条远端命令都用 `timeout N` 包住（短命命令，SSH 读超时也不留长跑进程）；
  · 每个查询都带显式 LIMIT；只 SELECT，无任何写语句、不建表、不导入。

凭据只从环境变量读（与 `tools/ssh_pipe.py` 一致，不落盘到仓库）：
    SSH_HOST / SSH_USER / SSH_PASSWORD / SSH_PORT
密码通过一次性 askpass 脚本传给系统 ssh（Windows 用 .cmd，POSIX 用 .sh），用完立刻删除。

用法：
    python tools/qa_phase04_corpus_export.py \
        --pack family_office --pack automotive_industry --per-pack 100 \
        --out baseline/qa-hunter-corpus-snapshot.json
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SNAPSHOT_VERSION = "qa-hunter-corpus-snapshot-v1"

# 每条查询都是只读 + 有界（LIMIT）；列名与本地 sqlite 表结构逐字对齐，便于回放时直接入库。
ARTICLE_IDS_SQL = (
    "SELECT c.article_id FROM article_intel_classifications c "
    "JOIN articles a ON a.id=c.article_id "
    "WHERE c.industry_pack_id=$${pack}$$ AND a.status='active' {match} "
    "ORDER BY COALESCE(a.publish_date,a.first_crawled,'') DESC, a.id DESC LIMIT {limit}"
)


def match_clause(terms) -> str:
    """按问题实词做词面命中过滤（取样更贴近 benchmark 要问的东西，而不是只取最新 60 篇）。

    仍然是只读 + 有界：只加 `LIKE` 条件，不改行、不排序以外的任何东西。
    """
    cleaned = [str(term).replace("'", "").strip() for term in (terms or [])]
    cleaned = [term for term in cleaned if len(term) >= 2][:8]
    if not cleaned:
        return ""
    parts = []
    for term in cleaned:
        for column in ("a.title", "a.content", "c.matched_keywords_json"):
            parts.append("%s LIKE '%%%s%%'" % (column, term))
    return "AND (" + " OR ".join(parts) + ")"

ARTICLES_SQL = """
SELECT row_to_json(t) FROM (
  SELECT id, title, url, domain, publish_date, first_crawled, status, quality_score,
         content_length, published_at_utc, published_timezone, published_precision,
         substr(COALESCE(content,''), 1, {chars}) AS content
  FROM articles WHERE id IN ({ids}) ORDER BY id
) t
"""

CLASSIFICATIONS_SQL = """
SELECT row_to_json(t) FROM (
  SELECT article_id, industry_pack_id, activation_id, industry_pack_version, classifier_version,
         article_content_hash, rule_category, rule_confidence, score_details_json,
         matched_keywords_json, topic_tags_json, final_category, result_source
  FROM article_intel_classifications
  WHERE article_id IN ({ids}) AND industry_pack_id IN ({packs}) ORDER BY article_id
) t
"""

RAGFLOW_SQL = """
SELECT row_to_json(t) FROM (
  SELECT article_id, kb_id, document_id, document_name, sync_status, doc_type, issuer, doc_no,
         article_no, policy_title, publish_date, effective_date, source_url, authority_level
  FROM article_ragflow_documents WHERE article_id IN ({ids}) ORDER BY article_id LIMIT {limit}
) t
"""

EVENTS_SQL = """
SELECT row_to_json(t) FROM (
  SELECT article_id, event_index, industry_pack_id, subject, action, object, event_time,
         event_type, subject_type, state_before, state_after, event_hash, content_hash
  FROM intel_article_events WHERE article_id IN ({ids}) ORDER BY article_id, event_index
  LIMIT {limit}
) t
"""

ATTRIBUTES_SQL = """
SELECT row_to_json(t) FROM (
  SELECT article_id, attr_index, industry_pack_id, subject, attribute, value, value_type,
         valid_from, valid_to, as_of, evidence_quote, content_hash
  FROM intel_article_attributes WHERE article_id IN ({ids}) ORDER BY article_id, attr_index
  LIMIT {limit}
) t
"""

EMBEDDINGS_SQL = """
SELECT row_to_json(t) FROM (
  SELECT article_id, model_id, embedding_dim,
         replace(encode(embedding, 'base64'), E'\\n', '') AS embedding_b64, status
  FROM intel_article_embeddings
  WHERE article_id IN ({ids}) AND status='ready' ORDER BY article_id LIMIT {limit}
) t
"""


def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


def _askpass_dir(password: str) -> str:
    """一次性 askpass 脚本（不落仓库、不落 shell 历史）。"""
    path = tempfile.mkdtemp(prefix="qa-p04-askpass-")
    if os.name == "nt":
        script = os.path.join(path, "askpass.cmd")
        with open(script, "w", encoding="ascii", newline="\r\n") as handle:
            handle.write("@echo %s\r\n" % password)
    else:
        script = os.path.join(path, "askpass.sh")
        with open(script, "w", encoding="ascii") as handle:
            handle.write("#!/bin/sh\nprintf '%s\\n' %s\n" % ("%s", json.dumps(password)))
        os.chmod(script, stat.S_IRWXU)
    return script


def ssh_run(host: str, user: str, password: str, port: int, command: str, *,
            timeout: int = 60, attempts: int = 3) -> tuple:
    """跑一条**短命**远端命令，返回 (rc, stdout, stderr)。

    连接被 sshd 临时拒（`kex_exchange_identification: Connection closed by remote host`，
    短时间内反复连接会触发）时退避重试；仍然失败就如实返回，绝不无限重连。
    """
    import time as _time

    last = (1, "", "未执行")
    for attempt in range(max(1, int(attempts))):
        last = _ssh_once(host, user, password, port, command, timeout=timeout)
        rc, _out, err = last
        if rc == 0 or "Connection closed by remote host" not in str(err):
            return last
        _time.sleep(2 + 2 * attempt)
    return last


def _ssh_once(host: str, user: str, password: str, port: int, command: str, *,
              timeout: int = 60) -> tuple:
    askpass = _askpass_dir(password)
    env = dict(os.environ)
    env.update({"SSH_ASKPASS": askpass, "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": ":0"})
    args = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
            "-o", "PreferredAuthentications=password", "-o", "NumberOfPasswordPrompts=1",
            "-p", str(port), "%s@%s" % (user, host), command]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=timeout,
                              stdin=subprocess.DEVNULL)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.stdout or ""), "本机 ssh 超时（远端命令已被 timeout 包住）"
    finally:
        shutil.rmtree(os.path.dirname(askpass), ignore_errors=True)


def psql_rows(host, user, password, port, *, container, db_user, database, sql,
              timeout=40, statement_timeout="8s") -> list:
    """只读会话里跑一条 SELECT，返回 stdout 的非空行（未解析）。"""
    inner = ("SET statement_timeout='%s'; SET default_transaction_read_only=on; %s"
             % (statement_timeout, " ".join(sql.split())))
    remote = ("timeout %d docker exec %s psql -U %s -d %s -A -t -v ON_ERROR_STOP=1 -c %s"
              % (timeout, container, db_user, database, _shell_quote(inner)))
    rc, out, err = ssh_run(host, user, password, port, remote, timeout=timeout + 20)
    if rc != 0:
        raise RuntimeError("远端查询失败 rc=%s err=%s" % (rc, err.strip()[:400]))
    return [line.strip() for line in out.splitlines()
            if line.strip() and line.strip() != "SET"]


def psql_json_rows(host, user, password, port, *, container, db_user, database, sql,
                   timeout=40, statement_timeout="8s") -> list:
    """只读会话里跑一条 `row_to_json` 查询（JSONL，一行一个对象）。"""
    rows = []
    for line in psql_rows(host, user, password, port, container=container, db_user=db_user,
                          database=database, sql=sql, timeout=timeout,
                          statement_timeout=statement_timeout):
        if not line.startswith("{"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _shell_quote(text: str) -> str:
    """单引号包裹（远端 shell 用）；内部的单引号按 POSIX 规则转义。"""
    return "'" + str(text).replace("'", "'\"'\"'") + "'"


def _ids_clause(ids) -> str:
    return ",".join(str(int(item)) for item in ids) or "NULL"


def export(packs, *, per_pack: int, content_chars: int, host, user, password, port,
           container, db_user, database, terms_by_pack=None, verbose=True) -> dict:
    terms_by_pack = dict(terms_by_pack or {})
    snapshot = {
        "snapshot_version": SNAPSHOT_VERSION,
        "source": {
            "access": "readonly (SET default_transaction_read_only=on, statement_timeout=8s, "
                      "每条命令 timeout 包住)",
            "backend": "%s/%s" % (db_user, database),
            "container": container,
            "host": host,
            "queries": ["articles", "article_intel_classifications", "article_ragflow_documents",
                        "intel_article_events", "intel_article_attributes",
                        "intel_article_embeddings"],
            "note": "只 SELECT、带 LIMIT、无写语句；向量按 base64 导出（float32 原始字节）",
        },
        "packs": {},
    }
    all_ids = []
    for pack in packs:
        terms = terms_by_pack.get(pack) or []
        sql = ARTICLE_IDS_SQL.format(pack=pack, limit=int(per_pack),
                                    match=match_clause(terms))
        ids = []
        for line in psql_rows(host, user, password, port, container=container, db_user=db_user,
                              database=database, sql=sql):
            try:
                ids.append(int(line))
            except ValueError:
                continue
        snapshot["packs"][pack] = {"article_ids": ids, "match_terms": list(terms)}
        all_ids.extend(ids)
        if verbose:
            print("[导出] pack=%s 抽样文章 %d 篇（词面条件：%s）"
                  % (pack, len(ids), "、".join(terms) or "无（按最新）"))
    if not all_ids:
        raise RuntimeError("没有取到任何文章 id，检查 pack 名与容器/库名")
    ids_clause = _ids_clause(all_ids)
    packs_clause = ",".join("'%s'" % str(pack).replace("'", "") for pack in packs)

    def _query(label, sql):
        rows = psql_json_rows(host, user, password, port, container=container, db_user=db_user,
                              database=database, sql=sql)
        if verbose:
            print("[导出] %-28s %d 行" % (label, len(rows)))
        return rows

    articles = _query("articles", ARTICLES_SQL.format(ids=ids_clause, chars=int(content_chars)))
    classifications = _query("article_intel_classifications",
                             CLASSIFICATIONS_SQL.format(ids=ids_clause, packs=packs_clause))
    ragflow = _query("article_ragflow_documents",
                     RAGFLOW_SQL.format(ids=ids_clause, limit=max(200, len(all_ids))))
    events = _query("intel_article_events",
                    EVENTS_SQL.format(ids=ids_clause, limit=max(400, len(all_ids) * 3)))
    attributes = _query("intel_article_attributes",
                        ATTRIBUTES_SQL.format(ids=ids_clause, limit=max(200, len(all_ids))))
    embeddings = _query("intel_article_embeddings",
                        EMBEDDINGS_SQL.format(ids=ids_clause, limit=len(all_ids)))
    for row in embeddings:
        # 向量按 base64 留存（float32 原始字节）；回放时 base64 解码即得与生产一致的表数据。
        blob = base64.b64decode(str(row.get("embedding_b64") or ""))
        dim = int(row.get("embedding_dim") or 0)
        row.pop("vector", None)
        row["embedding_bytes"] = len(blob)
        row["embedding_ok"] = bool(dim and len(blob) == dim * 4)
    snapshot.update({"articles": articles, "classifications": classifications,
                     "ragflow_documents": ragflow, "events": events,
                     "attributes": attributes, "embeddings": embeddings})
    snapshot["counts"] = {key: len(snapshot[key]) for key in
                          ("articles", "classifications", "ragflow_documents", "events",
                           "attributes", "embeddings")}
    return snapshot


def terms_for_packs(packs, questions_path: str) -> dict:
    """从冻结 benchmark 里按行业包取问题实词（作为抽样条件，不改题目本身）。"""
    import json as _json

    if not questions_path or not os.path.exists(questions_path):
        return {}
    with open(questions_path, encoding="utf-8") as handle:
        payload = _json.load(handle)
    questions = payload.get("questions") if isinstance(payload, dict) else payload
    result: dict = {}
    for item in questions or []:
        pack = str(item.get("industry_pack_id") or "")
        if pack not in packs:
            continue
        bucket = result.setdefault(pack, [])
        for term in item.get("expect_terms") or []:
            text = str(term or "").strip()
            if text and text not in bucket:
                bucket.append(text)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Phase 04 舰队验收语料快照（A 机只读导出）")
    parser.add_argument("--pack", action="append", default=[],
                        help="行业包 id（可多次；默认 family_office）")
    parser.add_argument("--per-pack", type=int, default=100, help="每个包抽样多少篇文章")
    parser.add_argument("--content-chars", type=int, default=1000, help="正文截断字数")
    parser.add_argument("--out", default="baseline/qa-hunter-corpus-snapshot.json")
    parser.add_argument("--container", default="collectinfo-postgres")
    parser.add_argument("--db-user", default="postgres")
    parser.add_argument("--database", default="collectinfo")
    parser.add_argument("--match-questions", default="",
                        help="按这份 benchmark 的问题实词抽样（默认 config/qa_acceptance_questions.json）")
    parser.add_argument("--no-match-questions", action="store_true",
                        help="只按发布时间取最新 N 篇（不按问题实词过滤）")
    args = parser.parse_args(argv)

    host = _env("SSH_HOST")
    user = _env("SSH_USER")
    password = _env("SSH_PASSWORD")
    port = int(_env("SSH_PORT", "22") or 22)
    if not (host and user and password):
        print("缺少 SSH 凭据：请设置 SSH_HOST / SSH_USER / SSH_PASSWORD（可选 SSH_PORT）")
        return 2
    packs = args.pack or ["family_office"]
    terms_by_pack = {}
    if not args.no_match_questions:
        path = args.match_questions or os.path.join("config", "qa_acceptance_questions.json")
        terms_by_pack = terms_for_packs(packs, path)
    snapshot = export(packs, per_pack=max(1, min(int(args.per_pack), 500)),
                      content_chars=max(200, min(int(args.content_chars), 8000)),
                      host=host, user=user, password=password, port=port,
                      container=args.container, db_user=args.db_user, database=args.database,
                      terms_by_pack=terms_by_pack)
    snapshot["packs_count"] = len(packs)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=1, sort_keys=True)
    size_mb = os.path.getsize(args.out) / 1024.0 / 1024.0
    print("写出 %s（%.2f MB）：%s" % (args.out, size_mb, snapshot["counts"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
