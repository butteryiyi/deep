# browser_manager.py
"""
DeepSeek 反代（双通道模式）
- 主通道：addInitScript 网络拦截，实时逐 chunk 捕获 SSE
- 备用通道：等复制按钮出现后一次性读 DOM（selenium 思路）
- 主通道优先（抗审查），备用通道兜底（抗拦截失败）
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


INIT_INTERCEPT_SCRIPT = """
(() => {
    if (window.__ds_interceptor_installed) return;
    window.__ds_interceptor_installed = true;

    window.__ds_chunks = [];
    window.__ds_stream_done = false;
    window.__ds_stream_error = null;
    window.__ds_capture_active = false;
    window.__ds_raw_events = [];

    window.__ds_reset = () => {
        window.__ds_chunks = [];
        window.__ds_stream_done = false;
        window.__ds_stream_error = null;
        window.__ds_raw_events = [];
    };

    function extractContent(dataStr) {
        try {
            const parsed = JSON.parse(dataStr);

            // DeepSeek 格式：fragments 里面有 content
            if (parsed.v && parsed.v.response) {
                const fragments = parsed.v.response.fragments || [];
                let text = '';
                for (const frag of fragments) {
                    if (frag.content !== undefined && frag.content !== null) {
                        text += frag.content;
                    }
                }
                return { type: 'deepseek', text: text };
            }

            // OpenAI 格式
            if (parsed.choices && parsed.choices.length > 0) {
                const delta = parsed.choices[0].delta || {};
                if (delta.content) return { type: 'openai', text: delta.content };
            }

            return null;
        } catch (e) {
            return null;
        }
    }

    async function processStream(reader) {
        const decoder = new TextDecoder();
        let buffer = '';
        let lastFullText = '';

        try {
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\\n');
                buffer = lines.pop();

                for (const line of lines) {
                    const trimmed = line.trim();
                    if (!trimmed.startsWith('data: ')) continue;
                    const dataStr = trimmed.substring(6).trim();

                    // 保存原始事件用于调试
                    if (window.__ds_raw_events.length < 50) {
                        window.__ds_raw_events.push(dataStr.substring(0, 200));
                    }

                    if (dataStr === '[DONE]') {
                        window.__ds_stream_done = true;
                        return;
                    }

                    const result = extractContent(dataStr);
                    if (!result) continue;

                    if (result.type === 'deepseek') {
                        // DeepSeek 每次推送完整累积文本，取增量
                        if (result.text.length > lastFullText.length) {
                            const delta = result.text.substring(lastFullText.length);
                            window.__ds_chunks.push(delta);
                            lastFullText = result.text;
                        }
                    } else if (result.type === 'openai') {
                        window.__ds_chunks.push(result.text);
                    }
                }
            }
        } catch (e) {
            window.__ds_stream_error = e.message;
        }
        window.__ds_stream_done = true;
    }

    // ═══ Patch fetch ═══
    const _origFetch = window.fetch;
    window.fetch = async function(...args) {
        const resp = await _origFetch.apply(this, args);
        const url = (typeof args[0] === 'string') ? args[0] : (args[0]?.url || '');

        if (window.__ds_capture_active && url.includes('/chat/completion')) {
            const ct = resp.headers.get('content-type') || '';
            if (ct.includes('text/event-stream') || ct.includes('application/')) {
                console.log('[DS] fetch 捕获 completion');
                const cloned = resp.clone();
                processStream(cloned.body.getReader());
            }
        }
        return resp;
    };

    // ═══ Patch XHR ═══
    const _origOpen = XMLHttpRequest.prototype.open;
    const _origSend = XMLHttpRequest.prototype.send;

    XMLHttpRequest.prototype.open = function(method, url, ...rest) {
        this.__ds_url = url;
        return _origOpen.call(this, method, url, ...rest);
    };

    XMLHttpRequest.prototype.send = function(body) {
        if (window.__ds_capture_active && this.__ds_url &&
            this.__ds_url.includes('/chat/completion')) {
            console.log('[DS] XHR 捕获 completion');
            let xhrBuf = '';
            let xhrLast = '';

            this.addEventListener('progress', function() {
                try {
                    const newData = (this.responseText || '').substring(xhrBuf.length);
                    xhrBuf = this.responseText || '';

                    for (const line of newData.split('\\n')) {
                        const t = line.trim();
                        if (!t.startsWith('data: ')) continue;
                        const ds = t.substring(6).trim();
                        if (ds === '[DONE]') { window.__ds_stream_done = true; return; }

                        const r = extractContent(ds);
                        if (!r) continue;
                        if (r.type === 'deepseek') {
                            if (r.text.length > xhrLast.length) {
                                window.__ds_chunks.push(r.text.substring(xhrLast.length));
                                xhrLast = r.text;
                            }
                        } else {
                            window.__ds_chunks.push(r.text);
                        }
                    }
                } catch(e) {}
            });
            this.addEventListener('loadend', () => { window.__ds_stream_done = true; });
            this.addEventListener('error', () => {
                window.__ds_stream_error = 'XHR error';
                window.__ds_stream_done = true;
            });
        }
        return _origSend.call(this, body);
    };

    // ═══ Patch EventSource ═══
    const _OrigES = window.EventSource;
    if (_OrigES) {
        window.EventSource = function(url, cfg) {
            const es = new _OrigES(url, cfg);
            if (window.__ds_capture_active && url.includes('/chat/completion')) {
                console.log('[DS] EventSource 捕获');
                let esLast = '';
                es.addEventListener('message', function(ev) {
                    if (ev.data === '[DONE]') { window.__ds_stream_done = true; return; }
                    const r = extractContent(ev.data);
                    if (!r) return;
                    if (r.type === 'deepseek') {
                        if (r.text.length > esLast.length) {
                            window.__ds_chunks.push(r.text.substring(esLast.length));
                            esLast = r.text;
                        }
                    } else {
                        window.__ds_chunks.push(r.text);
                    }
                });
                es.addEventListener('error', () => { window.__ds_stream_done = true; });
            }
            return es;
        };
        window.EventSource.prototype = _OrigES.prototype;
    }

    console.log('[DS] 全协议拦截器已安装');
})();
"""


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

    async def wait_until_ready(self, timeout: float = 180.0) -> bool:
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    @property
    def is_ready(self) -> bool:
        return self._ready

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

        auth = AuthHandler(self.page, context=self.context)
        self.logged_in = await auth.login(self.email, self.password)

        if not self.logged_in:
            print("⚠️ 登录可能未完成")
        else:
            print("🎉 登录成功！")

        self._ready = True
        self._ready_event.set()
        print(f"✅ 就绪（引擎: {self._engine}，双通道模式）")

    async def _start_with_camoufox(self):
        from camoufox.async_api import AsyncCamoufox
        self._camoufox = AsyncCamoufox(headless=self.headless, geoip=False)
        self.browser = await self._camoufox.__aenter__()
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        await self.context.add_init_script(INIT_INTERCEPT_SCRIPT)
        self.page = await self.context.new_page()
        print("  ✅ Camoufox 已启动")

    async def _start_with_playwright(self):
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
        await self.context.add_init_script(INIT_INTERCEPT_SCRIPT)
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

    async def _ensure_interceptor(self):
        try:
            ok = await self.page.evaluate("() => window.__ds_interceptor_installed === true")
            if not ok:
                print("  ⚠️ 拦截器丢失，重新注入")
                await self.page.evaluate(INIT_INTERCEPT_SCRIPT)
        except Exception:
            try:
                await self.page.evaluate(INIT_INTERCEPT_SCRIPT)
            except Exception as e:
                print(f"  ❌ 注入失败: {e}")

    async def _start_new_chat(self):
        if "chat.deepseek.com" not in self.page.url:
            await self.page.goto(
                "https://chat.deepseek.com/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(2)

        for sel in [
            "xpath=//*[contains(text(), '开启新对话')]",
            "xpath=//*[contains(text(), '新对话')]",
            "xpath=//*[contains(text(), 'New chat')]",
            "div.ds-icon-button",
            "[class*='new-chat']",
        ]:
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
                await self._start_new_chat()
                await asyncio.sleep(1)
                await self._ensure_interceptor()

                # 重置 + 开启捕获
                await self.page.evaluate("""
                    () => {
                        window.__ds_reset();
                        window.__ds_capture_active = true;
                    }
                """)

                await self._type_and_send(message)

                # ═══ 双通道并行读取 ═══
                print(f"  [{req_id}] 双通道等待...")

                full_text = ""
                read_index = 0
                max_wait = 600.0
                waited = 0.0
                idle_count = 0
                stream_started = False
                network_has_data = False

                while waited < max_wait:
                    # ── 通道1：网络拦截 ──
                    result = await self.page.evaluate("""
                        (fromIndex) => ({
                            chunks: window.__ds_chunks.slice(fromIndex),
                            total: window.__ds_chunks.length,
                            done: window.__ds_stream_done,
                            error: window.__ds_stream_error,
                            rawCount: window.__ds_raw_events.length,
                            rawSample: window.__ds_raw_events.slice(0, 3),
                        })
                    """, read_index)

                    if result.get("error"):
                        print(f"  [{req_id}] ❌ 网络层错误: {result['error']}")
                        break

                    new_chunks = result.get("chunks", [])
                    if new_chunks:
                        if not stream_started:
                            stream_started = True
                            network_has_data = True
                            print(f"  [{req_id}] 🌐 网络通道流开始")

                        for chunk in new_chunks:
                            if chunk == '[CONTENT_REPLACED]':
                                print(f"  [{req_id}] ⚠️ 内容被审查替换")
                                continue
                            full_text += chunk
                            yield chunk
                        read_index += len(new_chunks)
                        idle_count = 0

                    if result.get("done") and not new_chunks:
                        if network_has_data:
                            print(f"  [{req_id}] 🌐 网络通道完成")
                        break

                    # ── 通道2：DOM 备用（复制按钮检测）──
                    if not new_chunks:
                        idle_count += 1

                        # 每隔一段时间检查复制按钮
                        if idle_count % 10 == 0:
                            dom_status = await self.page.evaluate("""
                                () => {
                                    const items = document.querySelectorAll(
                                        'div[data-virtual-list-item-key]'
                                    );
                                    if (items.length === 0) return { ready: false };
                                    const last = items[items.length - 1];
                                    const btns = last.querySelectorAll('div[role="button"]');
                                    const md = last.querySelectorAll('[class*="ds-markdown"]');
                                    const text = md.length > 0
                                        ? md[md.length - 1].textContent || ''
                                        : '';
                                    return {
                                        ready: btns.length > 0,
                                        textLen: text.length,
                                        hasText: text.length > 0,
                                    };
                                }
                            """)

                            if dom_status.get("ready") and dom_status.get("hasText"):
                                if not network_has_data:
                                    # 网络通道完全没数据，用 DOM
                                    print(f"  [{req_id}] 📋 网络通道无数据，切换 DOM 通道")
                                    dom_text = await self._read_dom_response()
                                    if dom_text:
                                        full_text = dom_text
                                        yield dom_text
                                    break
                                else:
                                    # 网络通道有数据但已停止，检查 DOM 是否有更多
                                    if idle_count > 20:
                                        break

                    sleep_time = 0.05 if idle_count < 50 else 0.2
                    await asyncio.sleep(sleep_time)
                    waited += sleep_time

                    # 长时间无数据
                    if not stream_started and waited > 30:
                        # 30秒了，打印调试信息
                        if idle_count % 50 == 0:
                            raw_count = result.get("rawCount", 0)
                            raw_sample = result.get("rawSample", [])
                            print(f"  [{req_id}] ⏳ 等待中 waited={waited:.0f}s "
                                  f"rawEvents={raw_count}")
                            if raw_sample:
                                for s in raw_sample:
                                    print(f"    raw: {s[:150]}")

                    if not stream_started and waited > 90:
                        print(f"  [{req_id}] ⚠️ 90s 无网络数据，尝试 DOM")
                        dom_text = await self._read_dom_response()
                        if dom_text:
                            full_text = dom_text
                            yield dom_text
                        else:
                            yield "[错误] 响应超时"
                        break

                # 关闭捕获
                try:
                    await self.page.evaluate(
                        "() => { window.__ds_capture_active = false; }"
                    )
                except Exception:
                    pass

                # 最终兜底
                if not full_text:
                    await asyncio.sleep(2)
                    dom_text = await self._read_dom_response()
                    if dom_text:
                        print(f"  [{req_id}] 📋 最终 DOM 兜底，长度: {len(dom_text)}")
                        yield dom_text
                        full_text = dom_text

                print(f"  [{req_id}] ✅ 完成，长度: {len(full_text)}")

            except Exception as e:
                try:
                    await self.page.evaluate(
                        "() => { window.__ds_capture_active = false; }"
                    )
                except Exception:
                    pass
                print(f"  [{req_id}] ❌ {e}")
                import traceback
                traceback.print_exc()
                yield f"[错误] {str(e)}"

    async def _read_dom_response(self) -> str:
        """Selenium 思路：从最后一个对话项的 ds-markdown 读取文本"""
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
            "mode": "dual-channel",
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
                random.randint(100, 1800), random.randint(100, 900)
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
