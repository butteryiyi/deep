# browser_manager.py
"""
DeepSeek 反代（网络拦截模式）
- 通过 DOM 输入消息、点击发送
- 通过拦截浏览器网络请求的 SSE 响应来读取回复
- PoW 由浏览器自己处理，代码完全不涉及
- 不受虚拟列表 DOM 回收影响，一个 token 都不丢
"""

import os
import sys
import time
import json
import asyncio
import base64
import shutil
from pathlib import Path
from datetime import datetime
from typing import AsyncGenerator, Optional

from auth_handler import AuthHandler


class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.logged_in = False
        self.start_time = time.time()
        self.heartbeat_count = 0
        self.requests_handled = 0
        self.total_requests = 0

        self.email = os.getenv("DEEPSEEK_EMAIL", "")
        self.password = os.getenv("DEEPSEEK_PASSWORD", "")
        self.headless = os.getenv("HEADLESS", "true").lower() == "true"
        self._engine = "unknown"

        # 请求锁
        self._lock = asyncio.Lock()

        # 就绪控制
        self._ready = False
        self._ready_event = asyncio.Event()

        # SSE 流捕获队列（每次请求一个新的）
        self._sse_queue: asyncio.Queue = None
        self._active_response_handler = None

    # ── 就绪控制 ──

    async def wait_until_ready(self, timeout: float = 180.0) -> bool:
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    @property
    def is_ready(self) -> bool:
        return self._ready

    # ── Camoufox 缓存 ──

    def _prepare_camoufox_cache(self):
        home_cache = Path.home() / ".cache"
        store_dir = home_cache / "camoufox_store"
        cache_dir = home_cache / "camoufox"
        if store_dir.exists() and any(store_dir.iterdir()):
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                shutil.copytree(store_dir, cache_dir, dirs_exist_ok=True)
                print(f"  ✅ Camoufox 缓存已恢复")
            except Exception as e:
                print(f"  ⚠️ 恢复缓存失败: {e}")
        else:
            store_dir.mkdir(parents=True, exist_ok=True)

    def _save_camoufox_cache(self):
        home_cache = Path.home() / ".cache"
        store_dir = home_cache / "camoufox_store"
        cache_dir = home_cache / "camoufox"
        if cache_dir.exists() and any(cache_dir.iterdir()):
            try:
                store_dir.mkdir(parents=True, exist_ok=True)
                shutil.copytree(cache_dir, store_dir, dirs_exist_ok=True)
            except Exception:
                pass

    # ── 初始化 ──

    async def initialize(self):
        print("🔧 正在初始化浏览器...")

        if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
            if os.path.isdir("/opt/browsers"):
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/browsers"

        camoufox_ok = False
        try:
            os.environ['CAMOUFOX_NO_UPDATE_CHECK'] = '1'
            self._prepare_camoufox_cache()
            await self._start_with_camoufox()
            camoufox_ok = True
            self._engine = "camoufox"
            self._save_camoufox_cache()
        except Exception as e:
            print(f"⚠️ Camoufox 失败: {e}，回退 Playwright Firefox")
            if hasattr(self, '_camoufox'):
                try:
                    await self._camoufox.__aexit__(None, None, None)
                except Exception:
                    pass

        if not camoufox_ok:
            await self._start_with_playwright()
            self._engine = "playwright-firefox"

        await self._inject_stealth_scripts()

        # 登录
        auth = AuthHandler(self.page, context=self.context)
        self.logged_in = await auth.login(self.email, self.password)

        if not self.logged_in:
            print("⚠️ 登录可能未完成")
        else:
            print("🎉 登录成功！")

        # 注册网络响应拦截器（核心）
        self._setup_response_interceptor()

        self._ready = True
        self._ready_event.set()
        print(f"✅ 就绪（引擎: {self._engine}，模式: 网络拦截，无 PoW）")

    async def _start_with_camoufox(self):
        print("  → Camoufox...")
        from camoufox.async_api import AsyncCamoufox
        self._camoufox = AsyncCamoufox(headless=self.headless, geoip=False)
        self.browser = await self._camoufox.__aenter__()
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        self.page = await self.context.new_page()
        print("  ✅ Camoufox 已启动")

    async def _start_with_playwright(self):
        print("  → Playwright Firefox...")
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()

        try:
            self.browser = await self.playwright.firefox.launch(
                headless=self.headless, args=["--no-sandbox"]
            )
        except Exception:
            import subprocess
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "firefox"],
                capture_output=True, text=True, timeout=120,
            )
            self.browser = await self.playwright.firefox.launch(
                headless=self.headless, args=["--no-sandbox"]
            )

        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
                "Gecko/20100101 Firefox/126.0"
            ),
        )
        self.page = await self.context.new_page()
        print("  ✅ Playwright Firefox 已启动")

    async def _inject_stealth_scripts(self):
        if self._engine == "camoufox":
            await self.context.add_init_script(
                "if(navigator.webdriver!==undefined)"
                "{Object.defineProperty(navigator,'webdriver',{get:()=>undefined})}"
            )
        else:
            await self.context.add_init_script("""
                Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
                Object.defineProperty(navigator,'languages',{
                    get:()=>['zh-CN','zh','en-US','en']
                });
            """)

    # ══════════════════════════════════════════════════════
    # 核心：网络响应拦截器
    # ══════════════════════════════════════════════════════

    def _setup_response_interceptor(self):
        """
        监听浏览器发出的所有网络响应。
        当检测到 /chat/completion 的 SSE 流响应时，
        在 JS 层用 ReadableStream 逐块读取并推入队列。
        """
        self.page.on("response", self._on_response)
        print("  🔌 网络响应拦截器已注册")

    async def _on_response(self, response):
        """Playwright response 事件回调"""
        url = response.url
        # 只拦截聊天完成的 SSE 流
        if "/chat/completion" not in url:
            return
        if response.status != 200:
            return

        # 检查是否是 SSE
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            return

        print(f"  🎯 捕获到 SSE 响应: {url[:80]}")

        # 读取完整响应体，解析 SSE
        try:
            body = await response.text()
            self._parse_sse_body(body)
        except Exception as e:
            print(f"  ⚠️ 读取 SSE 响应失败: {e}")
            if self._sse_queue:
                await self._sse_queue.put({"type": "error", "data": str(e)})
                await self._sse_queue.put({"type": "done"})

    def _parse_sse_body(self, body: str):
        """解析 SSE 响应体，将内容推入队列"""
        if not self._sse_queue:
            return

        loop = asyncio.get_event_loop()
        lines = body.split("\n")

        for line in lines:
            line = line.strip()
            if not line.startswith("data: "):
                continue
            data_str = line[6:].strip()

            if data_str == "[DONE]":
                asyncio.ensure_future(self._sse_queue.put({"type": "done"}))
                return

            try:
                parsed = json.loads(data_str)
                choices = parsed.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        asyncio.ensure_future(
                            self._sse_queue.put({"type": "content", "data": content})
                        )
            except json.JSONDecodeError:
                pass

        # 如果没有遇到 [DONE]，也标记结束
        asyncio.ensure_future(self._sse_queue.put({"type": "done"}))

    # ══════════════════════════════════════════════════════
    # 备用方案：通过 JS 层面拦截 fetch 响应
    # （某些情况下 Playwright response 事件无法获取流式 body）
    # ══════════════════════════════════════════════════════

    async def _setup_js_fetch_interceptor(self):
        """
        在页面中注入 fetch 拦截器。
        monkey-patch window.fetch，当检测到 /chat/completion 请求时，
        clone 响应并逐块读取推入全局数组。
        """
        await self.page.evaluate("""
            () => {
                if (window.__ds_fetch_patched) return;
                window.__ds_fetch_patched = true;

                // 全局存储捕获的 SSE 数据
                window.__ds_sse_chunks = [];
                window.__ds_sse_done = false;
                window.__ds_sse_error = null;

                const originalFetch = window.fetch;
                window.fetch = async function(...args) {
                    const response = await originalFetch.apply(this, args);
                    const url = (typeof args[0] === 'string') ? args[0] : args[0]?.url || '';

                    if (url.includes('/chat/completion')) {
                        const contentType = response.headers.get('content-type') || '';
                        if (contentType.includes('text/event-stream')) {
                            // 重置
                            window.__ds_sse_chunks = [];
                            window.__ds_sse_done = false;
                            window.__ds_sse_error = null;

                            // clone 响应，原始响应正常返回给页面
                            const cloned = response.clone();

                            // 异步读取 clone 的流
                            (async () => {
                                try {
                                    const reader = cloned.body.getReader();
                                    const decoder = new TextDecoder();
                                    let buffer = '';

                                    while (true) {
                                        const { done, value } = await reader.read();
                                        if (done) break;

                                        buffer += decoder.decode(value, { stream: true });
                                        const lines = buffer.split('\\n');
                                        buffer = lines.pop(); // 保留不完整的最后一行

                                        for (const line of lines) {
                                            const trimmed = line.trim();
                                            if (!trimmed.startsWith('data: ')) continue;
                                            const dataStr = trimmed.substring(6).trim();

                                            if (dataStr === '[DONE]') {
                                                window.__ds_sse_done = true;
                                                return;
                                            }

                                            try {
                                                const parsed = JSON.parse(dataStr);
                                                const choices = parsed.choices || [];
                                                if (choices.length > 0) {
                                                    const content = choices[0]?.delta?.content || '';
                                                    if (content) {
                                                        window.__ds_sse_chunks.push(content);
                                                    }
                                                }
                                            } catch (e) {}
                                        }
                                    }
                                    window.__ds_sse_done = true;
                                } catch (e) {
                                    window.__ds_sse_error = e.message;
                                    window.__ds_sse_done = true;
                                }
                            })();
                        }
                    }
                    return response;
                };
                console.log('[DS] fetch 拦截器已注入');
            }
        """)
        print("  🔌 JS fetch 拦截器已注入")

    async def _ensure_js_interceptor(self):
        """确保 JS 拦截器仍然有效"""
        try:
            patched = await self.page.evaluate(
                "() => window.__ds_fetch_patched === true"
            )
            if not patched:
                await self._setup_js_fetch_interceptor()
        except Exception:
            await self._setup_js_fetch_interceptor()

    # ── 新对话 ──

    async def _start_new_chat(self):
        if "chat.deepseek.com" not in self.page.url:
            await self.page.goto(
                "https://chat.deepseek.com/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(2)

        selectors = [
            "xpath=//*[contains(text(), '开启新对话')]",
            "xpath=//*[contains(text(), '新对话')]",
            "xpath=//*[contains(text(), 'New chat')]",
            "div.ds-icon-button",
            "[class*='new-chat']",
        ]
        for sel in selectors:
            try:
                btn = self.page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await asyncio.sleep(1)
                    print("  ✅ 新对话")
                    return
            except Exception:
                continue

        await self.page.goto(
            "https://chat.deepseek.com/",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await asyncio.sleep(3)

    # ── 输入并发送 ──

    async def _type_and_send(self, message: str):
        textarea = self.page.locator(
            "textarea[placeholder*='DeepSeek'], "
            "textarea[placeholder*='发送消息'], "
            "textarea, "
            "[contenteditable='true']"
        ).first
        await textarea.wait_for(state="visible", timeout=10000)
        await textarea.click()
        await asyncio.sleep(0.3)

        try:
            await textarea.fill("")
            await asyncio.sleep(0.1)
            await textarea.fill(message)
        except Exception:
            await self.page.evaluate("""
                (text) => {
                    const el = document.querySelector('textarea')
                        || document.querySelector('[contenteditable="true"]');
                    if (!el) return;
                    if (el.tagName === 'TEXTAREA') {
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLTextAreaElement.prototype, 'value'
                        ).set;
                        setter.call(el, text);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    } else {
                        el.innerText = text;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                }
            """, message)

        await asyncio.sleep(0.5)
        await textarea.press("Enter")
        print(f"  → 已发送 ({len(message)} 字符)")
        await asyncio.sleep(1)

    # ══════════════════════════════════════════════════════
    # 核心发送方法
    # ══════════════════════════════════════════════════════

    async def send_message(self, message: str) -> str:
        full = ""
        async for chunk in self.send_message_stream(message):
            full += chunk
        return full

    async def send_message_stream(self, message: str) -> AsyncGenerator[str, None]:
        if not self._ready:
            ok = await self.wait_until_ready(timeout=180)
            if not ok:
                yield "[错误] 浏览器初始化超时"
                return

        async with self._lock:
            self.total_requests += 1
            self.requests_handled += 1
            req_id = self.total_requests
            print(f"📨 请求 #{req_id} (长度: {len(message)} 字符)")

            if not self.page:
                yield "[错误] 浏览器未就绪"
                return

            try:
                # 准备：注入 JS 拦截器 + 开启新对话
                await self._ensure_js_interceptor()
                await self._start_new_chat()
                await asyncio.sleep(1)
                await self._ensure_js_interceptor()

                # 重置 JS 层的 SSE 捕获状态
                await self.page.evaluate("""
                    () => {
                        window.__ds_sse_chunks = [];
                        window.__ds_sse_done = false;
                        window.__ds_sse_error = null;
                    }
                """)

                # 同时准备 Playwright 层的队列
                self._sse_queue = asyncio.Queue()

                # 发送消息
                await self._type_and_send(message)

                # ── 从两个来源竞争读取 SSE 数据 ──
                # 优先使用 JS 拦截器（更可靠），Playwright response 事件作为备用
                print(f"  [{req_id}] 等待 SSE 流...")

                full_text = ""
                read_index = 0
                max_wait = 600.0  # 10分钟
                waited = 0.0
                idle_count = 0
                stream_started = False

                while waited < max_wait:
                    # 从 JS 层读取
                    result = await self.page.evaluate("""
                        (fromIndex) => {
                            return {
                                chunks: window.__ds_sse_chunks.slice(fromIndex),
                                total: window.__ds_sse_chunks.length,
                                done: window.__ds_sse_done,
                                error: window.__ds_sse_error,
                            };
                        }
                    """, read_index)

                    if result.get("error"):
                        err = result["error"]
                        print(f"  [{req_id}] ❌ SSE 错误: {err}")
                        if not full_text:
                            yield f"[错误] {err}"
                        break

                    new_chunks = result.get("chunks", [])
                    if new_chunks:
                        if not stream_started:
                            stream_started = True
                            print(f"  [{req_id}] 流开始")

                        for chunk in new_chunks:
                            full_text += chunk
                            yield chunk
                        read_index += len(new_chunks)
                        idle_count = 0

                    if result.get("done"):
                        if not new_chunks:
                            break
                        continue

                    if not new_chunks:
                        idle_count += 1

                        # 如果 JS 拦截器没数据，尝试从 Playwright 队列读
                        if not self._sse_queue.empty():
                            try:
                                msg = self._sse_queue.get_nowait()
                                if msg["type"] == "content":
                                    if not stream_started:
                                        stream_started = True
                                        print(f"  [{req_id}] 流开始 (PW)")
                                    full_text += msg["data"]
                                    yield msg["data"]
                                    idle_count = 0
                                elif msg["type"] == "done":
                                    break
                                elif msg["type"] == "error":
                                    if not full_text:
                                        yield f"[错误] {msg['data']}"
                                    break
                            except asyncio.QueueEmpty:
                                pass

                    sleep_time = 0.1 if idle_count < 50 else 0.3
                    await asyncio.sleep(sleep_time)
                    waited += sleep_time

                    # 超时检查
                    if not stream_started and waited > 60:
                        print(f"  [{req_id}] ⚠️ 60秒未收到流数据")
                        # 尝试 DOM 兜底
                        fallback = await self._dom_fallback()
                        if fallback:
                            yield fallback
                        else:
                            yield "[错误] 等待响应超时"
                        break

                    if idle_count > 0 and idle_count % 100 == 0:
                        print(f"  [{req_id}] ⏳ idle={idle_count}")

                # 清理
                self._sse_queue = None
                print(f"  [{req_id}] ✅ 完成，长度: {len(full_text)}")

            except Exception as e:
                print(f"  [{req_id}] ❌ {e}")
                import traceback
                traceback.print_exc()
                yield f"[错误] {str(e)}"

    async def _dom_fallback(self) -> str:
        """DOM 兜底：万一网络拦截失败，从 DOM 读取"""
        try:
            text = await self.page.evaluate("""
                () => {
                    const items = document.querySelectorAll(
                        'div[data-virtual-list-item-key]'
                    );
                    if (items.length > 0) {
                        const last = items[items.length - 1];
                        const md = last.querySelectorAll('[class*="ds-markdown"]');
                        if (md.length > 0)
                            return md[md.length - 1].textContent || '';
                    }
                    const allMd = document.querySelectorAll('[class*="ds-markdown"]');
                    if (allMd.length > 0)
                        return allMd[allMd.length - 1].textContent || '';
                    return '';
                }
            """)
            return (text or "").strip()
        except Exception:
            return ""

    # ── 其他方法 ──

    async def is_alive(self) -> bool:
        try:
            if not self._ready or not self.page or self.page.is_closed():
                return False
            await self.page.evaluate("() => document.title")
            return True
        except Exception:
            return False

    async def get_status(self) -> dict:
        alive = await self.is_alive()
        return {
            "browser_alive": alive,
            "logged_in": self.logged_in,
            "ready": self._ready,
            "engine": self._engine,
            "mode": "network-intercept",
            "has_token": True,
            "cookie_count": 0,
            "uptime_seconds": time.time() - self.start_time,
            "heartbeat_count": self.heartbeat_count,
            "requests_handled": self.requests_handled,
            "total_requests": self.total_requests,
            "current_url": (
                self.page.url
                if self.page and not self.page.is_closed()
                else "N/A"
            ),
            "timestamp": datetime.now().isoformat(),
        }

    async def take_screenshot_base64(self) -> Optional[str]:
        try:
            if not self.page or self.page.is_closed():
                return None
            buf = await self.page.screenshot(full_page=False)
            return base64.b64encode(buf).decode("utf-8")
        except Exception:
            return None

    async def simulate_activity(self):
        if not self.page or self.page.is_closed():
            return
        try:
            self.heartbeat_count += 1
            import random
            await self.page.mouse.move(
                random.randint(100, 1800),
                random.randint(100, 900),
            )
            await self.page.evaluate("""
                () => {
                    document.dispatchEvent(new MouseEvent('mousemove', {
                        clientX: Math.random() * window.innerWidth,
                        clientY: Math.random() * window.innerHeight
                    }));
                    window.scrollBy(0, Math.random() > 0.5 ? 1 : -1);
                }
            """)
            if self.heartbeat_count % 10 == 0:
                print(f"💓 心跳 #{self.heartbeat_count}")
        except Exception as e:
            print(f"⚠️ 心跳异常: {e}")

    async def shutdown(self):
        try:
            self._save_camoufox_cache()
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            print("🔒 已关闭")
        except Exception as e:
            print(f"⚠️ {e}")
