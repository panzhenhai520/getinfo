// 文章选择器功能
class ArticleSelector {
    constructor() {
        this.selectedArticles = new Set();
        this.articles = [];
        this.allArticles = []; // 存储所有页面的文章
        this.currentPage = 1;
        this.hasMorePages = true;
        this.init();
    }

    init() {
        this.loadArticlesBtn = document.getElementById('loadArticlesBtn');
        this.articlesContainer = document.getElementById('articlesContainer');
        this.articlesList = document.getElementById('articlesList');
        this.selectedArticlesCount = document.getElementById('selectedArticlesCount');
        
        // 如果相关元素不存在，则不初始化
        if (!this.loadArticlesBtn || !this.articlesContainer || !this.articlesList) {
            console.log('Article selector elements not found, skipping initialization');
            return;
        }
        
        this.bindEvents();
    }

    bindEvents() {
        // 加载文章列表按钮事件
        this.loadArticlesBtn.addEventListener('click', () => {
            this.loadArticles();
        });
    }

    async loadArticles() {
        const selectedUrls = this.getSelectedUrls();
        if (selectedUrls.length === 0) {
            alert('请先选择一个URL');
            return;
        }

        // 根据选中的菜单项确定要访问的新闻列表URL
        let url = selectedUrls[0]; // 使用第一个选中的URL
        
        // 检查是否有选中的菜单项，如果有，则访问对应的新闻列表页面
        const selectedMenus = this.getSelectedMenus();
        if (selectedMenus && selectedMenus.length > 0) {
            // 根据菜单项映射到对应的新闻列表URL
            const menuUrlMap = {
                '君合新闻': '/news',
                '君合业绩': '/deals', 
                '君合声誉': '/reputations',
                '君合法评': '/legal-updates',
                '君合人文': '/humanities'
            };
            
            for (const menu of selectedMenus) {
                if (menuUrlMap[menu.title]) {
                    // 构建完整的新闻列表URL
                    const baseUrl = selectedUrls[0].replace(/\/$/, ''); // 移除末尾的斜杠
                    url = baseUrl + menuUrlMap[menu.title];
                    console.log(`根据菜单"${menu.title}"访问新闻列表: ${url}`);
                    break;
                }
            }
        }
        
        // 重置分页状态
        this.currentPage = 1;
        this.allArticles = [];
        this.hasMorePages = true;
        
        this.loadArticlesBtn.innerHTML = '<i class="fa fa-spinner fa-spin mr-2"></i> 加载中...';
        this.loadArticlesBtn.disabled = true;

        // 开始加载所有页面的文章
        await this.loadAllPages(url);
    }

    async loadAllPages(baseUrl) {
        try {
            while (this.hasMorePages) {
                const pageUrl = this.currentPage === 1 ? baseUrl : `${baseUrl}?page=${this.currentPage}`;
                console.log(`正在加载第 ${this.currentPage} 页: ${pageUrl}`);
                
                const response = await fetch('/api/get-articles', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ url: pageUrl })
                });

                const data = await response.json();

                if (data.success) {
                    if (data.articles && data.articles.length > 0) {
                        // 去重：只添加新的文章
                        const newArticles = data.articles.filter(article => 
                            !this.allArticles.some(existing => existing.url === article.url)
                        );
                        this.allArticles.push(...newArticles);
                        console.log(`第 ${this.currentPage} 页找到 ${data.articles.length} 篇文章，新增 ${newArticles.length} 篇`);
                        
                        // 更新显示
                        this.articles = this.allArticles;
                        this.renderArticles();
                        this.articlesContainer.classList.remove('hidden');
                        
                        // 检查是否还有更多页面
                        this.currentPage++;
                        
                        // 如果这一页的文章数量少于预期，可能没有更多页面了
                        if (data.articles.length < 5) { // 假设每页至少5篇文章
                            this.hasMorePages = false;
                        }
                        
                        // 限制最大页数，避免无限循环
                        if (this.currentPage > 10) {
                            this.hasMorePages = false;
                        }
                    } else {
                        this.hasMorePages = false;
                    }
                } else {
                    console.log(`第 ${this.currentPage} 页加载失败: ${data.error}`);
                    this.hasMorePages = false;
                }
                
                // 添加短暂延迟，避免请求过于频繁
                await new Promise(resolve => setTimeout(resolve, 500));
            }
            
            console.log(`总共加载了 ${this.allArticles.length} 篇文章`);
            this.updateLoadButtonText();
            
        } catch (error) {
            console.error('加载文章失败:', error);
            alert('加载文章失败: ' + error.message);
        } finally {
            this.loadArticlesBtn.innerHTML = '<i class="fa fa-refresh mr-2"></i> 重新加载文章列表';
            this.loadArticlesBtn.disabled = false;
        }
    }

    updateLoadButtonText() {
        if (this.allArticles.length > 0) {
            this.loadArticlesBtn.innerHTML = `<i class="fa fa-refresh mr-2"></i> 重新加载 (已加载 ${this.allArticles.length} 篇)`;
        }
    }

    renderArticles() {
        if (this.articles.length === 0) {
            this.articlesList.innerHTML = '<div class="text-gray-500 text-center py-4">未找到文章</div>';
            return;
        }

        const articlesHtml = this.articles.map((article, index) => `
            <div class="flex items-start space-x-3 py-2 border-b border-gray-200 last:border-b-0">
                <input 
                    type="checkbox" 
                    id="article-${index}" 
                    class="mt-1 h-4 w-4 text-blue-600 focus:ring-blue-500 border-gray-300 rounded article-checkbox"
                    data-url="${article.url}"
                    data-title="${article.title}"
                >
                <div class="flex-1 min-w-0">
                    <label for="article-${index}" class="block text-sm font-medium text-gray-900 cursor-pointer hover:text-blue-600">
                        ${article.title}
                    </label>
                    <p class="text-xs text-gray-500 mt-1 truncate">${article.url}</p>
                </div>
            </div>
        `).join('');

        this.articlesList.innerHTML = articlesHtml;

        // 绑定复选框事件
        this.articlesList.querySelectorAll('.article-checkbox').forEach(checkbox => {
            checkbox.addEventListener('change', (e) => {
                this.handleArticleSelection(e.target);
            });
        });
    }

    handleArticleSelection(checkbox) {
        const url = checkbox.dataset.url;
        const title = checkbox.dataset.title;

        if (checkbox.checked) {
            this.selectedArticles.add(url);
        } else {
            this.selectedArticles.delete(url);
        }

        this.updateSelectedCount();
        this.updateIncludeKeywords();
    }

    updateSelectedCount() {
        const count = this.selectedArticles.size;
        this.selectedArticlesCount.textContent = `已选择 ${count} 篇文章`;
    }

    updateIncludeKeywords() {
        // 将选中的文章标题添加到包含关键词输入框
        const includeKeywordsInput = document.getElementById('includeKeywords');
        const selectedTitles = Array.from(this.selectedArticles).map(url => {
            const article = this.articles.find(a => a.url === url);
            return article ? article.title : '';
        }).filter(title => title);

        if (selectedTitles.length > 0) {
            includeKeywordsInput.value = selectedTitles.join(', ');
        }
    }

    getSelectedUrls() {
        // 获取当前选中的URL
        const checkboxes = document.querySelectorAll('.crawl-url-checkbox:checked');
        return Array.from(checkboxes).map(cb => cb.dataset.url);
    }

    getSelectedMenus() {
        // 获取当前选中的菜单项
        if (window.selectedMenuForUrl) {
            return window.selectedMenuForUrl.menus || [];
        }
        return [];
    }

    getSelectedArticles() {
        // 返回选中的文章信息
        return Array.from(this.selectedArticles).map(url => {
            const article = this.articles.find(a => a.url === url);
            return {
                url: url,
                title: article ? article.title : '',
                short_url: article ? article.short_url : ''
            };
        });
    }

    clearSelection() {
        this.selectedArticles.clear();
        this.articlesList.querySelectorAll('.article-checkbox').forEach(checkbox => {
            checkbox.checked = false;
        });
        this.updateSelectedCount();
        this.updateIncludeKeywords();
    }
}

// 初始化文章选择器
let articleSelector;

document.addEventListener('DOMContentLoaded', function() {
    articleSelector = new ArticleSelector();
});

// 导出给其他脚本使用
window.articleSelector = articleSelector;
