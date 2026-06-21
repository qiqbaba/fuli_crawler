import os
import boto3
from botocore.exceptions import ClientError


class R2Uploader:
    """
    Cloudflare R2 文件上传工具（通过 S3 兼容接口）。
    上传成功后会删除本地临时文件。
    """

    def __init__(self, endpoint_url, access_key_id, secret_access_key, bucket_name):
        from botocore.config import Config
        self.bucket_name = bucket_name
        self.s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",  # R2 固定使用 auto
            config=Config(s3={'addressing_style': 'path'}),
        )
        print(f"[*] R2Uploader 已初始化，Bucket: {bucket_name}")

    def upload_pdf(self, local_path: str, remote_key: str) -> str:
        """
        上传 PDF 文件到 R2。

        Args:
            local_path: 本地文件路径（如 /tmp/pdfs/2025/xxx.pdf）
            remote_key: R2 对象 Key（如 pdfs/2025/xxx.pdf）

        Returns:
            上传成功时返回 remote_key；失败时返回空字符串。
        """
        if not os.path.exists(local_path):
            print(f"[-] R2 上传失败：本地文件不存在: {local_path}")
            return ""

        try:
            self.s3.upload_file(
                local_path,
                self.bucket_name,
                remote_key,
                ExtraArgs={"ContentType": "application/pdf"},
            )
            print(f"[+] PDF 已上传至 R2: {remote_key}")

            # 上传成功后立即删除本地临时文件
            try:
                os.remove(local_path)
                print(f"[+] 本地临时文件已删除: {local_path}")
            except OSError as e:
                print(f"[-] 删除本地临时文件失败（不影响主流程）: {e}")

            return remote_key

        except ClientError as e:
            print(f"[-] R2 上传失败: {e}")
            return ""
        except Exception as e:
            print(f"[-] R2 上传时发生未知错误: {e}")
            return ""


def get_r2_uploader():
    """
    从环境变量读取配置，创建并返回 R2Uploader 实例。
    若环境变量未配置，返回 None。
    """
    from config import (
        R2_ENDPOINT_URL,
        R2_ACCESS_KEY_ID,
        R2_SECRET_ACCESS_KEY,
        R2_BUCKET_NAME,
        use_r2,
    )

    if not use_r2():
        return None

    return R2Uploader(
        endpoint_url=R2_ENDPOINT_URL,
        access_key_id=R2_ACCESS_KEY_ID,
        secret_access_key=R2_SECRET_ACCESS_KEY,
        bucket_name=R2_BUCKET_NAME,
    )
