# -*- coding: utf-8 -*-
"""
关键词过滤器模块
用于过滤文章中是否包含指定关键词
支持简繁体自动转换匹配

匹配语义由 ``term_matching`` 统一裁决：**英文/数字类关键词按整词匹配**
（`anc` 不再命中 `Finance`、`guidance`），中文与多词短语按子串匹配
（中文没有词边界，`family office` 仍应命中 `family offices`）。
"""

import re

from term_matching import normalize_text, term_occurs_in_normalized

# 尝试导入opencc进行简繁体转换
try:
    from opencc import OpenCC
    _cc_s2t = OpenCC('s2t')  # 简体转繁体
    _cc_t2s = OpenCC('t2s')  # 繁体转简体
    OPENCC_AVAILABLE = True
except ImportError:
    OPENCC_AVAILABLE = False
    _cc_s2t = None
    _cc_t2s = None
    # 控制台常是 GBK；这里若输出非 ASCII 会抛 UnicodeEncodeError，
    # 把整个模块的 import 一起带走。保持纯 ASCII，缺失时只降级不崩。
    print("[keyword_filter] opencc not installed; simplified/traditional "
          "matching disabled. Install: pip install opencc-python-reimplemented")


def to_simplified(text: str) -> str:
    """将文本转换为简体中文"""
    if OPENCC_AVAILABLE and text:
        return _cc_t2s.convert(text)
    return text


def to_traditional(text: str) -> str:
    """将文本转换为繁体中文"""
    if OPENCC_AVAILABLE and text:
        return _cc_s2t.convert(text)
    return text


def get_all_variants(keyword: str) -> list:
    """
    获取关键词的所有变体（简体和繁体）
    
    Args:
        keyword: 原始关键词
        
    Returns:
        list: 包含原始、简体、繁体的关键词列表（去重）
    """
    variants = {keyword}
    
    if OPENCC_AVAILABLE:
        # 添加简体版本
        simplified = to_simplified(keyword)
        if simplified:
            variants.add(simplified)
        
        # 添加繁体版本
        traditional = to_traditional(keyword)
        if traditional:
            variants.add(traditional)
    
    return list(variants)


class KeywordFilter:
    """关键词过滤器类 - 支持简繁体自动匹配"""
    
    def __init__(self, keywords_str=''):
        """
        初始化关键词过滤器
        
        Args:
            keywords_str: 关键词字符串，多个关键词用逗号、分号或空格分隔
        """
        self.keywords = []  # 原始关键词列表
        self.keyword_variants = {}  # {原始关键词: [变体列表]}
        
        if keywords_str and keywords_str.strip():
            # 分割关键词（支持逗号、分号、换行）。不按普通空格拆分，
            # 避免把 "family office" 这类英文短语拆成两个弱关键词。
            keywords_str = keywords_str.replace('；', ';').replace('，', ',')
            raw_keywords = re.split(r'[,;，；\r\n]+', keywords_str.strip())
            # 过滤空关键词
            self.keywords = [kw.strip() for kw in raw_keywords if kw.strip()]
            
            # 为每个关键词生成简繁体变体
            for kw in self.keywords:
                self.keyword_variants[kw] = get_all_variants(kw)
    
    def is_enabled(self):
        """
        检查关键词过滤是否启用
        
        Returns:
            bool: 如果有关键词则返回True，否则返回False
        """
        return len(self.keywords) > 0
    
    def _haystacks(self, text: str) -> tuple:
        """待匹配的文本版本（原文/简体/繁体），已统一规范化。

        三个版本常常完全相同（纯英文文章不受简繁转换影响），所以去重后再匹配，
        避免同一份大文本被反复扫描三遍。
        """
        versions = [text]
        if OPENCC_AVAILABLE:
            versions.append(to_simplified(text))
            versions.append(to_traditional(text))
        seen = {}
        for version in versions:
            normalized = normalize_text(version)
            if normalized not in seen:
                seen[normalized] = True
        return tuple(seen)

    def _matches_any_variant(self, variants, haystacks) -> bool:
        """任一简繁变体在任一份文本里按规范规则命中即算命中。"""
        for variant in variants:
            normalized_variant = normalize_text(variant).strip()
            if not normalized_variant:
                continue
            for haystack in haystacks:
                if term_occurs_in_normalized(normalized_variant, haystack):
                    return True
        return False

    def match_article(self, article_data):
        """
        检查文章是否匹配关键词（支持简繁体匹配）
        
        Args:
            article_data: 文章数据字典，应包含 'title' 和 'content' 字段
            
        Returns:
            bool: 如果文章包含任何关键词（包括简繁体变体）返回True，否则返回False
        """
        if not self.is_enabled():
            return True  # 没有关键词时默认通过
        
        # 获取标题和内容
        title = article_data.get('title', '')
        content = article_data.get('content', '')
        haystacks = self._haystacks(f"{title} {content}")
        
        # 检查是否包含任何关键词变体
        for variants in self.keyword_variants.values():
            if self._matches_any_variant(variants, haystacks):
                return True
        
        return False
    
    def get_matched_keywords(self, text):
        """
        获取文本中匹配的关键词列表（支持简繁体匹配）
        
        Args:
            text: 要检查的文本
            
        Returns:
            list: 匹配的原始关键词列表（不是变体）
        """
        if not self.is_enabled():
            return []
        
        haystacks = self._haystacks(text or '')
        matched = []
        
        for keyword, variants in self.keyword_variants.items():
            if self._matches_any_variant(variants, haystacks):
                if keyword not in matched:
                    matched.append(keyword)
        
        return matched
    
    def get_matched_keywords_with_variants(self, text):
        """
        获取文本中匹配的关键词及其变体信息
        
        Args:
            text: 要检查的文本
            
        Returns:
            list: [{'keyword': 原始关键词, 'matched_variant': 匹配的变体, 'all_variants': 所有变体}]
        """
        if not self.is_enabled():
            return []
        
        haystacks = self._haystacks(text or '')
        matched = []
        
        for keyword, variants in self.keyword_variants.items():
            for variant in variants:
                normalized_variant = normalize_text(variant).strip()
                if not normalized_variant:
                    continue
                if any(term_occurs_in_normalized(normalized_variant, haystack)
                       for haystack in haystacks):
                    matched.append({
                        'keyword': keyword,
                        'matched_variant': variant,
                        'all_variants': variants
                    })
                    break
        
        return matched
    
    def get_matched_keywords_by_location(self, title: str, content: str) -> dict:
        """
        获取标题和内容中分别匹配的关键词（支持简繁体匹配）
        
        Args:
            title: 文章标题
            content: 文章内容
            
        Returns:
            dict: {
                'title_keywords': [标题中匹配的关键词],
                'content_keywords': [内容中匹配的关键词],
                'all_keywords': [所有匹配的关键词，去重],
                'matched_keywords_str': '逗号分隔的关键词字符串'
            }
        """
        if not self.is_enabled():
            return {
                'title_keywords': [],
                'content_keywords': [],
                'all_keywords': [],
                'matched_keywords_str': ''
            }
        
        title_keywords = self.get_matched_keywords(title or '')
        content_keywords = self.get_matched_keywords(content or '')
        
        # 合并去重
        all_keywords = list(set(title_keywords + content_keywords))
        
        # 生成带位置标记的字符串: 标题关键词前加[标], 内容关键词前加[文]
        marked_keywords = []
        for kw in all_keywords:
            if kw in title_keywords:
                marked_keywords.append(f"[标]{kw}")
            if kw in content_keywords:
                marked_keywords.append(f"[文]{kw}")
        
        return {
            'title_keywords': title_keywords,
            'content_keywords': content_keywords,
            'all_keywords': all_keywords,
            'matched_keywords_str': ','.join(marked_keywords)
        }


if __name__ == '__main__':
    # 测试代码
    print(f"OpenCC可用: {OPENCC_AVAILABLE}")
    
    # 测试简繁体转换
    if OPENCC_AVAILABLE:
        print(f"'中国' 简体->繁体: {to_traditional('中国')}")
        print(f"'中國' 繁体->简体: {to_simplified('中國')}")
        print(f"'楼市' 变体: {get_all_variants('楼市')}")
        print(f"'樓市' 变体: {get_all_variants('樓市')}")
    
    # 测试关键词过滤
    kf = KeywordFilter('中国楼市, 房产')
    print(f"\n关键词已启用: {kf.is_enabled()}")
    print(f"关键词列表: {kf.keywords}")
    print(f"关键词变体: {kf.keyword_variants}")
    
    # 测试繁体文章匹配简体关键词
    article_traditional = {
        'title': '中國樓市最新動態',
        'content': '關於中國樓市的分析報告...'
    }
    
    print(f"\n繁体文章匹配简体关键词'中国楼市': {kf.match_article(article_traditional)}")
    print(f"匹配的关键词: {kf.get_matched_keywords(article_traditional['title'] + article_traditional['content'])}")
    
    # 测试简体文章匹配
    article_simplified = {
        'title': '中国楼市分析',
        'content': '本文分析了中国房产市场...'
    }
    
    print(f"\n简体文章匹配: {kf.match_article(article_simplified)}")
    print(f"匹配的关键词: {kf.get_matched_keywords(article_simplified['title'] + article_simplified['content'])}")

