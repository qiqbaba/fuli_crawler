import threading
import time
import hashlib
import boto3
from botocore.exceptions import ClientError
from utils.logger import get_logger

logger = get_logger(__name__)


class BloomFilter:
    """轻量 Bloom Filter，用于减少 DynamoDB 查询次数
    
    使用多个哈希函数（基于 hashlib）的位数组实现。
    存在假阳性（可配置），但不存在假阴性。
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
        self._url_bloom = BloomFilter(capacity=200000, error_rate=0.01)
        self._bloom_sync_count = 0             # 已同步到 Bloom Filter 的 URL 数
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

    def check_url_exists(self, url):
        """检查单条 URL 是否已存在于 AWS DynamoDB"""
        if not url:
            return False
        with self._lock:
            if url in self._cached_urls:
                return True
        try:
            response = self.client.get_item(
                TableName=self.table_name,
                Key={"url": {"S": url}},
                ProjectionExpression="#u",
                ExpressionAttributeNames={"#u": "url"}
            )
            return "Item" in response
        except Exception as e:
            logger.error("AWS DynamoDB check_url_exists 失败: %s", e)
            return False

    def filter_existing_urls(self, urls):
        """批量检查哪些 URL 已存在于 AWS DynamoDB 中，返回已存在的 URL 集合
        
        使用 Bloom Filter 作为一级缓存，减少 DynamoDB 查询次数。
        """
        if not urls:
            return set()
        existing = set()
        
        # 1. Bloom Filter 快速过滤：一定不在 Bloom Filter 中的 URL 肯定不存在
        urls_to_query = []
        with self._lock:
            for url in urls:
                if not url:
                    continue
                if url in self._cached_urls:
                    existing.add(url)
                elif url in self._url_bloom:
                    # Bloom Filter 命中（可能有假阳性），仍需查 DynamoDB 确认
                    urls_to_query.append(url)
                else:
                    # Bloom Filter 未命中 → 肯定不存在，跳过 DynamoDB 查询
                    pass
        
        if not urls_to_query:
            return existing

        urls_list = list(urls_to_query)
        # batch_get_item 每次最多获取 100 个
        for i in range(0, len(urls_list), 100):
            chunk = urls_list[i:i+100]
            try:
                request_items = {
                    self.table_name: {
                        "Keys": [{"url": {"S": url}} for url in chunk],
                        "ProjectionExpression": "#u",
                        "ExpressionAttributeNames": {"#u": "url"}
                    }
                }
                response = self.client.batch_get_item(RequestItems=request_items)
                
                # 处理已返回的 Items
                responses = response.get("Responses", {}).get(self.table_name, [])
                for item in responses:
                    url_val = item.get("url", {}).get("S")
                    if url_val:
                        existing.add(url_val)
                
                # 将 DynamoDB 中已存在的 URL 同步回 Bloom Filter，减少未来查询
                with self._lock:
                    for item in responses:
                        url_val = item.get("url", {}).get("S")
                        if url_val:
                            self._url_bloom.add(url_val)
                
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
                        url_val = item.get("url", {}).get("S")
                        if url_val:
                            existing.add(url_val)
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
                # 使用 IN 操作符批量查询，每批最多 100 个值（DynamoDB 限制）
                for i in range(0, len(links_to_query), 100):
                    chunk = links_to_query[i:i+100]
                    # 构建 IN 表达式: resource_link IN (:v0, :v1, ...)
                    placeholders = [f":v{j}" for j in range(len(chunk))]
                    in_expr = "resource_link IN (" + ", ".join(placeholders) + ")"
                    attr_values = {f":v{j}": {"S": link} for j, link in enumerate(chunk)}

                    response = self.client.query(
                        TableName=self.table_name,
                        IndexName="resource_link-index",
                        KeyConditionExpression=in_expr,
                        ExpressionAttributeValues=attr_values,
                        ProjectionExpression="resource_link"
                    )
                    for item in response.get("Items", []):
                        link_val = item.get("resource_link", {}).get("S")
                        if link_val:
                            existing.add(link_val)
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

    def insert_resource(self, url, resource_link):
        """向 AWS DynamoDB 异步写入一条数据"""
        if not url:
            return False
        
        # 立即更新本地内存缓存和 Bloom Filter，防去重击穿（线程安全）
        with self._lock:
            self._cached_urls.add(url)
            self._url_bloom.add(url)
            if resource_link:
                self._cached_resource_links.add(resource_link)
                if self._scanned_resource_links is not None:
                    self._scanned_resource_links.add(resource_link)

        # 异步提交写入任务
        if self._executor:
            self._executor.submit(self._async_put_item, url, resource_link)
        return True

    def _async_put_item(self, url, resource_link):
        """实际在线程池中运行的 DynamoDB 写入任务"""
        item = {"url": {"S": url}}
        if resource_link:
            item["resource_link"] = {"S": resource_link}

        try:
            self.client.put_item(
                TableName=self.table_name,
                Item=item
            )
        except Exception as e:
            # 异步写入失败不应影响主流程，记录即可
            logger.error("AWS DynamoDB 异步写入记录失败 (%s): %s", url, e)

    def shutdown(self):
        """在爬虫关闭时清理后台线程池并关闭 DynamoDB 客户端连接"""
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