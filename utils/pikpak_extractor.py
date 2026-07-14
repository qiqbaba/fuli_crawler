import re
import time
import requests
from urllib.parse import urlparse, parse_qs
from utils.logger import get_logger

logger = get_logger(__name__)

def get_pikpak_link(keepshare_url, timeout=30, poll_interval=2, quiet=False):
    """
    直接获取 keepshare.org 链接对应的 pikpak 分享链接，免去浏览器等待时间。
    
    参数:
        keepshare_url: 原始 keepshare 链接，例如 https://keepshare.org/7f70llj0/magnet%3A%3F...
        timeout: 针对未转存完成资源的轮询超时时间(秒)
        poll_interval: 轮询间隔(秒)，用作指数退避的基数(base)
        quiet: 是否静音模式，不输出明细日志
        
    返回:
        成功转存或已有缓存时返回 pikpak 链接(str)；
        如果超时或失败则返回 None。
    """
    def log(msg):
        if not quiet:
            logger.info(msg)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    log(f"开始请求 KeepShare 链接: {keepshare_url}")
    
    # 获取全局代理配置以规避网络异常
    proxies = None
    try:
        from config import get_effective_proxy
        proxies = get_effective_proxy()
    except Exception:
        pass

    try:
        # 发送 GET 请求，禁止自动跳转
        res = requests.get(keepshare_url, headers=headers, allow_redirects=False, timeout=10, proxies=proxies)
        
        # 1. 检查重定向地址
        location = res.headers.get("Location")
        if not location:
            log("[-] 未在响应头中找到 Location 重定向地址。")
            return None
            
        log(f"[+] 初始重定向目标: {location}")
        
        # 情况 A: 已经转存过，直接 302 到 pikpak 链接
        if "mypikpak.com" in location:
            log("[OK] 资源已存在缓存，直接秒级获取成功！")
            return location
            
        # 情况 B: 尚未转存成功，跳转到了状态等待页
        if "console/shared/status" in location:
            log("[*] 资源尚未转存完成，正在解析 API 并开始后台轮询...")
            
            # 解析 URL 中的参数 id 和 request_id
            parsed_loc = urlparse(location)
            params = parse_qs(parsed_loc.query)
            
            task_id = params.get("id", [None])[0]
            request_id = params.get("request_id", [""])[0]
            
            if not task_id:
                log("[-] 无法从状态 URL 中解析出任务 id。")
                return None
                
            # 开始轮询后台 API
            api_url = f"https://keepshare.org/api/shared_link?id={task_id}&request_id={request_id}&is_end=false"
            log(f"[+] API 查询接口: {api_url}")
            
            start_time = time.time()
            attempt = 0
            max_interval = 30  # 退避上限 30 秒
            while time.time() - start_time < timeout:
                try:
                    attempt += 1
                    api_res = requests.get(api_url, headers=headers, timeout=10, proxies=proxies)
                    if api_res.status_code == 200:
                        data = api_res.json()
                        state = data.get("state")
                        pikpak_link = data.get("host_shared_link")
                        
                        log(f"    [轮询] 当前状态: {state} | 链接: {pikpak_link or '等待生成中...'}")
                        
                        if pikpak_link:
                            log(f"[OK] 资源转存成功，轮询获取到 PikPak 链接！花费时间: {int(time.time() - start_time)}秒")
                            return pikpak_link
                            
                        # 如果出现 ERROR 状态
                        if state == "ERROR":
                            log(f"[-] 转存任务出错，错误原因: {data.get('error')}")
                            return None
                    else:
                        log(f"    [警告] API 请求失败，状态码: {api_res.status_code}")
                except Exception as api_err:
                    log(f"    [警告] 轮询请求发生异常: {api_err}")
                
                # 指数退避：间隔 = min(base * 2^attempt, max_interval)
                sleep_time = min(poll_interval * (2 ** (attempt - 1)), max_interval)
                # 同时确保不超过剩余超时时间
                remaining = timeout - (time.time() - start_time)
                sleep_time = min(sleep_time, max(remaining, 0))
                if sleep_time > 0:
                    time.sleep(sleep_time)
                
            log(f"[-] 轮询超时 ({timeout}秒)，资源可能在排队离线中，您可以稍后直接请求该 KeepShare 链接。")
            return None
            
        # 其他不认识的重定向
        log(f"[-] 遇到未知的重定向目标: {location}")
        return None
        
    except Exception as e:
        log(f"[-] 请求发生错误: {e}")
        return None

if __name__ == "__main__":
    # 测试用例 1: 已缓存的资源（秒跳）
    test_cached = "https://keepshare.org/7f70llj0/magnet%3A%3Fxt%3Durn%3Abtih%3A20e7c99dd69926c7b617f6e74268de9b961e7f10"
    print("=" * 60)
    print("测试用例 1 (已缓存资源):")
    res_link1 = get_pikpak_link(test_cached)
    print(f"最终结果: {res_link1}")
    
    # 测试用例 2: 队列中的资源
    test_queued = "https://keepshare.org/7f70llj0/magnet%3A%3Fxt%3Durn%3Abtih%3A648c31129ae03a0626c95153883f4434d0e2b1e2"
    print("\n" + "=" * 60)
    print("测试用例 2 (列队中资源，测试轮询和超时处理):")
    res_link2 = get_pikpak_link(test_queued, timeout=10, poll_interval=2)
    print(f"最终结果: {res_link2}")
