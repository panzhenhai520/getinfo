#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
文章管理API模块
提供独立的文章管理功能
"""

from flask import Blueprint, request, jsonify, render_template
from sqlite_database import sqlite_db
from decorators import login_required
from article_spacetime_analyzer import analyze_article_spacetime, profile_to_point
from industry_pack_runtime import active_industry_composition_service
from intel_contracts import parse_time_range, utc_text
from intel_database import intel_repository
import json
import re
from datetime import datetime
from utils import coerce_int, get_china_time


SPACETIME_REFERENCES = [
    {
        'title': 'Mordecai 3: A Neural Geoparser and Event Geocoder',
        'type': 'paper',
        'url': 'https://arxiv.org/abs/2303.13675',
        'note': '事件地理编码与文本地名消歧，可作为后续升级模型参考。'
    },
    {
        'title': 'TEI2GO: A Multilingual Approach for Fast Temporal Expression Identification',
        'type': 'paper',
        'url': 'https://arxiv.org/abs/2403.16804',
        'note': '多语言时间表达识别，适合新闻与网页文本的时间抽取。'
    },
    {
        'title': 'TimeML / ISO-TimeML',
        'type': 'standard',
        'url': 'http://www.timeml.org/',
        'note': '事件、时间表达和时间关系标注标准。'
    },
    {
        'title': 'HeidelTime',
        'type': 'tool',
        'url': 'https://github.com/HeidelTime/heideltime',
        'note': '经典时间表达识别与归一化工具。'
    },
    {
        'title': 'CLAVIN',
        'type': 'tool',
        'url': 'https://github.com/Berico-Technologies/CLAVIN',
        'note': '开源 geoparser，处理文本地名识别和地理消歧。'
    },
    {
        'title': 'deck.gl TripsLayer',
        'type': 'visualization',
        'url': 'https://deck.gl/docs/api-reference/geo-layers/trips-layer',
        'note': '可用于后续高性能时间轨迹和地图动画。'
    },
    {
        'title': 'Leaflet.TimeDimension',
        'type': 'visualization',
        'url': 'https://github.com/socib/Leaflet.TimeDimension',
        'note': '轻量时间维度地图插件，可参考时间轴交互。'
    },
]


def _split_keyword_value(value):
    return [item.strip() for item in re.split(r'[,，;；、\s]+', str(value or '')) if item.strip()]


def _parse_matched_keywords_for_map(value):
    parsed = set()

    def add(keyword):
        clean = str(keyword or '').strip()
        if clean.startswith('[标]') or clean.startswith('[標]'):
            clean = clean[3:].strip()
        elif clean.startswith('[文]'):
            clean = clean[3:].strip()
        if clean and not re.fullmatch(r'\d+', clean):
            parsed.add(clean)

    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        for item in value:
            add(item)
        return sorted(parsed)
    if isinstance(value, dict):
        for key in ('title_keywords', 'content_keywords', 'all_keywords'):
            for item in value.get(key) or []:
                add(item)
        return sorted(parsed)

    raw = str(value).strip()
    if not raw:
        return []
    if raw.startswith('{') or raw.startswith('['):
        try:
            return _parse_matched_keywords_for_map(json.loads(raw))
        except Exception:
            pass

    location_pattern = re.compile(r'(标题|標題|title|内容|內容|正文|content)\s*[\(:：]\s*([^）)]+)', re.I)
    for match in location_pattern.finditer(raw):
        for item in _split_keyword_value(match.group(2)):
            add(item)
    for token in [item.strip() for item in re.split(r'[,，;；]+', raw) if item.strip()]:
        add(token)
    return sorted(parsed)


def _is_clean_spacetime_article(article):
    """Homepage map must only expose keyword-related, cleaned article text."""
    keywords = _parse_matched_keywords_for_map(article.get('matched_keywords'))
    if not keywords:
        return False

    content = re.sub(r'\s+', ' ', article.get('content') or '').strip()
    if len(content) < 80:
        return False

    url_count = content.count('http://') + content.count('https://') + content.count('www.')
    if url_count > 0:
        return False

    ad_patterns = [
        r'广告|Advertisement|推广|赞助|Sponsored',
        r'点击下载|立即下载|扫码|二维码',
        r'上一篇|下一篇|相关阅读|热门推荐',
        r'分享到|微信扫一扫|关注公众号',
    ]
    if any(re.search(pattern, content, re.I) for pattern in ad_patterns):
        return False

    return True


def _quality_issue_report(article):
    content = (article or {}).get('content') or ''
    title = ((article or {}).get('title') or '').strip()
    url = ((article or {}).get('url') or '').strip()
    length = len(content.strip())
    issues = []

    if not title or title in {'无标题', 'None', 'null'}:
        issues.append('标题缺失')
    if not url:
        issues.append('URL缺失')
    if length == 0:
        issues.append('正文为空')
    elif length < 80:
        issues.append('正文过短')
    elif length < 300:
        issues.append('正文偏短')

    lowered = content.lower()
    if any(marker in lowered for marker in ('404 not found', 'page not found', 'access denied', 'forbidden')):
        issues.append('疑似错误页')
    if content.count('http://') + content.count('https://') > 8:
        issues.append('正文URL过多')

    try:
        from smart_article_extractor import evaluate_content_quality
        quality_score = int(evaluate_content_quality(content)) if content else 0
    except Exception:
        quality_score = int((article or {}).get('quality_score') or 0)
        if not quality_score:
            if length >= 1000:
                quality_score = 80
            elif length >= 300:
                quality_score = 65
            elif length >= 80:
                quality_score = 45

    stored_score = int((article or {}).get('quality_score') or 0)
    if stored_score and quality_score:
        quality_score = max(quality_score, stored_score)

    is_good = quality_score >= 60 and not any(
        issue in issues for issue in ('标题缺失', 'URL缺失', '正文为空', '正文过短', '疑似错误页')
    )
    return {
        'article_id': (article or {}).get('id'),
        'title': title,
        'url': url,
        'content_length': length,
        'quality_score': quality_score,
        'issues': issues,
        'issues_count': len(issues),
        'good': is_good
    }


def _diagnose_article_by_id(article_id):
    article = sqlite_db.get_article_by_id(article_id)
    if not article:
        return {'success': False, 'error': '文章不存在'}
    report = _quality_issue_report(article)
    report['success'] = True
    return report


def _diagnose_articles(limit=100):
    limit = coerce_int(limit, 100, 1, 5000)
    industry_pack_id, activation_id = _active_article_scope()
    articles, total_available = sqlite_db.get_articles(
        1, limit,
        industry_pack_id=industry_pack_id,
        activation_id=activation_id,
    )
    reports = [_quality_issue_report(article) for article in articles]
    issues_summary = {}
    for report in reports:
        for issue in report['issues']:
            issues_summary[issue] = issues_summary.get(issue, 0) + 1

    good = sum(1 for report in reports if report['good'])
    bad = len(reports) - good
    return {
        'total': len(reports),
        'total_available': total_available,
        'good': good,
        'bad': bad,
        'issues_summary': issues_summary,
        'bad_articles': [report for report in reports if not report['good']][:100]
    }


def _repair_article_content(article_id):
    article = sqlite_db.get_article_by_id(article_id)
    if not article:
        return {'success': False, 'error': '文章不存在'}

    old_report = _quality_issue_report(article)
    try:
        from smart_article_extractor import extract_article_content_from_url
        extracted = extract_article_content_from_url(article.get('url'), skip_db_check=True)
    except Exception as exc:
        return {'success': False, 'error': f'重新提取失败: {exc}'}

    if not extracted or not extracted.get('success'):
        return {
            'success': False,
            'error': (extracted or {}).get('error') or '重新提取未返回有效正文'
        }

    new_content = (extracted.get('content') or '').strip()
    if not new_content:
        return {'success': False, 'error': '重新提取后正文为空'}

    new_title = (extracted.get('title') or article.get('title') or '无标题').strip()
    new_quality = int(extracted.get('score') or extracted.get('quality_score') or 0)
    article_data = {
        'title': new_title,
        'content': new_content,
        'category_id': article.get('category_id'),
        'source_url_id': article.get('source_url_id'),
        'publish_date': extracted.get('publish_date') or article.get('publish_date'),
        'extraction_method': extracted.get('method') or 'manual_repair',
        'quality_score': new_quality
    }

    updated_id = sqlite_db.update_article(article_id, article_data)
    if not updated_id:
        return {'success': False, 'error': '数据库更新失败'}

    updated_article = sqlite_db.get_article_by_id(article_id)
    new_report = _quality_issue_report(updated_article)
    return {
        'success': True,
        'article_id': article_id,
        'old_quality': old_report['quality_score'],
        'new_quality': new_report['quality_score'],
        'old_issues': old_report['issues_count'],
        'new_issues': new_report['issues_count'],
        'improved': new_report['quality_score'] > old_report['quality_score'] or new_report['issues_count'] < old_report['issues_count']
    }

# 创建蓝图
article_management_bp = Blueprint('article_management', __name__, url_prefix='/article-management')

# ==================== 页面路由 ====================

@article_management_bp.route('/')
@login_required
def index():
    """文章管理主页"""
    # 行业包隔离：跟随会话感知的行业包身份（包用户=绑定包、管理员=会话所选包），
    # 不能用全局激活包快照——否则管理员切包/包用户登录都会看到全局激活包的内容。
    try:
        from industry_pack_runtime import active_industry_identity
        identity = active_industry_identity()
        active_industry = {
            'id': str(identity.get('id') or ''),
            'name': str(identity.get('name') or identity.get('id') or ''),
            'activation_id': str(identity.get('activation_id') or ''),
        }
    except Exception:
        active = active_industry_composition_service.snapshot()
        active_industry = {
            'id': active['active_industry_pack_id'],
            'name': active['primary_pack'].get('name') or active['active_industry_pack_id'],
            'activation_id': active.get('active_industry_activation_id') or '',
        }
    return render_template(
        'article_management.html',
        active_industry=active_industry,
    )


def _active_article_scope():
    # 行业包隔离：与页面渲染同一口径（会话感知），API 数据不落回全局激活包
    try:
        from industry_pack_runtime import active_industry_identity
        identity = active_industry_identity()
        return (
            str(identity.get('id') or ''),
            str(identity.get('activation_id') or ''),
        )
    except Exception:
        active = active_industry_composition_service.snapshot()
        return (
            str(active['active_industry_pack_id']),
            str(active.get('active_industry_activation_id') or ''),
        )

# ==================== API路由 ====================

@article_management_bp.route('/api/statistics', methods=['GET'])
@login_required
def get_statistics():
    """获取文章统计信息"""
    try:
        domain = request.args.get('domain')
        industry_pack_id, activation_id = _active_article_scope()
        stats = sqlite_db.get_statistics(
            domain,
            industry_pack_id=industry_pack_id,
            activation_id=activation_id,
        )
        
        return jsonify({
            'success': True,
            'stats': stats
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取统计信息失败: {str(e)}'
        }), 500

@article_management_bp.route('/api/articles', methods=['GET'])
@login_required
def get_articles():
    """获取文章列表"""
    try:
        page = coerce_int(request.args.get('page'), 1, 1)
        per_page = coerce_int(request.args.get('per_page'), 20, 1, 500)
        domain = request.args.get('domain')
        category_id = request.args.get('category_id')
        source_url_id = request.args.get('source_url_id')
        search = request.args.get('search')
        keyword = request.args.get('keyword')
        keywords = request.args.get('keywords')
        if keywords:
            try:
                parsed_keywords = json.loads(keywords)
                if isinstance(parsed_keywords, list):
                    keyword = parsed_keywords
            except Exception:
                keyword = [item.strip() for item in str(keywords).split('|') if item.strip()]
        
        # 转换category_id为整数
        if category_id:
            category_id = coerce_int(category_id, None, 1)
        
        # 转换source_url_id为整数
        if source_url_id:
            source_url_id = coerce_int(source_url_id, None, 1)
        
        industry_pack_id, activation_id = _active_article_scope()
        articles, total = sqlite_db.get_articles(
            page, per_page, domain, category_id, source_url_id, search, keyword,
            industry_pack_id=industry_pack_id,
            activation_id=activation_id,
        )
        
        return jsonify({
            'success': True,
            'articles': articles,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': (total + per_page - 1) // per_page
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取文章列表失败: {str(e)}'
        }), 500


@article_management_bp.route('/api/keyword-map', methods=['GET'])
@login_required
def get_keyword_map():
    """获取关键词信息图谱"""
    try:
        limit = coerce_int(request.args.get('limit'), 500, 1, 5000)
        industry_pack_id, activation_id = _active_article_scope()
        keywords = sqlite_db.get_keyword_map(
            limit,
            industry_pack_id=industry_pack_id,
            activation_id=activation_id,
        )
        # The graph is an industry graph, not a raw crawl-token cloud.  Keep
        # only configured industry terms; generic action words (发布、获奖、
        # 报告、政策等) remain classification signals and cannot become a
        # misleading standalone node.
        from industry_packs import industry_anchor_keywords, industry_pack_loader, normalize_intel_text
        pack = industry_pack_loader.load(industry_pack_id)
        allowed = {
            normalize_intel_text(item)
            for item in (
                industry_anchor_keywords(pack)
                + list(pack.get('core_keywords') or [])
                + list(pack.get('expanded_keywords') or [])
            )
            if normalize_intel_text(item)
        }
        keywords = [item for item in keywords if normalize_intel_text(item.get('keyword')) in allowed]
        return jsonify({
            'success': True,
            'keywords': keywords,
            'total_keywords': len(keywords),
            'total_keyword_articles': sum(item.get('article_count', 0) for item in keywords),
            'deduplicated': True,
            'industry_pack_id': industry_pack_id,
            'activation_id': activation_id,
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取关键词信息图谱失败: {str(e)}'
        }), 500


@article_management_bp.route('/api/keywords/merge-preview', methods=['POST'])
@login_required
def preview_keyword_merge():
    """Preview impact before merging keyword circles."""
    try:
        data = request.get_json(silent=True) or {}
        source_keywords = data.get('source_keywords') or data.get('sources') or []
        if isinstance(source_keywords, str):
            source_keywords = [source_keywords]
        target_keyword = data.get('target_keyword') or data.get('target') or ''

        preview = sqlite_db.preview_keyword_merge(source_keywords, target_keyword)
        return jsonify({
            'success': not bool(preview.get('error')),
            'preview': preview,
            'error': preview.get('error', '')
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'预览关键词合并失败: {str(e)}'
        }), 500


@article_management_bp.route('/api/keywords/merge', methods=['POST'])
@login_required
def merge_keywords():
    """Persist canonical keyword merge rules and sync task keyword fields."""
    try:
        data = request.get_json(silent=True) or {}
        source_keywords = data.get('source_keywords') or data.get('sources') or []
        if isinstance(source_keywords, str):
            source_keywords = [source_keywords]
        target_keyword = data.get('target_keyword') or data.get('target') or ''
        apply_to_tasks = data.get('apply_to_tasks', True)

        result = sqlite_db.merge_keywords(
            source_keywords=source_keywords,
            target_keyword=target_keyword,
            apply_to_tasks=bool(apply_to_tasks),
            created_by='web',
        )
        status_code = 200 if result.get('success') else 400
        return jsonify(result), status_code
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'合并关键词失败: {str(e)}'
        }), 500


@article_management_bp.route('/api/keywords/hide', methods=['POST'])
@login_required
def hide_keyword():
    """Hide a keyword circle without changing crawl rules."""
    try:
        data = request.get_json(silent=True) or {}
        keyword = data.get('keyword') or ''
        reason = data.get('reason') or ''
        result = sqlite_db.set_keyword_node_state(keyword, 'hidden', reason=reason, created_by='web')
        return jsonify(result), (200 if result.get('success') else 400)
    except Exception as e:
        return jsonify({'success': False, 'error': f'隐藏关键词失败: {str(e)}'}), 500


@article_management_bp.route('/api/keywords/show', methods=['POST'])
@login_required
def show_keyword():
    """Show a previously hidden keyword circle."""
    try:
        data = request.get_json(silent=True) or {}
        keyword = data.get('keyword') or ''
        result = sqlite_db.set_keyword_node_state(keyword, 'visible', reason='', created_by='web')
        return jsonify(result), (200 if result.get('success') else 400)
    except Exception as e:
        return jsonify({'success': False, 'error': f'恢复关键词失败: {str(e)}'}), 500


@article_management_bp.route('/api/keywords/delete-preview', methods=['POST'])
@login_required
def preview_keyword_delete():
    """Create a pending keyword delete preview job."""
    try:
        data = request.get_json(silent=True) or {}
        keyword = data.get('keyword') or ''
        result = sqlite_db.create_keyword_delete_preview(keyword, created_by='web')
        return jsonify(result), (200 if result.get('success') else 400)
    except Exception as e:
        return jsonify({'success': False, 'error': f'预览关键词删除失败: {str(e)}'}), 500


@article_management_bp.route('/api/keywords/delete', methods=['POST'])
@login_required
def delete_keyword():
    """Confirm keyword delete job and delete mapped RAGFlow documents by exact id."""
    try:
        data = request.get_json(silent=True) or {}
        job_id = coerce_int(data.get('job_id'), 0, 1)
        confirm_token = data.get('confirm_token') or ''
        delete_ragflow = data.get('delete_ragflow', True)

        result = sqlite_db.execute_keyword_delete_job(job_id, confirm_token, created_by='web')
        if not result.get('success'):
            return jsonify(result), 400

        ragflow_docs = result.get('ragflow_documents') or []
        if not delete_ragflow or not ragflow_docs:
            sqlite_db.update_keyword_delete_job_status(
                job_id,
                'completed',
                'skipped' if not delete_ragflow else 'completed',
            )
            result['ragflow_delete_status'] = 'skipped' if not delete_ragflow else 'completed'
            return jsonify(result)

        delete_errors = []
        grouped = {}
        for doc in ragflow_docs:
            kb_id = doc.get('kb_id')
            doc_id = doc.get('document_id')
            if kb_id and doc_id:
                grouped.setdefault(kb_id, []).append(doc_id)

        try:
            from ragflow_client import RagflowClient
            client = RagflowClient()
            for kb_id, doc_ids in grouped.items():
                try:
                    client.delete_documents(kb_id, doc_ids)
                except Exception as exc:
                    delete_errors.append({'kb_id': kb_id, 'document_ids': doc_ids, 'error': str(exc)})
                    for doc_id in doc_ids:
                        sqlite_db.update_article_ragflow_document_status(kb_id, doc_id, 'delete_failed', str(exc))
        except Exception as exc:
            delete_errors.append({'error': str(exc)})

        if delete_errors:
            sqlite_db.update_keyword_delete_job_status(
                job_id,
                'partial_failed',
                'partial_failed',
                json.dumps(delete_errors, ensure_ascii=False),
            )
            result['success'] = False
            result['ragflow_delete_status'] = 'partial_failed'
            result['ragflow_delete_errors'] = delete_errors
            return jsonify(result), 207

        sqlite_db.update_keyword_delete_job_status(job_id, 'completed', 'completed')
        result['ragflow_delete_status'] = 'completed'
        result['ragflow_deleted_documents'] = len(ragflow_docs)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': f'删除关键词失败: {str(e)}'}), 500


@article_management_bp.route('/api/spacetime', methods=['GET'])
@login_required
def get_spacetime_points():
    """获取当前激活行业聚合文章的时空地图点位数据。"""
    try:
        limit = coerce_int(request.args.get('limit'), 1000, 1, 5000)
        min_confidence = float(request.args.get('min_confidence') or 0.3)
        from_date = request.args.get('from') or None
        to_date = request.args.get('to') or None
        keyword = request.args.get('keyword') or None
        # 会话感知的行业包身份（包用户=绑定包、管理员=会话所选包），
        # 不能用全局激活包快照——否则首页地图数据返回全局包，前端 applyIndustryIdentity
        # 会用错误包名覆盖首页标题（曾出现选汽车却显示"具身智能资讯"）。
        try:
            from industry_pack_runtime import active_industry_identity
            _sp_identity = active_industry_identity()
            industry_pack_id = str(_sp_identity.get('id') or '')
            activation_id = str(_sp_identity.get('activation_id') or '')
            _sp_pack_name = str(_sp_identity.get('name') or industry_pack_id)
            _sp_pack_version = str(_sp_identity.get('pack_version') or '')
            _sp_map_enabled = bool(_sp_identity.get('show_spatiotemporal_map', True))
        except Exception:
            active = active_industry_composition_service.snapshot()
            industry_pack_id = str(active['active_industry_pack_id'])
            activation_id = str(active.get('active_industry_activation_id') or '')
            _sp_pack_name = str(active['primary_pack'].get('name') or industry_pack_id)
            _sp_pack_version = str(active['primary_pack'].get('pack_version') or '')
            _sp_map_enabled = True
        # 时空地图未启用的行业包：直接返回空点位，不查库、不做回填分析，
        # 避免该接口在移动端首屏造成无意义开销（前端此时也不会调用）。
        if not _sp_map_enabled:
            return jsonify({
                'success': True,
                'points': [],
                'time_range': {'from': None, 'to': None},
                'stats': {'total_points': 0, 'low_confidence_points': 0, 'themes': {}},
                'industry_pack': {
                    'id': industry_pack_id,
                    'name': _sp_pack_name,
                    'pack_version': _sp_pack_version,
                    'activation_id': activation_id,
                },
                'references': SPACETIME_REFERENCES,
            })
        if not from_date and not to_date:
            start, end = parse_time_range(
                f"{intel_repository.dashboard_window_days()}d"
            )
            from_date = utc_text(start)
            to_date = utc_text(end)

        rows = sqlite_db.get_article_spacetime_points(
            from_date=from_date,
            to_date=to_date,
            keyword=keyword,
            min_confidence=min_confidence,
            limit=limit,
            industry_pack_id=industry_pack_id,
            activation_id=activation_id,
        )

        if not rows:
            backfill_articles = sqlite_db.get_articles_for_spacetime_analysis(
                mode='incremental',
                limit=min(limit, 500),
                min_confidence=min_confidence,
                industry_pack_id=industry_pack_id,
                activation_id=activation_id,
            )
            for article in backfill_articles:
                profile = analyze_article_spacetime(article)
                sqlite_db.save_article_spacetime_profile(article.get('id'), profile)
            rows = sqlite_db.get_article_spacetime_points(
                from_date=from_date,
                to_date=to_date,
                keyword=keyword,
                min_confidence=min_confidence,
                limit=limit,
                industry_pack_id=industry_pack_id,
                activation_id=activation_id,
            )

        points = []
        for row in rows:
            industry_keywords = (
                row.get('industry_matched_keywords')
                or row.get('matched_keywords')
                or ''
            )
            article = {
                'id': row.get('id'),
                'title': row.get('title'),
                'url': row.get('url'),
                'domain': row.get('domain'),
                'content': row.get('content'),
                'matched_keywords': industry_keywords,
                'quality_score': row.get('quality_score'),
                'crawler_engine_used': row.get('crawler_engine_used'),
                'source_method': row.get('source_method'),
                'extraction_method': row.get('extraction_method'),
            }
            if not _is_clean_spacetime_article(article):
                continue
            point = profile_to_point(article, row)
            if point:
                point['industry_pack_id'] = industry_pack_id
                point['activation_id'] = activation_id
                point['article'] = {
                    'article_id': row.get('id'),
                    'url': row.get('url'),
                    'title': row.get('title'),
                    'domain': row.get('domain'),
                    'publish_date': row.get('publish_date'),
                    'effective_time': row.get('time_value'),
                    'content_length': row.get('content_length') or len(
                        str(row.get('content') or '')
                    ),
                    'content_preview': str(row.get('content') or '')[:360],
                    'matched_keywords': _parse_matched_keywords_for_map(
                        industry_keywords
                    ),
                    'classification_keywords': _parse_matched_keywords_for_map(
                        industry_keywords
                    ),
                    'source_display_name': row.get('source_display_name'),
                    'final_category': row.get('final_category'),
                    'final_confidence': row.get('final_confidence'),
                    'final_reason': row.get('final_reason'),
                    'why_important': row.get('why_important'),
                    'trend_summary': row.get('trend_summary'),
                    'industry_pack_id': industry_pack_id,
                    'activation_id': activation_id,
                }
                points.append(point)

        times = [point['time']['value'] for point in points if point.get('time', {}).get('value')]
        stats = {
            'total_points': len(points),
            'low_confidence_points': sum(1 for point in points if point.get('location', {}).get('confidence', 0) < 0.5),
            'themes': {},
        }
        for point in points:
            theme = point.get('theme') or 'general'
            stats['themes'][theme] = stats['themes'].get(theme, 0) + 1

        return jsonify({
            'success': True,
            'points': points,
            'time_range': {
                'from': min(times) if times else None,
                'to': max(times) if times else None,
            },
            'stats': stats,
            'industry_pack': {
                'id': industry_pack_id,
                'name': _sp_pack_name,
                'pack_version': _sp_pack_version,
                'activation_id': activation_id,
            },
            'references': SPACETIME_REFERENCES,
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取时空地图数据失败: {str(e)}'
        }), 500

@article_management_bp.route('/api/article/<int:article_id>', methods=['GET'])
@login_required
def get_article_detail(article_id):
    """获取文章详情（多租户：包用户只可读其授权包下的文章，防越包读取）"""
    try:
        from pack_tenant import current_pack_id_or_none
        _owned = current_pack_id_or_none()
        if _owned:
            with sqlite_db.lock:
                cur = sqlite_db.connection.cursor()
                cur.execute(
                    "SELECT 1 FROM article_intel_classifications WHERE article_id=? AND industry_pack_id=? "
                    "UNION ALL "
                    "SELECT 1 FROM intel_topic_articles ta JOIN intel_topics t ON ta.topic_id=t.id "
                    "  WHERE ta.article_id=? AND t.industry_pack_id=? LIMIT 1",
                    (int(article_id), _owned, int(article_id), _owned),
                )
                _ok = cur.fetchone() is not None
                cur.close()
            if not _ok:
                return jsonify({'success': False, 'error': '无权访问该文章（不属于你的行业包）'}), 403
        article = sqlite_db.get_article_by_id(article_id)
        # 主题标签按用户优先（隔离）：该用户自定义过主题时，附上"他自己视角"的主题标签。
        # 采用**附加字段**而不是覆盖 topic_tags——避免把现有展示改坏；前端可优先读这两个字段。
        # 没有个人主题的用户不产生这些字段，行为与原来完全一致。
        try:
            from pack_user_gate import current_pack_user_id as _detail_current_uid
            _detail_uid = _detail_current_uid()
            if _detail_uid and isinstance(article, dict):
                with sqlite_db.lock:
                    _detail_cur = sqlite_db.connection.cursor()
                    try:
                        _detail_row = _detail_cur.execute(
                            "SELECT topic_keys_json FROM article_user_visibility "
                            "WHERE article_id=? AND pack_user_id=?",
                            (int(article_id), int(_detail_uid)),
                        ).fetchone()
                    finally:
                        _detail_cur.close()
                _detail_keys = []
                if _detail_row:
                    try:
                        _detail_keys = json.loads(dict(_detail_row).get("topic_keys_json") or "[]") or []
                    except Exception:
                        _detail_keys = []
                if _detail_keys:
                    _detail_names = {}
                    try:
                        from industry_packs import industry_pack_loader as _detail_loader
                        _detail_pack = _detail_loader.load(_owned, enabled_only=False) if _owned else {}
                        for _detail_topic in (_detail_pack.get("fixed_topics") or []):
                            _detail_names[str(_detail_topic.get("key"))] = str(
                                _detail_topic.get("name") or _detail_topic.get("key"))
                    except Exception:
                        _detail_names = {}
                    article = dict(article)
                    article["personal_topic_keys"] = [str(k) for k in _detail_keys]
                    article["personal_topic_names"] = [_detail_names.get(str(k), str(k)) for k in _detail_keys]
        except Exception:
            pass
        # ② 主题关键词：返回"该文章归属主题"的关键词，供详情页显示与正文高亮。
        # 说明：article.matched_keywords 是**行业准入**命中的词（整车/智能座舱/新能源汽车…），
        # 与主题关键词不是一回事 ✗ —— 详情页要展示并高亮的是后者 ✓
        try:
            if article is not None:
                article = dict(article)          # 该库返回 CompatRow 行对象，不是 dict ✓
                with sqlite_db.lock:
                    _tk_cur = sqlite_db.connection.cursor()
                    try:
                        _tk_rows = _tk_cur.execute(
                            "SELECT t.topic_key, t.topic_name, t.keywords_json "
                            "FROM intel_topic_articles ta JOIN intel_topics t ON t.id = ta.topic_id "
                            "WHERE ta.article_id=? ORDER BY ta.association_score DESC",
                            (int(article_id),),
                        ).fetchall()
                    finally:
                        _tk_cur.close()
                _tk_keys, _tk_names, _tk_words = [], [], []
                for _row in _tk_rows:
                    _d = dict(_row)
                    _tk_keys.append(str(_d.get("topic_key") or ""))
                    _tk_names.append(str(_d.get("topic_name") or ""))
                    try:
                        for _w in json.loads(_d.get("keywords_json") or "[]"):
                            _w = str(_w).strip()
                            if _w and _w not in _tk_words:
                                _tk_words.append(_w)
                    except Exception:
                        pass
                if _tk_keys:
                    article = dict(article)
                    article["topic_keys"] = _tk_keys
                    article["topic_names"] = _tk_names
                    article["topic_keywords"] = _tk_words
        except Exception:
            pass
        # 🔥 阶段3：无正文条目的原文链接 → 命中转换缓存则附上 Markdown（只读展示）
        try:
            if article is not None and len(str(article.get('content') or '').strip()) < 20:
                _a_url = str(article.get('url') or '')
                if _a_url.startswith(('http://', 'https://')):
                    from dynamic_link_converter import get_cached_markdown
                    _dm = get_cached_markdown(sqlite_db, _a_url)
                    article = dict(article)
                    article["converted"] = bool(_dm)
                    if _dm:
                        article["dynamic_markdown"] = _dm
        except Exception:
            pass
        if article:
            return jsonify({
                'success': True,
                'article': article
            })
        else:
            return jsonify({
                'success': False,
                'error': '文章不存在'
            }), 404
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取文章详情失败: {str(e)}'
        }), 500

@article_management_bp.route('/api/article/<int:article_id>', methods=['DELETE'])
@login_required
def delete_article(article_id):
    """删除文章"""
    try:
        # 行业包用户只有后台开启「允许删除文章」才能删除（管理员不受限）。
        # 前端按钮已按该开关隐藏，这里兜底拦截直接调接口的请求。
        try:
            from pack_tenant import me_pack_user
            _pack_user = me_pack_user()
            if _pack_user is not None and not bool(_pack_user.get('can_delete_articles')):
                return jsonify({
                    'success': False,
                    'error': '当前账号未开启删除文章权限'
                }), 403
        except Exception:
            pass
        success = sqlite_db.delete_article(article_id)
        if success:
            return jsonify({
                'success': True,
                'message': '文章删除成功'
            })
        else:
            return jsonify({
                'success': False,
                'error': '文章删除失败'
            }), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'删除文章失败: {str(e)}'
        }), 500

@article_management_bp.route('/api/articles/batch-delete', methods=['POST'])
@login_required
def batch_delete_articles():
    """批量删除文章"""
    try:
        data = request.get_json(silent=True) or {}
        article_ids = data.get('article_ids', [])

        # 行业包用户只有后台开启「允许删除文章」才能删除（管理员不受限）
        try:
            from pack_tenant import me_pack_user
            _pack_user = me_pack_user()
            if _pack_user is not None and not bool(_pack_user.get('can_delete_articles')):
                return jsonify({
                    'success': False,
                    'error': '当前账号未开启删除文章权限'
                }), 403
        except Exception:
            pass

        if not article_ids:
            return jsonify({
                'success': False,
                'error': '缺少文章ID列表'
            }), 400
        
        deleted_count = 0
        failed_items = []
        
        for article_id in article_ids:
            if sqlite_db.delete_article(article_id):
                deleted_count += 1
            else:
                failed_items.append(article_id)
        
        return jsonify({
            'success': True,
            'deleted_count': deleted_count,
            'failed_items': failed_items,
            'message': f'成功删除 {deleted_count} 篇文章'
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'批量删除失败: {str(e)}'
        }), 500

@article_management_bp.route('/api/articles/clear-local', methods=['POST'])
@login_required
def clear_local_articles():
    """Hard-clear local article data for test resets."""
    try:
        # 危险操作：仅管理员或行业后台开启「允许删除文章」的包用户可执行
        try:
            from pack_tenant import me_pack_user
            _pack_user = me_pack_user()
            if _pack_user is not None and not bool(_pack_user.get('can_delete_articles')):
                return jsonify({
                    'success': False,
                    'error': '当前账号未开启删除文章权限'
                }), 403
        except Exception:
            pass
        data = request.get_json(silent=True) or {}
        if data.get('confirm') != 'DELETE_LOCAL_ARTICLES':
            return jsonify({
                'success': False,
                'error': 'confirmation required'
            }), 400

        result = sqlite_db.clear_local_articles()
        if result.get('success'):
            return jsonify({
                'success': True,
                'result': result,
                'message': 'local articles cleared'
            })

        return jsonify({
            'success': False,
            'result': result,
            'error': 'failed to clear local articles'
        }), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'clear local articles failed: {str(e)}'
        }), 500

@article_management_bp.route('/api/domains', methods=['GET'])
@login_required
def get_domains():
    """获取所有域名列表"""
    try:
        industry_pack_id, activation_id = _active_article_scope()
        stats = sqlite_db.get_statistics(
            industry_pack_id=industry_pack_id,
            activation_id=activation_id,
        )
        domain_stats = stats.get('domain_stats', {})
        
        domains = [
            {'domain': domain, 'count': count}
            for domain, count in domain_stats.items()
        ]
        
        return jsonify({
            'success': True,
            'domains': domains
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'获取域名列表失败: {str(e)}'
        }), 500

@article_management_bp.route('/api/export', methods=['GET'])
@login_required
def export_articles():
    """导出文章数据（支持按分类、来源URL、域名筛选）"""
    try:
        format_type = request.args.get('format', 'zip')  # 默认zip格式
        domain = request.args.get('domain')
        category_id = request.args.get('category_id')
        source_url_id = request.args.get('source_url_id')
        search = request.args.get('search')
        
        # 转换ID参数
        if category_id:
            category_id = coerce_int(category_id, None, 1)
        if source_url_id:
            source_url_id = coerce_int(source_url_id, None, 1)
        
        # 获取所有匹配的文章
        keyword = request.args.get('keyword')
        industry_pack_id, activation_id = _active_article_scope()
        articles, total = sqlite_db.get_articles(
            1, 10000, domain, category_id, source_url_id, search, keyword,
            industry_pack_id=industry_pack_id,
            activation_id=activation_id,
        )
        
        if format_type == 'json':
            return jsonify({
                'success': True,
                'articles': articles,
                'total': total,
                'exported_at': get_china_time().isoformat()
            })
        elif format_type == 'zip':
            # 生成ZIP文件
            from io import BytesIO
            import zipfile
            
            memory_file = BytesIO()
            
            with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
                # 生成文件名前缀
                prefix = ''
                if category_id:
                    # 获取分类名称
                    cat = sqlite_db.get_category_by_id(category_id)
                    if cat:
                        prefix = f"{cat['name']}_"
                elif source_url_id:
                    # 获取来源URL名称
                    url = sqlite_db.get_managed_url_by_id(source_url_id)
                    if url:
                        prefix = f"{url['name']}_"
                elif domain:
                    prefix = f"{domain}_"
                
                # 添加汇总文件
                summary = f"文章下载报告\n"
                summary += f"{'='*60}\n"
                summary += f"下载时间: {get_china_time().strftime('%Y-%m-%d %H:%M:%S')}\n"
                summary += f"总文章数: {total}\n"
                if category_id:
                    cat = sqlite_db.get_category_by_id(category_id)
                    summary += f"筛选条件: 分类 = {cat['name'] if cat else '未知'}\n"
                elif source_url_id:
                    url = sqlite_db.get_managed_url_by_id(source_url_id)
                    summary += f"筛选条件: 来源 = {url['name'] if url else '未知'}\n"
                elif domain:
                    summary += f"筛选条件: 域名 = {domain}\n"
                summary += f"{'='*60}\n\n"
                
                for i, article in enumerate(articles, 1):
                    summary += f"{i}. {article['title']}\n"
                    summary += f"   URL: {article['url']}\n"
                    summary += f"   发布日期: {article.get('publish_date', '未知')}\n\n"
                
                zf.writestr("00_文章列表.txt", summary.encode('utf-8'))
                
                # 添加每篇文章
                for i, article in enumerate(articles, 1):
                    filename = f"{str(i).zfill(3)}_{article['title'][:50]}.txt"
                    # 移除文件名中的非法字符
                    filename = filename.replace('/', '_').replace('\\', '_').replace(':', '_').replace('*', '_').replace('?', '_').replace('"', '_').replace('<', '_').replace('>', '_').replace('|', '_')
                    
                    # 获取文章内容并清理元数据
                    content = article.get('content', '无内容')
                    
                    # 清理内容开头的元数据（标题、URL、发布日期、来源、分类、聚合时间、分隔线）
                    import re
                    lines = content.split('\n')
                    cleaned_lines = []
                    skip_metadata = True
                    
                    for line in lines:
                        # 检测元数据行
                        if skip_metadata:
                            # 跳过以这些关键词开头的行
                            if (line.startswith('标题:') or 
                                line.startswith('URL:') or 
                                line.startswith('发布日期:') or 
                                line.startswith('来源:') or 
                                line.startswith('分类:') or 
                                line.startswith('聚合时间:') or
                                line.strip() == '=' * 60 or
                                line.strip() == ''):
                                continue
                            else:
                                # 遇到第一行非元数据内容，停止跳过
                                skip_metadata = False
                        
                        if not skip_metadata:
                            cleaned_lines.append(line)
                    
                    content = '\n'.join(cleaned_lines).strip()
                    
                    zf.writestr(filename, content.encode('utf-8'))
            
            memory_file.seek(0)
            
            # 生成下载文件名
            timestamp = get_china_time().strftime('%Y%m%d_%H%M%S')
            download_filename = f"{prefix}文章_{timestamp}.zip"
            
            from flask import send_file
            return send_file(
                memory_file,
                mimetype='application/zip',
                as_attachment=True,
                download_name=download_filename
            )
        else:
            return jsonify({
                'success': False,
                'error': '不支持的导出格式'
            }), 400
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'导出失败: {str(e)}'
        }), 500


# ==================== 诊断和修复API ====================

@article_management_bp.route('/api/diagnose', methods=['POST'])
@login_required
def diagnose_articles():
    """诊断文章内容质量"""
    try:
        data = request.get_json(silent=True) or {}
        article_id = data.get('article_id')
        limit = data.get('limit', 100)
        
        if article_id:
            # 诊断单篇文章
            result = _diagnose_article_by_id(article_id)
            return jsonify({
                'success': True,
                'result': result
            })
        else:
            # 诊断所有文章
            result = _diagnose_articles(limit=limit)
            return jsonify({
                'success': True,
                'result': result
            })
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'诊断失败: {str(e)}'
        }), 500


@article_management_bp.route('/api/fix-article', methods=['POST'])
@login_required
def fix_single_article():
    """修复单篇文章"""
    try:
        data = request.get_json(silent=True) or {}
        article_id = data.get('article_id')
        
        if not article_id:
            return jsonify({
                'success': False,
                'error': '缺少article_id参数'
            }), 400
        
        result = _repair_article_content(article_id)
        return jsonify(result)
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'修复失败: {str(e)}'
        }), 500


@article_management_bp.route('/api/batch-fix', methods=['POST'])
@login_required
def batch_fix_articles():
    """批量修复文章"""
    try:
        data = request.get_json(silent=True) or {}
        article_ids = data.get('article_ids')
        quality_threshold = data.get('quality_threshold', 60)
        
        if article_ids:
            target_ids = article_ids
        else:
            diagnosis = _diagnose_articles(limit=5000)
            target_ids = [
                item['article_id']
                for item in diagnosis.get('bad_articles', [])
                if item.get('quality_score', 0) < quality_threshold
            ]

        result = {
            'total': len(target_ids),
            'success': 0,
            'no_improvement': 0,
            'failed': 0,
            'items': []
        }
        for item_id in target_ids:
            item_result = _repair_article_content(item_id)
            result['items'].append(item_result)
            if item_result.get('success'):
                if item_result.get('improved'):
                    result['success'] += 1
                else:
                    result['no_improvement'] += 1
            else:
                result['failed'] += 1
        
        return jsonify({
            'success': True,
            'result': result
        })
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'批量修复失败: {str(e)}'
        }), 500
