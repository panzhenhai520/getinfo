#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Rule-based article spacetime profile analyzer.

The MVP is intentionally deterministic and offline: it extracts a time coordinate,
infers a location coordinate, records evidence, and returns a profile that can be
stored independently from the source article.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Dict, Optional


ANALYSIS_VERSION = 'rules-v2-2026-08-09'

SPACETIME_PLACES = [
    # —— 中国（通用与汽车城市）——
    ('China', ['中国', '中國', '内地', '內地', 'mainland china', "people's republic of china"], 35.8617, 104.1954, 'jurisdiction'),
    ('Beijing', ['北京', 'beijing'], 39.9042, 116.4074, 'organization_location'),
    ('Shanghai', ['上海', 'shanghai'], 31.2304, 121.4737, 'organization_location'),
    ('Shenzhen', ['深圳', 'shenzhen'], 22.5431, 114.0579, 'organization_location'),
    ('Guangzhou', ['广州', '廣州', 'guangzhou', 'canton'], 23.1291, 113.2644, 'organization_location'),
    ('Hangzhou', ['杭州', 'hangzhou'], 30.2741, 120.1551, 'organization_location'),
    ('Chongqing', ['重庆', '重慶', 'chongqing'], 29.5630, 106.5516, 'organization_location'),
    ('Changchun', ['长春', '長春', 'changchun'], 43.8171, 125.3235, 'organization_location'),
    ('Wuhan', ['武汉', '武漢', 'wuhan'], 30.5928, 114.3055, 'organization_location'),
    ('Hefei', ['合肥', 'hefei'], 31.8206, 117.2272, 'organization_location'),
    # —— 港澳 ——
    ('Hong Kong', ['香港', 'hong kong', 'hongkong', '港交所', '证监会', '證監會', 'sfc'], 22.3193, 114.1694, 'jurisdiction'),
    # —— 亚洲 ——
    ('Singapore', ['新加坡', 'singapore', 'monetary authority of singapore'], 1.3521, 103.8198, 'jurisdiction'),
    ('Japan', ['日本', 'japan', 'japanese'], 36.2048, 138.2529, 'jurisdiction'),
    ('Tokyo', ['东京', '東京', 'tokyo'], 35.6762, 139.6503, 'organization_location'),
    ('Nagoya', ['名古屋', 'nagoya'], 35.1815, 136.9066, 'organization_location'),
    ('South Korea', ['韩国', '韓國', 'south korea', 'republic of korea'], 35.9078, 127.7669, 'jurisdiction'),
    ('Seoul', ['首尔', '首爾', 'seoul'], 37.5665, 126.9780, 'organization_location'),
    # —— 欧洲 ——
    ('Sweden', ['瑞典', 'sweden', 'swedish'], 60.1282, 18.6435, 'jurisdiction'),
    ('Gothenburg', ['哥德堡', 'gothenburg', 'göteborg', 'goteborg'], 57.7089, 11.9746, 'event_location'),
    ('Germany', ['德国', '德國', 'germany', 'german'], 51.1657, 10.4515, 'jurisdiction'),
    ('Stuttgart', ['斯图加特', 'stuttgart'], 48.7758, 9.1829, 'organization_location'),
    ('Munich', ['慕尼黑', 'munich', 'münchen', 'munchen'], 48.1351, 11.5820, 'organization_location'),
    ('Wolfsburg', ['沃尔夫斯堡', 'wolfsburg'], 52.4278, 10.7862, 'organization_location'),
    ('France', ['法国', '法國', 'france', 'french'], 46.2276, 2.2137, 'jurisdiction'),
    ('Paris', ['巴黎', 'paris'], 48.8566, 2.3522, 'organization_location'),
    ('Italy', ['意大利', '義大利', 'italy', 'italian'], 41.8719, 12.5674, 'jurisdiction'),
    ('Turin', ['都灵', '都靈', 'turin', 'torino'], 45.0703, 7.6869, 'organization_location'),
    ('United Kingdom', ['英国', '英國', 'united kingdom', 'britain'], 55.3781, -3.4360, 'jurisdiction'),
    ('London', ['伦敦', '倫敦', 'london'], 51.5072, -0.1276, 'event_location'),
    ('Switzerland', ['瑞士', 'switzerland', 'swiss'], 46.8182, 8.2275, 'jurisdiction'),
    ('Netherlands', ['荷兰', '荷蘭', 'netherlands', 'dutch'], 52.1320, 5.2913, 'jurisdiction'),
    # —— 美洲 ——
    ('United States', ['美国', '美國', 'united states', 'usa', 'u.s.a.', 'america'], 37.0902, -95.7129, 'jurisdiction'),
    ('Detroit', ['底特律', 'detroit'], 42.3314, -83.0458, 'organization_location'),
    ('New York', ['纽约', '紐約', 'new york'], 40.7128, -74.006, 'event_location'),
    ('Canada', ['加拿大', 'canada', 'canadian'], 56.1304, -106.3468, 'jurisdiction'),
    ('Mexico', ['墨西哥', 'mexico', 'mexican'], 23.6345, -102.5528, 'jurisdiction'),
    # —— 中东 ——
    ('United Arab Emirates', ['阿联酋', '阿聯酋', 'united arab emirates', 'uae'], 23.4241, 53.8478, 'jurisdiction'),
    ('Dubai', ['迪拜', 'dubai'], 25.2048, 55.2708, 'event_location'),
    # —— 大洋洲 ——
    ('Australia', ['澳大利亚', '澳洲', 'australia', 'australian'], -25.2744, 133.7751, 'jurisdiction'),
    # —— 离岸法域 ——
    ('Cayman Islands', ['开曼', '開曼', 'cayman islands', 'cayman'], 19.3133, -81.2546, 'jurisdiction'),
    ('British Virgin Islands', ['英属维尔京', '英屬維爾京', 'british virgin islands'], 18.4207, -64.64, 'jurisdiction'),
    # —— 其他 ——
    ('India', ['印度', 'india', 'indian'], 20.5937, 78.9629, 'jurisdiction'),
]

DOMAIN_FALLBACK_PLACES = {
    'hkej.com': 'Hong Kong',
    'junhe.com': 'Beijing',
    'tembusulaw.com': 'Singapore',
    'mas.gov.sg': 'Singapore',
    'investhk.gov.hk': 'Hong Kong',
}

EVENT_WORDS = ('举办', '舉辦', '召开', '召開', '培训', '培訓', '出席', '判决', '判決', '发布', '發布', '举行', '舉行')
EFFECTIVE_WORDS = ('施行', '生效', 'effective from', 'comes into effect')


def _parse_date_text(text: str) -> Optional[str]:
    if not text:
        return None

    patterns = [
        r'(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?',
        r'(\d{4})\.(\d{1,2})\.(\d{1,2})',
    ]
    for pattern in patterns:
        match = re.search(pattern, str(text))
        if not match:
            continue
        year, month, day = [int(part) for part in match.groups()]
        try:
            return datetime(year, month, day).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return None


def _parse_article_time(article: Dict) -> Dict:
    title = article.get('title') or ''
    content = article.get('content') or ''
    combined = f'{title}\n{content[:5000]}'

    event_date = _parse_date_text(combined)
    if event_date:
        lowered = combined.lower()
        if any(word.lower() in lowered for word in EFFECTIVE_WORDS):
            return {
                'value': event_date,
                'type': 'effective_time',
                'confidence': 0.88,
                'evidence': '正文或标题出现生效/施行时间',
            }
        if any(word in combined for word in EVENT_WORDS):
            return {
                'value': event_date,
                'type': 'event_time',
                'confidence': 0.9,
                'evidence': '正文或标题出现事件触发词附近日期',
            }

    for field, time_type, confidence in (
        ('publish_date', 'publish_time', 0.82),
        ('last_crawled', 'crawl_time', 0.48),
        ('first_crawled', 'crawl_time', 0.42),
        ('created_at', 'crawl_time', 0.38),
    ):
        parsed = _parse_date_text(article.get(field) or '')
        if parsed:
            return {
                'value': parsed,
                'type': time_type,
                'confidence': confidence,
                'evidence': field,
            }

    return {
        'value': None,
        'type': 'missing',
        'confidence': 0,
        'evidence': '未找到可用时间',
    }


def _find_place_by_name(place_name: str) -> Optional[Dict]:
    for name, aliases, lat, lng, place_type in SPACETIME_PLACES:
        if name == place_name:
            return {
                'name': name,
                'lat': lat,
                'lng': lng,
                'type': place_type,
                'confidence': 0.35,
                'evidence': '',
            }
    return None


def _alias_matcher(alias: str):
    """构造别名匹配器。

    含英文字母/数字的别名用“词边界正则”匹配，避免 mas/uk/usa/u.s. 这类短串
    在 Thomas、format、URL 片段里被误命中；纯中文别名返回 None（用 str.count 计数）。
    返回 (alias_lower, compiled_regex_or_None)。
    """
    lowered = alias.lower()
    if re.search(r'[a-z0-9]', lowered):
        # 容忍中间空白/连字符差异（united states / united-states）
        token = r'[\s\-]+'.join(re.escape(part) for part in lowered.split())
        return lowered, re.compile(r'(?<![a-z0-9])' + token + r'(?![a-z0-9])')
    return lowered, None


def _alias_count(haystack: str, alias_lower: str, regex) -> int:
    if regex is not None:
        return len(regex.findall(haystack))
    return haystack.count(alias_lower)


def _infer_article_place(article: Dict) -> Dict:
    title = (article.get('title') or '').lower()
    content = article.get('content') or ''
    lead = content[:1200].lower()
    body = content[:5000].lower()

    best = None
    for name, aliases, lat, lng, place_type in SPACETIME_PLACES:
        title_hits = lead_hits = body_hits = 0
        evidence = []
        for alias in aliases:
            alias_lower, regex = _alias_matcher(alias)
            t = _alias_count(title, alias_lower, regex)
            l = _alias_count(lead, alias_lower, regex)
            b = _alias_count(body, alias_lower, regex)
            if t or l or b:
                evidence.append(alias)
            title_hits += t
            lead_hits += l
            body_hits += b
        if not (title_hits or lead_hits or body_hits):
            continue

        # 词频加权：标题命中权重最高，其次首段，最后全文。高频出现者胜出，
        # 避免“正文偶尔提到 U.S.”压过“首段明确写到瑞典”。
        score = 0.4 + min(0.3, title_hits * 0.15) + min(0.2, lead_hits * 0.05) + min(0.15, body_hits * 0.012)
        if title_hits:
            score = min(0.95, score + 0.05)   # 标题出现地名=强信号
        score = round(min(0.95, score), 2)

        location_type = place_type
        if any(word in f'{article.get("title") or ""}\n{content[:1200]}' for word in EVENT_WORDS):
            location_type = 'event_location' if place_type != 'jurisdiction' else place_type

        candidate = {
            'name': name,
            'lat': lat,
            'lng': lng,
            'type': location_type,
            'confidence': score,
            'evidence': '、'.join(evidence[:4]),
        }
        if not best or candidate['confidence'] > best['confidence']:
            best = candidate

    if best:
        return best

    normalized_domain = (article.get('domain') or '').replace('www.', '').lower()
    for domain_key, place_name in DOMAIN_FALLBACK_PLACES.items():
        if domain_key in normalized_domain:
            place = _find_place_by_name(place_name)
            if place:
                place.update({
                    'type': 'source_location',
                    'confidence': 0.35,
                    'evidence': f'domain:{article.get("domain") or ""}',
                })
                return place

    return {
        'name': 'Unknown',
        'lat': 22.3193,
        'lng': 114.1694,
        'type': 'unknown_default',
        'confidence': 0.12,
        'evidence': 'fallback',
    }


def _article_theme(article: Dict) -> str:
    text = f"{article.get('title') or ''} {article.get('matched_keywords') or ''} {article.get('content') or ''}".lower()
    themes = [
        ('family-office', ['家族办公室', '家族辦公室', 'family office']),
        ('family-trust', ['家族信托', '家族信託', 'family trust', 'trust']),
        ('regulation', ['监管', '監管', 'regulation', 'sfc', 'mas', 'compliance']),
        ('tax', ['税', '稅', 'tax']),
        ('fund', ['基金', 'fund']),
        ('succession', ['传承', '傳承', 'succession', 'estate planning']),
    ]
    for theme, needles in themes:
        if any(needle.lower() in text for needle in needles):
            return theme
    return 'general'


def analyze_article_spacetime(article: Dict) -> Dict:
    """Return a normalized spacetime profile for an article."""
    article = article or {}
    time_info = _parse_article_time(article)
    place = _infer_article_place(article)
    status = 'ready' if time_info.get('value') and place.get('name') != 'Unknown' else 'low_confidence'
    if not time_info.get('value'):
        status = 'failed'

    source_place = _infer_article_place({'domain': article.get('domain') or '', 'title': '', 'content': ''})
    metadata = {
        'theme': _article_theme(article),
        'source': {
            'crawler_engine': article.get('crawler_engine_used') or 'primary',
            'method': article.get('source_method') or article.get('extraction_method') or '',
        },
        'trajectory': {
            'from': source_place,
            'to': place,
        },
    }

    return {
        'article_id': article.get('id'),
        'time_value': time_info.get('value'),
        'time_type': time_info.get('type'),
        'time_confidence': time_info.get('confidence', 0),
        'time_evidence': time_info.get('evidence', ''),
        'location_name': place.get('name'),
        'location_lat': place.get('lat'),
        'location_lng': place.get('lng'),
        'location_type': place.get('type'),
        'location_confidence': place.get('confidence', 0),
        'location_evidence': place.get('evidence', ''),
        'source_location_name': source_place.get('name'),
        'source_location_lat': source_place.get('lat'),
        'source_location_lng': source_place.get('lng'),
        'event_location_name': place.get('name') if place.get('type') == 'event_location' else '',
        'event_location_lat': place.get('lat') if place.get('type') == 'event_location' else None,
        'event_location_lng': place.get('lng') if place.get('type') == 'event_location' else None,
        'jurisdiction_name': place.get('name') if place.get('type') == 'jurisdiction' else '',
        'spacetime_status': status,
        'analysis_version': ANALYSIS_VERSION,
        'metadata': metadata,
    }


def profile_to_point(article: Dict, profile: Dict) -> Optional[Dict]:
    """Convert an article and persisted profile into map API point format."""
    if not article or not profile or not profile.get('time_value'):
        return None

    content = re.sub(r'\s+', ' ', article.get('content') or '').strip()
    keywords = article.get('matched_keywords') or ''
    try:
        metadata = profile.get('metadata') or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata) if metadata else {}
    except Exception:
        metadata = {}

    importance = min(10, max(2, int((article.get('quality_score') or 40) / 14) + len(str(keywords).split(',')) // 3))
    return {
        'id': article.get('id'),
        'title': article.get('title') or '无标题',
        'url': article.get('url') or '',
        'domain': article.get('domain') or '',
        'summary': content[:180],
        'keywords': keywords,
        'theme': metadata.get('theme') or _article_theme(article),
        'importance': importance,
        'time': {
            'value': profile.get('time_value'),
            'type': profile.get('time_type'),
            'confidence': profile.get('time_confidence') or 0,
            'evidence': profile.get('time_evidence') or '',
        },
        'location': {
            'name': profile.get('location_name'),
            'lat': profile.get('location_lat'),
            'lng': profile.get('location_lng'),
            'type': profile.get('location_type'),
            'confidence': profile.get('location_confidence') or 0,
            'evidence': profile.get('location_evidence') or '',
        },
        'source': metadata.get('source') or {},
        'trajectory': metadata.get('trajectory') or {},
        'spacetime_status': profile.get('spacetime_status') or 'ready',
    }
