# browser_manager.py
"""
DeepSeek 反代（路由拦截模式）
- 用 Playwright route API 做中间人，拦截所有 /chat/completion 请求
- 读取真实响应后同时转发给页面和推入队列
- 不依赖 fetch monkey-patch，不受 Worker/XHR/EventSource 限制
- PoW 由浏览器自己处理
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

        self._lock = asyncio.Lock()
        self._ready = False
        self._ready_event = asyncio.Event()

        # SSE 捕获
        self._sse_queue: asyncio.Queue = None
        self._capture_active = False

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
            except Exception:
                pass
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

        # 注册路由拦截器
        await self._setup_route_interceptor()

        self._ready = True
        self._ready_event.set()
        print(f"✅ 就绪（引擎: {self._engine}，模式: 路由拦截）")

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
    # 核心：Playwright route 拦截器
    # ══════════════════════════════════════════════════════

    async def _setup_route_interceptor(self):
        """
        用 page.route 拦截 /chat/completion 请求。
        拿到真实响应后：
        1. 解析 SSE 内容推入队列
        2. 把完整响应原样转发回页面（页面正常工作）
        """
        async def handle_completion_route(route):
            """拦截 /chat/completion 请求"""
            if not self._capture_active:
                # 不在捕获模式，直接放行
                await route.continue_()
                return

            print("  🎯 拦截到 /chat/completion 请求")

            try:
                # 获取真实响应
                response = await route.fetch()
                status = response.status
                headers = response.headers
                body_bytes = await response.body()

                # 把响应原样返回给页面
                await route.fulfill(
                    status=status,
                    headers=headers,
                    body=body_bytes,
                )

                # 同时解析 SSE 内容推入队列
                if status == 200 and self._sse_queue:
                    body_text = body_bytes.decode("utf-8", errors="replace")
                    self._parse_sse_to_queue(body_text)
                elif self._sse_queue:
                    body_text = body_bytes.decode("utf-8", errors="replace")
                    print(f"  ⚠️ 响应状态码: {status}, body: {body_text[:200]}")
                    await self._sse_queue.put({
                        "type": "error",
                        "data": f"HTTP {status}: {body_text[:500]}"
                    })
                    await self._sse_queue.put({"type": "done"})

            except Exception as e:
                print(f"  ❌ 路由拦截异常: {e}")
                # 出错时放行原始请求，避免页面卡死
                try:
                    await route.continue_()
                except Exception:
                    pass
                if self._sse_queue:
                    await self._sse_queue.put({
                        "type": "error",
                        "data": str(e)
                    })
                    await self._sse_queue.put({"type": "done"})

        # 注册路由：匹配所有包含 /chat/completion 的 URL
        await self.page.route("**/chat/completion*", handle_completion_route)
        print("  🔌 路由拦截器已注册")

    def _parse_sse_to_queue(self, body: str):
        """解析 SSE 文本，把内容逐条推入队列"""
        if not self._sse_queue:
            return

        lines = body.split("\n")
        has_content = False

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
                        has_content = True
                        asyncio.ensure_future(
                            self._sse_queue.put({
                                "type": "content",
                                "data": content
                            })
                        )
            except json.JSONDecodeError:
                pass

        # 兜底：确保队列一定有结束信号
        asyncio.ensure_future(self._sse_queue.put({"type": "done"}))

        if not has_content:
            print(f"  ⚠️ SSE 解析完成但无内容，body 长度: {len(body)}")
            print(f"  ⚠️ body 预览: {body[:500]}")

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
    # 核心发送
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
                # 开启新对话
                await self._start_new_chat()
                await asyncio.sleep(1)

                # 准备队列，开启捕获
                self._sse_queue = asyncio.Queue()
                self._capture_active = True

                # 发送消息
                await self._type_and_send(message)

                # ── 从队列读取 SSE 数据 ──
                print(f"  [{req_id}] 等待 SSE 流...")
                full_text = ""
                max_wait = 600.0
                stream_started = False

                while True:
                    try:
                        # 等待队列中的消息，超时则检查状态
                        msg = await asyncio.wait_for(
                            self._sse_queue.get(),
                            timeout=120.0 if not stream_started else 60.0
                        )
                    except asyncio.TimeoutError:
                        if not stream_started:
                            print(f"  [{req_id}] ⚠️ 120秒未收到 SSE 数据")
                            # DOM 兜底
                            fallback = await self._dom_fallback()
                            if fallback:
                                print(f"  [{req_id}] 🔄 DOM 兜底成功，长度: {len(fallback)}")
                                yield fallback
                                full_text = fallback
                            else:
                                yield "[错误] 等待响应超时，请重试"
                        else:
                            print(f"  [{req_id}] ⚠️ 流中断（60秒无新数据）")
                        break

                    if msg["type"] == "content":
                        if not stream_started:
                            stream_started = True
                            print(f"  [{req_id}] 流开始")
                        full_text += msg["data"]
                        yield msg["data"]

                    elif msg["type"] == "done":
                        break

                    elif msg["type"] == "error":
                        err = msg["data"]
                        print(f"  [{req_id}] ❌ SSE 错误: {err}")
                        if not full_text:
                            # 尝试 DOM 兜底
                            fallback = await self._dom_fallback()
                            if fallback:
                                yield fallback
                                full_text = fallback
                            else:
                                yield f"[错误] {err}"
                        break

                # 关闭捕获
                self._capture_active = False
                self._sse_queue = None

                # 如果完全没内容，最终 DOM 兜底
                if not full_text:
                    await asyncio.sleep(3)
                    fallback = await self._dom_fallback()
                    if fallback:
                        print(f"  [{req_id}] 🔄 最终 DOM 兜底，长度: {len(fallback)}")
                        yield fallback
                        full_text = fallback

                print(f"  [{req_id}] ✅ 完成，长度: {len(full_text)}")

            except Exception as e:
                self._capture_active = False
                self._sse_queue = None
                print(f"  [{req_id}] ❌ {e}")
                import traceback
                traceback.print_exc()
                yield f"[错误] {str(e)}"

    async def _dom_fallback(self) -> str:
        """DOM 兜底"""
        try:
            text = await self.page.evaluate("""
                () => {
                    // 方法1: DeepSeek 虚拟列表
                    const items = document.querySelectorAll(
                        'div[data-virtual-list-item-key]'
                    );
                    if (items.length > 0) {
                        const last = items[items.length - 1];
                        const md = last.querySelectorAll('[class*="ds-markdown"]');
                        if (md.length > 0) {
                            return md[md.length - 1].textContent || '';
                        }
                    }
                    // 方法2: 通用
                    const allMd = document.querySelectorAll('[class*="ds-markdown"]');
                    if (allMd.length > 0) {
                        return allMd[allMd.length - 1].textContent || '';
                    }
                    return '';
                }
            """)
            return (text or "").strip()
        except Exception:
            return ""

    # ── 其他 ──

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
            "mode": "route-intercept",
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
