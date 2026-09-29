#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能URL转换器 - 自动识别并转换移动版链接
无需配置，自动检测URL模式
"""

import re
import requests
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

# 移动版URL的常见特征（自动识别）
MOBILE_URL_PATTERNS = [
    r'/m/',           # /m/article/123
    r'/mobile/',      # /mobile/article/123
    r'/mobi/',        # /mobi/article/123
    r'/mobarticle',   # /mobarticle2/id/123
    r'/wap/',         # /wap/article/123
    r'/landing/',     # /landing/article/123
    r'\.m\.',         # m.example.com
    r'mobile\.',      # mobile.example.com
]

# 移动版URL参数特征
MOBILE_URL_PARAMS = [
    'mobile=',
    'm=1',
    'device=mobile',
]


def is_mobile_url(url: str) -> bool:
    """
    智能检测URL是否为移动版
    
    Args:
        url: 要检测的URL
        
    Returns:
        bool: 是否为移动版URL
    """
    if not url:
        return False
    
    # 检查路径特征
    for pattern in MOBILE_URL_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return True
    
    # 检查参数特征
    for param in MOBILE_URL_PARAMS:
        if param in url.lower():
            return True
    
    return False


def extract_article_id_and_slug(url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    从URL中提取文章ID和标题slug
    
    Args:
        url: 文章URL
        
    Returns:
        Tuple[str, str]: (文章ID, 标题slug)，如果找不到则返回(None, None)
    """
    # 常见的ID+slug模式
    id_slug_patterns = [
        r'/id/(\d+)/(.*?)(?:\?|#|$)',              # /id/12345/title
        r'/article/(\d+)/(.*?)(?:\?|#|$)',         # /article/12345/title
        r'/news/(\d+)/(.*?)(?:\?|#|$)',            # /news/12345/title
        r'/post/(\d+)/(.*?)(?:\?|#|$)',            # /post/12345/title
    ]
    
    for pattern in id_slug_patterns:
        match = re.search(pattern, url)
        if match:
            article_id = match.group(1)
            slug = match.group(2).rstrip('/')
            return (article_id, slug if slug else None)
    
    # 如果没有slug，尝试只提取ID
    id_patterns = [
        r'/id/(\d+)',              # /id/12345
        r'/article/(\d+)',         # /article/12345
        r'/news/(\d+)',            # /news/12345
        r'/post/(\d+)',            # /post/12345
        r'/p/(\d+)',               # /p/12345
        r'/(\d{4,})',              # 至少4位数字
    ]
    
    for pattern in id_patterns:
        match = re.search(pattern, url)
        if match:
            return (match.group(1), None)
    
    return (None, None)


def extract_article_id(url: str) -> Optional[str]:
    """
    从URL中提取文章ID（向后兼容函数）
    
    Args:
        url: 文章URL
        
    Returns:
        str: 文章ID，如果找不到则返回None
    """
    article_id, _ = extract_article_id_and_slug(url)
    return article_id


def guess_desktop_url(url: str, source_url: str = None) -> List[str]:
    """
    🔥 通用智能URL转换器 - 自动分析URL模式，无需网站特定配置
    
    Args:
        url: 移动版URL
        source_url: 来源列表页URL（用于智能推断栏目路径）
        
    Returns:
        List[str]: 可能的桌面版URL列表（按优先级排序）
    
    策略：
        1. 从来源URL提取路径模式（如 /property/secondhand）
        2. 从移动版URL提取文章ID
        3. 智能组合成桌面版URL
    """
    if not is_mobile_url(url):
        return [url]  # 不是移动版，直接返回
    
    parsed = urlparse(url)
    domain = parsed.netloc
    path = parsed.path
    
    # 提取文章ID和标题slug
    article_id, slug = extract_article_id_and_slug(url)
    slug_part = f'/{slug}' if slug else ''
    
    candidates = []
    
    # ========== 通用策略1: 从来源URL智能推断 ==========
    if source_url and article_id:
        source_parsed = urlparse(source_url)
        source_path = source_parsed.path.rstrip('/')
        
        # 提取来源URL的基础路径（去掉文件名、参数等）
        # 例如：/property/secondhand → 用这个作为基础路径
        path_parts = [p for p in source_path.split('/') if p]
        
        # 🔥 智能组合策略
        if path_parts:
            base_path = '/' + '/'.join(path_parts)  # 如: /property/secondhand
            
            # 尝试多种组合方式
            formats = [
                f'{base_path}/article/id/{article_id}{slug_part}',     # /property/secondhand/article/id/123/title
                f'{base_path}/article/{article_id}{slug_part}',        # /property/secondhand/article/123/title
                f'/{path_parts[0]}/article/id/{article_id}{slug_part}', # /property/article/id/123/title
                f'/{path_parts[0]}/article/{article_id}{slug_part}',   # /property/article/123/title
            ]
            
            for fmt in formats:
                candidates.append(f"{parsed.scheme}://{domain}{fmt}")
    
    # ========== 通用策略2: 直接替换移动版标识 ==========
    if article_id:
        # 常见的文章URL格式（通用）
        common_formats = [
            f'/article/id/{article_id}{slug_part}',
            f'/article/{article_id}{slug_part}',
            f'/news/{article_id}{slug_part}',
            f'/post/{article_id}{slug_part}',
            f'/p/{article_id}{slug_part}',
            f'/content/{article_id}{slug_part}',
        ]
        
        for fmt in common_formats:
            candidates.append(f"{parsed.scheme}://{domain}{fmt}")
    
    # 策略1: 替换移动版标识
    for pattern in MOBILE_URL_PATTERNS:
        if re.search(pattern, path, re.IGNORECASE):
            # 移动版路径 -> 标准路径
            desktop_path = re.sub(pattern, '/', path, flags=re.IGNORECASE)
            desktop_path = re.sub(r'/+', '/', desktop_path)  # 清理多余斜杠
            candidates.append(f"{parsed.scheme}://{domain}{desktop_path}")
    
    # 策略2: 使用文章ID构造通用格式
    if article_id:
        # 通用格式
        slug_part = f'/{slug}' if slug else ''
        common_formats = [
            f'/article/{article_id}{slug_part}',
            f'/news/{article_id}{slug_part}',
            f'/post/{article_id}{slug_part}',
            f'/articles/{article_id}{slug_part}',
            f'/p/{article_id}{slug_part}',
        ]
        
        for format_path in common_formats:
            candidates.append(f"{parsed.scheme}://{domain}{format_path}")
    
    # 策略3: 移除子域名中的移动标识
    if re.search(r'\b(m|mobile|wap)\b', domain, re.IGNORECASE):
        desktop_domain = re.sub(r'\b(m|mobile|wap)\.', '', domain, flags=re.IGNORECASE)
        candidates.append(f"{parsed.scheme}://{desktop_domain}{path}")
    
    # 策略4: 移除URL参数中的移动标识
    if parsed.query:
        desktop_url = f"{parsed.scheme}://{domain}{path}"
        candidates.append(desktop_url)
    
    # 去重并保持顺序
    seen = set()
    unique_candidates = []
    for candidate in candidates:
        if candidate not in seen and candidate != url:
            seen.add(candidate)
            unique_candidates.append(candidate)
    
    return unique_candidates if unique_candidates else [url]


def verify_url_accessible(url: str, timeout: int = 5) -> Tuple[bool, Optional[str]]:
    """
    验证URL是否可访问
    
    Args:
        url: 要验证的URL
        timeout: 超时时间（秒）
        
    Returns:
        Tuple[bool, str]: (是否可访问, 修复后的URL或None)
    """
    try:
        response = requests.head(url, timeout=timeout, allow_redirects=True, verify=False)
        
        is_accessible = 200 <= response.status_code < 400
        return (is_accessible, None)
    except:
        return (False, None)


def detect_space_encoding_style(url: str) -> str:
    """
    自动检测URL中空格的编码风格
    
    Args:
        url: 原始URL
        
    Returns:
        str: 'plus' 或 'percent' 或 'unknown'
    """
    if '+' in url:
        return 'plus'
    elif '%20' in url:
        return 'percent'
    else:
        return 'unknown'


def normalize_url_encoding(url: str, style: str = 'auto') -> str:
    """
    规范化URL编码，统一空格的编码方式
    
    Args:
        url: 原始URL
        style: 编码风格 'plus'(使用+) 或 'percent'(使用%20) 或 'auto'(自动检测)
        
    Returns:
        str: 规范化后的URL
    """
    if style == 'auto':
        # 自动检测：使用原URL的风格
        detected = detect_space_encoding_style(url)
        if detected == 'plus':
            style = 'plus'
        elif detected == 'percent':
            style = 'percent'
        else:
            # 默认使用+（更简洁）
            style = 'plus'
    
    if style == 'plus':
        # 将 %20 替换为 +
        url = url.replace('%20', '+')
    elif style == 'percent':
        # 将 + 替换为 %20
        url = url.replace('+', '%20')
    
    return url


def transform_url(url: str, verbose: bool = True, verify: bool = True, quick_verify: bool = True, source_url: str = None) -> str:
    """
    智能转换URL（自动识别移动版并转换为桌面版，默认快速验证）
    
    Args:
        url: 原始URL
        verbose: 是否打印转换信息
        verify: 是否验证转换后的URL可访问性（默认True）
        quick_verify: 是否使用快速验证（仅验证前3个候选，默认True）
        source_url: 来源URL（用于判断栏目，如 https://example.com/property/secondhand）
        
    Returns:
        str: 转换后的URL
    """
    if not url:
        return url
    
    # 检查是否为移动版URL
    if not is_mobile_url(url):
        return url
    
    if verbose:
        print(f"🔍 检测到移动版URL，开始智能转换...")
        print(f"   原始: {url[:80]}...")
    
    # 生成候选桌面版URL
    # 🔥 优先使用传入的 source_url，否则尝试从上下文获取
    if not source_url:
        try:
            # 尝试从线程本地存储获取
            import threading
            if hasattr(threading.current_thread(), 'source_url'):
                source_url = threading.current_thread().source_url
        except:
            pass
    
    candidates = guess_desktop_url(url, source_url=source_url)
    
    # 规范化URL编码（自动检测原URL的空格编码风格）
    candidates = [normalize_url_encoding(c, style='auto') for c in candidates]
    
    if not candidates:
        if verbose:
            print(f"⚠️  无法生成桌面版URL候选，保持原URL")
        return url
    
    if verbose:
        print(f"   生成 {len(candidates)} 个候选URL")
    
    # 如果需要验证，测试每个候选URL
    if verify:
        # 快速验证模式：只验证前N个候选
        verify_count = 3 if quick_verify else len(candidates)
        
        for i, candidate in enumerate(candidates[:verify_count], 1):
            if verbose:
                print(f"   🔍 验证候选{i}/{verify_count}: {candidate[:70]}...")
            
            if verify_url_accessible(candidate, timeout=3):
                if verbose:
                    print(f"   ✅ 验证通过！")
                return candidate
        
        if verbose:
            if quick_verify and len(candidates) > verify_count:
                print(f"   ⚠️  前{verify_count}个候选均不可访问，使用第1个候选")
            else:
                print(f"   ⚠️  所有候选URL均不可访问，使用第1个候选")
        return candidates[0]
    else:
        # 不验证，直接返回第一个候选
        best_candidate = candidates[0]
        if verbose:
            print(f"   ✅ 转换为: {best_candidate[:80]}...")
        return best_candidate


def get_dynamic_content_config(url: str) -> Optional[Dict]:
    """
    获取网站的动态内容等待配置
    
    Args:
        url: 网站URL
        
    Returns:
        Dict: 配置信息，如果没有则返回None
    """
    for domain, config in DYNAMIC_CONTENT_SITES.items():
        if domain in url:
            return config
    
    return None


def should_wait_for_dynamic_content(url: str) -> bool:
    """
    判断URL是否需要等待动态内容
    
    Args:
        url: 网站URL
        
    Returns:
        bool: 是否需要等待
    """
    return get_dynamic_content_config(url) is not None


# 向后兼容的函数（保持原有接口）
def convert_hkej_mobile_to_desktop_url(url: str) -> str:
    """
    向后兼容函数：历史名称保留，实际调用通用 transform_url。
    实际调用通用的 transform_url 函数
    """
    return transform_url(url, verbose=True)


def fix_newspaper3k_url_bug(url: str) -> str:
    """
    修复 newspaper3k 可能产生的 URL 双点问题
    例如：https://www..example.com -> https://www.example.com
    """
    if not url:
        return url
    
    from urllib.parse import urlparse, urlunparse
    
    # 解析 URL
    parsed = urlparse(url)
    
    # 修复主机名中的双点或多点
    fixed_netloc = re.sub(r'\.{2,}', '.', parsed.netloc)
    
    # 重新构建 URL
    fixed_url = urlunparse((
        parsed.scheme,
        fixed_netloc,
        parsed.path,
        parsed.params,
        parsed.query,
        parsed.fragment
    ))
    
    if fixed_url != url:
        print(f"🔧 修复 URL 双点: {url[:60]}... -> {fixed_url[:60]}...")
    
    return fixed_url


# 测试函数
def test_transformations():
    """测试智能URL转换"""
    test_urls = [
        # 移动版
        "https://www.example.com/landing/mobarticle2/id/4237059/example-title",
        "https://www..example.com:443/article/id/4237008",
        
        # 桌面版（不应转换）
        "https://www.example.com/news/article/4237069/",
        
        # 其他常见移动版模式
        "https://m.example.com/article/12345",
        "https://www.example.com/mobile/news/67890",
        "https://www.example.com/m/post/11111",
    ]
    
    print("="*80)
    print("🤖 智能URL转换测试（自动识别，无需配置）")
    print("="*80)
    
    for i, url in enumerate(test_urls, 1):
        print(f"\n【测试 {i}】")
        print(f"原始URL: {url}")
        
        # 先修复双点
        fixed = fix_newspaper3k_url_bug(url)
        if fixed != url:
            print(f"修复后: {fixed}")
        
        # 智能转换
        print()
        transformed = transform_url(fixed, verbose=True, verify=False)
        
        print(f"\n最终URL: {transformed}")
        print("─" * 80)


if __name__ == '__main__':
    test_transformations()



def clean_article_url(url: str) -> str:
    """
    清理文章URL，移除URL编码的标题部分
    
    很多网站的URL包含编码的标题，但这些标题部分通常不是必需的，
    而且可能导致404错误。此函数移除这些不必要的部分。
    
    例如：
    https://www.example.com/article/id/4242611/%E6%A0%87%E9%A2%98...
    → https://www.example.com/article/id/4242611
    
    或：
    https://www.example.com/finance/article/4242611/%E6%A0%87%E9%A2%98...
    → https://www.example.com/finance/article/4242611
    
    Args:
        url: 原始URL
        
    Returns:
        str: 清理后的URL
    """
    if not url:
        return url
    
    from urllib.parse import urlparse, urlunparse
    
    parsed = urlparse(url)
    path = parsed.path
    
    # 匹配模式：/article/数字/编码内容 或 /article/id/数字/编码内容
    patterns = [
        (r'(/article/id/\d+)/.*', r'\1'),  # /article/id/12345/xxx → /article/id/12345
        (r'(/article/\d+)/.*', r'\1'),      # /article/12345/xxx → /article/12345
        (r'(/[^/]+/article/\d+)/.*', r'\1'),  # /section/article/12345/xxx → /section/article/12345
        (r'(/[^/]+/[^/]+/article/\d+)/.*', r'\1'),  # /section/sub/article/12345/xxx → /section/sub/article/12345
    ]
    
    original_path = path
    for pattern, replacement in patterns:
        if re.search(pattern, path):
            path = re.sub(pattern, replacement, path)
            break
    
    # 重新组装URL
    cleaned_url = urlunparse((parsed.scheme, parsed.netloc, path, '', '', ''))
    
    if cleaned_url != url:
        print(f"🧹 清理URL编码: {url[:80]}... → {cleaned_url}")
    
    return cleaned_url
