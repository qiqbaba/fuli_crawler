"""
代理IP验证模块
负责高并发异步检测代理IP的连通性与可用性
"""
import random
import asyncio
import aiohttp
import time
from typing import List, Dict, Optional
from aiohttp_socks import ProxyConnector
from config import PROXY_VERIFY_TIMEOUT, PROXY_VERIFY_WORKERS, PROXY_VERIFY_SSL

# 测试目标URL（用于验证代理是否可用）
PROXY_TEST_URLS = [
    "https://www.cloudflare.com",
    "https://api.myip.com",
    "https://www.baidu.com",
]


async def _check_tcp_port(ip: str, port: int, timeout: float = 1.0) -> bool:
    """快速进行 TCP 握手以预筛代理"""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port),
            timeout=timeout
        )
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


class ProxyVerifier:
    """代理IP验证器 - 支持高并发异步验证代理列表的可用性"""

    def __init__(self, test_url: str = "http://www.baidu.com", expected_content: Optional[str] = None):
        self.test_url = test_url
        self.expected_content = expected_content

    def verify_proxies(
        self,
        proxies: List[Dict[str, str]],
        force: bool = False,
        max_workers: Optional[int] = None,
        target_count: int = 300,
        test_url: Optional[str] = None,
        expected_content: Optional[str] = None
    ) -> List[Dict[str, str]]:
        """
        异步验证代理IP是否可用
        
        Args:
            proxies: 待验证的代理列表
            force: 是否强制重新验证
            max_workers: 并发验证协程数，默认使用 config 中的 PROXY_VERIFY_WORKERS
            target_count: 目标可用代理数量，达到后提前退出
            test_url: 可选。用于测试的网址
            expected_content: 可选。验证页面内是否包含此内容
            
        Returns:
            可用代理列表
        """
        if not proxies:
            return []

        if max_workers is None:
            max_workers = PROXY_VERIFY_WORKERS

        # 验证超时取配置值，但不超过 5 秒以加速验证
        verify_timeout = min(PROXY_VERIFY_TIMEOUT, 5)

        current_test_url = test_url or self.test_url
        current_expected_content = expected_content or self.expected_content

        # 在多协程验证队列开始前，将待校验代理按历史评分 score 降序排序，优先测试表现好的代理
        proxies.sort(key=lambda x: x.get("score", 0.0), reverse=True)

        print(f"[ProxyVerifier] 开始异步验证 {len(proxies)} 个代理（并发: {max_workers}，超时: {verify_timeout}s，目标数: {target_count}，测试目标: {current_test_url}）...")

        working = []
        total = len(proxies)
        verified_count = [0]
        start_time = time.time()

        async def main_verify():
            stop_event = asyncio.Event()
            queue = asyncio.Queue()
            # 随机化探测顺序，避免同网段IP集中出现
            shuffled = proxies[:]
            random.shuffle(shuffled)
            for p in shuffled:
                queue.put_nowait(p)

            workers = []

            async def worker():
                while not queue.empty() and not stop_event.is_set():
                    proxy = await queue.get()
                    protocol = proxy["protocol"].lower()
                    address = proxy["address"]

                    try:
                        # 每次探测前加入随机延迟（50-300ms），打破扫描特征
                        await asyncio.sleep(random.uniform(0.05, 0.3))

                        # 1. 快速 TCP 端口预检测
                        try:
                            ip, port_str = address.split(":", 1)
                            port = int(port_str)
                        except ValueError:
                            proxy["fail_count"] = proxy.get("fail_count", 0) + 1
                            proxy["score"] = proxy.get("success_count", 0) - 3 * proxy["fail_count"]
                            continue

                        tcp_ok = await _check_tcp_port(ip, port, timeout=min(verify_timeout, 1.5))
                        if not tcp_ok:
                            proxy["fail_count"] = proxy.get("fail_count", 0) + 1
                            proxy["score"] = proxy.get("success_count", 0) - 3 * proxy["fail_count"]
                            continue

                        # 2. 完整协议与连通性检测
                        proxy_url = f"{protocol}://{address}"
                        connector = None
                        client_proxy = None
                        try:
                            # 配置代理类型
                            if protocol in ("socks5", "socks4"):
                                # rdns=True 表示域名解析由 SOCKS 代理服务器在远端执行，避免本地 DNS 拥堵与误判
                                connector = ProxyConnector.from_url(proxy_url, rdns=True)
                            else:
                                # 开启本地 DNS 缓存，防止高并发 HTTP 代理请求下的本地 DNS 超时
                                connector = aiohttp.TCPConnector(use_dns_cache=True)
                                client_proxy = proxy_url

                            async with aiohttp.ClientSession(connector=connector) as session:
                                async with session.get(
                                    current_test_url,
                                    proxy=client_proxy,
                                    timeout=aiohttp.ClientTimeout(total=verify_timeout),
                                    headers={"User-Agent": "Mozilla/5.0"},
                                    ssl=PROXY_VERIFY_SSL
                                ) as resp:
                                    if resp.status == 200:
                                        is_valid = True
                                        if current_expected_content:
                                            try:
                                                html = await resp.text(errors='ignore')
                                                if current_expected_content not in html:
                                                    is_valid = False
                                            except Exception:
                                                is_valid = False
                                        
                                        if is_valid:
                                            if not stop_event.is_set():
                                                proxy["success_count"] = proxy.get("success_count", 0) + 1
                                                proxy["score"] = proxy["success_count"] - 3 * proxy.get("fail_count", 0)
                                                working.append(proxy)
                                                if len(working) >= target_count:
                                                    stop_event.set()
                                        else:
                                            proxy["fail_count"] = proxy.get("fail_count", 0) + 1
                                            proxy["score"] = proxy.get("success_count", 0) - 3 * proxy["fail_count"]
                                    else:
                                        proxy["fail_count"] = proxy.get("fail_count", 0) + 1
                                        proxy["score"] = proxy.get("success_count", 0) - 3 * proxy["fail_count"]
                        except asyncio.CancelledError:
                            break
                        except Exception:
                            proxy["fail_count"] = proxy.get("fail_count", 0) + 1
                            proxy["score"] = proxy.get("success_count", 0) - 3 * proxy["fail_count"]
                    except asyncio.CancelledError:
                        break
                    except Exception:
                        proxy["fail_count"] = proxy.get("fail_count", 0) + 1
                        proxy["score"] = proxy.get("success_count", 0) - 3 * proxy["fail_count"]
                    finally:
                        queue.task_done()
                        if not stop_event.is_set():
                            verified_count[0] += 1
                            curr_count = verified_count[0]
                            if curr_count % 1000 == 0 or curr_count == total:
                                elapsed = time.time() - start_time
                                print(f"[ProxyVerifier]   进度: {curr_count}/{total}（已找到 {len(working)} 个可用，耗时 {elapsed:.1f}s）")

            # 启动并发 worker 协程
            num_workers = min(max_workers, total)
            for _ in range(num_workers):
                t = asyncio.create_task(worker())
                workers.append(t)

            await asyncio.gather(*workers, return_exceptions=True)

        # 检测是否已在异步上下文中（有事件循环正在运行）
        try:
            asyncio.get_running_loop()
            in_async_context = True
        except RuntimeError:
            in_async_context = False

        if in_async_context:
            # 已在异步上下文中，在独立线程中运行验证，避免事件循环冲突
            import concurrent.futures
            def _run_verify():
                _loop = asyncio.new_event_loop()
                asyncio.set_event_loop(_loop)
                try:
                    _loop.run_until_complete(main_verify())
                except Exception as e:
                    print(f"[ProxyVerifier] 异步事件循环发生异常: {e}")
                finally:
                    _loop.close()
                    asyncio.set_event_loop(None)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(_run_verify).result()
        else:
            # 没有运行中的事件循环，直接使用当前线程
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                loop.run_until_complete(main_verify())
            except Exception as e:
                print(f"[ProxyVerifier] 异步事件循环发生异常: {e}")
            finally:
                loop.close()
                asyncio.set_event_loop(None)

        elapsed = time.time() - start_time
        print(f"[ProxyVerifier] 验证完成: {len(working)}/{total} 个代理可用（总耗时 {elapsed:.1f}s）")
        return working
