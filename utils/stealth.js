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
            get: function () { return undefined; },
            configurable: true,
            enumerable: true
        });
    } catch (_) { }

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
    } catch (_) { }

    // ============================================================
    // 3. navigator.plugins — 真实的 PluginArray
    // ============================================================
    try {
        function _FakePlugin(name, filename, desc) {
            this.name = name;
            this.filename = filename;
            this.description = desc;
            this.length = 0;
            Object.setPrototypeOf(this, Plugin.prototype);
            this.item = function () { return null; };
            this.namedItem = function () { return null; };
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
            this.item = function (i) { return this[i] || null; };
            this.namedItem = function (n) { for (var k = 0; k < this.length; k++) { if (this[k].name === n) return this[k]; } return null; };
            this.refresh = function () { };
            Object.setPrototypeOf(this, PluginArray.prototype);
        }
        Object.defineProperty(navigator, 'plugins', {
            get: function () { return new _FakePluginArray(); },
            configurable: true
        });
    } catch (_) { }

    // ============================================================
    // 4. navigator.mimeTypes — 真实的 MimeTypeArray
    // ============================================================
    try {
        function _FakeMimeType(type, desc, suffixes) {
            this.type = type;
            this.description = desc;
            this.suffixes = suffixes;
            this.enabledPlugin = null;
            Object.setPrototypeOf(this, MimeType.prototype);
        }
        function _FakeMimeTypeArray() {
            var types = [
                new _FakeMimeType('application/pdf', 'Portable Document Format', 'pdf'),
                new _FakeMimeType('text/pdf', 'Portable Document Format', 'pdf'),
            ];
            for (var j = 0; j < types.length; j++) { this[j] = types[j]; }
            this.length = types.length;
            this.item = function (i) { return this[i] || null; };
            this.namedItem = function (n) { for (var k = 0; k < this.length; k++) { if (this[k].type === n) return this[k]; } return null; };
            Object.setPrototypeOf(this, MimeTypeArray.prototype);
        }
        Object.defineProperty(navigator, 'mimeTypes', {
            get: function () { return new _FakeMimeTypeArray(); },
            configurable: true
        });
    } catch (_) { }

    // ============================================================
    // 5. navigator.languages — 正常设置
    // ============================================================
    try {
        Object.defineProperty(navigator, 'languages', {
            get: function () { return ['zh-CN', 'zh', 'en']; },
            configurable: true
        });
    } catch (_) { }

    // ============================================================
    // 6. window.chrome — 补全缺失属性
    // ============================================================
    try {
        if (window.chrome) {
            if (!window.chrome.runtime) window.chrome.runtime = {};
            var _safeMethods = ['connect', 'sendMessage', 'getManifest', 'getURL', 'reload', 'restart'];
            for (var j = 0; j < _safeMethods.length; j++) {
                if (typeof window.chrome.runtime[_safeMethods[j]] !== 'function') {
                    window.chrome.runtime[_safeMethods[j]] = function () { };
                }
            }
            if (!window.chrome.webstore) window.chrome.webstore = {};
            if (!window.chrome.csi) window.chrome.csi = function () { };
            if (!window.chrome.loadTimes) window.chrome.loadTimes = function () { };
        }
    } catch (_) { }

    // ============================================================
    // 7. Permissions API — 关键权限返回 granted
    // ============================================================
    try {
        if (navigator.permissions && navigator.permissions.query) {
            var _origQuery = navigator.permissions.query.bind(navigator.permissions);
            navigator.permissions.query = function (desc) {
                var _grantedNames = ['notifications', 'clipboard-read', 'clipboard-write', 'geolocation'];
                if (desc && _grantedNames.indexOf(desc.name) !== -1) {
                    return Promise.resolve({ state: 'granted', onchange: null });
                }
                return _origQuery(desc)['catch'](function () { return { state: 'prompt', onchange: null }; });
            };
        }
    } catch (_) { }

    // ============================================================
    // 8. WebGL 指纹伪装
    // ============================================================
    try {
        var _glGetParam = WebGLRenderingContext.prototype.getParameter;
        var _gl2GetParam = WebGL2RenderingContext.prototype.getParameter;
        var _patchGL = function (origFn) {
            return function (pname) {
                if (pname === 37445) return 'Intel Inc.';
                if (pname === 37446) return 'Intel Iris OpenGL Engine';
                return origFn.call(this, pname);
            };
        };
        if (_glGetParam) WebGLRenderingContext.prototype.getParameter = _patchGL(_glGetParam);
        if (_gl2GetParam) WebGL2RenderingContext.prototype.getParameter = _patchGL(_gl2GetParam);
    } catch (_) { }

    // ============================================================
    // 9. Screen 尺寸规格化
    // ============================================================
    try {
        if (screen.width <= 0 || screen.height <= 0) {
            Object.defineProperties(screen, {
                width: { get: function () { return 1920; }, configurable: true },
                height: { get: function () { return 1080; }, configurable: true },
                availWidth: { get: function () { return 1920; }, configurable: true },
                availHeight: { get: function () { return 1040; }, configurable: true },
                colorDepth: { get: function () { return 24; }, configurable: true },
                pixelDepth: { get: function () { return 24; }, configurable: true },
            });
        }
    } catch (_) { }

    // ============================================================
    // 10. Headless Chromium 额外修补 (仅限 headless)
    // ============================================================
    try {
        // headless Chrome 缺少这些 userAgent 中的特征
        var _currUA = navigator.userAgent;
        if (_currUA.indexOf('Headless') !== -1 || _currUA.indexOf('headless') !== -1) {
            // 由 Playwright 替换 userAgent，这里仅做兜底
        }
    } catch (_) { }
})();