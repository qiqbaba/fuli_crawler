    (function() {
        function initPdfViewportLazyLoading() {
            try {
                const pDoc = (window.parent && window.parent.document) || document;
                if (!pDoc) return;

                function loadSingleImage(img) {
                    if (img && img.dataset.src && (!img.src || img.src !== img.dataset.src)) {
                        img.src = img.dataset.src;
                        img.onload = () => {
                            img.classList.remove('lazy-pdf-img');
                            img.classList.add('loaded');
                            checkContainerScroll(img.closest('.pdf-scroll-container'));
                        };
                        img.onerror = () => {
                            img.classList.remove('lazy-pdf-img');
                            img.classList.add('loaded');
                        };
                    }
                }

                function loadAllImagesInContainer(container) {
                    if (!container) return;
                    const lazyImgs = container.querySelectorAll('img.lazy-pdf-img, img[data-src]');
                    lazyImgs.forEach(img => {
                        loadSingleImage(img);
                        try {
                            if (window._pdfImgObserver) window._pdfImgObserver.unobserve(img);
                        } catch(e) {}
                    });
                }

                function checkContainerScroll(container) {
                    if (!container) return;
                    const curItemId = container.dataset.itemId || "";
                    if (container.dataset.renderedItemId !== curItemId) {
                        container.dataset.renderedItemId = curItemId;
                        container.dataset.initScrolled = "0";
                    }
                    if (container.dataset.initScrolled === "1") return;
                    const firstImg = container.querySelector('.pdf-page-img');
                    if (firstImg && firstImg.complete && firstImg.naturalHeight > 0) {
                        if (container.scrollHeight > container.clientHeight) {
                            container.scrollTop = 200;
                            container.dataset.initScrolled = "1";
                        }
                    }
                }

                // 1. 初始化 IntersectionObserver（提前 300px 预加载，确保滑到时已加载完毕）
                if (!window._pdfImgObserver) {
                    window._pdfImgObserver = new IntersectionObserver((entries, observer) => {
                        entries.forEach(entry => {
                            if (entry.isIntersecting) {
                                const img = entry.target;
                                loadSingleImage(img);
                                observer.unobserve(img);
                            }
                        });
                    }, {
                        root: null,
                        rootMargin: '300px 0px 300px 0px',
                        threshold: 0.01
                    });
                }

                // 2. 扫描并观察所有懒加载图片
                const lazyImages = pDoc.querySelectorAll('img.lazy-pdf-img');
                lazyImages.forEach(img => {
                    if (img.dataset.src && img.src !== img.dataset.src) {
                        window._pdfImgObserver.observe(img);
                    }
                });

                // 3. 为所有卡片容器绑定内部滚动事件（用户在容器内向下滑动时，即刻动态渲染容器内后续所有页码）
                const containers = pDoc.querySelectorAll('.pdf-scroll-container');
                containers.forEach(container => {
                    if (!container.dataset.scrollBound) {
                        container.dataset.scrollBound = "1";
                        container.addEventListener('scroll', () => {
                            loadAllImagesInContainer(container);
                        }, { passive: true });
                    }
                    checkContainerScroll(container);
                });
            } catch(e) {}
        }

        initPdfViewportLazyLoading();
        const timers = [30, 80, 150, 300, 600, 1200, 2000];
        timers.forEach(t => setTimeout(initPdfViewportLazyLoading, t));

        try {
            const pDoc = (window.parent && window.parent.document) || document;
            if (!window._pdfMutationObserver && pDoc && pDoc.body) {
                window._pdfMutationObserver = new MutationObserver(initPdfViewportLazyLoading);
                window._pdfMutationObserver.observe(pDoc.body, { childList: true, subtree: true });
            }
        } catch(e) {}
    })();
