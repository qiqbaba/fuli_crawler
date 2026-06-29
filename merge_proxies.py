import json

# Read proxies.txt
with open('proxies.txt', 'r') as f:
    txt_proxies = [line.strip() for line in f if line.strip()]

# Read proxy_cache.json
with open('temp_profiles/proxy_cache.json', 'r') as f:
    cache = json.load(f)

# Get existing addresses
existing = set(p['address'] for p in cache['proxies'])

# Add new proxies from txt
added = 0
for addr in txt_proxies:
    if addr not in existing:
        cache['proxies'].append({
            'protocol': 'http',
            'address': addr,
            'source': 'proxies_txt'
        })
        existing.add(addr)
        added += 1

print(f'proxies.txt 总数: {len(txt_proxies)}')
print(f'原有代理数: {len(cache["proxies"]) - added}')
print(f'新增代理数: {added}')
print(f'合并后总数: {len(cache["proxies"])}')

# Write back
with open('temp_profiles/proxy_cache.json', 'w') as f:
    json.dump(cache, f, indent=2)

print('已保存到 temp_profiles/proxy_cache.json')
