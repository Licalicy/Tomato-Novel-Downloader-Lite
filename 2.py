import time
import requests
import bs4
import re
import os
import random
import json
import urllib3
import threading
import signal
import sys
import stem
from stem import Signal
from stem.control import Controller
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from collections import OrderedDict
from fake_useragent import UserAgent
from typing import Optional, Dict
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from Crypto.Random import get_random_bytes
import base64
import gzip
from urllib.parse import urlencode

# 禁用SSL证书验证警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
requests.packages.urllib3.disable_warnings()

# 添加Tor配置
TOR_CONFIG = {
    "enabled": False,
    "proxy_port": 9050,
    "max_retries": 3,
    "change_ip_after": 980,
    "request_timeout": 35
}

# 初始化请求计数器
request_counter = 0

def get_tor_session():
    """创建新的Tor会话"""
    session = requests.session()
    session.proxies = {
        'http': f'socks5h://127.0.0.1:{TOR_CONFIG["proxy_port"]}',
        'https': f'socks5h://127.0.0.1:{TOR_CONFIG["proxy_port"]}'
    }
    return session

def renew_tor_ip():
    """重建会话"""
    if not TOR_CONFIG["enabled"]:
        return
        
    print("正在重建Tor会话更换IP...")
    global request_counter
    request_counter = 0
    time.sleep(5)
    print("IP更换完成")

def check_tor_connection():
    """检查Tor连接是否正常"""
    try:
        session = get_tor_session()
        response = session.get(
            "https://check.torproject.org/",
            timeout=TOR_CONFIG["request_timeout"]
        )
        if "Congratulations" in response.text:
            print("Tor连接成功!")
            return True
    except Exception as e:
        print(f"Tor连接检查失败: {str(e)}")
    return False

def enable_tor_support():
    """启用Tor支持"""
    TOR_CONFIG["enabled"] = True
    print("正在启用Tor支持...")
    if check_tor_connection():
        print("Tor支持已启用!")
        return True
    else:
        print("无法连接到Tor网络，请确保Tor服务正在运行，将不能正常下载章节！")
        TOR_CONFIG["enabled"] = False
        return False

def make_request(url, headers=None, params=None, data=None, method='GET', verify=False, use_tor=False, timeout=None):
    """通用的请求函数"""
    global request_counter
    
    if headers is None:
        headers = get_headers()
    
    session = None
    if use_tor and TOR_CONFIG["enabled"]:
        session = get_tor_session()
        # 计数器逻辑
        request_counter += 1
        if request_counter % TOR_CONFIG["change_ip_after"] == 0:
            renew_tor_ip()
    else:
        session = requests.Session()
    
    try:
        request_params = {
            'headers': headers,
            'params': params,
            'verify': verify,
            'timeout': timeout if timeout is not None else TOR_CONFIG["request_timeout"]
        }
        
        if data:
            request_params['data'] = data

        if method.upper() == 'GET':
            response = session.get(url, **request_params)
        elif method.upper() == 'POST':
            response = session.post(url, **request_params)
        else:
            raise ValueError(f"不支持的HTTP方法: {method}")
        
        return response
    except Exception as e:
        print(f"请求失败: {str(e)}")
        if use_tor and TOR_CONFIG["enabled"]:
            renew_tor_ip()
            return make_request(url, headers, params, data, method, verify, use_tor, timeout)
        raise

# 全局配置
CONFIG = {
    "max_workers": 5,
    "max_retries": 3,
    "request_timeout": 15,
    "status_file": "chapter.json",
    "request_rate_limit": 0.4,
    "api_endpoints": [
        "https://fqphp.gxom.cn/content?item_id={chapter_id}",
        "https://api.cenguigui.cn/api/tomato/content.php?item_id={chapter_id}",
        "https://lsjk.zyii.xyz:3666/content?item_id={chapter_id}",
        "http://nu1.jingluo.love/content?item_id={chapter_id}",
        "http://nu2.jingluo.love/content?item_id={chapter_id}"
    ],
    "api_keys": {
        "fqphp.gxom.cn": "BkmpOhGYbhFv"
    }
}

def get_headers() -> Dict[str, str]:
    """生成随机请求头"""
    browsers = ['chrome', 'edge']
    browser = random.choice(browsers)
    
    if browser == 'chrome':
        user_agent = UserAgent().chrome
    else:
        user_agent = UserAgent().edge
    
    return {
        "User-Agent": user_agent,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://fanqienovel.com/",
        "X-Requested-With": "XMLHttpRequest",
    }

def extract_chapters(soup):
    """解析章节列表"""
    chapters = []
    for idx, item in enumerate(soup.select('div.chapter-item')):
        a_tag = item.find('a')
        if not a_tag:
            continue
        
        raw_title = a_tag.get_text(strip=True)
        
        # 特殊章节
        if re.match(r'^(番外|特别篇|if线)\s*', raw_title):
            final_title = raw_title
        else:
            clean_title = re.sub(
                r'^第[一二三四五六七八九十百千\d]+章\s*',
                '', 
                raw_title
            ).strip()
            final_title = f"第{idx+1}章 {clean_title}"
        
        chapters.append({
            "id": a_tag['href'].split('/')[-1],
            "title": final_title,
            "url": f"https://fanqienovel.com{a_tag['href']}",
            "index": idx
        })
    return chapters

def down_text(chapter_id, headers, book_id=None):
    """下载章节内容"""
    content = ""
    chapter_title = ""

    # 初始化API端点状态
    if not hasattr(down_text, "api_status"):
        down_text.api_status = {endpoint: {
            "last_response_time": float('inf'),
            "error_count": 0,
            "last_try_time": 0
        } for endpoint in CONFIG["api_endpoints"]}

    # 顺序尝试API
    for api_endpoint in CONFIG["api_endpoints"]:
        current_endpoint = api_endpoint.format(chapter_id=chapter_id)
        down_text.api_status[api_endpoint]["last_try_time"] = time.time()
        
        try:
            # 随机延迟
            time.sleep(random.uniform(0.1, 0.5))
            
            # 添加密钥参数
            if "fqphp.gxom.cn" in api_endpoint:
                separator = "&" if "?" in current_endpoint else "?"
                current_endpoint += f"{separator}key={CONFIG['api_keys']['fqphp.gxom.cn']}"
            
            start_time = time.time()
            response = make_request(
                current_endpoint, 
                headers=headers.copy(),
                timeout=CONFIG["request_timeout"],
                verify=False,
                use_tor=True
            )
            
            response_time = time.time() - start_time
            down_text.api_status[api_endpoint].update({
                "last_response_time": response_time,
                "error_count": max(0, down_text.api_status[api_endpoint]["error_count"] - 1)
            })
            
            data = response.json()
            content = data.get("data", {}).get("content", "")
            chapter_title = data.get("data", {}).get("title", "")

            if "fqphp.gxom.cn" in api_endpoint and content:
                # 处理内容
                if len(content) > 20:
                    content = content[:-20]
                
                content = re.sub(r'<header>.*?</header>', '', content, flags=re.DOTALL)
                content = re.sub(r'<footer>.*?</footer>', '', content, flags=re.DOTALL)
                content = re.sub(r'</?article>', '', content)
                content = re.sub(r'<p[^>]*>', '\n    ', content)
                content = re.sub(r'</p>', '', content)
                content = re.sub(r'<[^>]+>', '', content)
                content = re.sub(r'\\u003c|\\u003e', '', content)
                
                content = re.sub(r'\n{3,}', '\n\n', content).strip()
                lines = [line.strip() for line in content.split('\n') if line.strip()]
                formatted_content = '\n'.join(['    ' + line for line in lines])
                return chapter_title, formatted_content
            
            elif "api.cenguigui.cn" in api_endpoint and data.get("code") == 200 and content:
                # 处理内容
                content = re.sub(r'<header>.*?</header>', '', content, flags=re.DOTALL)
                content = re.sub(r'<footer>.*?</footer>', '', content, flags=re.DOTALL)
                content = re.sub(r'</?article>', '', content)
                content = re.sub(r'<p idx="\d+">', '\n', content)
                content = re.sub(r'</p>', '\n', content)
                content = re.sub(r'<[^>]+>', '', content)
                content = re.sub(r'\\u003c|\\u003e', '', content)
                
                if chapter_title and content.startswith(chapter_title):
                    content = content[len(chapter_title):].lstrip()
                
                content = re.sub(r'\n{2,}', '\n', content).strip()
                formatted_content = '\n'.join(['    ' + line if line.strip() else line for line in content.split('\n')])
                return chapter_title, formatted_content

            elif "lsjk.zyii.xyz" in api_endpoint and content:
                # 处理内容
                paragraphs = re.findall(r'<p idx="\d+">(.*?)</p>', content)
                cleaned_content = "\n".join(p.strip() for p in paragraphs if p.strip())
                formatted_content = '\n'.join('    ' + line if line.strip() else line 
                                            for line in cleaned_content.split('\n'))
                return chapter_title, formatted_content
                
            elif "jingluo.love" in api_endpoint and content:
                # 处理内容
                content = re.sub(r'<header>.*?</header>', '', content, flags=re.DOTALL)
                content = re.sub(r'</?article>', '', content)
                content = re.sub(r'<p idx="\d+">', '\n', content)
                content = re.sub(r'</p>', '\n', content)
                content = re.sub(r'<[^>]+>', '', content)
                content = re.sub(r'\\u003c|\\u003e', '', content)
                
                if chapter_title and content.startswith(chapter_title):
                    content = content[len(chapter_title):].lstrip()
                
                content = re.sub(r'\n{2,}', '\n', content).strip()
                formatted_content = '\n'.join(['    ' + line if line.strip() else line for line in content.split('\n')])
                return chapter_title, formatted_content

            print(f"API端点 {api_endpoint} 返回空内容，继续尝试下一个API...")
            down_text.api_status[api_endpoint]["error_count"] += 1

        except Exception as e:
            print(f"API端点 {api_endpoint} 请求失败: {str(e)}")
            down_text.api_status[api_endpoint]["error_count"] += 1
            time.sleep(3)

    print(f"所有API尝试失败，无法下载章节 {chapter_id}")
    return None, None
        
def get_chapters_from_api(book_id, headers):
    """从API获取章节列表（包含完整标题）"""
    try:
        # 获取章节列表
        page_url = f'https://fanqienovel.com/page/{book_id}'
        response = requests.get(page_url, headers=headers, timeout=CONFIG["request_timeout"])
        soup = bs4.BeautifulSoup(response.text, 'html.parser')
        chapters = extract_chapters(soup)  
        
        # 获取章节ID顺序
        api_url = f"https://fanqienovel.com/api/reader/directory/detail?bookId={book_id}"
        api_response = requests.get(api_url, headers=headers, timeout=CONFIG["request_timeout"])
        api_data = api_response.json()
        chapter_ids = api_data.get("data", {}).get("allItemIds", [])
        
        # 合并数据
        final_chapters = []
        for idx, chapter_id in enumerate(chapter_ids):
            # 查找网页解析的对应章节
            web_chapter = next((ch for ch in chapters if ch["id"] == chapter_id), None)
            
            if web_chapter:
                # 使用网页解析的完整标题
                final_chapters.append({
                    "id": chapter_id,
                    "title": web_chapter["title"],
                    "index": idx
                })
            else:
                final_chapters.append({
                    "id": chapter_id,
                    "title": f"第{idx+1}章",
                    "index": idx
                })
        
        return final_chapters
        
    except Exception as e:
        print(f"获取章节列表失败: {str(e)}")
        return None

def download_chapter(chapter, headers, save_path, book_name, downloaded, book_id):
    """下载单个章节"""
    if chapter["id"] in downloaded:
        return None
    
    # 获取内容
    _, content = down_text(chapter["id"], headers, book_id)
    
    if content:
        output_file_path = os.path.join(save_path, f"{book_name}.txt")
        try:
            with open(output_file_path, 'a', encoding='utf-8') as f:
                # 直接使用预先获取的完整标题
                f.write(f'{chapter["title"]}\n')
                f.write(content + '\n\n')
            
            downloaded.add(chapter["id"])
            save_status(save_path, downloaded)
            return chapter["index"], content
        except Exception as e:
            print(f"写入文件失败: {str(e)}")
    return None

def get_book_info(book_id, headers):
    """获取书名、作者、简介"""
    url = f'https://fanqienovel.com/page/{book_id}'
    try:
        response = requests.get(url, headers=headers, timeout=CONFIG["request_timeout"])
        if response.status_code != 200:
            print(f"网络请求失败，状态码: {response.status_code}")
            return None, None, None

        soup = bs4.BeautifulSoup(response.text, 'html.parser')
        
        # 获取书名
        name_element = soup.find('h1')
        name = name_element.text if name_element else "未知书名"
        
        # 获取作者
        author_name = "未知作者"
        author_name_element = soup.find('div', class_='author-name')
        if author_name_element:
            author_name_span = author_name_element.find('span', class_='author-name-text')
            if author_name_span:
                author_name = author_name_span.text
        
        # 获取简介
        description = "无简介"
        description_element = soup.find('div', class_='page-abstract-content')
        if description_element:
            description_p = description_element.find('p')
            if description_p:
                description = description_p.text
        
        return name, author_name, description
    except Exception as e:
        print(f"获取书籍信息失败: {str(e)}")
        return None, None, None

def load_status(save_path):
    """加载下载状态"""
    status_file = os.path.join(save_path, CONFIG["status_file"])
    if os.path.exists(status_file):
        try:
            with open(status_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return set(data)
                return set()
        except:
            pass
    return set()

def save_status(save_path, downloaded):
    """保存下载状态"""
    status_file = os.path.join(save_path, CONFIG["status_file"])
    with open(status_file, 'w', encoding='utf-8') as f:
        json.dump(list(downloaded), f, ensure_ascii=False, indent=2)

def Run(book_id, save_path):
    """运行下载"""
    def signal_handler(sig, frame):
        print("\n检测到程序中断，正在保存已下载内容...")
        write_downloaded_chapters_in_order()
        save_status(save_path, downloaded)
        print(f"已保存 {len(downloaded)} 个章节的进度")
        sys.exit(0)
    
    def write_downloaded_chapters_in_order():
        """按章节顺序写入"""
        if not chapter_results:
            return
            
        # 获取所有已下载的章节索引
        downloaded_indices = sorted(chapter_results.keys())

        with open(output_file_path, 'w', encoding='utf-8') as f:
            f.write(f"小说名: {name}\n作者: {author_name}\n内容简介: {description}\n\n")
            
            # 按顺序写入所有章节
            for idx in range(len(chapters)):
                if idx in chapter_results:
                    result = chapter_results[idx]
                    if result["api_title"]:
                        title = f'{result["base_title"]} {result["api_title"]}'
                    else:
                        title = result["base_title"]
                    f.write(f"{title}\n")
                    f.write(result["content"] + '\n\n')
                elif chapters[idx]["id"] in downloaded:
                    continue
                else:
                    pass
    
    # 信号处理
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        headers = get_headers()
        
        # 获取章节列表
        chapters = get_chapters_from_api(book_id, headers)
        if not chapters:
            print("未找到任何章节，请检查小说ID是否正确。")
            return
            
        # 获取书籍信息
        name, author_name, description = get_book_info(book_id, headers)
        if not name:
            print("无法获取书籍信息，将使用默认名称")
            name = f"未知小说_{book_id}"
            author_name = "未知作者"
            description = "无简介"

        downloaded = load_status(save_path)
        if downloaded:
            print(f"检测到您曾经下载过小说《{name}》。")
            user_input = input("是否需要再次下载？如果需要请输入1并回车，如果不需要请直接回车即可返回主程序：")
            if user_input != "1":
                print("已取消下载，返回主程序。")
                return

        # 准备下载队列
        todo_chapters = [ch for ch in chapters if ch["id"] not in downloaded]
        if not todo_chapters:
            print("所有章节已是最新，无需下载")
            return

        print(f"开始下载：《{name}》, 总章节数: {len(chapters)}, 待下载: {len(todo_chapters)}")
        os.makedirs(save_path, exist_ok=True)

        # 写入书籍信息
        output_file_path = os.path.join(save_path, f"{name}.txt")
        if not os.path.exists(output_file_path):
            with open(output_file_path, 'w', encoding='utf-8') as f:
                f.write(f"小说名: {name}\n作者: {author_name}\n内容简介: {description}\n\n")

        # 多线程变量
        success_count = 0
        failed_chapters = []
        chapter_results = {}
        lock = threading.Lock()
        
        def download_task(chapter):
            """多线程下载任务"""
            nonlocal success_count
            try:
                chapter_title, content = down_text(chapter["id"], headers, book_id)
                if content:
                    with lock:
                        chapter_results[chapter["index"]] = {
                            "base_title": chapter["title"],
                            "api_title": chapter_title,
                            "content": content
                        }
                        downloaded.add(chapter["id"])
                        success_count += 1
                else:
                    with lock:
                        failed_chapters.append(chapter)
            except Exception as e:
                print(f"章节 {chapter['title']} 下载异常: {str(e)}")
                with lock:
                    failed_chapters.append(chapter)
        
        # 持续尝试直到下载完成
        attempt = 1
        while todo_chapters:
            print(f"\n第 {attempt} 次尝试，剩余 {len(todo_chapters)} 个章节...")
            attempt += 1
            
            # 当前批次
            current_batch = todo_chapters.copy()
            
            with ThreadPoolExecutor(max_workers=CONFIG["max_workers"]) as executor:
                futures = [executor.submit(download_task, ch) for ch in current_batch]
                
                with tqdm(total=len(current_batch), desc="下载进度") as pbar:
                    for future in as_completed(futures):
                        pbar.update(1)
            
            # 按顺序写入已下载章节
            write_downloaded_chapters_in_order()
            save_status(save_path, downloaded)
            
            # 更新待下载列表
            todo_chapters = failed_chapters.copy()
            failed_chapters = []
            
            if todo_chapters:
                time.sleep(1)

        print(f"下载完成！成功下载 {success_count} 个章节")

    except Exception as e:
        print(f"运行过程中发生错误: {str(e)}")
        # 在异常时也保存进度
        if 'downloaded' in locals() and 'chapter_results' in locals():
            write_downloaded_chapters_in_order()
            save_status(save_path, downloaded)

def main():
    print("""欢迎使用番茄小说下载器精简版！
作者：Dlmos（Dlmily）
当前版本：v1.7 (预发布版)
Github：https://github.com/Dlmily/Tomato-Novel-Downloader-Lite
赞助/了解新产品：https://afdian.com/a/dlbaokanluntanos
*使用前须知*：
    1.开始下载之后，您可能会过于着急而查看下载文件的位置，这是徒劳的，请耐心等待小说下载完成再查看！另外如果你要下载之前已经下载过的小说(在此之前已经删除了原txt文件)，那么你有可能会遇到"所有章节已是最新，无需下载"的情况，这时就请删除掉chapter.json，然后再次运行程序。
    2.强制使用了Tor网络进行下载，原因是能够很好地防止Api开发者封ip，所以请下载Orbot！

另：如果有另外的api，按照您的意愿投到“Issues”页中。
------------------------------------------""")

    print("\n注意：本版本强制使用Tor网络进行下载，请确保已安装并启动Tor服务！")
    use_tor = input("是否要测试Tor网络环境(y/n):").strip().lower()
    if use_tor != 'y':
        print("必须测试并启用Tor网络才能继续使用下载功能！")
        return
    
    if not enable_tor_support():
        print("无法连接到Tor网络，程序退出")
        return

    while True:
        book_id = input("请输入小说ID（输入q退出）：").strip()
        if book_id.lower() == 'q':
            break
            
        save_path = input("保存路径（留空为当前目录）：").strip() or os.getcwd()
        
        try:
            Run(book_id, save_path)
        except Exception as e:
            print(f"运行错误: {str(e)}")
        
        print("\n" + "="*50 + "\n")
        
if __name__ == "__main__":
    main()