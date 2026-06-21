from utils.pikpak_extractor import get_pikpak_link

if __name__ == "__main__":
    import sys
    # 如果作为脚本运行，同样可进行测试
    test_cached = "https://keepshare.org/7f70llj0/magnet%3A%3Fxt%3Durn%3Abtih%3A20e7c99dd69926c7b617f6e74268de9b961e7f10"
    print("=" * 60)
    print("测试用例 1 (已缓存资源):")
    res_link1 = get_pikpak_link(test_cached)
    print(f"最终结果: {res_link1}")
