import os
import threading
import time
import hashlib
import urllib.parse
import boto3
from botocore.exceptions import ClientError
from concurrent.futures import ThreadPoolExecutor, as_completed
from utils.logger import get_logger

logger = get_logger(__name__)


def extract_url_relative_path(url: str, include_query: bool = True) -> str:
    """提取 URL 的相对路径（可选包含标准化排序后的 query 参数），去除协议和域名"""
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path or "/"
        while "//" in path:
            path = path.replace("//", "/")
        if include_query and parsed.query:
            query_parts = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            query_parts.sort(key=lambda x: x[0])
            sorted_query = urllib.parse.urlencode(query_parts)
            return f"{path}?{sorted_query}"
        return path
    except Exception:
        return url


def get_url_dedup_key(url: str, source: str = None) -> str:
    """生成用于去重的规范化键（相对路径 + 数据源命名空间）
    
    例如: 
      url = 'https://ekd.686932.xyz/html/movie/pc/guochan_123.html', source = 'jingpin'
      -> 'rel:jingpin:/html/movie/pc/guochan_123.html'
    """
    if not url:
        return ""
    if url.startswith("rel:"):
        return url
    rel_path = extract_url_relative_path(url)
    if source:
        return f"rel:{source}:{rel_path}"
    return f"rel:{rel_path}"


class BloomFilter:
    """轻量 Bloom Filter，用于减少 DynamoDB 查询次数

    使用多个哈希函数（基于 hashlib）的位数组实现。
    存在假阳性（可配置），但不存在假阴性。
    
    支持序列化到本地文件，避免进程重启后的冷启动穿透。
    """
    def __init__(self, capacity: int = 100000, error_rate: float = 0.01):
        import math
        self.capacity = capacity
        self.error_rate = error_rate
        # 计算位数组大小和哈希函数数量
        self.bit_size = int(-capacity * math.log(error_rate) / (math.log(2) ** 2))
        self.hash_count = int(self.bit_size / capacity * math.log(2))
        self.bit_size = max(self.bit_size, 1)
        self.hash_count = max(self.hash_count, 1)
        self._bit_array = 0  # Python int 充当位数组
        self._count = 0

    def _hashes(self, item: str):
        """生成多个哈希值"""
        result = []
        h = hashlib.md5(item.encode('utf-8'))
        h1 = int(h.hexdigest(), 16)
        h = hashlib.sha1(item.encode('utf-8'))
        h2 = int(h.hexdigest(), 16)
        for i in range(self.hash_count):
            result.append((h1 + i * h2) % self.bit_size)
        return result

    def add(self, item: str):
        """添加元素到 Bloom Filter"""
        for bit in self._hashes(item):
            self._bit_array |= (1 << bit)
        self._count += 1

    def __contains__(self, item: str) -> bool:
        """检查元素是否可能在集合中（存在假阳性）"""
        for bit in self._hashes(item):
            if not (self._bit_array & (1 << bit)):
                return False
        return True

    def clear(self):
        """清空 Bloom Filter"""
        self._bit_array = 0
        self._count = 0

    def save(self, filepath: str):
        """将 Bloom Filter 的位数组序列化到本地文件
        
        Args:
            filepath: 保存路径
        """
        import pickle
        import os
        data = {
            'capacity': self.capacity,
            'error_rate': self.error_rate,
            'bit_size': self.bit_size,
            'hash_count': self.hash_count,
            'bit_array': self._bit_array,
            'count': self._count,
        }
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'wb') as f:
            pickle.dump(data, f)

    @classmethod
    def load(cls, filepath: str) -> 'BloomFilter':
        """从本地文件加载 Bloom Filter
        
        Args:
            filepath: 保存路径
            
        Returns:
            加载后的 BloomFilter 实例，如果文件不存在或损坏则返回空的 BloomFilter
        """
        import pickle
        import os
        if not os.path.exists(filepath):
            logger.info("未检测到本地 Bloom Filter 缓存文件，将使用空 Bloom Filter 初始化")
            return cls(capacity=200000, error_rate=0.01)

        try:
            with open(filepath, 'rb') as f:
                data = pickle.load(f)
            bf = cls(capacity=data['capacity'], error_rate=data['error_rate'])
            bf.bit_size = data['bit_size']
            bf.hash_count = data['hash_count']
            bf._bit_array = data['bit_array']
            bf._count = data['count']
            logger.info("从 %s 加载 Bloom Filter 成功，位大小=%s，哈希数=%s，已记录元素=%s",
                        filepath, bf.bit_size, bf.hash_count, bf._count)
            return bf
        except (pickle.UnpicklingError, EOFError, KeyError) as e:
            logger.warning("从 %s 加载 Bloom Filter 失败 (%s)，使用空 Bloom Filter", filepath, e)
            return cls(capacity=200000, error_rate=0.01)
        except Exception as e:
            logger.warning("从 %s 加载 Bloom Filter 异常 (%s)，使用空 Bloom Filter", filepath, e)
            return cls(capacity=200000, error_rate=0.01)


class DynamoDBDeduplicationService:
    """AWS DynamoDB 数据库助手，用于比对重复项和保存资源"""
    def __init__(self):
        # 导入 config 以统一获取配置，使用绝对/相对导入
        import config as _cfg
        self.aws_access_key_id = _cfg.AWS_ACCESS_KEY_ID
        self.aws_secret_access_key = _cfg.AWS_SECRET_ACCESS_KEY
        self.region_name = _cfg.AWS_REGION
        self.table_name = "fuli_resources"
        self.use_gsi = True
        self._lock = threading.Lock()          # 线程安全锁
        self._scanned_resource_links = None    # 扫描结果本地缓存
        self._scan_cache_time = 0.0            # 扫描缓存的时间戳
        self._scan_cache_ttl = 300             # 扫描缓存 TTL（秒），5 分钟后过期
        self._cached_urls = set()              # 新插入的 URL 缓存
        self._cached_resource_links = set()    # 新插入的磁力链接缓存
        # Bloom Filter 缓存 — 减少 DynamoDB 查询次数
        # 尝试从本地文件加载，避免进程重启后的冷启动穿透
        self._bloom_cache_path = os.path.join(_cfg.PROJECT_ROOT, "cache", "bloom_filter.pkl")
        self._url_bloom = BloomFilter.load(self._bloom_cache_path)
        self._bloom_sync_count = 0             # 已同步到 Bloom Filter 的 URL 数
        self._bloom_dirty = False              # Bloom Filter 是否有未保存的变更
        self._bloom_save_interval = 120        # 自动保存间隔（秒）
        self._bloom_last_save = time.time()
        self._bloom_save_timer = None           # 定时保存定时器
        self._executor = None                  # 线程池延迟加载

        if not self.aws_access_key_id or not self.aws_secret_access_key:
            raise ValueError(
                "AWS 凭证未配置！请检查相关环境变量（AWS 标准凭证变量）是否设置正确。"
            )

        self.client = boto3.client(
            "dynamodb",
            region_name=self.region_name,
            aws_access_key_id=self.aws_access_key_id,
            aws_secret_access_key=self.aws_secret_access_key,
        )
        self.ensure_table_exists()
        from concurrent.futures import ThreadPoolExecutor
        self._executor = ThreadPoolExecutor(max_workers=5)
        # 启动定时保存 Bloom Filter 的定时器
        self._start_bloom_save_timer()

    def ensure_table_exists(self):
        """确保 DynamoDB 表已存在，若不存在则创建"""
        try:
            existing_tables = self.client.list_tables()["TableNames"]
            if self.table_name in existing_tables:
                return

            logger.info("AWS DynamoDB 表 %s 不存在，正在自动创建...", self.table_name)
            self.client.create_table(
                TableName=self.table_name,
                AttributeDefinitions=[{"AttributeName": "url", "AttributeType": "S"}],
                KeySchema=[{"AttributeName": "url", "KeyType": "HASH"}],
                BillingMode="PAY_PER_REQUEST"
            )
            # 等待表激活
            waiter = self.client.get_waiter("table_exists")
            waiter.wait(TableName=self.table_name)
            logger.info("AWS DynamoDB 表 %s 创建成功！", self.table_name)
        except Exception as e:
            logger.error("创建 AWS DynamoDB 表失败: %s", e)
            raise

    def check_url_exists(self, url, source: str = None):
        """检查单条 URL 或其规范化相对路径是否已存在于 AWS DynamoDB"""
        if not url:
            return False
        rel_key = get_url_dedup_key(url, source)
        with self._lock:
            if url in self._cached_urls or (rel_key and rel_key in self._cached_urls):
                return True
        try:
            # 优先查完整 URL
            response = self.client.get_item(
                TableName=self.table_name,
                Key={"url": {"S": url}},
                ProjectionExpression="#u",
                ExpressionAttributeNames={"#u": "url"}
            )
            if "Item" in response:
                return True
            # 次查规范化相对路径键
            if rel_key and rel_key != url:
                response = self.client.get_item(
                    TableName=self.table_name,
                    Key={"url": {"S": rel_key}},
                    ProjectionExpression="#u",
                    ExpressionAttributeNames={"#u": "url"}
                )
                return "Item" in response
            return False
        except Exception as e:
            logger.error("AWS DynamoDB check_url_exists 失败: %s", e)
            return False

    def filter_existing_urls(self, urls, source: str = None):
        """批量检查哪些 URL 已存在于 AWS DynamoDB 中，返回已存在的 URL 集合
        
        支持完整 URL 比对与相对路径（Relative Path + Source）规范化键比对，
        能够跨域名轮换（*.xyz）有效命中历史已抓取数据。
        """
        if not urls:
            return set()
        existing = set()
        
        # key_to_original 记录用于查询的每个 key 对应哪些原始输入 URL
        key_to_original = {}
        urls_to_query = set()
        
        with self._lock:
            for url in urls:
                if not url:
                    continue
                rel_key = get_url_dedup_key(url, source)
                
                # 1. 优先比对本地内存中新写入的缓存 URL / 相对路径键
                if url in self._cached_urls or (rel_key and rel_key in self._cached_urls):
                    existing.add(url)
                    continue
                
                # 2. 登记映射关系
                if url not in key_to_original:
                    key_to_original[url] = []
                key_to_original[url].append(url)
                urls_to_query.add(url)
                
                if rel_key and rel_key != url:
                    if rel_key not in key_to_original:
                        key_to_original[rel_key] = []
                    key_to_original[rel_key].append(url)
                    urls_to_query.add(rel_key)
        
        if not urls_to_query:
            return existing

        query_keys_list = list(urls_to_query)
        # batch_get_item 每次最多获取 100 个
        for i in range(0, len(query_keys_list), 100):
            chunk = query_keys_list[i:i+100]
            try:
                request_items = {
                    self.table_name: {
                        "Keys": [{"url": {"S": k}} for k in chunk],
                        "ProjectionExpression": "#u",
                        "ExpressionAttributeNames": {"#u": "url"}
                    }
                }
                response = self.client.batch_get_item(RequestItems=request_items)
                
                # 处理已返回的 Items
                responses = response.get("Responses", {}).get(self.table_name, [])
                for item in responses:
                    key_val = item.get("url", {}).get("S")
                    if key_val and key_val in key_to_original:
                        for orig_url in key_to_original[key_val]:
                            existing.add(orig_url)
                
                # 将 DynamoDB 中已存在的 key 同步回 Bloom Filter 和本地内存缓存
                with self._lock:
                    for item in responses:
                        key_val = item.get("url", {}).get("S")
                        if key_val:
                            self._cached_urls.add(key_val)
                            self._url_bloom.add(key_val)
                            self._bloom_dirty = True
                
                # 处理未处理完的 Keys（最大重试 5 次）
                unprocessed = response.get("UnprocessedKeys", {}).get(self.table_name, {})
                max_retries = 5
                retry_count = 0
                while unprocessed and "Keys" in unprocessed and unprocessed["Keys"] and retry_count < max_retries:
                    retry_count += 1
                    time.sleep(0.5 * (1 + retry_count * 0.5))  # 退避重试，逐渐增加等待
                    response = self.client.batch_get_item(RequestItems=unprocessed)
                    responses = response.get("Responses", {}).get(self.table_name, [])
                    for item in responses:
                        key_val = item.get("url", {}).get("S")
                        if key_val and key_val in key_to_original:
                            for orig_url in key_to_original[key_val]:
                                existing.add(orig_url)
                    unprocessed = response.get("UnprocessedKeys", {}).get(self.table_name, {})
                if unprocessed and "Keys" in unprocessed and unprocessed["Keys"]:
                    logger.warning("AWS DynamoDB filter_existing_urls 有 %s 个未处理 Keys，已超过最大重试次数 %s", len(unprocessed['Keys']), max_retries)
            except Exception as e:
                logger.error("AWS DynamoDB filter_existing_urls 失败: %s", e)
        return existing

    def filter_existing_resource_links(self, resource_links):
        """批量检查哪些 resource_link 已存在于 AWS DynamoDB 中，返回已存在的 resource_link 集合"""
        if not resource_links:
            return set()
        
        valid_links = [l for l in resource_links if l]
        if not valid_links:
            return set()

        existing = set()

        # 优先比对本地内存中新写入的缓存磁力（线程安全）
        links_to_query = []
        with self._lock:
            for link in valid_links:
                if link in self._cached_resource_links:
                    existing.add(link)
                else:
                    links_to_query.append(link)

        if not links_to_query:
            return existing

        if self.use_gsi:
            try:
                # DynamoDB Query 的 KeyConditionExpression 不支持 IN 操作符
                # 改为逐个查询，使用线程池并发执行以提升性能
                def _query_single_link(link):
                    """查询单个 resource_link 是否存在"""
                    response = self.client.query(
                        TableName=self.table_name,
                        IndexName="resource_link-index",
                        KeyConditionExpression="resource_link = :v",
                        ExpressionAttributeValues={":v": {"S": link}},
                        ProjectionExpression="resource_link"
                    )
                    return link if response.get("Count", 0) > 0 else None

                # 使用线程池并发查询，每批最多 100 个并发任务
                max_workers = min(100, len(links_to_query))
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {executor.submit(_query_single_link, link): link
                               for link in links_to_query}
                    for future in as_completed(futures):
                        result = future.result()
                        if result:
                            existing.add(result)
                return existing
            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code")
                error_msg = e.response.get("Error", {}).get("Message", "")
                if error_code == "ValidationException" and "index" in error_msg.lower():
                    logger.warning("检测到 AWS DynamoDB 表中未创建 resource_link-index 索引。")
                    logger.warning("正在回退到 Scan 缓存兼容模式。")
                    logger.warning("为了更好的性能，建议您在 AWS DynamoDB 控制台中为表 fuli_resources 创建二级索引（分区键: resource_link, 索引名: resource_link-index）。")
                    self.use_gsi = False
                    # 回退到 Scan 扫描缓存模式（仅首次扫描全表获取所有 resource_link，带 TTL 过期）
                    needs_scan = False
                    with self._lock:
                        if self._scanned_resource_links is None or (time.time() - self._scan_cache_time) > self._scan_cache_ttl:
                            needs_scan = True
                    if needs_scan:
                        logger.info("正在执行 AWS DynamoDB 全表扫描以同步磁力链接缓存...")
                        with self._lock:
                            if self._scanned_resource_links is None or (time.time() - self._scan_cache_time) > self._scan_cache_ttl:
                                 self._scanned_resource_links = self.get_all_resource_links_by_scan()
                                 self._scan_cache_time = time.time()
                                 logger.info("扫描缓存同步完成，已加载 %s 条磁力链接，缓存 TTL %s 秒。", len(self._scanned_resource_links), self._scan_cache_ttl)

                    with self._lock:
                        for link in links_to_query:
                            if link in self._scanned_resource_links:
                                existing.add(link)
                    return existing
                else:
                    logger.error("AWS DynamoDB query GSI 失败: %s", e)
                    return existing
            except Exception as e:
                logger.error("AWS DynamoDB query GSI 失败: %s", e)
                return existing

        # 正常使用 Scan 扫描缓存模式（仅首次扫描全表获取所有 resource_link，带 TTL 过期）
        needs_scan = False
        with self._lock:
            if self._scanned_resource_links is None or (time.time() - self._scan_cache_time) > self._scan_cache_ttl:
                needs_scan = True
        if needs_scan:
            logger.info("正在执行 AWS DynamoDB 全表扫描以同步磁力链接缓存...")
            with self._lock:
                if self._scanned_resource_links is None or (time.time() - self._scan_cache_time) > self._scan_cache_ttl:
                     self._scanned_resource_links = self.get_all_resource_links_by_scan()
                     self._scan_cache_time = time.time()
                     logger.info("扫描缓存同步完成，已加载 %s 条磁力链接，缓存 TTL %s 秒。", len(self._scanned_resource_links), self._scan_cache_ttl)

        with self._lock:
            for link in links_to_query:
                if link in self._scanned_resource_links:
                    existing.add(link)
        return existing

    def get_all_resource_links_by_scan(self):
        """全表扫描获取所有的 resource_link 集合（无索引时的兼容模式）"""
        existing_links = set()
        last_evaluated_key = None
        page_count = 0
        while True:
            kwargs = {
                "TableName": self.table_name,
                "ProjectionExpression": "resource_link",
            }
            if last_evaluated_key:
                kwargs["ExclusiveStartKey"] = last_evaluated_key
            try:
                response = self.client.scan(**kwargs)
                page_count += 1
                items = response.get("Items", [])
                for item in items:
                    link_val = item.get("resource_link", {})
                    if "S" in link_val and link_val["S"]:
                        existing_links.add(link_val["S"])
                
                if page_count % 5 == 0:
                    logger.info("扫描进度: 已处理 %s 页数据，当前缓存 %s 条磁力链接...", page_count, len(existing_links))

                last_evaluated_key = response.get("LastEvaluatedKey")
                if not last_evaluated_key:
                    break
            except Exception as e:
                logger.error("AWS DynamoDB Scan 失败: %s", e)
                break
        return existing_links

    def insert_resource(self, url, resource_link, source: str = None):
        """向 AWS DynamoDB 异步写入一条数据（双键写入：完整 URL 与相对路径规范化键）"""
        if not url:
            return False
        
        rel_key = get_url_dedup_key(url, source)
        
        # 立即更新本地内存缓存和 Bloom Filter，防去重击穿（线程安全）
        with self._lock:
            self._cached_urls.add(url)
            self._url_bloom.add(url)
            if rel_key and rel_key != url:
                self._cached_urls.add(rel_key)
                self._url_bloom.add(rel_key)
            self._bloom_dirty = True
            if resource_link:
                self._cached_resource_links.add(resource_link)
                if self._scanned_resource_links is not None:
                    self._scanned_resource_links.add(resource_link)

        # 异步提交写入任务
        if self._executor:
            self._executor.submit(self._async_put_item, url, resource_link)
            if rel_key and rel_key != url:
                self._executor.submit(self._async_put_item, rel_key, resource_link)
        return True

    def insert_resources_batch(self, items_list, source: str = None):
        """向 AWS DynamoDB 异步批量写入数据列表（双键写入）"""
        if not items_list:
            return
        
        keys_to_put = []
        with self._lock:
            for d in items_list:
                url = d.get('url')
                item_source = d.get('source') or source
                resource_link = d.get('resource_link')
                if url:
                    self._cached_urls.add(url)
                    self._url_bloom.add(url)
                    keys_to_put.append((url, resource_link))
                    
                    rel_key = get_url_dedup_key(url, item_source)
                    if rel_key and rel_key != url:
                        self._cached_urls.add(rel_key)
                        self._url_bloom.add(rel_key)
                        keys_to_put.append((rel_key, resource_link))
                        
                if resource_link:
                    self._cached_resource_links.add(resource_link)
                    if self._scanned_resource_links is not None:
                        self._scanned_resource_links.add(resource_link)
            self._bloom_dirty = True

        if self._executor:
            for u, r in keys_to_put:
                self._executor.submit(self._async_put_item, u, r)

    def mark_urls_processed(self, urls, source: str = None):
        """批量将已处理但未入主库的 URL（如磁力重复、语言/番号过滤）标记到去重缓存与 DynamoDB 中"""
        if not urls:
            return
        
        keys_to_put = []
        with self._lock:
            for url in urls:
                if not url:
                    continue
                self._cached_urls.add(url)
                self._url_bloom.add(url)
                keys_to_put.append((url, None))
                
                rel_key = get_url_dedup_key(url, source)
                if rel_key and rel_key != url:
                    self._cached_urls.add(rel_key)
                    self._url_bloom.add(rel_key)
                    keys_to_put.append((rel_key, None))
            self._bloom_dirty = True

        if self._executor:
            for u, r in keys_to_put:
                self._executor.submit(self._async_put_item, u, r)

    def _async_put_item(self, url, resource_link):
        """实际在线程池中运行的 DynamoDB 写入任务"""
        item = {"url": {"S": url}}
        # 针对 GSI resource_link-index（最大限制 2048 字节），超长则跳过写入索引字段，避免 ValidationException
        if resource_link and len(resource_link.encode('utf-8')) <= 2048:
            item["resource_link"] = {"S": resource_link}

        try:
            self.client.put_item(
                TableName=self.table_name,
                Item=item
            )
        except Exception as e:
            # 异步写入失败不应影响主流程，记录即可
            logger.error("AWS DynamoDB 异步写入记录失败 (%s): %s", url, e)

    def _start_bloom_save_timer(self):
        """启动定时保存 Bloom Filter 的后台定时器"""
        def _timer_loop():
            while True:
                time.sleep(self._bloom_save_interval)
                self._save_bloom_filter()

        self._bloom_save_timer = threading.Thread(
            target=_timer_loop, daemon=True, name="bloom-save-timer"
        )
        self._bloom_save_timer.start()

    def _save_bloom_filter(self):
        """将 Bloom Filter 保存到本地文件（如果有关联变更）"""
        if not self._bloom_dirty:
            return
        self._save_bloom_filter_sync()

    def _save_bloom_filter_sync(self):
        """同步保存 Bloom Filter 到本地文件（带锁）"""
        try:
            with self._lock:
                self._url_bloom.save(self._bloom_cache_path)
                self._bloom_dirty = False
                self._bloom_last_save = time.time()
        except Exception as e:
            logger.error("Bloom Filter 持久化保存失败: %s", e)

    def shutdown(self):
        """在爬虫关闭时清理后台线程池并关闭 DynamoDB 客户端连接
        
        在关闭前将 Bloom Filter 持久化到本地，避免数据丢失。
        """
        # 关闭前确保 Bloom Filter 已持久化
        if self._bloom_dirty:
            self._save_bloom_filter_sync()
        self._bloom_save_timer = None
        if self._executor:
            try:
                self._executor.shutdown(wait=True)
            except Exception as e:
                logger.error("DynamoDBDeduplicationService shutdown executor 异常: %s", e)
        if self.client:
            try:
                self.client.close()
            except Exception as e:
                logger.error("DynamoDBDeduplicationService shutdown client 异常: %s", e)