"""调整行业包主题关键词（基础数据修正）：把"文章里几乎不会原样出现"的窄关键词换成实际用词，
并清理被多个主题共用的机构名（机构名属门禁维度，不该出现在技术主题关键词里）。

背景：automotive_industry 有 6 个主题长期 0 篇，原因是关键词写成 "NVH技术"、"智能新能源汽车静谧
舒适性规程" 这类文章不会原样出现的词组；而库里明明有 19 篇含 NVH、14 篇含空气动力学。

只改**草稿** manifest（版本不可变，不就地改已发布版本）；改完需"发布新版本 → 激活"才生效。
用法：python _fix_topic_keywords.py [--dry-run]
"""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, '/app' if os.path.isdir('/app') else r'F:\CollectInfo')

import sqlite_database as sdb

PACK_ID = 'automotive_industry'

# 主题 key → 新的关键词列表（按"文章里真会出现的词"重写）
NEW_KEYWORDS = {
    'nvh_tech': ['NVH', '噪声', '振动', '静谧'],
    'wind_test': ['风洞', '空气动力学', '风阻', '风噪'],
    'off_road_vehicle': ['越野车', '越野', '全地形', '四驱'],
    'anc_tech': ['ANC', '主动降噪', '降噪'],
    'e_control': ['电控', '仿真', '域控制器', '电子控制系统'],
    'low_altitude_test': ['eVTOL', '低空', '飞行汽车', '垂直起降'],
    'car_risk_level': ['风险等级', '保险', '评级', '安全评价'],
}

# 被多个主题共用的机构名：从主题关键词里移除（它们是"备案机构/门禁"维度）
SHARED_ORG_KEYWORDS = ['中汽中心工程院', '中汽工程研究院(天津)有限公司']


def main() -> int:
    dry_run = '--dry-run' in sys.argv
    db = sdb.sqlite_db
    db._ensure_connection()
    cursor = db.connection.cursor()
    row = cursor.execute(
        "SELECT industry_pack_id, revision, manifest_json FROM industry_pack_drafts WHERE industry_pack_id=?",
        (PACK_ID,),
    ).fetchone()
    if not row:
        print('未找到草稿:', PACK_ID)
        return 1
    draft = dict(row)
    manifest = json.loads(draft['manifest_json'] or '{}')
    topics = manifest.get('fixed_topics') or []
    print('草稿修订 =', draft['revision'], ' 主题数 =', len(topics))

    changed = []
    for topic in topics:
        key = str(topic.get('key') or '')
        old = [str(k) for k in (topic.get('keywords') or [])]
        new = list(old)
        # 1) 清理共用机构名
        new = [k for k in new if k not in SHARED_ORG_KEYWORDS]
        # 2) 替换窄关键词
        if key in NEW_KEYWORDS:
            new = list(NEW_KEYWORDS[key])
        if new != old:
            topic['keywords'] = new
            changed.append({'key': key, 'name': topic.get('name'), 'old': old, 'new': new})

    for item in changed:
        print('  %-20s %s' % (item['key'], item['name']))
        print('      旧: %s' % '、'.join(item['old'][:6]))
        print('      新: %s' % '、'.join(item['new'][:6]))

    if not changed:
        print('无需调整')
        return 0

    audit = '/app/data/topic_keywords_fix_%s.json' if os.path.isdir('/app') else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'topic_keywords_fix_%s.json')
    audit = audit % datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    os.makedirs(os.path.dirname(audit), exist_ok=True)
    with open(audit, 'w', encoding='utf-8') as handle:
        json.dump({'created_at': datetime.utcnow().isoformat() + 'Z', 'pack_id': PACK_ID,
                   'changes': changed}, handle, ensure_ascii=False, indent=2)
    print('审计文件 =', audit)

    if dry_run:
        print('DRY-RUN：未写入')
        return 0

    manifest['fixed_topics'] = topics
    with db.lock:
        cursor.execute(
            "UPDATE industry_pack_drafts SET manifest_json=?, updated_at=? "
            "WHERE industry_pack_id=? AND revision=?",
            (json.dumps(manifest, ensure_ascii=False), datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
             PACK_ID, draft['revision']),
        )
        db.connection.commit()
    print('已写入草稿（%d 个主题被调整）。注意：需"发布新版本 → 激活"才生效。' % len(changed))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
