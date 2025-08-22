import os
import json
import time
import threading
import logging
import shutil
import cloudscraper
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template_string, request, send_from_directory

# --- 默认配置 ---
DEFAULT_CONFIG = {
    "target_tag_groups": [
        ["chinese", "loli"],
        ["chinese", "dick girl"]
    ],
    "download_path": "comics",
    "check_interval_hours": 24,
    "proxies": {
        "http": "",
        "https": ""
    }
}

# --- 全局变量 ---
CONFIG_FILE = "config.json"
METADATA_FILE = "library_metadata.json"
app_config = {}
library_metadata = {}
DOWNLOAD_LOG_FILE = ""
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

# --- 日志配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 配置与元数据管理 ---
def load_data():
    """加载配置和元数据文件"""
    global app_config, library_metadata, DOWNLOAD_LOG_FILE
    # 加载配置
    if not os.path.exists(CONFIG_FILE):
        logging.info(f"创建默认配置文件: {CONFIG_FILE}")
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        app_config = DEFAULT_CONFIG
    else:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            app_config = json.load(f)

    # 加载元数据
    if not os.path.exists(METADATA_FILE):
        library_metadata = {}
    else:
        with open(METADATA_FILE, 'r', encoding='utf-8') as f:
            library_metadata = json.load(f)

    DOWNLOAD_LOG_FILE = os.path.join(app_config.get("download_path", "comics"), "download_log.json")

def save_config():
    """保存配置"""
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(app_config, f, indent=4)

def save_metadata():
    """保存元数据"""
    with open(METADATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(library_metadata, f, indent=4)

# --- 后端下载器 ---

def load_download_log():
    if not os.path.exists(DOWNLOAD_LOG_FILE): return []
    try:
        with open(DOWNLOAD_LOG_FILE, 'r') as f: return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError): return []

def save_download_log(downloaded_ids):
    download_path = app_config.get("download_path", "comics")
    os.makedirs(download_path, exist_ok=True)
    with open(DOWNLOAD_LOG_FILE, 'w') as f: json.dump(list(downloaded_ids), f, indent=4)

def sanitize_filename(name):
    return "".join([c for c in name if c.isalpha() or c.isdigit() or c in (' ', '-', '_')]).rstrip()

def download_image(url, path, session):
    try:
        proxies = app_config.get("proxies", {})
        # 清理空的代理设置
        valid_proxies = {k: v for k, v in proxies.items() if v}
        response = session.get(url, headers=HEADERS, proxies=valid_proxies or None, timeout=30)
        response.raise_for_status()
        with open(path, 'wb') as f: f.write(response.content)
        return True
    except Exception as e:
        logging.error(f"下载图片失败: {url}, 错误: {e}")
        return False

def download_comic(comic_id, session):
    base_url = f"https://nhentai.net/g/{comic_id}/"
    logging.info(f"开始处理漫画: {base_url}")
    try:
        proxies = app_config.get("proxies", {})
        valid_proxies = {k: v for k, v in proxies.items() if v}
        response = session.get(base_url, headers=HEADERS, proxies=valid_proxies or None, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        title_element = soup.find('h1', class_='title')
        if not title_element: logging.error(f"无法找到漫画 {comic_id} 的标题"); return
        title = title_element.find('span', class_='pretty').text
        sanitized_title = sanitize_filename(title)

        download_path = app_config.get("download_path", "comics")
        comic_folder_name = f"{comic_id}_{sanitized_title}"
        comic_path = os.path.join(download_path, comic_folder_name)
        os.makedirs(comic_path, exist_ok=True)

        thumbnail_elements = soup.find_all('a', class_='gallerythumb')
        num_pages = len(thumbnail_elements)
        if num_pages == 0: logging.error(f"无法找到漫画 {comic_id} 的任何图片"); return

        logging.info(f"漫画 '{title}' 共 {num_pages} 页. 开始下载...")
        first_thumb_url = thumbnail_elements[0].find('img')['data-src']
        gallery_id = first_thumb_url.split('/galleries/')[1].split('/')[0]

        for i, thumb in enumerate(thumbnail_elements, 1):
            img_tag = thumb.find('img')
            img_ext = os.path.splitext(img_tag['data-src'])[1]
            img_url = f"https://i.nhentai.net/galleries/{gallery_id}/{i}{img_ext}"
            img_filename = f"{i}{img_ext}"
            img_filepath = os.path.join(comic_path, img_filename)

            if not os.path.exists(img_filepath):
                logging.info(f"  下载中: 第 {i}/{num_pages} 页 -> {img_filename}")
                if not download_image(img_url, img_filepath, session):
                    logging.error(f"下载第 {i} 页失败，中止此漫画下载。"); return
                time.sleep(0.5)
            else:
                logging.info(f"  已存在，跳过: 第 {i}/{num_pages} 页")

        logging.info(f"漫画 '{title}' 下载完成!")
        return comic_id
    except Exception as e:
        logging.error(f"处理漫画 {comic_id} 时发生错误: {e}"); return None

def run_downloader():
    logging.info("--- 开始执行下载任务 ---")
    download_path = app_config.get("download_path", "comics")
    os.makedirs(download_path, exist_ok=True)
    downloaded_ids = set(load_download_log())
    session = cloudscraper.create_scraper()
    new_comics_found_in_session = 0
    target_tag_groups = app_config.get("target_tag_groups", [])

    for i, tag_group in enumerate(target_tag_groups):
        logging.info(f"--- 开始搜索第 {i+1}/{len(target_tag_groups)} 组标签: {tag_group} ---")
        query = "+".join([f'tag:"{tag}"' for tag in tag_group])
        search_url = f"https://nhentai.net/search/?q={query}"
        page = 1
        while True:
            try:
                url = f"{search_url}&page={page}"
                logging.info(f"正在搜索第 {page} 页: {url}")
                proxies = app_config.get("proxies", {})
                valid_proxies = {k: v for k, v in proxies.items() if v}
                response = session.get(url, headers=HEADERS, proxies=valid_proxies or None, timeout=30)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, 'html.parser')
                galleries = soup.find_all('div', class_='gallery')
                if not galleries: logging.info("当前页没有找到更多漫画，此标签组搜索结束。"); break
                page_comic_ids = [int(g.find('a')['href'].strip('/').split('/')[-1]) for g in galleries]
                new_ids_on_page = [cid for cid in page_comic_ids if cid not in downloaded_ids]
                if not new_ids_on_page: logging.info(f"第 {page} 页的所有漫画都已下载过，停止搜索此标签组。"); break
                for comic_id in new_ids_on_page:
                    logging.info(f"发现新漫画，ID: {comic_id}")
                    downloaded_id = download_comic(comic_id, session)
                    if downloaded_id:
                        downloaded_ids.add(downloaded_id)
                        save_download_log(downloaded_ids)
                        new_comics_found_in_session += 1
                page += 1; time.sleep(2)
            except Exception as e:
                logging.error(f"搜索或解析页面时失败: {e}"); logging.warning("由于网络错误，此标签组的搜索提前结束。"); break
    logging.info(f"--- 所有标签组搜索完毕，本次任务共下载了 {new_comics_found_in_session} 本新漫画 ---")

def scheduled_downloader():
    while True:
        run_downloader()
        check_interval = app_config.get("check_interval_hours", 24)
        logging.info(f"任务完成，将在 {check_interval} 小时后再次运行。")
        time.sleep(check_interval * 3600)

# --- 前端网页服务 (Flask) ---
app = Flask(__name__)

# --- HTML, CSS, JS 模板 ---
STYLE_CSS = """
:root{--bg-color:#202124;--text-color:#e8eaed;--card-bg:#303134;--border-color:#5f6368;--accent-color:#8ab4f8;--success-color:#34a853;--error-color:#ea4335;--favorite-color:#fbbc04}
body{background-color:var(--bg-color);color:var(--text-color);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;margin:0;padding:20px}
.container{max-width:1600px;margin:0 auto}
h1,h2{color:var(--accent-color);border-bottom:2px solid var(--border-color);padding-bottom:10px}
.grid-2-col{display:grid;grid-template-columns:1fr 1fr;gap:20px}
.config-section,.filter-section{background-color:var(--card-bg);padding:15px;border-radius:8px;margin-bottom:20px}
.config-section h2,.filter-section h2{margin-top:0;border:none;font-size:1.2em}
#tags-editor{width:100%;min-height:80px;background-color:var(--bg-color);color:var(--text-color);border:1px solid var(--border-color);border-radius:4px;padding:10px;box-sizing:border-box;font-family:monospace}
.form-group{margin-bottom:10px}
.form-group label{display:block;margin-bottom:5px}
.form-group input{width:100%;padding:8px;box-sizing:border-box;background-color:var(--bg-color);color:var(--text-color);border:1px solid var(--border-color);border-radius:4px}
#save-btn{background-color:var(--accent-color);color:var(--bg-color);border:none;padding:10px 20px;border-radius:5px;cursor:pointer;font-weight:bold;margin-top:10px}
#save-status{margin-left:15px;font-weight:bold}
.filter-controls{display:flex;gap:20px;align-items:center}
#search-box{flex-grow:1}
.toggle-switch{display:flex;align-items:center;gap:10px}
.comic-wall{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:20px}
.comic-item{background-color:var(--card-bg);border-radius:8px;overflow:hidden;transition:transform .2s ease-in-out,box-shadow .2s ease-in-out;border:1px solid var(--border-color);position:relative}
.comic-item.hidden{display:none}
.comic-item:hover{transform:translateY(-5px);box-shadow:0 8px 16px rgba(0,0,0,.3)}
.comic-item a{text-decoration:none;color:var(--text-color);display:block}
.comic-item img{width:100%;height:250px;object-fit:cover;display:block}
.comic-item .title{padding:10px 10px 30px;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.comic-actions{position:absolute;bottom:5px;right:5px;display:flex;gap:5px}
.action-btn{background:rgba(0,0,0,.6);border:none;color:#fff;cursor:pointer;padding:5px;border-radius:50%;width:28px;height:28px;display:flex;align-items:center;justify-content:center}
.action-btn.favorite.is-favorite{color:var(--favorite-color)}
.action-btn.delete{color:#ff8a80}
.modal-overlay{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.7);display:flex;justify-content:center;align-items:center;z-index:1000}
.modal-content{background:var(--card-bg);padding:20px;border-radius:8px;text-align:center}
.modal-actions button{margin:0 10px;padding:10px 20px;border-radius:5px;border:none;cursor:pointer}
#modal-confirm{background:var(--error-color);color:#fff}
.reader-container{display:flex;flex-direction:column;height:100vh;box-sizing:border-box;padding:0;margin:-20px}
#reader-header{display:flex;justify-content:space-between;align-items:center;padding:10px 20px;background-color:var(--card-bg);border-bottom:1px solid var(--border-color);flex-shrink:0}
#reader-header h2{margin:0;font-size:1.2em;border:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.back-button,#reader-footer button{background-color:var(--accent-color);color:var(--bg-color);border:none;padding:8px 16px;border-radius:5px;cursor:pointer;text-decoration:none;font-weight:bold}
#image-container{flex-grow:1;display:flex;justify-content:center;align-items:center;overflow:hidden;position:relative}
#current-page-image{max-width:100%;max-height:100%;object-fit:contain}
.nav-overlay{position:absolute;top:0;bottom:0;width:50%;cursor:pointer}
.nav-overlay.left{left:0}
.nav-overlay.right{right:0}
#reader-footer{display:flex;justify-content:center;gap:20px;padding:10px;background-color:var(--card-bg);border-top:1px solid var(--border-color);flex-shrink:0}
#page-selector{background-color:var(--bg-color);color:var(--text-color);border:1px solid var(--border-color);border-radius:4px}
"""

INDEX_HTML = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>我的本地漫画库</title>
    <style>{STYLE_CSS}</style>
</head>
<body>
    <div class="container">
        <h1>我的本地漫画库</h1>
        <div class="grid-2-col">
            <div class="config-section">
                <h2>配置</h2>
                <div class="form-group">
                    <label for="tags-editor">标签组 (JSON格式)</label>
                    <textarea id="tags-editor"></textarea>
                </div>
                <div class="form-group">
                    <label for="proxy-editor">代理 (例如: http://127.0.0.1:7890)</label>
                    <input type="text" id="proxy-editor" placeholder="HTTP/HTTPS Proxy">
                </div>
                <div class="form-group">
                    <label for="download-path-editor">下载路径</label>
                    <input type="text" id="download-path-editor" placeholder="例如: comics">
                </div>
                <button id="save-btn">保存配置</button>
                <span id="save-status"></span>
            </div>
            <div class="filter-section">
                <h2>筛选</h2>
                <div class="filter-controls">
                    <input type="text" id="search-box" class="form-group" placeholder="按标题、标签或ID搜索...">
                    <div class="toggle-switch">
                        <label for="show-favorites">只看收藏</label>
                        <input type="checkbox" id="show-favorites">
                    </div>
                </div>
            </div>
        </div>
        <div id="loading">正在加载漫画列表...</div>
        <div id="comic-wall" class="comic-wall"></div>
    </div>
    <div class="modal-overlay" id="delete-modal" style="display: none;">
        <div class="modal-content">
            <p>确定要删除这本漫画吗？此操作不可恢复。</p>
            <div class="modal-actions">
                <button id="modal-cancel">取消</button>
                <button id="modal-confirm">删除</button>
            </div>
        </div>
    </div>
    <script>
    document.addEventListener('DOMContentLoaded', () => {{
        const tagsEditor = document.getElementById('tags-editor');
        const proxyEditor = document.getElementById('proxy-editor');
        const downloadPathEditor = document.getElementById('download-path-editor');
        const saveBtn = document.getElementById('save-btn');
        const saveStatus = document.getElementById('save-status');
        const searchBox = document.getElementById('search-box');
        const showFavorites = document.getElementById('show-favorites');
        const comicWall = document.getElementById('comic-wall');
        let allComics = [];

        // 加载配置
        fetch('/api/config').then(r => r.json()).then(config => {{
            tagsEditor.value = JSON.stringify(config.target_tag_groups || [], null, 2);
            proxyEditor.value = config.proxies ? (config.proxies.http || config.proxies.https || '') : '';
            downloadPathEditor.value = config.download_path || 'comics';
        }});

        // 保存配置
        saveBtn.addEventListener('click', () => {{
            try {{
                const newTags = JSON.parse(tagsEditor.value);
                if (!Array.isArray(newTags)) throw new Error("标签格式必须是数组");
                const newProxy = proxyEditor.value.trim();
                const newDownloadPath = downloadPathEditor.value.trim();
                if (!newDownloadPath) throw new Error("下载路径不能为空");

                const payload = {{
                    target_tag_groups: newTags,
                    proxies: {{ http: newProxy, https: newProxy }},
                    download_path: newDownloadPath
                }};
                fetch('/api/config', {{ method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(payload) }})
                .then(r => r.json()).then(data => {{
                    if (data.status === 'success') {{
                        saveStatus.textContent = '保存成功! 刷新后生效。'; saveStatus.style.color = 'var(--success-color)';
                        setTimeout(() => window.location.reload(), 1500);
                    }} else {{ throw new Error(data.message); }}
                }}).catch(e => {{ saveStatus.textContent = `保存失败: ${{e.message}}`; saveStatus.style.color = 'var(--error-color)'; }});
            }} catch (e) {{ saveStatus.textContent = `格式错误: ${{e.message}}`; saveStatus.style.color = 'var(--error-color)'; }}
            setTimeout(() => saveStatus.textContent = '', 3000);
        }});

        // 筛选功能
        const applyFilters = () => {{
            const searchTerm = searchBox.value.toLowerCase();
            const favoritesOnly = showFavorites.checked;
            document.querySelectorAll('.comic-item').forEach(item => {{
                const title = item.dataset.title.toLowerCase();
                const isFavorite = item.dataset.favorite === 'true';
                const matchesSearch = title.includes(searchTerm);
                const matchesFavorite = !favoritesOnly || isFavorite;
                item.classList.toggle('hidden', !(matchesSearch && matchesFavorite));
            }});
        }};
        searchBox.addEventListener('input', applyFilters);
        showFavorites.addEventListener('change', applyFilters);

        // 删除和收藏功能
        comicWall.addEventListener('click', e => {{
            const target = e.target.closest('.action-btn');
            if (!target) return;
            const comicItem = target.closest('.comic-item');
            const comicId = comicItem.dataset.id;

            if (target.classList.contains('favorite')) {{
                fetch(`/api/favorite/${{comicId}}`, {{ method: 'POST' }})
                .then(r => r.json()).then(data => {{
                    if (data.status === 'success') {{
                        const isFavorite = data.is_favorite;
                        comicItem.dataset.favorite = isFavorite;
                        target.classList.toggle('is-favorite', isFavorite);
                        applyFilters();
                    }}
                }});
            }} else if (target.classList.contains('delete')) {{
                const modal = document.getElementById('delete-modal');
                modal.style.display = 'flex';
                const confirmBtn = document.getElementById('modal-confirm');
                const cancelBtn = document.getElementById('modal-cancel');
                const confirmHandler = () => {{
                    fetch(`/api/comic/${{comicItem.dataset.folder}}`, {{ method: 'DELETE' }})
                    .then(r => r.json()).then(data => {{
                        if (data.status === 'success') comicItem.remove();
                        closeModal();
                    }});
                }};
                const closeModal = () => {{
                    modal.style.display = 'none';
                    confirmBtn.replaceWith(confirmBtn.cloneNode(true));
                    cancelBtn.replaceWith(cancelBtn.cloneNode(true));
                }};
                document.getElementById('modal-confirm').addEventListener('click', confirmHandler, {{ once: true }});
                document.getElementById('modal-cancel').addEventListener('click', closeModal, {{ once: true }});
            }}
        }});

        // 加载漫画列表
        fetch('/api/comics').then(r => r.json()).then(data => {{
            document.getElementById('loading').style.display = 'none';
            if (data.length === 0) {{ comicWall.innerHTML = '<p>漫画库为空。</p>'; return; }}
            allComics = data;
            comicWall.innerHTML = allComics.map(comic => `
                <div class="comic-item" data-id="${{comic.id}}" data-folder="${{comic.folder}}" data-title="${{comic.title}}" data-favorite="${{comic.is_favorite}}">
                    <a href="/reader?comic=${{encodeURIComponent(comic.folder)}}">
                        <img src="/comics/${{encodeURIComponent(comic.folder)}}/${{encodeURIComponent(comic.cover)}}" alt="${{comic.title}}" loading="lazy">
                        <div class="title">${{comic.title}}</div>
                    </a>
                    <div class="comic-actions">
                        <button class="action-btn favorite ${{comic.is_favorite ? 'is-favorite' : ''}}" title="收藏">
                            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" viewBox="0 0 16 16"><path d="M3.612 15.443c-.386.198-.824-.149-.746-.592l.83-4.73L.173 6.765c-.329-.314-.158-.888.283-.95l4.898-.696L7.538.792c.197-.39.73-.39.927 0l2.184 4.327 4.898.696c.441.062.612.636.282.95l-3.522 3.356.83 4.73c.078.443-.36.79-.746.592L8 13.187l-4.389 2.256z"/></svg>
                        </button>
                        <button class="action-btn delete" title="删除">
                            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" viewBox="0 0 16 16"><path d="M5.5 5.5A.5.5 0 0 1 6 6v6a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5m2.5 0a.5.5 0 0 1 .5.5v6a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5m3 .5a.5.5 0 0 0-1 0v6a.5.5 0 0 0 1 0z"/><path d="M14.5 3a1 1 0 0 1-1 1H13v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V4h-.5a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1H6a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1h3.5a1 1 0 0 1 1 1zM4.118 4 4 4.059V13a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1V4.059L11.882 4zM2.5 3h11V2h-11z"/></svg>
                        </button>
                    </div>
                </div>
            `).join('');
        }});
    }});
    </script>
</body>
</html>
"""

READER_HTML = f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>漫画阅读器</title>
    <style>{STYLE_CSS}</style>
</head>
<body>
    <div class="reader-container">
        <div id="reader-header">
            <a href="/" class="back-button">返回书架</a>
            <h2 id="comic-title">加载中...</h2>
            <div id="page-indicator">
                <select id="page-selector"></select> / <span id="total-pages">?</span>
            </div>
        </div>
        <div id="image-container">
            <img id="current-page-image" src="" alt="Loading page...">
            <div class="nav-overlay left" id="prev-page"></div>
            <div class="nav-overlay right" id="next-page"></div>
        </div>
        <div id="reader-footer">
            <button id="prev-btn">上一页</button>
            <button id="next-btn">下一页</button>
        </div>
    </div>
    <script>
        document.addEventListener('DOMContentLoaded', () => {{
            const params = new URLSearchParams(window.location.search);
            const comicFolder = params.get('comic');
            if (!comicFolder) {{
                document.body.innerHTML = '<h1>错误：未指定漫画</h1><a href="/">返回首页</a>';
                return;
            }}
            const title = decodeURIComponent(comicFolder).split('_').slice(1).join('_');
            document.getElementById('comic-title').textContent = title;
            document.title = title + " - 漫画阅读器";
            const imageElement = document.getElementById('current-page-image');
            const pageSelector = document.getElementById('page-selector');
            const totalPagesSpan = document.getElementById('total-pages');
            let pages = [];
            let currentPage = 1;
            function updatePage() {{
                if (pages.length === 0) return;
                imageElement.src = `/comics/${{encodeURIComponent(comicFolder)}}/${{encodeURIComponent(pages[currentPage - 1])}}`;
                pageSelector.value = currentPage;
                if (currentPage < pages.length) {{
                    new Image().src = `/comics/${{encodeURIComponent(comicFolder)}}/${{encodeURIComponent(pages[currentPage])}}`;
                }}
            }}
            function goToPage(pageNumber) {{
                if (pageNumber >= 1 && pageNumber <= pages.length) {{
                    currentPage = pageNumber;
                    updatePage();
                }}
            }}
            fetch(`/api/comic/${{encodeURIComponent(comicFolder)}}`)
                .then(response => response.json())
                .then(data => {{
                    pages = data.pages;
                    totalPagesSpan.textContent = pages.length;
                    pageSelector.innerHTML = '';
                    for(let i = 1; i <= pages.length; i++) {{
                        const option = document.createElement('option');
                        option.value = i;
                        option.textContent = i;
                        pageSelector.appendChild(option);
                    }}
                    goToPage(1);
                }});
            document.getElementById('prev-btn').addEventListener('click', () => goToPage(currentPage - 1));
            document.getElementById('next-btn').addEventListener('click', () => goToPage(currentPage + 1));
            document.getElementById('prev-page').addEventListener('click', () => goToPage(currentPage - 1));
            document.getElementById('next-page').addEventListener('click', () => goToPage(currentPage + 1));
            pageSelector.addEventListener('change', (e) => goToPage(parseInt(e.target.value)));
            document.addEventListener('keydown', (e) => {{
                if (e.key === 'ArrowLeft') goToPage(currentPage - 1);
                if (e.key === 'ArrowRight') goToPage(currentPage + 1);
            }});
        }});
    </script>
</body>
</html>
"""

@app.route('/')
def index(): return render_template_string(INDEX_HTML)

@app.route('/reader')
def reader(): return render_template_string(READER_HTML)

@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    if request.method == 'POST':
        try:
            new_data = request.get_json()
            app_config['target_tag_groups'] = new_data.get('target_tag_groups', [])
            app_config['proxies'] = new_data.get('proxies', {"http": "", "https": ""})
            app_config['download_path'] = new_data.get('download_path', 'comics')
            save_config()
            load_data() # 重新加载配置以更新全局变量
            logging.info("配置已通过网页更新。")
            return jsonify({"status": "success"})
        except Exception as e:
            logging.error(f"更新配置失败: {e}")
            return jsonify({"status": "error", "message": str(e)}), 400
    else: return jsonify(app_config)

@app.route('/api/comics')
def get_comics():
    comics = []
    download_path = app_config.get("download_path", "comics")
    if not os.path.exists(download_path): return jsonify([])
    try:
        subdirs = [d for d in os.listdir(download_path) if os.path.isdir(os.path.join(download_path, d))]
        subdirs.sort(key=lambda d: os.path.getmtime(os.path.join(download_path, d)), reverse=True)
    except FileNotFoundError: return jsonify([])
    for folder in subdirs:
        try:
            parts = folder.split('_', 1)
            if len(parts) < 2: continue
            comic_id_str = parts[0]
            title = parts[1]
            comic_dir = os.path.join(download_path, folder)
            cover_file = None
            for ext in ['.jpg', '.png', '.jpeg', '.webp']:
                potential_cover = '1' + ext
                if os.path.exists(os.path.join(comic_dir, potential_cover)):
                    cover_file = potential_cover; break
            if cover_file:
                is_favorite = library_metadata.get(comic_id_str, {}).get('favorite', False)
                comics.append({"id": comic_id_str, "folder": folder, "title": title, "cover": cover_file, "is_favorite": is_favorite})
        except Exception as e:
            logging.warning(f"处理目录 '{folder}' 时出错: {e}")
    return jsonify(comics)

@app.route('/api/comic/<path:comic_folder>', methods=['DELETE'])
def delete_comic(comic_folder):
    download_path = app_config.get("download_path", "comics")
    if '..' in comic_folder or comic_folder.startswith('/'):
        return jsonify({"status": "error", "message": "Invalid folder name"}), 400

    comic_path = os.path.join(download_path, comic_folder)
    try:
        if os.path.isdir(comic_path):
            shutil.rmtree(comic_path)
            logging.info(f"已删除漫画: {comic_folder}")
            return jsonify({"status": "success"})
        else:
            return jsonify({"status": "error", "message": "Folder not found"}), 404
    except Exception as e:
        logging.error(f"删除漫画失败 {comic_folder}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/favorite/<comic_id>', methods=['POST'])
def toggle_favorite(comic_id):
    try:
        if comic_id not in library_metadata:
            library_metadata[comic_id] = {}

        current_status = library_metadata[comic_id].get('favorite', False)
        library_metadata[comic_id]['favorite'] = not current_status
        save_metadata()
        logging.info(f"漫画 {comic_id} 收藏状态更新为: {not current_status}")
        return jsonify({"status": "success", "is_favorite": not current_status})
    except Exception as e:
        logging.error(f"更新收藏状态失败 {comic_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/comic/<path:comic_folder>')
def get_comic_pages(comic_folder):
    download_path = app_config.get("download_path", "comics")
    comic_path = os.path.join(download_path, comic_folder)
    if not os.path.isdir(comic_path): return jsonify({"error": "Comic not found"}), 404
    try:
        files = [f for f in os.listdir(comic_path) if f.lower().endswith(('.jpg', '.png', '.jpeg', '.webp'))]
        files.sort(key=lambda x: int(os.path.splitext(x)[0]))
        return jsonify({"pages": files})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/comics/<path:filename>')
def serve_comic_files(filename):
    download_path = app_config.get("download_path", "comics")
    return send_from_directory(download_path, filename)

# --- 主程序入口 ---
if __name__ == '__main__':
    load_data()

    check_interval_hours = app_config.get("check_interval_hours", 24)
    logging.info(f"启动后台定时任务，将首先执行一次下载，然后每隔 {check_interval_hours} 小时检查一次更新。")
    scheduler_thread = threading.Thread(target=scheduled_downloader, daemon=True)
    scheduler_thread.start()

    logging.info("启动本地网页服务器...")
    logging.info("请在浏览器中打开 http://127.0.0.1:5000 来访问您的漫画库。")
    app.run(host='0.0.0.0', port=5000, debug=False)
