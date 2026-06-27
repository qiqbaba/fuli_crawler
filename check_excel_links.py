"""检查Excel文件中的下载链接是否有效"""
import pandas as pd
import requests
from urllib.parse import urlparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

def check_link(url, timeout=15):
    """检查单个链接是否有效"""
    if pd.isna(url) or not url or str(url).strip() == '':
        return url, "空链接", "N/A"
    
    url = str(url).strip()
    
    # 验证URL格式
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return url, "无效URL格式", "N/A"
    except Exception as e:
        return url, f"URL解析错误: {e}", "N/A"
    
    try:
        # 先尝试HEAD请求（更快）
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        response = requests.head(url, timeout=timeout, headers=headers, allow_redirects=True)
        
        # 如果HEAD返回405或403，尝试GET
        if response.status_code in [405, 403, 404]:
            response = requests.get(url, timeout=timeout, headers=headers, allow_redirects=True, stream=True)
        
        status = response.status_code
        
        if status == 200:
            return url, "✓ 有效", str(status)
        elif status in [301, 302, 307, 308]:
            final_url = response.url
            return url, f"⚠ 重定向到: {final_url[:80]}...", str(status)
        elif status == 404:
            return url, "✗ 404 不存在", str(status)
        elif status == 403:
            return url, "⚠ 403 禁止访问（可能需要登录）", str(status)
        elif status == 429:
            return url, "⚠ 429 请求过于频繁", str(status)
        elif status >= 500:
            return url, f"✗ 服务器错误", str(status)
        else:
            return url, f"? 状态码: {status}", str(status)
            
    except requests.exceptions.Timeout:
        return url, "✗ 超时", "Timeout"
    except requests.exceptions.ConnectionError as e:
        return url, f"✗ 连接失败", "ConnectionError"
    except requests.exceptions.RequestException as e:
        return url, f"✗ 请求异常: {str(e)[:50]}", "Error"
    except Exception as e:
        return url, f"✗ 未知错误: {str(e)[:50]}", "Error"


def main():
    excel_path = 'd:/comfyui_models_analysis_updated.xlsx'
    output_path = 'd:/comfyui_models_link_check_report.xlsx'
    
    print(f"📖 读取Excel文件: {excel_path}")
    df = pd.read_excel(excel_path)
    
    print(f"📊 共 {len(df)} 条记录")
    print(f"📋 列名: {df.columns.tolist()}")
    
    # 检查是否有"下载链接"列
    link_col = None
    for col in df.columns:
        if '下载链接' in str(col) or '链接' in str(col) or 'url' in str(col).lower() or 'download' in str(col).lower():
            link_col = col
            break
    
    if link_col is None:
        print("❌ 未找到下载链接列")
        return
    
    print(f"🔗 找到链接列: {link_col}")
    
    # 提取所有链接
    urls = df[link_col].tolist()
    valid_urls = [u for u in urls if pd.notna(u) and str(u).strip()]
    empty_count = len(urls) - len(valid_urls)
    
    print(f"\n📈 统计:")
    print(f"   总记录数: {len(urls)}")
    print(f"   有链接: {len(valid_urls)}")
    print(f"   空链接: {empty_count}")
    print(f"\n⏳ 开始检查链接（这可能需要几分钟）...\n")
    
    # 并发检查链接
    results = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(check_link, url): url for url in valid_urls}
        
        completed = 0
        for future in as_completed(futures):
            completed += 1
            url, status, code = future.result()
            results[url] = (status, code)
            
            # 进度显示
            if completed % 5 == 0 or completed == len(valid_urls):
                print(f"   进度: {completed}/{len(valid_urls)}")
    
    # 更新DataFrame
    df['链接状态'] = df[link_col].apply(lambda x: results.get(x, ("空链接", "N/A"))[0] if pd.notna(x) and str(x).strip() else "空链接")
    df['HTTP状态码'] = df[link_col].apply(lambda x: results.get(x, ("N/A", "N/A"))[1] if pd.notna(x) and str(x).strip() else "N/A")
    
    # 统计结果
    print("\n" + "="*80)
    print("📊 检查结果汇总:")
    print("="*80)
    
    status_counts = df['链接状态'].value_counts()
    for status, count in status_counts.items():
        print(f"   {status}: {count}")
    
    # 筛选出有问题的链接
    problem_df = df[~df['链接状态'].str.contains('✓|空链接', na=False)]
    
    if len(problem_df) > 0:
        print(f"\n❌ 发现 {len(problem_df)} 个有问题的链接:")
        print("-"*80)
        for idx, row in problem_df.iterrows():
            model_name = row.get('模型名称', row.get('文件名', f'第{idx+1}行'))
            link = row[link_col]
            status = row['链接状态']
            print(f"   [{idx+1}] {model_name}")
            print(f"       链接: {link}")
            print(f"       状态: {status}")
            print()
    
    # 保存结果
    df.to_excel(output_path, index=False)
    print(f"\n✅ 详细报告已保存到: {output_path}")
    print("="*80)


if __name__ == '__main__':
    main()
