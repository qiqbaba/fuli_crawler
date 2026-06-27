"""为Excel文件添加模型更新时间"""
import pandas as pd
import requests
import re
import time
from datetime import datetime

def get_civitai_model_update_time(model_url, max_retries=3):
    """从Civitai API获取模型更新时间"""
    if pd.isna(model_url) or 'civitai.com' not in str(model_url):
        return None
    
    try:
        # 提取模型ID
        url = str(model_url)
        match = re.search(r'civitai\.com/models/(\d+)', url)
        if not match:
            return None
        
        model_id = match.group(1)
        api_url = f"https://civitai.com/api/v1/models/{model_id}"
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json'
        }
        
        for attempt in range(max_retries):
            try:
                response = requests.get(api_url, headers=headers, timeout=20, verify=True)
                break
            except requests.exceptions.SSLError:
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                # 最后一次尝试禁用SSL验证
                response = requests.get(api_url, headers=headers, timeout=20, verify=False)
                break
            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                raise e
        
        if response.status_code == 200:
            data = response.json()
            # 获取模型版本信息中的更新时间
            model_versions = data.get('modelVersions', [])
            if model_versions:
                # 取最新版本的创建时间
                updated_at = model_versions[0].get('createdAt') or model_versions[0].get('updatedAt')
                if updated_at:
                    dt = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
                    return dt.strftime('%Y-%m-%d')
            # 备用：使用模型本身的更新时间
            updated_at = data.get('lastVersionAt') or data.get('updatedAt')
            if updated_at:
                dt = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
                return dt.strftime('%Y-%m-%d')
        elif response.status_code == 404:
            return "模型已删除"
        
        return None
        
    except Exception as e:
        print(f"  ⚠ Civitai API错误 ({model_url}): {e}")
        return None


def get_huggingface_model_update_time(model_url, max_retries=3):
    """从HuggingFace API获取模型更新时间"""
    if pd.isna(model_url) or 'huggingface.co' not in str(model_url):
        return None
    
    try:
        url = str(model_url)
        # 提取repo路径，例如 https://huggingface.co/stabilityai/sdxl-vae -> stabilityai/sdxl-vae
        match = re.search(r'huggingface\.co/([^/]+/[^/]+)', url)
        if not match:
            return None
        
        repo_id = match.group(1)
        api_url = f"https://huggingface.co/api/models/{repo_id}"
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        for attempt in range(max_retries):
            try:
                response = requests.get(api_url, headers=headers, timeout=20)
                break
            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                raise e
        
        if response.status_code == 200:
            data = response.json()
            updated_at = data.get('lastModified')
            if updated_at:
                dt = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
                return dt.strftime('%Y-%m-%d')
        elif response.status_code == 401:
            return "需要认证/私有仓库"
        elif response.status_code == 404:
            return "仓库不存在"
        
        return None
        
    except Exception as e:
        print(f"  ⚠ HuggingFace API错误 ({model_url}): {e}")
        return None


def get_model_update_time(model_url):
    """根据链接类型获取更新时间"""
    if pd.isna(model_url) or not str(model_url).strip():
        return "无链接"
    
    url = str(model_url)
    
    if 'civitai.com' in url:
        return get_civitai_model_update_time(url)
    elif 'huggingface.co' in url:
        return get_huggingface_model_update_time(url)
    else:
        return "未知来源"


def main():
    excel_path = 'd:/comfyui_models_link_check_report.xlsx'
    output_path = 'd:/comfyui_models_analysis_with_dates.xlsx'
    
    print(f"📖 读取Excel文件: {excel_path}")
    df = pd.read_excel(excel_path)
    print(f"📊 共 {len(df)} 条记录\n")
    
    print("⏳ 正在获取模型更新时间...\n")
    
    update_times = []
    for idx, row in df.iterrows():
        model_name = row.get('模型名称', f'第{idx+1}行')
        download_url = row.get('下载链接')
        
        update_time = get_model_update_time(download_url)
        update_times.append(update_time)
        
        status_icon = "✓" if update_time and update_time not in ["无链接", "未知来源", "需要认证/私有仓库", "模型已删除", "仓库不存在"] else "✗"
        print(f"  [{idx+1}/{len(df)}] {status_icon} {model_name}: {update_time or '未获取到'}")
        
        # 避免请求过快
        if idx % 5 == 0 and idx > 0:
            time.sleep(1)
    
    # 添加新列
    df['当前版本更新时间'] = update_times
    
    # 统计
    print("\n" + "="*80)
    print("📊 更新时间获取结果统计:")
    print("="*80)
    
    valid_dates = df[df['当前版本更新时间'].apply(lambda x: x and x not in ["无链接", "未知来源", "需要认证/私有仓库", "模型已删除", "仓库不存在", None])]
    no_link = df[df['当前版本更新时间'] == "无链接"]
    auth_required = df[df['当前版本更新时间'] == "需要认证/私有仓库"]
    not_found = df[df['当前版本更新时间'].isin(["模型已删除", "仓库不存在"])]
    unknown = df[df['当前版本更新时间'] == "未知来源"]
    
    print(f"  ✓ 成功获取: {len(valid_dates)}")
    print(f"  📭 无链接: {len(no_link)}")
    print(f"  🔒 需要认证: {len(auth_required)}")
    print(f"  ❌ 已删除/不存在: {len(not_found)}")
    print(f"  ❓ 未知来源: {len(unknown)}")
    
    # 保存结果
    df.to_excel(output_path, index=False)
    print(f"\n✅ 已保存到: {output_path}")
    print("="*80)


if __name__ == '__main__':
    main()
