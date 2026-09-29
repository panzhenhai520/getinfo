#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🔍 通用文章智能识别器 - 自动打开链接验证是否为真正的文章
核心功能：
1. 打开每个链接，自动识别是否为文章
2. 使用多种Python库（newspaper3k、trafilatura、readability）综合判断
3. 只保存真正的文章内容
4. 完全通用，支持任何网站
"""

import requests
from urllib.parse import urlparse, urljoin
from typing import Dict, List, Optional
import re
from datetime import datetime

# 导入多个文章识别库
try:
    from newspaper import Article as NewspaperArticle
    HAS_NEWSPAPER = True
except ImportError:
    HAS_NEWSPAPER = False
    print("⚠️ newspaper3k 未安装: pip install newspaper3k")

try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False
    print("⚠️ trafilatura 未安装: pip install trafilatura")

try:
    from readability import Document
    HAS_READABILITY = True
except ImportError:
    HAS_READABILITY = False
    print("⚠️ readability 未安装: pip install readability-lxml")

from bs4 import BeautifulSoup


class UniversalArticleValidator:
    """通用文章验证器 - 自动识别链接是否为真正的文章"""
    
    def __init__(self, proxies=None):
        """
        初始化验证器
        
        Args:
            proxies: 代理配置，格式：{'http': '...', 'https': '...'}
        """
        # 如果没有提供代理，从配置中读取；{} 表示调用方明确要求直连。
        if proxies is None:
            try:
                import config
                proxies = config.get_proxies()
                if proxies:
                    print(f"✅ 智能验证器使用代理: {proxies.get('http', 'N/A')}")
            except:
                pass
        
        self.proxies = proxies or {}
        self.session = requests.Session()
        self.session.trust_env = bool(self.proxies)
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        
        # 配置session的代理
        if self.proxies:
            self.session.proxies.update(self.proxies)
        
        # 检查可用的库
        self.available_methods = []
        if HAS_NEWSPAPER:
            self.available_methods.append('newspaper3k')
        if HAS_TRAFILATURA:
            self.available_methods.append('trafilatura')
        if HAS_READABILITY:
            self.available_methods.append('readability')
        self.available_methods.append('beautifulsoup')  # 总是可用
        
        print(f"✅ 可用的识别方法: {', '.join(self.available_methods)}")
    
    def validate_and_extract(self, url: str, timeout: int = 15) -> Dict:
        """
        打开链接，验证是否为文章，并提取内容
        
        Args:
            url: 待验证的URL
            timeout: 超时时间
            
        Returns:
            Dict: {
                'is_article': bool,  # 是否为文章
                'confidence': int,   # 置信度 0-100
                'title': str,        # 标题
                'content': str,      # 内容
                'publish_date': str, # 发布日期
                'method': str,       # 使用的方法
                'reason': str        # 判断理由
            }
        """
        print(f"\n🔍 验证URL: {url[:80]}...")
        
        # 🔥 快速检查：如果是明显的列表页/分页链接，直接拒绝
        if self._is_list_page_url(url):
            return {
                'is_article': False,
                'confidence': 0,
                'reason': '检测到列表页/分页链接',
                'url': url
            }
        
        # 1. 先下载HTML（确保使用代理）
        try:
            # 显式传递代理，即使session已配置
            response = self.session.get(
                url, 
                timeout=timeout, 
                proxies=self.proxies if self.proxies else {},  # 显式传递；空字典代表直连
                verify=False,
                allow_redirects=True
            )
            response.raise_for_status()
            html = response.content
            response.encoding = response.apparent_encoding or 'utf-8'
        except Exception as e:
            return {
                'is_article': False,
                'confidence': 0,
                'reason': f'无法访问URL: {e}',
                'url': url
            }
        
        # 2. 使用多种方法识别
        results = []
        
        # 方法1: newspaper3k（最智能）
        if HAS_NEWSPAPER:
            result = self._validate_with_newspaper(url, html)
            results.append(result)
            print(f"   📰 newspaper3k: 置信度 {result['confidence']}%")
        
        # 方法2: trafilatura（专门用于文章提取）
        if HAS_TRAFILATURA:
            result = self._validate_with_trafilatura(url, html)
            results.append(result)
            print(f"   📝 trafilatura: 置信度 {result['confidence']}%")
        
        # 方法3: readability（Mozilla开发）
        if HAS_READABILITY:
            result = self._validate_with_readability(url, html)
            results.append(result)
            print(f"   📖 readability: 置信度 {result['confidence']}%")
        
        # 方法4: BeautifulSoup（基础方法）
        result = self._validate_with_beautifulsoup(url, html)
        results.append(result)
        print(f"   🍜 beautifulsoup: 置信度 {result['confidence']}%")
        
        # 3. 综合判断（投票机制）
        final_result = self._combine_results(results, url)
        
        if final_result['is_article']:
            print(f"   ✅ 确认为文章！置信度: {final_result['confidence']}%")
            print(f"   📄 标题: {final_result['title'][:60]}...")
            print(f"   📏 内容长度: {len(final_result['content'])} 字符")
        else:
            print(f"   ❌ 不是文章: {final_result['reason']}")
        
        return final_result
    
    def _validate_with_newspaper(self, url: str, html: bytes) -> Dict:
        """使用 newspaper3k 识别"""
        try:
            article = NewspaperArticle(url, language='zh')
            article.download_state = 2
            article.html = html
            article.parse()
            
            # newspaper3k 自带NLP分析
            try:
                article.nlp()
            except:
                pass
            
            content = article.text.strip()
            title = article.title.strip() if article.title else ""
            
            # 🔥 特殊处理：如果提取的内容包含大量无关声明，说明提取失败
            unwanted_markers = [
                '《信報》印刷版出報日',
                '休刊日',
                'Cookie Policy',
                '版權所有',
                'All Rights Reserved'
            ]
            
            if any(marker in content for marker in unwanted_markers):
                # 内容主要是声明，不是文章正文
                if len(content) < 300:
                    print(f"      ⚠️ newspaper3k 提取到声明文字，不是正文")
                    return {
                        'is_article': False,
                        'confidence': 0,
                        'method': 'newspaper3k',
                        'error': '提取的是页面声明而非文章正文'
                    }
            
            # 评估置信度
            confidence = 0
            
            # 有内容
            if len(content) > 100:
                confidence += 40
            elif len(content) > 50:
                confidence += 20
            else:
                # 内容太短，说明提取失败
                return {
                    'is_article': False,
                    'confidence': 0,
                    'method': 'newspaper3k',
                    'error': f'内容太短: {len(content)} 字符'
                }
            
            # 有标题
            if title and len(title) > 5:
                confidence += 20
            
            # 有发布日期
            if article.publish_date:
                confidence += 15
            
            # 有作者
            if article.authors:
                confidence += 10
            
            # 内容质量
            if len(content) > 500:
                confidence += 15
            
            return {
                'is_article': confidence >= 60,
                'confidence': min(confidence, 100),
                'title': title or "无标题",
                'content': content,
                'publish_date': article.publish_date.strftime('%Y-%m-%d') if article.publish_date else None,
                'authors': ', '.join(article.authors) if article.authors else None,
                'method': 'newspaper3k'
            }
        except Exception as e:
            return {
                'is_article': False,
                'confidence': 0,
                'method': 'newspaper3k',
                'error': str(e)
            }
    
    def _validate_with_trafilatura(self, url: str, html: bytes) -> Dict:
        """使用 trafilatura 识别（专门用于文章提取）"""
        try:
            # trafilatura 提取主要内容
            content = trafilatura.extract(html, include_comments=False, 
                                         include_tables=False,
                                         no_fallback=False)
            
            # 提取元数据
            metadata = trafilatura.extract_metadata(html)
            
            if not content:
                return {
                    'is_article': False,
                    'confidence': 0,
                    'method': 'trafilatura',
                    'reason': '无法提取内容'
                }
            
            content = content.strip()
            title = metadata.title if metadata and metadata.title else ""
            
            # 评估置信度
            confidence = 0
            
            if len(content) > 100:
                confidence += 50
            elif len(content) > 50:
                confidence += 25
            
            if title and len(title) > 5:
                confidence += 20
            
            if metadata and metadata.date:
                confidence += 15
            
            if len(content) > 500:
                confidence += 15
            
            return {
                'is_article': confidence >= 60,
                'confidence': min(confidence, 100),
                'title': title or "无标题",
                'content': content,
                'publish_date': metadata.date if metadata else None,
                'method': 'trafilatura'
            }
        except Exception as e:
            return {
                'is_article': False,
                'confidence': 0,
                'method': 'trafilatura',
                'error': str(e)
            }
    
    def _validate_with_readability(self, url: str, html: bytes) -> Dict:
        """使用 readability 识别"""
        try:
            doc = Document(html)
            title = doc.title()
            content_html = doc.summary()
            
            # 转换HTML为纯文本
            soup = BeautifulSoup(content_html, 'html.parser')
            content = soup.get_text(separator='\n', strip=True)
            
            # 评估置信度
            confidence = 0
            
            if len(content) > 100:
                confidence += 45
            elif len(content) > 50:
                confidence += 25
            
            if title and len(title) > 5:
                confidence += 25
            
            if len(content) > 500:
                confidence += 15
            
            # readability score
            if len(content.split()) > 50:  # 至少50个词
                confidence += 15
            
            return {
                'is_article': confidence >= 60,
                'confidence': min(confidence, 100),
                'title': title or "无标题",
                'content': content,
                'method': 'readability'
            }
        except Exception as e:
            return {
                'is_article': False,
                'confidence': 0,
                'method': 'readability',
                'error': str(e)
            }
    
    def _validate_with_beautifulsoup(self, url: str, html: bytes) -> Dict:
        """使用 BeautifulSoup 基础识别（增强版 - 智能过滤声明和导航）"""
        try:
            soup = BeautifulSoup(html, 'html.parser')
            
            # 移除不需要的元素
            for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe']):
                tag.decompose()
            
            # 🔥 移除常见的无关内容块（通用）
            unwanted_classes = [
                'sidebar', 'navigation', 'menu', 'header', 'footer',
                'ad', 'advertisement', 'banner', 'popup', 'modal',
                'related', 'recommend', 'share', 'comment', 'social',
                'breadcrumb', 'pagination', 'widget'
            ]
            
            for class_pattern in unwanted_classes:
                for elem in soup.find_all(class_=re.compile(class_pattern, re.I)):
                    elem.decompose()
            
            # 🔥 提取标题（优先使用h1，避免提取网站名）
            title = ""
            # 第一优先级：h1标签（通常是文章标题）
            h1 = soup.find('h1')
            if h1:
                title = h1.get_text(strip=True)
            
            # 第二优先级：article内的h1/h2
            if not title or len(title) < 5:
                article_elem = soup.find('article')
                if article_elem:
                    for tag in ['h1', 'h2']:
                        h = article_elem.find(tag)
                        if h:
                            title = h.get_text(strip=True)
                            break
            
            # 第三优先级：class包含title的元素
            if not title or len(title) < 5:
                title_elems = soup.select('[class*="title"]')
                for elem in title_elems:
                    t = elem.get_text(strip=True)
                    if 5 < len(t) < 200 and '信報' not in t and 'hkej' not in t.lower():
                        title = t
                        break
            
            # 最后才使用title标签，但要过滤掉网站名
            if not title or len(title) < 5:
                title_tag = soup.find('title')
                if title_tag:
                    t = title_tag.get_text(strip=True)
                    # 过滤掉包含网站名的标题
                    if t and '信報' not in t and 'hkej' not in t.lower() and 5 < len(t) < 200:
                        # 如果标题包含分隔符，取第一部分
                        if ' - ' in t:
                            t = t.split(' - ')[0].strip()
                        elif ' | ' in t:
                            t = t.split(' | ')[0].strip()
                        title = t
            
            # 🔥 智能查找主要内容（尝试多种策略）
            main_content = None
            best_score = 0
            
            # 策略1: 语义化标签优先
            for tag_name in ['article', 'main']:
                elem = soup.find(tag_name)
                if elem:
                    text = elem.get_text(separator='\n', strip=True)
                    # 检查是否包含无关声明
                    if not self._contains_unwanted_content(text):
                        score = self._calculate_content_score(elem)
                        if score > best_score:
                            best_score = score
                            main_content = elem
            
            # 策略2: class选择器
            if not main_content or best_score < 40:
                content_selectors = ['.article-content', '.content', '.post-content', 
                                   '[class*="article"]', '[class*="content"]']
                for selector in content_selectors:
                    elems = soup.select(selector)
                    for elem in elems:
                        text = elem.get_text(separator='\n', strip=True)
                        if not self._contains_unwanted_content(text):
                            score = self._calculate_content_score(elem)
                            if score > best_score:
                                best_score = score
                                main_content = elem
            
            # 策略3: 查找段落最多的容器
            if not main_content or best_score < 30:
                for elem in soup.find_all(['div', 'section']):
                    paragraphs = elem.find_all('p', recursive=False)
                    if len(paragraphs) >= 2:
                        text = elem.get_text(separator='\n', strip=True)
                        if not self._contains_unwanted_content(text):
                            score = self._calculate_content_score(elem)
                            if score > best_score:
                                best_score = score
                                main_content = elem
            
            if not main_content:
                return {
                    'is_article': False,
                    'confidence': 0,
                    'method': 'beautifulsoup',
                    'reason': '无法找到有效内容'
                }
            
            # 提取并清理内容
            content = self._clean_extracted_content(main_content)
            
            if len(content) < 50:
                return {
                    'is_article': False,
                    'confidence': 0,
                    'method': 'beautifulsoup',
                    'reason': f'提取内容太短: {len(content)} 字符'
                }
            
            # 评估置信度
            confidence = best_score
            
            return {
                'is_article': confidence >= 50,
                'confidence': max(0, min(confidence, 100)),
                'title': title or "无标题",
                'content': content,
                'method': 'beautifulsoup'
            }
        except Exception as e:
            return {
                'is_article': False,
                'confidence': 0,
                'method': 'beautifulsoup',
                'error': str(e)
            }
    
    def _is_list_page_url(self, url: str) -> bool:
        """检查URL是否为列表页/分页链接（通用）"""
        url_lower = url.lower()
        
        # 常见的列表页URL模式
        list_patterns = [
            r'\?page=\d+',        # ?page=1
            r'/page/\d+',         # /page/1
            r'[&?]p=\d+',         # &p=1
            r'/list',             # /list
            r'/archive',          # /archive
            r'/category',         # /category
            r'/tag/',             # /tag/
        ]
        
        for pattern in list_patterns:
            if re.search(pattern, url_lower):
                print(f"      ⚠️ 检测到列表页URL模式: {pattern}")
                return True
        
        return False
    
    def _contains_unwanted_content(self, text: str) -> bool:
        """检查文本是否主要包含无关声明（通用）"""
        if not text or len(text) < 50:
            return True
        
        # 通用的无关内容标记
        unwanted_markers = [
            '印刷版出報日', '休刊日', '假期安排',
            'Cookie Policy', '隱私政策', '服務條款',
            '版權所有', 'All Rights Reserved', 'Copyright',
            '登录', '注册', 'Login', 'Register',
            '网站地图', 'Sitemap', '联系我们', 'Contact'
        ]
        
        # 计算无关内容的比例
        unwanted_count = sum(1 for marker in unwanted_markers if marker in text)
        
        # 如果文本很短且包含2个以上无关标记，判定为无关
        if len(text) < 300 and unwanted_count >= 2:
            return True
        
        # 如果文本中无关标记占比超过30%，判定为无关
        if unwanted_count > len(unwanted_markers) * 0.3:
            return True
        
        return False
    
    def _calculate_content_score(self, elem) -> int:
        """计算内容质量分数（通用）"""
        score = 0
        text = elem.get_text(separator='\n', strip=True)
        
        # 文本长度
        if len(text) > 200:
            score += 40
        elif len(text) > 100:
            score += 30
        elif len(text) > 50:
            score += 20
        
        # 段落数
        paragraphs = elem.find_all('p')
        if len(paragraphs) >= 3:
            score += 20
        elif len(paragraphs) >= 1:
            score += 10
        
        # 链接密度（低=好）
        links = elem.find_all('a')
        if len(text) > 0:
            link_density = len(links) / max(len(text), 1) * 1000
            if link_density < 10:
                score += 20
            elif link_density > 50:
                score -= 20
        
        # 中文内容
        import re
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        if chinese_chars > 50:
            score += 20
        
        return max(0, score)
    
    def _clean_extracted_content(self, elem) -> str:
        """清理提取的内容（通用）"""
        # 移除无关子元素
        for child in elem.find_all(['nav', 'aside', 'footer']):
            child.decompose()
        
        # 移除包含特定文本的元素
        unwanted_texts = [
            '版權所有', '印刷版出報日', '休刊日', 'Cookie',
            '登录', '注册', '返回', '分享', '评论', '相关文章'
        ]
        
        for child in elem.find_all():
            child_text = child.get_text(strip=True)
            if any(unwanted in child_text for unwanted in unwanted_texts):
                if len(child_text) < 100:  # 只移除短文本块
                    child.decompose()
        
        # 提取文本
        text = elem.get_text(separator='\n', strip=True)
        
        # 按行清理
        lines = []
        for line in text.split('\n'):
            line = line.strip()
            if len(line) < 10:
                continue
            # 跳过纯符号、纯数字
            if re.match(r'^[\s\-_=*#|]+$', line) or re.match(r'^[\d\s\-_.,]+$', line):
                continue
            lines.append(line)
        
        return '\n'.join(lines)
    
    def _combine_results(self, results: List[Dict], url: str) -> Dict:
        """综合多个方法的结果（投票+加权+智能结构分析）"""
        if not results:
            return {
                'is_article': False,
                'confidence': 0,
                'reason': '没有可用的识别方法',
                'url': url
            }
        
        # 过滤掉失败的结果
        valid_results = [r for r in results if 'error' not in r]
        
        if not valid_results:
            return {
                'is_article': False,
                'confidence': 0,
                'reason': '所有识别方法都失败',
                'url': url
            }
        
        # 🔥 智能选择最佳结果：优先选择有好标题+好内容的
        best_result = None
        best_score = 0
        
        for r in valid_results:
            score = 0
            r_title = r.get('title', '')
            r_content = r.get('content', '')
            
            # 标题质量评分
            if r_title and 5 < len(r_title) < 200:
                score += 50
                # 过滤掉网站名标题
                if '信報' in r_title or 'hkej' in r_title.lower() or r_title == '信報網站 hkej.com':
                    score -= 40
            
            # 内容长度评分
            score += min(len(r_content) // 10, 50)
            
            if score > best_score:
                best_score = score
                best_result = r
        
        # 如果没找到好结果，用内容最长的
        if not best_result:
            best_result = max(valid_results, key=lambda r: len(r.get('content', '')))
        
        content = best_result.get('content', '')
        title = best_result.get('title', '')
        
        # 🔥 最后检查：如果标题还是网站名，尝试从URL或内容提取
        if not title or '信報' in title or 'hkej' in title.lower():
            # 从内容第一行提取
            lines = [l.strip() for l in content.split('\n') if l.strip()]
            if lines and len(lines[0]) < 100:
                title = lines[0]
        
        # 🔥 智能结构分析 - 不只看长度，看结构
        structure_score = 0
        
        # 1. 有合理的标题（5-200字符）
        if title and 5 <= len(title) <= 200:
            structure_score += 30
            # 标题包含关键词（房产、新闻特征）
            if any(kw in title for kw in ['蝕讓', '租出', '售出', '成交', '升值', '下跌', 
                                          '新盤', '樓市', '物業', '房產']):
                structure_score += 10
        
        # 2. 有合理的内容（不只看长度）
        if len(content) > 50:  # 降低最低要求
            structure_score += 20
            
            # 检查段落结构
            paragraphs = [p.strip() for p in content.split('\n') if len(p.strip()) > 20]
            if len(paragraphs) >= 2:
                structure_score += 20
            elif len(paragraphs) >= 1:
                structure_score += 10
            
            # 检查是否有完整句子
            sentences = [s for s in re.split(r'[。！？.!?]', content) if len(s.strip()) > 10]
            if len(sentences) >= 2:
                structure_score += 15
            elif len(sentences) >= 1:
                structure_score += 8
            
            # 检查中文内容
            chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', content))
            if chinese_chars >= 50:  # 至少50个中文字
                structure_score += 15
        
        # 3. 检查是否有数字和金额（房产新闻特征）
        if re.search(r'\d+萬|\d+億|方呎|實用面積', content):
            structure_score += 10
        
        # 4. 检查是否主要是无关声明
        if self._contains_unwanted_content(content):
            structure_score -= 50
        
        # 🔥 最终判断：基于结构分数而不是固定阈值
        is_article = structure_score >= 50  # 降低阈值
        confidence = min(max(structure_score, 0), 100)
        
        # 如果多个方法都认为是文章，额外加分
        votes_yes = sum(1 for r in valid_results if r.get('is_article'))
        if votes_yes >= 2:
            confidence = min(confidence + 15, 100)
        
        return {
            'is_article': is_article,
            'confidence': int(confidence),
            'title': title or "无标题",
            'content': content,
            'publish_date': best_result.get('publish_date'),
            'authors': best_result.get('authors'),
            'method': f"智能结构分析(投票{votes_yes}/{len(valid_results)})",
            'reason': f"结构分数{structure_score}, 投票{votes_yes}/{len(valid_results)}" if is_article else f"结构分数不足({structure_score}<50)",
            'url': url,
            'all_methods': [r['method'] for r in valid_results],
            'structure_details': {
                'title_length': len(title),
                'content_length': len(content),
                'paragraphs': len([p.strip() for p in content.split('\n') if len(p.strip()) > 20]),
                'chinese_chars': len(re.findall(r'[\u4e00-\u9fff]', content))
            }
        }
    
    def batch_validate(self, urls: List[str], max_workers: int = 5) -> List[Dict]:
        """
        批量验证URL
        
        Args:
            urls: URL列表
            max_workers: 并发数
            
        Returns:
            List[Dict]: 验证结果列表
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        results = []
        total = len(urls)
        
        print(f"\n{'='*70}")
        print(f"📋 批量验证 {total} 个链接")
        print(f"{'='*70}")
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_url = {executor.submit(self.validate_and_extract, url): url for url in urls}
            
            for i, future in enumerate(as_completed(future_to_url), 1):
                url = future_to_url[future]
                try:
                    result = future.result()
                    results.append(result)
                    
                    status = "✅" if result['is_article'] else "❌"
                    print(f"[{i}/{total}] {status} {url[:60]}... (置信度: {result['confidence']}%)")
                except Exception as e:
                    print(f"[{i}/{total}] ❌ {url[:60]}... (异常: {e})")
                    results.append({
                        'is_article': False,
                        'confidence': 0,
                        'url': url,
                        'reason': f'验证异常: {e}'
                    })
        
        # 统计
        article_count = sum(1 for r in results if r['is_article'])
        print(f"\n{'='*70}")
        print(f"✅ 验证完成！")
        print(f"   总计: {total} 个链接")
        print(f"   文章: {article_count} 个 ({article_count/total*100:.1f}%)")
        print(f"   非文章: {total-article_count} 个")
        print(f"{'='*70}\n")
        
        return results


def test_validator():
    """测试验证器"""
    validator = UniversalArticleValidator()
    
    # 测试URL
    test_urls = [
        "https://www.fangdalaw.com/content/details34_9069.html",  # 应该是文章
        "https://www.fangdalaw.com/news/",  # 应该是列表页
        "https://www.fangdalaw.com/about/",  # 应该是关于页面
    ]
    
    for url in test_urls:
        result = validator.validate_and_extract(url)
        print(f"\n结果: {result}")
        print("-" * 70)


if __name__ == "__main__":
    test_validator()

