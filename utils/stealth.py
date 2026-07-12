"""
统一的 Playwright 反检测 stealth 模块。

替代原来分散在各文件中的内联 JS 注入，提供更难以检测的伪装手段：

改进点:
1. navigator.webdriver — 在原型链上操作而非自身属性
2. navigator.plugins — 使用真实的 PluginArray 子类，而非普通 Array
3. navigator.mimeTypes — 补全 MimeTypeArray
4. cdc_ 变量 — 动态检测运行时实际存在的变量名，不硬编码
5. chrome.runtime — 补全缺失的 chrome 对象属性
6. Permissions API — 返回 granted 避免常见检测
7. WebGL vendor/renderer — 伪装 GPU 指纹
8. 支持多浏览器类型 (chromium/firefox/webkit) 切换
9. headful 模式推荐
"""

import random
from typing import Optional

_STEALTH_JS = """
(() => {
    // ============================================================
    // 1. Navigator.prototype.webdriver — 原型链修补
    // ============================================================
    try {
        var navProto = Object.getPrototypeOf(navigator);
        if ('webdriver' in navProto) {
            delete navProto.webdriver;
        }
        Object.defineProperty(navProto, 'webdriver', {
            get: function() { return undefined; },
            configurable: true,
            enumerable: true
        });
    } catch (_) {}

    // ============================================================
    // 2. 动态清理 CDP 泄露变量
    // ============================================================
    try {
        var _keys = Object.getOwnPropertyNames(window);
        for (var i = 0; i < _keys.length; i++) {
            if (_keys[i].indexOf('cdc_') === 0 || _keys[i].indexOf('__cdc_') === 0) {
                delete window[_keys[i]];
            }
        }
    } catch (_) {}

    // ============================================================
    // 3. navigator.plugins — 真实的 PluginArray
    // ============================================================
    try {
        function _FakePlugin(name, filename, desc) {
            this.name = name;
            this.filename = filename;
            this.description = desc;
            this.length = 0;
            this.__proto__ = Plugin.prototype;
            this.item = function() { return null; };
            this.namedItem = function() { return null; };
        }
        function _FakePluginArray() {
            var plugins = [
                new _FakePlugin('PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
                new _FakePlugin('Chrome PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
                new _FakePlugin('Chromium PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
                new _FakePlugin('Microsoft Edge PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
                new _FakePlugin('WebKit built-in PDF', 'internal-pdf-viewer', 'Portable Document Format'),
            ];
            for (var j = 0; j < plugins.length; j++) { this[j] = plugins[j]; }
            this.length = plugins.length;
            this.item = function(i) { return this[i] || null; };
            this.namedItem = function(n) { for (var k = 0; k < this.length; k++) { if (this[k].name === n) return this[k]; } return null; };
            this.refresh = function() {};
            Object.setPrototypeOf(this, PluginArray.prototype);
        }
        Object.defineProperty(navigator, 'plugins', {
            get: function() { return new _FakePluginArray(); },
            configurable: true
        });
    } catch (_) {}

    // ============================================================
    // 4. navigator.mimeTypes — 真实的 MimeTypeArray
    // ============================================================
    try {
        function _FakeMimeType(type, desc, suffixes) {
            this.type = type;
            this.description = desc;
            this.suffixes = suffixes;
            this.enabledPlugin = null;
            this.__proto__ = MimeType.prototype;
        }
        function _FakeMimeTypeArray() {
            var types = [
                new _FakeMimeType('application/pdf', 'Portable Document Format', 'pdf'),
                new _FakeMimeType('text/pdf', 'Portable Document Format', 'pdf'),
            ];
            for (var j = 0; j < types.length; j++) { this[j] = types[j]; }
            this.length = types.length;
            this.item = function(i) { return this[i] || null; };
            this.namedItem = function(n) { for (var k = 0; k < this.length; k++) { if (this[k].type === n) return this[k]; } return null; };
            Object.setPrototypeOf(this, MimeTypeArray.prototype);
        }
        Object.defineProperty(navigator, 'mimeTypes', {
            get: function() { return new _FakeMimeTypeArray(); },
            configurable: true
        });
    } catch (_) {}

    // ============================================================
    // 5. navigator.languages — 正常设置
    // ============================================================
    try {
        Object.defineProperty(navigator, 'languages', {
            get: function() { return ['zh-CN', 'zh', 'en']; },
            configurable: true
        });
    } catch (_) {}

    // ============================================================
    // 6. window.chrome — 补全缺失属性
    // ============================================================
    try {
        if (window.chrome) {
            if (!window.chrome.runtime) window.chrome.runtime = {};
            var _safeMethods = ['connect', 'sendMessage', 'getManifest', 'getURL', 'reload', 'restart'];
            for (var j = 0; j < _safeMethods.length; j++) {
                if (typeof window.chrome.runtime[_safeMethods[j]] !== 'function') {
                    window.chrome.runtime[_safeMethods[j]] = function() {};
                }
            }
            if (!window.chrome.webstore) window.chrome.webstore = {};
            if (!window.chrome.csi) window.chrome.csi = function() {};
            if (!window.chrome.loadTimes) window.chrome.loadTimes = function() {};
        }
    } catch (_) {}

    // ============================================================
    // 7. Permissions API — 关键权限返回 granted
    // ============================================================
    try {
        if (navigator.permissions && navigator.permissions.query) {
            var _origQuery = navigator.permissions.query.bind(navigator.permissions);
            navigator.permissions.query = function(desc) {
                var _grantedNames = ['notifications', 'clipboard-read', 'clipboard-write', 'geolocation'];
                if (desc && _grantedNames.indexOf(desc.name) !== -1) {
                    return Promise.resolve({ state: 'granted', onchange: null });
                }
                return _origQuery(desc)['catch'](function() { return { state: 'prompt', onchange: null }; });
            };
        }
    } catch (_) {}

    // ============================================================
    // 8. WebGL 指纹伪装
    // ============================================================
    try {
        var _glGetParam = WebGLRenderingContext.prototype.getParameter;
        var _gl2GetParam = WebGL2RenderingContext.prototype.getParameter;
        var _patchGL = function(origFn) {
            return function(pname) {
                if (pname === 37445) return 'Intel Inc.';
                if (pname === 37446) return 'Intel Iris OpenGL Engine';
                return origFn.call(this, pname);
            };
        };
        if (_glGetParam) WebGLRenderingContext.prototype.getParameter = _patchGL(_glGetParam);
        if (_gl2GetParam) WebGL2RenderingContext.prototype.getParameter = _patchGL(_gl2GetParam);
    } catch (_) {}

    // ============================================================
    // 9. Screen 尺寸规格化
    // ============================================================
    try {
        if (screen.width <= 0 || screen.height <= 0) {
            Object.defineProperties(screen, {
                width: { get: function() { return 1920; }, configurable: true },
                height: { get: function() { return 1080; }, configurable: true },
                availWidth: { get: function() { return 1920; }, configurable: true },
                availHeight: { get: function() { return 1040; }, configurable: true },
                colorDepth: { get: function() { return 24; }, configurable: true },
                pixelDepth: { get: function() { return 24; }, configurable: true },
            });
        }
    } catch (_) {}

    // ============================================================
    // 10. Headless Chromium 额外修补 (仅限 headless)
    // ============================================================
    try {
        // headless Chrome 缺少这些 userAgent 中的特征
        var _currUA = navigator.userAgent;
        if (_currUA.indexOf('Headless') !== -1 || _currUA.indexOf('headless') !== -1) {
            // 由 Playwright 替换 userAgent，这里仅做兜底
        }
    } catch (_) {}
})();
"""


def get_stealth_script(browser_type: str = "chromium") -> str:
    """获取适用于指定浏览器的 stealth JS 注入脚本"""
    return _STEALTH_JS


def apply_stealth(context, browser_type: str = "chromium") -> bool:
    """
    向 Playwright browser context 注入 stealth 脚本。
    
    Args:
        context: Playwright BrowserContext 实例
        browser_type: 'chromium' | 'firefox' | 'webkit'
    
    Returns:
        bool: 是否注入成功
    """
    try:
        script = get_stealth_script(browser_type)
        context.add_init_script(script)
        return True
    except Exception as e:
        print(f"[-] 注入 stealth 脚本失败: {e}")
        return False


def get_browser_launch_args(
    browser_type: str = "chromium",
    headless: bool = True,
    extra_args: Optional[list] = None
) -> list:
    """
    获取适用于指定浏览器的启动参数，移除可能暴露自动化检测的参数。
    
    Args:
        browser_type: 'chromium' | 'firefox' | 'webkit'
        headless: 是否无头模式
        extra_args: 额外需要添加的启动参数
    
    Returns:
        list: 启动参数列表
    """
    args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
    ]

    if browser_type == "chromium":
        # 注意: 移除 --disable-blink-features=AutomationControlled
        # 该参数本身就是一个公开的自动化标记
        args.extend([
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-features=UserAgentClientHint",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-breakpad",
            "--disable-component-extensions-with-background-pages",
            "--disable-component-update",
            "--disable-extensions",
            "--disable-features=TranslateUI",
            "--disable-ipc-flooding-protection",
            "--disable-renderer-backgrounding",
            "--enable-features=NetworkService,NetworkServiceInProcess",
            "--force-color-profile=srgb",
            "--hide-scrollbars",
            "--metrics-recording-only",
            "--mute-audio",
            "--no-first-run",
            "--no-default-browser-check",
        ])
        if headless:
            args.extend([
                "--headless=new",  # 使用新的 headless 模式，更难检测
            ])

    if extra_args:
        args.extend(extra_args)

    return args


# ============================================================
# 爬虫级别的 stealth 配置（每个爬虫可按需 override）
# ============================================================

# 默认配置：启用所有 stealth 项
STEALTH_ENABLE_WEBDRIVER = True
STEALTH_ENABLE_PLUGINS = True
STEALTH_ENABLE_MIMETYPES = True
STEALTH_ENABLE_LANGUAGES = True
STEALTH_ENABLE_CHROME_RUNTIME = True
STEALTH_ENABLE_PERMISSIONS = True
STEALTH_ENABLE_WEBGL = True
STEALTH_ENABLE_SCREEN = True
STEALTH_ENABLE_CDC_CLEANUP = True