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
DATA_DIR = "/data"
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
METADATA_FILE = os.path.join(DATA_DIR, "library_metadata.json")
app_config = {}
library_metadata = {}
DOWNLOAD_LOG_FILE = ""
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}
KNOWN_LANGUAGES = ["chinese", "english", "japanese", "translated"]
# --- 任务控制 ---
downloader_lock = threading.Lock()
stop_event = threading.Event()


# --- 日志配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 配置与元数据管理 ---
def load_data():
    """加载配置和元数据文件"""
    global app_config, library_metadata, DOWNLOAD_LOG_FILE

    os.makedirs(DATA_DIR, exist_ok=True)

    if not os.path.exists(CONFIG_FILE):
        logging.info(f"创建默认配置文件: {CONFIG_FILE}")
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        app_config = DEFAULT_CONFIG
    else:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            app_config = json.load(f)

    if not os.path.exists(METADATA_FILE):
        library_metadata = {}
    else:
        with open(METADATA_FILE, 'r', encoding='utf-8') as f:
            library_metadata = json.load(f)

    download_path = app_config.get("download_path", "comics")
    if not os.path.isabs(download_path):
        download_path = os.path.join(DATA_DIR, download_path)
    app_config["download_path"] = download_path

    DOWNLOAD_LOG_FILE = os.path.join(download_path, "download_log.json")
    os.makedirs(download_path, exist_ok=True)

def save_config():
    """保存配置"""
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(app_config, f, indent=4)

def save_metadata():
    """保存元数据"""
    # 使用线程锁确保文件写入安全
    with downloader_lock:
        with open(METADATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(library_metadata, f, indent=4)

# --- 后端下载器 ---

def load_download_log():
    if not os.path.exists(DOWNLOAD_LOG_FILE): return []
    try:
        with open(DOWNLOAD_LOG_FILE, 'r') as f: return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError): return []

def save_download_log(downloaded_ids):
    download_path = app_config.get("download_path")
    os.makedirs(download_path, exist_ok=True)
    with open(DOWNLOAD_LOG_FILE, 'w') as f: json.dump(list(downloaded_ids), f, indent=4)

def sanitize_filename(name):
    return "".join([c for c in name if c.isalpha() or c.isdigit() or c in (' ', '-', '_')]).rstrip()

def download_image(url, path, session, retries=3, delay=5):
    """下载单张图片，并带有重试机制"""
    for i in range(retries):
        if stop_event.is_set(): return False
        try:
            proxies = app_config.get("proxies", {})
            valid_proxies = {k: v for k, v in proxies.items() if v}
            response = session.get(url, headers=HEADERS, proxies=valid_proxies or None, timeout=30)
            response.raise_for_status()
            with open(path, 'wb') as f: f.write(response.content)
            return True
        except Exception as e:
            logging.warning(f"下载图片失败 ({i+1}/{retries}): {url}, 错误: {e}. {delay}秒后重试...")
            if i < retries - 1:
                time.sleep(delay)
    logging.error(f"下载图片失败，已达最大重试次数: {url}")
    return False

def fetch_and_save_metadata(comic_id, session):
    """只获取并保存元数据"""
    base_url = f"https://nhentai.net/g/{comic_id}/"
    try:
        proxies = app_config.get("proxies", {})
        valid_proxies = {k: v for k, v in proxies.items() if v}
        response = session.get(base_url, headers=HEADERS, proxies=valid_proxies or None, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        title_element = soup.find('h1', class_='title')
        if not title_element: return False
        title = title_element.find('span', class_='pretty').text

        all_tags = {}
        tag_sections = soup.find_all('div', class_='tag-container')
        for section in tag_sections:
            section_name_element = section.find('h3')
            if not section_name_element: continue
            section_name = section_name_element.text.strip().replace(':', '')
            tags = [a.find('span', class_='name').text for a in section.find_all('a', class_='tag')]
            if tags: all_tags[section_name] = tags

        comic_id_str = str(comic_id)
        if comic_id_str not in library_metadata: library_metadata[comic_id_str] = {}
        library_metadata[comic_id_str]['tags'] = all_tags
        library_metadata[comic_id_str]['title'] = title
        save_metadata()
        logging.info(f"成功刷新漫画 {comic_id} 的元数据。")
        return True
    except Exception as e:
        logging.error(f"刷新漫画 {comic_id} 元数据失败: {e}")
        return False

def download_comic(comic_id, session):
    """下载单本漫画，并响应停止事件"""
    if stop_event.is_set(): return None

    # 1. 获取元数据
    if not fetch_and_save_metadata(comic_id, session):
        logging.error(f"无法获取漫画 {comic_id} 的元数据，跳过下载。")
        return None

    # 2. 下载图片
    base_url = f"https://nhentai.net/g/{comic_id}/"
    logging.info(f"开始处理漫画: {base_url}")
    try:
        proxies = app_config.get("proxies", {})
        valid_proxies = {k: v for k, v in proxies.items() if v}
        response = session.get(base_url, headers=HEADERS, proxies=valid_proxies or None, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        title = library_metadata.get(str(comic_id), {}).get('title', f"comic_{comic_id}")
        sanitized_title = sanitize_filename(title)

        download_path = app_config.get("download_path")
        comic_folder_name = f"{comic_id}_{sanitized_title}"
        comic_path = os.path.join(download_path, comic_folder_name)
        os.makedirs(comic_path, exist_ok=True)

        thumbnail_elements = soup.find_all('a', class_='gallerythumb')
        num_pages = len(thumbnail_elements)
        if num_pages == 0: logging.error(f"无法找到漫画 {comic_id} 的任何图片"); return None

        logging.info(f"漫画 '{title}' 共 {num_pages} 页. 开始下载...")
        first_thumb_url = thumbnail_elements[0].find('img')['data-src']
        gallery_id = first_thumb_url.split('/galleries/')[1].split('/')[0]

        for i, thumb in enumerate(thumbnail_elements, 1):
            if stop_event.is_set(): logging.info("下载任务被手动停止。"); return None
            img_tag = thumb.find('img')
            img_ext = os.path.splitext(img_tag['data-src'])[1]
            img_url = f"https://i.nhentai.net/galleries/{gallery_id}/{i}{img_ext}"
            img_filename = f"{i}{img_ext}"
            img_filepath = os.path.join(comic_path, img_filename)

            if not os.path.exists(img_filepath):
                logging.info(f"  下载中: 第 {i}/{num_pages} 页 -> {img_filename}")
                if not download_image(img_url, img_filepath, session):
                    logging.error(f"下载第 {i} 页失败，放弃下载此漫画。"); return None
                time.sleep(0.5)
            else:
                logging.info(f"  已存在，跳过: 第 {i}/{num_pages} 页")

        logging.info(f"漫画 '{title}' 下载完成!")
        return comic_id
    except Exception as e:
        logging.error(f"下载漫画 {comic_id} 图片时发生错误: {e}"); return None

def run_downloader():
    if not downloader_lock.acquire(blocking=False):
        logging.warning("下载任务已在运行中，本次请求被跳过。")
        return

    stop_event.clear()
    try:
        logging.info("--- 开始执行下载任务 ---")
        download_path = app_config.get("download_path")
        os.makedirs(download_path, exist_ok=True)
        downloaded_ids = set(load_download_log())
        failed_ids_set = set(library_metadata.get('failed_ids', []))
        session = cloudscraper.create_scraper()
        new_comics_found_in_session = 0
        target_tag_groups = app_config.get("target_tag_groups", [])

        for i, tag_group in enumerate(target_tag_groups):
            if stop_event.is_set(): logging.info("下载任务被手动停止。"); break
            logging.info(f"--- 开始搜索第 {i+1}/{len(target_tag_groups)} 组标签: {tag_group} ---")

            query_parts = [f'language:"{t}"' if t.lower() in KNOWN_LANGUAGES else f'tag:"{t}"' for t in tag_group]
            query = "+".join(query_parts)

            search_url = f"https://nhentai.net/search/?q={query}"
            page = 1
            while not stop_event.is_set():
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
                        if stop_event.is_set(): break
                        logging.info(f"发现新漫画，ID: {comic_id}")
                        downloaded_id = download_comic(comic_id, session)
                        if downloaded_id:
                            downloaded_ids.add(downloaded_id)
                            new_comics_found_in_session += 1
                            if comic_id in failed_ids_set: failed_ids_set.remove(comic_id)
                        else:
                            if not stop_event.is_set(): failed_ids_set.add(comic_id)
                    page += 1; time.sleep(2)
                except Exception as e:
                    logging.error(f"搜索或解析页面时失败: {e}"); logging.warning("由于网络错误，此标签组的搜索提前结束。"); break

        library_metadata['failed_ids'] = list(failed_ids_set)
        save_metadata()
        save_download_log(downloaded_ids)
        logging.info(f"--- 所有标签组搜索完毕，本次任务共下载了 {new_comics_found_in_session} 本新漫画 ---")
    finally:
        downloader_lock.release()

def retry_failed_downloads():
    if not downloader_lock.acquire(blocking=False):
        logging.warning("下载/重试任务已在运行中，本次请求被跳过。")
        return

    stop_event.clear()
    try:
        logging.info("--- 开始重试失败的下载 ---")
        failed_ids_to_retry = library_metadata.get('failed_ids', []).copy()
        if not failed_ids_to_retry:
            logging.info("没有需要重试的漫画。")
            return

        session = cloudscraper.create_scraper()
        successfully_retried_ids = set()

        for comic_id in failed_ids_to_retry:
            if stop_event.is_set(): logging.info("重试任务被手动停止。"); break
            logging.info(f"重试下载: {comic_id}")
            if download_comic(comic_id, session):
                successfully_retried_ids.add(comic_id)

        if successfully_retried_ids:
            logging.info(f"成功重试 {len(successfully_retried_ids)} 本漫画。")
            current_failed_set = set(library_metadata.get('failed_ids', []))
            current_failed_set.difference_update(successfully_retried_ids)
            library_metadata['failed_ids'] = list(current_failed_set)
            save_metadata()
    finally:
        downloader_lock.release()

def refresh_metadata_task():
    """遍历本地文件，为缺少元数据的漫画补充信息"""
    if not downloader_lock.acquire(blocking=False):
        logging.warning("已有任务在运行中，无法刷新元数据。")
        return

    stop_event.clear()
    try:
        logging.info("--- 开始刷新本地漫画元数据 ---")
        download_path = app_config.get("download_path")
        if not os.path.exists(download_path):
            logging.warning("下载目录不存在，无法刷新。")
            return

        session = cloudscraper.create_scraper()
        refreshed_count = 0
        subdirs = [d for d in os.listdir(download_path) if os.path.isdir(os.path.join(download_path, d))]

        for folder in subdirs:
            if stop_event.is_set(): logging.info("元数据刷新任务被手动停止。"); break
            try:
                comic_id_str = folder.split('_')[0]
                # 检查是否缺少 'tags' 键作为需要刷新的标志
                if 'tags' not in library_metadata.get(comic_id_str, {}):
                    logging.info(f"发现缺少元数据的漫画: {comic_id_str}，正在刷新...")
                    if fetch_and_save_metadata(int(comic_id_str), session):
                        refreshed_count += 1
                        time.sleep(1) # 避免请求过快
                    else:
                        logging.warning(f"刷新 {comic_id_str} 失败，将跳过。")
            except (ValueError, IndexError):
                logging.warning(f"目录 '{folder}' 名称不规范，跳过刷新。")

        logging.info(f"--- 元数据刷新完成，共刷新了 {refreshed_count} 本漫画 ---")
    finally:
        downloader_lock.release()


def scheduled_downloader():
    while True:
        time.sleep(10)
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
.grid-2-col{display:grid;grid-template-columns:1fr 1fr;gap:20px;align-items:start}
.config-section,.filter-section{background-color:var(--card-bg);padding:15px;border-radius:8px;margin-bottom:20px}
.config-section h2,.filter-section h2{margin-top:0;border:none;font-size:1.2em}
#tags-editor{width:100%;min-height:80px;background-color:var(--bg-color);color:var(--text-color);border:1px solid var(--border-color);border-radius:4px;padding:10px;box-sizing:border-box;font-family:monospace}
.form-group{margin-bottom:10px}
.form-group label{display:block;margin-bottom:5px}
.form-group input{width:100%;padding:8px;box-sizing:border-box;background-color:var(--bg-color);color:var(--text-color);border:1px solid var(--border-color);border-radius:4px}
.button-group{display:flex;flex-wrap:wrap;gap:10px;margin-top:10px}
#save-btn,#run-stop-btn,#retry-btn,#sort-btn,#refresh-btn{background-color:var(--accent-color);color:var(--bg-color);border:none;padding:10px 20px;border-radius:5px;cursor:pointer;font-weight:bold}
#run-stop-btn.running{background-color:var(--error-color)}
#retry-btn{background-color:#fd7e14}
#sort-btn,#refresh-btn{background-color:#6c757d}
#save-status{margin-left:15px;font-weight:bold;align-self:center}
.filter-controls{display:flex;flex-wrap:wrap;gap:20px;align-items:center}
#search-box{flex-grow:1}
.toggle-switch{display:flex;align-items:center;gap:10px}
.comic-wall{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:20px;min-height:500px}
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
#pagination{display:flex;justify-content:center;align-items:center;margin-top:20px;padding:10px;gap:10px}
.page-btn{background-color:var(--card-bg);color:var(--text-color);border:1px solid var(--border-color);padding:8px 12px;border-radius:4px;cursor:pointer}
.page-btn.active{background-color:var(--accent-color);color:var(--bg-color);border-color:var(--accent-color)}
.page-btn:disabled{cursor:not-allowed;opacity:0.5}
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
                    <label for="download-path-editor">下载路径 (容器内路径)</label>
                    <input type="text" id="download-path-editor" placeholder="例如: comics">
                </div>
                <div class="button-group">
                    <button id="save-btn">保存配置</button>
                    <button id="run-stop-btn">立即执行一次扫描</button>
                    <button id="retry-btn" style="display: none;"></button>
                    <button id="refresh-btn">刷新元数据</button>
                    <span id="save-status"></span>
                </div>
            </div>
            <div class="filter-section">
                <h2>筛选与排序</h2>
                <div class="filter-controls">
                    <input type="text" id="search-box" class="form-group" placeholder="按标题或标签搜索...">
                    <div class="toggle-switch">
                        <label for="show-favorites">只看收藏</label>
                        <input type="checkbox" id="show-favorites">
                    </div>
                    <button id="sort-btn">切换排序 (ID 倒序)</button>
                </div>
            </div>
        </div>
        <div id="loading">正在加载漫画列表...</div>
        <div id="comic-wall" class="comic-wall"></div>
        <div id="pagination"></div>
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
        // DOM 元素
        const tagsEditor = document.getElementById('tags-editor');
        const proxyEditor = document.getElementById('proxy-editor');
        const downloadPathEditor = document.getElementById('download-path-editor');
        const saveBtn = document.getElementById('save-btn');
        const runStopBtn = document.getElementById('run-stop-btn');
        const retryBtn = document.getElementById('retry-btn');
        const refreshBtn = document.getElementById('refresh-btn');
        const sortBtn = document.getElementById('sort-btn');
        const saveStatus = document.getElementById('save-status');
        const searchBox = document.getElementById('search-box');
        const showFavorites = document.getElementById('show-favorites');
        const comicWall = document.getElementById('comic-wall');
        const paginationContainer = document.getElementById('pagination');
        
        // 状态变量
        let allComics = [];
        let filteredComics = [];
        let currentPage = 1;
        const comicsPerPage = 24;
        let currentSort = 'desc'; // 'desc' or 'asc'

        const showStatus = (message, isError = false) => {{
            saveStatus.textContent = message;
            saveStatus.style.color = isError ? 'var(--error-color)' : 'var(--success-color)';
            setTimeout(() => saveStatus.textContent = '', 4000);
        }};

        // --- 核心渲染逻辑 ---
        function render() {{
            applyFiltersAndSort();
            renderCurrentPage();
            renderPagination();
        }}

        function applyFiltersAndSort() {{
            const searchTerms = searchBox.value.toLowerCase().split(' ').filter(t => t);
            const favoritesOnly = showFavorites.checked;

            filteredComics = allComics.filter(comic => {{
                const favMatch = !favoritesOnly || comic.is_favorite;
                if (!favMatch) return false;
                
                if (searchTerms.length === 0) return true;

                let searchableText = comic.title.toLowerCase();
                if (comic.tags) {{
                    for (const category in comic.tags) {{
                        searchableText += ' ' + comic.tags[category].join(' ').toLowerCase();
                    }}
                }}
                
                return searchTerms.every(term => searchableText.includes(term));
            }});
            
            filteredComics.sort((a, b) => {{
                const idA = parseInt(a.id);
                const idB = parseInt(b.id);
                return currentSort === 'desc' ? idB - idA : idA - idB;
            }});
        }}
        
        function renderCurrentPage() {{
            const startIndex = (currentPage - 1) * comicsPerPage;
            const endIndex = startIndex + comicsPerPage;
            const pageComics = filteredComics.slice(startIndex, endIndex);

            if (pageComics.length === 0 && currentPage > 1) {{
                currentPage--;
                renderCurrentPage();
                return;
            }}

            comicWall.innerHTML = pageComics.map(comic => `
                <div class="comic-item" data-id="${{comic.id}}" data-folder="${{comic.folder}}">
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
            if (filteredComics.length === 0) {{
                 comicWall.innerHTML = '<p>没有找到符合条件的漫画。</p>';
            }}
        }}

        function renderPagination() {{
            const totalPages = Math.ceil(filteredComics.length / comicsPerPage);
            paginationContainer.innerHTML = '';
            if (totalPages <= 1) return;
            
            let paginationHTML = `<button class="page-btn" id="prev-page" ${{currentPage === 1 ? 'disabled' : ''}}>上一页</button>`;
            paginationHTML += `<span style="padding: 0 10px;">第 ${{currentPage}} / ${{totalPages}} 页</span>`;
            paginationHTML += `<button class="page-btn" id="next-page" ${{currentPage === totalPages ? 'disabled' : ''}}>下一页</button>`;
            
            paginationContainer.innerHTML = paginationHTML;
        }}


        // --- 事件监听与状态管理 ---
        function setupEventListeners() {{
            setInterval(() => {{
                fetch('/api/downloader_status').then(r => r.json()).then(data => {{
                    const isRunning = data.running;
                    const failedCount = data.failed_count || 0;
                    
                    saveBtn.disabled = isRunning;
                    refreshBtn.disabled = isRunning;
                    runStopBtn.disabled = isRunning && !runStopBtn.classList.contains('running');
                    
                    if(isRunning) {{ runStopBtn.textContent='停止任务'; runStopBtn.classList.add('running'); }}
                    else {{ runStopBtn.textContent='立即执行一次扫描'; runStopBtn.classList.remove('running'); }}
                    
                    if(failedCount > 0) {{ retryBtn.style.display='inline-block'; retryBtn.textContent=`重试失败 (${{failedCount}})`; retryBtn.disabled = isRunning; }}
                    else {{ retryBtn.style.display='none'; }}
                }});
            }}, 2000);

            // 配置按钮
            saveBtn.addEventListener('click', () => {{
                try {{
                    const payload = {{
                        target_tag_groups: JSON.parse(tagsEditor.value),
                        proxies: {{ http: proxyEditor.value.trim(), https: proxyEditor.value.trim() }},
                        download_path: downloadPathEditor.value.trim()
                    }};
                    fetch('/api/config', {{ method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(payload) }})
                    .then(r => r.json()).then(data => {{
                        if (data.status === 'success') showStatus('保存成功!');
                        else throw new Error(data.message);
                    }}).catch(e => showStatus(`保存失败: ${{e.message}}`, true));
                }} catch (e) {{ showStatus(`格式错误: ${{e.message}}`, true); }}
            }});

            const taskButtonHandler = (endpoint, actionText) => {{
                showStatus(`已发送${{actionText}}命令...`);
                fetch(endpoint, {{ method: 'POST' }})
                .then(r => r.json()).then(data => {{
                    if(data.status !== 'success') showStatus(data.message, true); else showStatus(data.message || `命令已发送`);
                }}).catch(e => showStatus(`命令发送失败: ${{e.message}}`, true));
            }};

            runStopBtn.addEventListener('click', () => {{
                const isRunning = runStopBtn.classList.contains('running');
                taskButtonHandler(isRunning ? '/api/stop_downloader' : '/api/run_downloader', isRunning ? '停止' : '执行');
            }});
            retryBtn.addEventListener('click', () => taskButtonHandler('/api/retry_failed', '重试'));
            refreshBtn.addEventListener('click', () => taskButtonHandler('/api/refresh_metadata', '刷新元数据'));


            // 筛选和排序按钮
            searchBox.addEventListener('input', () => {{ currentPage = 1; render(); }});
            showFavorites.addEventListener('change', () => {{ currentPage = 1; render(); }});
            sortBtn.addEventListener('click', () => {{
                currentSort = currentSort === 'desc' ? 'asc' : 'desc';
                sortBtn.textContent = `切换排序 (ID ${{currentSort === 'desc' ? '倒序' : '正序'}})`;
                currentPage = 1;
                render();
            }});
            
            // 分页按钮
            paginationContainer.addEventListener('click', e => {{
                if (e.target.id === 'prev-page' && currentPage > 1) {{ currentPage--; render(); }}
                if (e.target.id === 'next-page' && currentPage < Math.ceil(filteredComics.length / comicsPerPage)) {{ currentPage++; render(); }}
            }});

            // 漫画墙交互
            comicWall.addEventListener('click', e => {{
                const link = e.target.closest('a');
                if (link) {{
                    const state = {{
                        currentPage: currentPage, scrollY: window.scrollY, searchTerm: searchBox.value,
                        showFavorites: showFavorites.checked, sort: currentSort
                    }};
                    sessionStorage.setItem('comicLibraryState', JSON.stringify(state));
                    return;
                }}
                const target = e.target.closest('.action-btn');
                if (!target) return;
                const comicItem = target.closest('.comic-item');
                const comicId = comicItem.dataset.id;
                const comic = allComics.find(c => c.id === comicId);

                if (target.classList.contains('favorite')) {{
                     fetch(`/api/favorite/${{comicId}}`, {{ method: 'POST' }})
                        .then(r => r.json()).then(data => {{
                            if (data.status === 'success') {{
                                comic.is_favorite = data.is_favorite;
                                render();
                            }}
                        }});
                }} else if (target.classList.contains('delete')) {{
                    const modal = document.getElementById('delete-modal');
                    modal.style.display = 'flex';
                    document.getElementById('modal-confirm').onclick = () => {{
                        fetch(`/api/comic/${{comicItem.dataset.folder}}`, {{ method: 'DELETE' }})
                        .then(r => r.json()).then(data => {{
                            if (data.status === 'success') {{
                               allComics = allComics.filter(c => c.id !== comicId);
                               render();
                            }}
                            modal.style.display = 'none';
                        }});
                    }};
                    document.getElementById('modal-cancel').onclick = () => {{ modal.style.display = 'none'; }};
                }}
            }});
        }}

        // --- 初始化 ---
        function loadComicsAndInitialize() {{
            fetch('/api/comics').then(r => r.json()).then(data => {{
                document.getElementById('loading').style.display = 'none';
                allComics = data;
                const savedStateJSON = sessionStorage.getItem('comicLibraryState');
                
                if (savedStateJSON) {{
                    const savedState = JSON.parse(savedStateJSON);
                    currentPage = savedState.currentPage || 1;
                    searchBox.value = savedState.searchTerm || '';
                    showFavorites.checked = savedState.showFavorites || false;
                    currentSort = savedState.sort || 'desc';
                    sortBtn.textContent = `切换排序 (ID ${{currentSort === 'desc' ? '倒序' : '正序'}})`;
                    
                    render();
                    
                    setTimeout(() => window.scrollTo(0, savedState.scrollY || 0), 100);
                    sessionStorage.removeItem('comicLibraryState');
                }} else {{
                    render();
                }}
            }});
        }}
        
        loadComicsAndInitialize();
        setupEventListeners();

        // 加载配置
        fetch('/api/config').then(r => r.json()).then(config => {{
            tagsEditor.value = JSON.stringify(config.target_tag_groups || [], null, 2);
            proxyEditor.value = config.proxies ? (config.proxies.http || '') : '';
            const basePath = "/data/";
            let displayPath = config.download_path || 'comics';
            if (displayPath.startsWith(basePath)) {{ displayPath = displayPath.substring(basePath.length); }}
            downloadPathEditor.value = displayPath;
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

            new_path_relative = new_data.get('download_path', 'comics')
            new_path_abs = os.path.join(DATA_DIR, new_path_relative)

            old_path_abs = app_config.get("download_path")
            if old_path_abs != new_path_abs:
                old_log_file = os.path.join(old_path_abs, "download_log.json")
                if os.path.exists(old_log_file):
                    os.makedirs(new_path_abs, exist_ok=True)
                    new_log_file = os.path.join(new_path_abs, "download_log.json")
                    shutil.move(old_log_file, new_log_file)

            app_config['download_path'] = new_path_abs
            save_config()
            load_data()
            logging.info("配置已通过网页更新。")
            return jsonify({"status": "success"})
        except Exception as e:
            logging.error(f"更新配置失败: {e}")
            return jsonify({"status": "error", "message": str(e)}), 400
    else:
        config_copy = app_config.copy()
        display_path = config_copy.get("download_path", "comics")
        if display_path.startswith(DATA_DIR):
            display_path = os.path.relpath(display_path, DATA_DIR)
        config_copy["download_path"] = display_path
        return jsonify(config_copy)

@app.route('/api/downloader_status', methods=['GET'])
def downloader_status():
    return jsonify({
        "running": downloader_lock.locked(),
        "failed_count": len(library_metadata.get('failed_ids', []))
    })

@app.route('/api/run_downloader', methods=['POST'])
def trigger_downloader():
    if downloader_lock.locked():
        return jsonify({"status": "error", "message": "下载任务已在运行中"}), 409

    manual_run_thread = threading.Thread(target=run_downloader)
    manual_run_thread.start()
    return jsonify({"status": "success", "message": "扫描任务已在后台启动"})

@app.route('/api/stop_downloader', methods=['POST'])
def stop_downloader():
    if not downloader_lock.locked():
        return jsonify({"status": "error", "message": "当前没有下载任务在运行"}), 409

    stop_event.set()
    logging.info("收到停止命令，任务将在安全点停止。")
    return jsonify({"status": "success", "message": "已发送停止命令"})

@app.route('/api/retry_failed', methods=['POST'])
def trigger_retry():
    if downloader_lock.locked():
        return jsonify({"status": "error", "message": "下载任务已在运行中"}), 409

    retry_thread = threading.Thread(target=retry_failed_downloads)
    retry_thread.start()
    return jsonify({"status": "success", "message": "失败重试任务已启动"})

@app.route('/api/refresh_metadata', methods=['POST'])
def trigger_refresh_metadata():
    if downloader_lock.locked():
        return jsonify({"status": "error", "message": "已有任务在运行"}), 409

    refresh_thread = threading.Thread(target=refresh_metadata_task)
    refresh_thread.start()
    return jsonify({"status": "success", "message": "元数据刷新任务已启动"})


@app.route('/api/comics')
def get_comics():
    comics = []
    download_path = app_config.get("download_path")
    if not os.path.exists(download_path): return jsonify([])
    try:
        subdirs = [d for d in os.listdir(download_path) if os.path.isdir(os.path.join(download_path, d))]
    except FileNotFoundError: return jsonify([])
    for folder in subdirs:
        try:
            parts = folder.split('_', 1)
            if len(parts) < 2: continue
            comic_id_str = parts[0]

            comic_dir = os.path.join(download_path, folder)
            cover_file = None
            for ext in ['.jpg', '.png', '.jpeg', '.webp']:
                potential_cover = '1' + ext
                if os.path.exists(os.path.join(comic_dir, potential_cover)):
                    cover_file = potential_cover; break

            if cover_file:
                comic_metadata = library_metadata.get(comic_id_str, {})
                is_favorite = comic_metadata.get('favorite', False)
                tags = comic_metadata.get('tags', {})
                full_title = comic_metadata.get('title', parts[1])

                comics.append({
                    "id": comic_id_str,
                    "folder": folder,
                    "title": full_title,
                    "cover": cover_file,
                    "is_favorite": is_favorite,
                    "tags": tags
                })
        except Exception as e:
            logging.warning(f"处理目录 '{folder}' 时出错: {e}")
    return jsonify(comics)

@app.route('/api/comic/<path:comic_folder>', methods=['DELETE'])
def delete_comic(comic_folder):
    download_path = app_config.get("download_path")
    if '..' in comic_folder or comic_folder.startswith('/'):
        return jsonify({"status": "error", "message": "Invalid folder name"}), 400

    comic_path = os.path.join(download_path, comic_folder)
    try:
        if os.path.isdir(comic_path):
            shutil.rmtree(comic_path)
            logging.info(f"已删除漫画: {comic_folder}")
            # 从元数据中也移除，如果存在的话
            comic_id_str = comic_folder.split('_')[0]
            if comic_id_str in library_metadata:
                del library_metadata[comic_id_str]
                save_metadata()
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
    download_path = app_config.get("download_path")
    comic_path = os.path.join(download_path, comic_folder)
    if not os.path.isdir(comic_path): return jsonify({"error": "Comic not found"}), 404
    try:
        files = [f for f in os.listdir(comic_path) if f.lower().endswith(('.jpg', '.png', '.jpeg', '.webp'))]
        files.sort(key=lambda x: int(os.path.splitext(x)[0]))
        return jsonify({"pages": files})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/comics/<path:filename>')
def serve_comic_files(filename):
    download_path = app_config.get("download_path")
    abs_path = os.path.abspath(os.path.join(download_path, filename))
    if not abs_path.startswith(os.path.abspath(download_path)):
        return "Forbidden", 403

    return send_from_directory(download_path, filename)

# --- 主程序入口 ---
if __name__ == '__main__':
    load_data()

    logging.info("启动后台定时下载任务...")
    scheduler_thread = threading.Thread(target=scheduled_downloader, daemon=True)
    scheduler_thread.start()

    logging.info("启动本地网页服务器...")
    logging.info("请在浏览器中打开 http://127.0.0.1:5000 来访问您的漫画库。")
    app.run(host='0.0.0.0', port=5000, debug=False)

