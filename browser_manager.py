# browser_manager.py
# 策略：生成中只监控+保存快照，完成后点复制按钮拿原生 Markdown
# DOM 纯文本仅用于：实时流式输出保活、审查检测、长度监控、兜底
# v5: 增加服务器错误检测 + 自动重试 + 页面恢复 + 真正的实时流输出避免客户端空回超时

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

CENSORSHIP_PHRASES = [
    "这个问题我暂时无法回答",
    "让我们换个话题再聊聊吧",
    "我无法回答这个问题",
    "抱歉，我无法",
    "这个话题不太适合讨论",
    "我没法对此进行回答",
    "作为AI助手，我无法",
    "很抱歉，这个问题",
]

# ═══════════════════════════════════════════════════════════════
# 服务器错误/异常状态检测短语
# ═══════════════════════════════════════════════════════════════
SERVER_ERROR_PHRASES = [
    "服务器繁忙，请稍后重试",
    "服务器繁忙",
    "Server is busy",
    "server is busy",
    "Network Error",
    "网络错误",
    "请求过于频繁",
    "Too Many Requests",
    "rate limit",
    "Rate limit",
    "请稍后重试",
    "出错了",
    "Something went wrong",
    "something went wrong",
    "An error occurred",
    "服务暂时不可用",
    "Service Unavailable",
]


def _is_censored(text: str) -> bool:
    text = text.strip()
    if not text:
        return False
    for phrase in CENSORSHIP_PHRASES:
        if phrase in text:
            if len(text) < 150:
                return True
    return False


INSTALL_CLIPBOARD_HOOK_JS = """
() => {
    if (window.__clipHooked) return 'already';
    window.__clipData = { text: '', time: 0 };
    try {
        const orig = navigator.clipboard.writeText.bind(navigator.clipboard);
        navigator.clipboard.writeText = async function(text) {
            if (text && text.length > 5) {
                window.__clipData = { text: text, time: Date.now() };
            }
            return orig(text);
        };
        window.__clipHooked = true;
        return 'ok';
    } catch(e) {
        return 'fail:' + e.message;
    }
}
"""

READ_STATE_JS = """
() => {
    const R = {
        domText: '',
        domLen: 0,
        thinkLen: 0,
        hasButton: false,
        buttonCount: 0,
        isComplete: false,
        isGenerating: false,
        itemCount: 0,
        errorText: '',
        hasError: false,
        pageText: '',
    };

    const errorSelectors = [
        '.ds-toast', '.ds-notification', '[class*="error"]',
        '[class*="toast"]', '[class*="notice"]', '[class*="alert"]',
        '[class*="warning"]', '[class*="retry"]', '.ant-message',
        '.ant-notification',
    ];

    for (const sel of errorSelectors) {
        try {
            const els = document.querySelectorAll(sel);
            for (const el of els) {
                const text = (el.innerText || '').trim();
                if (text && text.length > 2 && text.length < 200) {
                    R.errorText += text + ' | ';
                }
            }
        } catch(e) {}
    }

    const floatingLayers = document.querySelectorAll(
        '[class*="toast"], [class*="modal"], [class*="dialog"], ' +
        '[class*="popup"], [class*="overlay"], [class*="notification"], ' +
        '[role="alert"], [role="dialog"]'
    );
    for (const layer of floatingLayers) {
        const t = (layer.innerText || '').trim();
        if (t && t.length < 300) {
            R.errorText += t + ' | ';
        }
    }

    const items = document.querySelectorAll('div[data-virtual-list-item-key]');
    R.itemCount = items.length;
    if (items.length === 0) return R;

    const lastItem = items[items.length - 1];
    const itemClass = lastItem.className || '';

    R.isComplete = itemClass.includes('_43c05b5');

    const msgDiv = lastItem.querySelector('div.ds-message');
    if (!msgDiv) return R;

    const lastItemText = (lastItem.innerText || '').trim();
    const retryBtn = lastItem.querySelector('[class*="retry"], [class*="regenerate"]');
    if (retryBtn) {
        R.errorText += '(has-retry-btn) ';
    }

    const allTextInLastItem = lastItemText;
    const errorKeywords = ['服务器繁忙', '请稍后重试', 'Server is busy', 'Network Error',
                           '网络错误', '出错了', 'Something went wrong', '请求过于频繁'];
    for (const kw of errorKeywords) {
        if (allTextInLastItem.includes(kw)) {
            R.hasError = true;
            R.errorText += kw + ' ';
        }
    }

    const directChildren = msgDiv.children;
    for (let i = directChildren.length - 1; i >= 0; i--) {
        const child = directChildren[i];
        if (child.tagName === 'DIV' &&
            child.classList.contains('ds-markdown') &&
            !child.classList.contains('ds-think-content')) {
            R.domText = child.innerText || '';
            R.domLen = R.domText.length;
            break;
        }
    }

    const thinkDiv = msgDiv.querySelector('div.ds-think-content');
    if (thinkDiv) {
        const thinkMd = thinkDiv.querySelector('div.ds-markdown');
        if (thinkMd) {
            R.thinkLen = (thinkMd.textContent || '').length;
        }
    }

    const btnContainer = lastItem.querySelector('div._965abe9');
    if (btnContainer) {
        const btns = btnContainer.querySelectorAll('div.ds-icon-button');
        R.buttonCount = btns.length;
        R.hasButton = btns.length > 0;
    } else {
        const btns = lastItem.querySelectorAll('div.ds-icon-button');
        R.buttonCount = btns.length;
        R.hasButton = btns.length >= 3;
    }

    R.isGenerating = !R.isComplete && R.itemCount >= 2;
    if (!R.isGenerating) {
        const thinkAnim = lastItem.querySelector('span.e4b3a110');
        if (thinkAnim) {
            const style = thinkAnim.getAttribute('style') || '';
            if (style.includes('running')) {
                R.isGenerating = true;
            }
        }
    }

    return R;
}
"""

CLICK_COPY_JS = """
() => {
    const items = document.querySelectorAll('div[data-virtual-list-item-key]');
    if (items.length === 0) return 'no-items';
    const lastItem = items[items.length - 1];

    if (window.__clipData) {
        window.__clipData = { text: '', time: 0 };
    }

    const btnContainer = lastItem.querySelector('div._965abe9');
    if (btnContainer) {
        const firstBtn = btnContainer.querySelector('div.ds-icon-button');
        if (firstBtn) {
            firstBtn.click();
            return 'clicked';
        }
    }

    const allBtns = lastItem.querySelectorAll('div.ds-icon-button');
    for (const btn of allBtns) {
        const path = btn.querySelector('svg path');
        if (path) {
            const d = path.getAttribute('d') || '';
            if (d.startsWith('M6.14923')) {
                btn.click();
                return 'clicked-svg';
            }
        }
    }

    return 'not-found';
}
"""

SCROLL_BOTTOM_JS = """
() => {
    const sa = document.querySelector('.ds-scroll-area');
    if (sa) { sa.scrollTop = sa.scrollHeight; return true; }
    return false;
}
"""

CLICK_REGENERATE_JS = """
() => {
    const allBtns = document.querySelectorAll('button, div[role="button"], [class*="btn"], [class*="button"]');
    for (const btn of allBtns) {
        const text = (btn.innerText || '').trim();
        if (text.includes('重新生成') || text.includes('重试') || text.includes('Retry') || text.includes('Regenerate')) {
            btn.click();
            return 'clicked:' + text;
        }
    }

    const items = document.querySelectorAll('div[data-virtual-list-item-key]');
    if (items.length === 0) return 'no-items';
    const lastItem = items[items.length - 1];

    const clickables = lastItem.querySelectorAll('[class*="retry"], [class*="regenerate"], [class*="refresh"]');
    for (const el of clickables) {
        el.click();
        return 'clicked-class';
    }

    return 'not-found';
}
"""


class ChatPage:
    def __init__(self, page, page_id: int):
        self.page = page
        self.page_id = page_id
        self.busy = False
        self.request_count = 0
        self.last_used = 0.0
        self._hook_installed = False

    async def ensure_clipboard_hook(self):
        try:
            hooked = await self.page.evaluate("() => !!window.__clipHooked")
            if hooked:
                return
        except Exception:
            pass
        try:
            result = await self.page.evaluate(INSTALL_CLIPBOARD_HOOK_JS)
            self._hook_installed = (result in ('ok', 'already'))
        except Exception as e:
            print(f"  ⚠️ P#{self.page_id} hook 失败: {e}")

    async def reset_clip(self):
        try:
            await self.page.evaluate(
                "() => { if(window.__clipData) window.__clipData = {text:'',time:0}; }"
            )
        except Exception:
            pass

    async def read_state(self) -> dict:
        try:
            return await self.page.evaluate(READ_STATE_JS)
        except Exception as e:
            return {
                "domText": "", "domLen": 0, "thinkLen": 0,
                "hasButton": False, "buttonCount": 0,
                "isComplete": False, "isGenerating": False,
                "itemCount": 0, "errorText": "", "hasError": False,
                "error": str(e),
            }

    async def click_copy_and_wait(self, timeout: float = 3.0) -> str:
        try:
            result = await self.page.evaluate(CLICK_COPY_JS)
            if result in ('not-found', 'no-items'):
                return ""

            deadline = time.time() + timeout
            while time.time() < deadline:
                await asyncio.sleep(0.15)
                clip = await self.page.evaluate(
                    "() => (window.__clipData && window.__clipData.text) || ''"
                )
                if clip:
                    return clip
            return ""
        except Exception as e:
            print(f"  ⚠️ 复制失败: {e}")
            return ""

    async def scroll_to_bottom(self):
        try:
            await self.page.evaluate(SCROLL_BOTTOM_JS)
        except Exception:
            pass

    async def click_regenerate(self) -> str:
        try:
            result = await self.page.evaluate(CLICK_REGENERATE_JS)
            return result
        except Exception as e:
            print(f"  ⚠️ 点击重新生成失败: {e}")
            return f"error:{e}"

    async def check_server_error(self) -> tuple[bool, str]:
        try:
            state = await self.read_state()
            error_text = state.get("errorText", "")
            has_error = state.get("hasError", False)

            if has_error:
                return True, error_text.strip()

            html_check = await self.page.evaluate("""
                () => {
                    const items = document.querySelectorAll('div[data-virtual-list-item-key]');
                    if (items.length === 0) return { found: false, text: '' };
                    const lastItem = items[items.length - 1];
                    const fullText = lastItem.innerText || '';

                    const keywords = ['服务器繁忙', '请稍后重试', 'Server is busy',
                                      'Network Error', '网络错误', '出错了',
                                      'Something went wrong', '请求过于频繁',
                                      'Too Many Requests'];
                    for (const kw of keywords) {
                        if (fullText.includes(kw)) {
                            return { found: true, text: kw };
                        }
                    }

                    const body = document.body.innerText || '';
                    for (const kw of keywords) {
                        if (body.includes(kw) && !fullText.includes(kw)) {
                            return { found: true, text: 'toast:' + kw };
                        }
                    }

                    return { found: false, text: '' };
                }
            """)

            if html_check and html_check.get("found"):
                return True, html_check.get("text", "unknown error")

            return False, ""
        except Exception as e:
            return False, f"check_error_failed:{e}"

    async def start_new_chat(self):
        self._hook_installed = False
        if "chat.deepseek.com" not in (self.page.url or ""):
            await self.page.goto(
                "https://chat.deepseek.com/",
                wait_until="domcontentloaded", timeout=30000,
            )
            await asyncio.sleep(2)

        for sel in [
            "xpath=//*[contains(text(), '开启新对话')]",
            "xpath=//*[contains(text(), '新对话')]",
            "xpath=//*[contains(text(), 'New chat')]",
            "[class*='new-chat']",
        ]:
            try:
                btn = self.page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await asyncio.sleep(1)
                    return
            except Exception:
                continue

        await self.page.goto(
            "https://chat.deepseek.com/",
            wait_until="domcontentloaded", timeout=30000,
        )
        await asyncio.sleep(3)

    async def type_and_send(self, message: str):
        textarea = self.page.locator("textarea").first
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
                    const el = document.querySelector('textarea');
                    if (!el) return;
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    ).set;
                    setter.call(el, text);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                }
            """, message)
        await asyncio.sleep(0.5)
        await textarea.press("Enter")
        await asyncio.sleep(0.5)

    async def is_alive(self) -> bool:
        try:
            if self.page.is_closed():
                return False
            await self.page.evaluate("() => 1")
            return True
        except Exception:
            return False


class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.logged_in = False
        self.start_time = time.time()
        self.heartbeat_count = 0
        self.requests_handled = 0
        self.total_requests = 0

        self.email = os.getenv("DEEPSEEK_EMAIL", "")
        self.password = os.getenv("DEEPSEEK_PASSWORD", "")
        self.headless = os.getenv("HEADLESS", "true").lower() == "true"
        self._engine = "unknown"

        self._page_count = int(os.getenv("PAGE_COUNT", "3"))
        self._pages: list[ChatPage] = []
        self._page_semaphore: asyncio.Semaphore = None

        self._ready = False
        self._ready_event = asyncio.Event()
        self._camoufox_ctx = None

        self._consecutive_errors = 0
        self._last_error_time = 0.0

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
            print(f"⚠️ Camoufox 失败: {e}，回退 Playwright")
            if self._camoufox_ctx:
                try:
                    await self._camoufox_ctx.__aexit__(None, None, None)
                except Exception:
                    pass
                self._camoufox_ctx = None

        if not camoufox_ok:
            await self._start_with_playwright()
            self._engine = "playwright-firefox"

        await self._inject_stealth()

        first_page = await self.context.new_page()
        auth = AuthHandler(first_page, context=self.context)
        self.logged_in = await auth.login(self.email, self.password)
        if not self.logged_in:
            print("⚠️ 登录可能未完成")
            await first_page.close()
        else:
            print("🎉 登录成功！")
            cp = ChatPage(first_page, 0)
            await cp.ensure_clipboard_hook()
            self._pages.append(cp)
            print(f"  📄 页面#0 就绪")

        for i in range(1, self._page_count):
            try:
                page = await self.context.new_page()
                await page.goto(
                    "https://chat.deepseek.com/",
                    wait_until="domcontentloaded", timeout=30000,
                )
                await asyncio.sleep(2)
                cp = ChatPage(page, i)
                await cp.ensure_clipboard_hook()
                self._pages.append(cp)
                print(f"  📄 页面#{i} 就绪")
            except Exception as e:
                print(f"  ⚠️ 页面#{i} 失败: {e}")

        self._page_semaphore = asyncio.Semaphore(len(self._pages))
        self._ready = True
        self._ready_event.set()
        print(f"✅ 就绪（{self._engine}，{len(self._pages)} 并发页面）")

    async def _start_with_camoufox(self):
        from camoufox.async_api import AsyncCamoufox
        self._camoufox_ctx = AsyncCamoufox(headless=self.headless, geoip=False)
        self.browser = await self._camoufox_ctx.__aenter__()
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN", timezone_id="Asia/Shanghai",
        )
        print("  ✅ Camoufox 启动")

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
            locale="zh-CN", timezone_id="Asia/Shanghai",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
                "Gecko/20100101 Firefox/126.0"
            ),
        )
        print("  ✅ Playwright Firefox 启动")

    async def _inject_stealth(self):
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

    async def _acquire_page(self) -> ChatPage:
        await self._page_semaphore.acquire()
        for cp in self._pages:
            if not cp.busy:
                cp.busy = True
                cp.last_used = time.time()
                return cp
        for _ in range(100):
            await asyncio.sleep(0.1)
            for cp in self._pages:
                if not cp.busy:
                    cp.busy = True
                    cp.last_used = time.time()
                    return cp
        raise RuntimeError("无法获取空闲页面")

    def _release_page(self, cp: ChatPage):
        cp.busy = False
        self._page_semaphore.release()

    async def _recover_page(self, cp: ChatPage):
        print(f"  🔄 正在恢复页面#{cp.page_id}...")
        try:
            if not await cp.is_alive():
                new_page = await self.context.new_page()
                await new_page.goto(
                    "https://chat.deepseek.com/",
                    wait_until="domcontentloaded", timeout=30000,
                )
                await asyncio.sleep(2)
                cp.page = new_page
                cp._hook_installed = False
                await cp.ensure_clipboard_hook()
                print(f"  ✅ 页面#{cp.page_id} 已通过新建恢复")
            else:
                await cp.page.goto(
                    "https://chat.deepseek.com/",
                    wait_until="domcontentloaded", timeout=30000,
                )
                await asyncio.sleep(2)
                cp._hook_installed = False
                await cp.ensure_clipboard_hook()
                print(f"  ✅ 页面#{cp.page_id} 已通过刷新恢复")
        except Exception as e:
            print(f"  ❌ 页面#{cp.page_id} 恢复失败: {e}")

    async def send_message(self, message: str) -> str:
        """非流式：如果有最终补全的 Markdown 就返回，否则用分块拼接"""
        final_text = ""
        chunks_text = ""
        async for chunk_type, content in self._send_message_internal(message):
            if chunk_type == "chunk":
                chunks_text += content
            elif chunk_type in ("final", "error"):
                final_text = content
        return final_text if final_text else chunks_text

    async def send_message_stream(self, message: str) -> AsyncGenerator[str, None]:
        """流式：实时输出 chunk 避免客户端断开超时"""
        async for chunk_type, content in self._send_message_internal(message):
            if chunk_type == "chunk":
                if content:
                    yield content
            elif chunk_type == "error":
                if content:
                    yield f"\n\n{content}"
            elif chunk_type == "final":
                # 在流式下为了避免重复，不再输出最后的 final
                pass

    async def _send_message_internal(self, message: str) -> AsyncGenerator[tuple[str, str], None]:
        if not self._ready:
            ok = await self.wait_until_ready(timeout=180)
            if not ok:
                yield "error", "[错误] 浏览器初始化超时"
                return

        self.total_requests += 1
        self.requests_handled += 1
        req_id = self.total_requests
        print(f"📨 #{req_id} ({len(message)} 字符)")

        cp = None
        try:
            cp = await asyncio.wait_for(self._acquire_page(), timeout=300)
        except (asyncio.TimeoutError, RuntimeError) as e:
            yield "error", f"[错误] {e}"
            return

        print(f"  [#{req_id}] → 页面#{cp.page_id}")

        max_retries = 2
        retry_count = 0

        try:
            while retry_count <= max_retries:
                error_type = None

                try:
                    async for c_type, c_data, e_type in self._do_send_and_wait_gen(cp, message, req_id, retry_count):
                        if e_type:
                            error_type = e_type
                            if c_data and c_type == "error":
                                yield "error", c_data
                            break
                        
                        if c_type in ("chunk", "final"):
                            yield c_type, c_data
                except Exception as e:
                    print(f"  [#{req_id}] ❌ 执行异常: {e}")
                    import traceback
                    traceback.print_exc()
                    yield "error", f"[错误] {str(e)}"
                    error_type = "exception"
                    break

                if error_type == "server_error" and retry_count < max_retries:
                    retry_count += 1
                    wait_time = 5 * retry_count
                    print(f"  [#{req_id}] 🔄 服务器错误，{wait_time}s 后重试 ({retry_count}/{max_retries})...")
                    await asyncio.sleep(wait_time)
                    await self._recover_page(cp)
                    continue
                elif error_type == "server_error":
                    self._consecutive_errors += 1
                    self._last_error_time = time.time()
                    yield "error", "[错误] 服务器繁忙，多次重试后仍然失败，请稍后重试。"
                    break
                else:
                    if error_type is None:
                        self._consecutive_errors = 0
                    break

        finally:
            if cp:
                self._release_page(cp)

    async def _do_send_and_wait_gen(
        self, cp: ChatPage, message: str, req_id: int, retry_num: int
    ) -> AsyncGenerator[tuple[str, str, Optional[str]], None]:
        """
        返回格式: yield (msg_type, content, error_type)
        msg_type: "chunk" (实时增量文本), "final" (最终合并的文本), "error" (错误信息)
        """
        cp.request_count += 1

        if not await cp.is_alive():
            print(f"  [#{req_id}] 页面死亡，恢复中...")
            await self._recover_page(cp)
            if not await cp.is_alive():
                yield ("error", "[错误] 页面恢复失败", "exception")
                return

        await cp.start_new_chat()
        await asyncio.sleep(0.5)
        await cp.ensure_clipboard_hook()
        await cp.reset_clip()

        await cp.type_and_send(message)
        retry_tag = f" (重试#{retry_num})" if retry_num > 0 else ""
        print(f"  [#{req_id}] 已发送{retry_tag}")

        max_wait = 600
        poll_interval = 0.4
        best_dom_text = ""
        last_yielded_len = 0
        gen_started = False
        no_change_count = 0
        prev_len = 0
        start_ts = time.time()
        scroll_counter = 0

        dom_zero_count = 0
        dom_was_positive = False
        dom_zero_start_time = 0.0

        for _ in range(60):
            await asyncio.sleep(0.5)
            st = await cp.read_state()
            has_err, err_msg = await cp.check_server_error()
            if has_err:
                print(f"  [#{req_id}] ❌ 等待期间检测到服务器错误: {err_msg}")
                yield ("error", f"[服务器错误] {err_msg}", "server_error")
                return
            if st.get("itemCount", 0) >= 2:
                break
            if time.time() - start_ts > 30:
                break

        while True:
            elapsed = time.time() - start_ts
            if elapsed > max_wait:
                print(f"  [#{req_id}] ⏰ 超时 {max_wait}s")
                yield ("error", "[错误] 生成超时", "timeout")
                return

            await asyncio.sleep(poll_interval)
            scroll_counter += 1

            if scroll_counter % 12 == 0:
                await cp.scroll_to_bottom()

            state = await cp.read_state()
            dom_text = state.get("domText", "")
            dom_len = state.get("domLen", 0)
            think_len = state.get("thinkLen", 0)
            is_complete = state.get("isComplete", False)
            has_button = state.get("hasButton", False)
            is_gen = state.get("isGenerating", False)
            btn_count = state.get("buttonCount", 0)
            has_error = state.get("hasError", False)
            error_text = state.get("errorText", "")

            # ═════════════════════════════════════════════════
            # 💡 核心修复：实时增量流输出。只要有新字符就直接 yield
            # ═════════════════════════════════════════════════
            if dom_len > last_yielded_len:
                chunk = dom_text[last_yielded_len:dom_len]
                last_yielded_len = dom_len
                yield ("chunk", chunk, None)

            if has_error:
                print(f"  [#{req_id}] ❌ 检测到服务器错误: {error_text}")
                msg = best_dom_text + "\n\n[注意：响应可能不完整，服务器中途报错]" if best_dom_text else "[错误] 服务器报错"
                yield ("error", msg, "server_error")
                return

            if scroll_counter % 15 == 0:
                deep_has_err, deep_err_msg = await cp.check_server_error()
                if deep_has_err:
                    print(f"  [#{req_id}] ❌ 深度检查发现服务器错误: {deep_err_msg}")
                    msg = best_dom_text + "\n\n[注意：响应可能不完整，服务器中途报错]" if best_dom_text else "[错误] 服务器深度报错"
                    yield ("error", msg, "server_error")
                    return

            if not gen_started and (dom_len > 0 or think_len > 0 or is_gen):
                gen_started = True
                no_change_count = 0
                print(f"  [#{req_id}] 🚀 开始 (think={think_len} reply={dom_len})")

            if dom_len > len(best_dom_text):
                best_dom_text = dom_text

            if gen_started and dom_len > 0:
                dom_was_positive = True
                dom_zero_count = 0
                dom_zero_start_time = 0

            if gen_started and dom_was_positive and dom_len == 0 and think_len == 0:
                if dom_zero_count == 0:
                    dom_zero_start_time = time.time()
                dom_zero_count += 1
                if dom_zero_count >= int(10 / poll_interval):
                    zero_has_err, zero_err_msg = await cp.check_server_error()
                    if zero_has_err:
                        msg = best_dom_text + "\n\n[注意：响应可能不完整，服务器中途报错]" if best_dom_text else "[错误] DOM归零"
                        yield ("error", msg, "server_error")
                        return
                    elif dom_zero_count >= int(20 / poll_interval):
                        msg = best_dom_text + "\n\n[注意：响应可能不完整，生成过程异常中断]" if best_dom_text else "[错误] DOM持续归零"
                        yield ("error", msg, "server_error")
                        return

            if (gen_started and len(best_dom_text) > 80
                and dom_text and dom_len < len(best_dom_text) * 0.4
                and _is_censored(dom_text)):
                print(f"  [#{req_id}] 🛡️ 生成中审查! ")
                yield ("final", best_dom_text, None)
                return

            if gen_started and is_complete and has_button and btn_count >= 3:
                await asyncio.sleep(0.3)
                confirm = await cp.read_state()
                if not (confirm.get("isComplete") and confirm.get("hasButton")):
                    continue

                if confirm.get("hasError"):
                    err = confirm.get("errorText", "")
                    msg = best_dom_text + "\n\n[注意：响应可能不完整]" if best_dom_text else err
                    yield ("error", msg, "server_error")
                    return

                await cp.scroll_to_bottom()
                await asyncio.sleep(0.2)
                final_state = await cp.read_state()
                final_dom = final_state.get("domText", "")
                final_dom_len = final_state.get("domLen", 0)

                if final_dom_len > len(best_dom_text):
                    best_dom_text = final_dom

                # 最后一次性把遗漏的 domText 发送为增量
                if final_dom_len > last_yielded_len:
                    chunk = best_dom_text[last_yielded_len:final_dom_len]
                    last_yielded_len = final_dom_len
                    yield ("chunk", chunk, None)

                if (_is_censored(final_dom) and len(best_dom_text) > final_dom_len * 2):
                    print(f"  [#{req_id}] 🛡️ 完成时已审查! ")
                    yield ("final", best_dom_text, None)
                    return

                clip_text = await cp.click_copy_and_wait(timeout=3.0)

                if clip_text and not _is_censored(clip_text):
                    print(f"  [#{req_id}] ✅ 完成: clip={len(clip_text)} dom={final_dom_len}")
                    yield ("final", clip_text, None)
                elif clip_text and _is_censored(clip_text):
                    print(f"  [#{req_id}] 🛡️ 剪贴板被审查! ")
                    yield ("final", best_dom_text, None)
                else:
                    final_text = final_dom if (final_dom and not _is_censored(final_dom)) else best_dom_text
                    yield ("final", final_text, None)
                return

            if dom_len == prev_len:
                no_change_count += 1
            else:
                no_change_count = 0
                prev_len = dom_len

            no_change_timeout = 45
            if no_change_count > int(no_change_timeout / poll_interval):
                nc_has_err, nc_err_msg = await cp.check_server_error()
                if nc_has_err:
                    msg = best_dom_text + "\n\n[注意：响应可能不完整，服务器中途报错]" if best_dom_text else "[错误] 无进展报错"
                    yield ("error", msg, "server_error")
                    return

                if gen_started and best_dom_text:
                    print(f"  [#{req_id}] ⏰ {no_change_timeout}s 无进展: {len(best_dom_text)} 字")
                    yield ("final", best_dom_text, None)
                    return
                elif not gen_started and elapsed > 60:
                    nr_has_err, nr_err_msg = await cp.check_server_error()
                    if nr_has_err:
                        yield ("error", f"[错误] 服务器错误: {nr_err_msg}", "server_error")
                    else:
                        yield ("error", "[错误] 60s无响应", "no_response")
                    return

            if scroll_counter % 37 == 0:
                err_info = f" err={error_text[:30]}" if error_text else ""
                print(f"  [#{req_id}] ⏳ {elapsed:.0f}s dom={dom_len} think={think_len} snap={len(best_dom_text)} comp={is_complete} btn={btn_count}{err_info}")

        clip = await cp.click_copy_and_wait(timeout=5.0)
        if clip and not _is_censored(clip):
            yield ("final", clip, None)
        elif best_dom_text:
            yield ("final", best_dom_text, None)
        else:
            await cp.scroll_to_bottom()
            await asyncio.sleep(1)
            st = await cp.read_state()
            dt = st.get("domText", "")
            if dt and not _is_censored(dt):
                yield ("final", dt, None)
            else:
                yield ("error", "抱歉，未能获取到响应。请稍后重试。", "no_response")
    # ═══════════════════════════════════════════════════════════════
    # 登录状态检测 & 自动重登录
    # ═══════════════════════════════════════════════════════════════

    async def check_login_status(self) -> bool:
        """检查当前是否仍处于登录状态"""
        for cp in self._pages:
            if cp.busy:
                continue
            try:
                if cp.page.is_closed():
                    continue

                result = await cp.page.evaluate("""
                    () => {
                        const url = window.location.href;

                        if (url.includes('sign_in') || url.includes('/login')) {
                            return { logged_in: false, reason: 'redirected_to_login', url: url };
                        }

                        if (!url.includes('deepseek.com')) {
                            return { logged_in: false, reason: 'not_on_deepseek', url: url };
                        }

                        const textarea = document.querySelector('textarea');
                        const sidebar = document.querySelector('[class*="sidebar"]');
                        const chatArea = document.querySelector('[class*="chat"]');

                        const loginBtns = document.querySelectorAll(
                            'a[href*="login"], a[href*="sign_in"], ' +
                            'button[class*="login"], button[class*="sign"]'
                        );
                        const bodyText = document.body ? (document.body.innerText || '') : '';
                        const hasLoginPrompt = bodyText.includes('登录') && bodyText.includes('注册');
                        const hasLogoutUI = loginBtns.length > 0 && !textarea;

                        if (hasLogoutUI || (hasLoginPrompt && !textarea)) {
                            return { logged_in: false, reason: 'login_ui_detected', url: url };
                        }

                        if (textarea || sidebar || chatArea) {
                            return { logged_in: true, reason: 'chat_elements_found', url: url };
                        }

                        return { logged_in: false, reason: 'no_chat_elements', url: url };
                    }
                """)

                logged_in = result.get("logged_in", False)
                reason = result.get("reason", "unknown")
                url = result.get("url", "")

                if not logged_in:
                    print(f"  🔍 登录检查：未登录 (原因: {reason}, URL: {url[:80]})")
                return logged_in

            except Exception as e:
                print(f"  ⚠️ 登录状态检查异常 (页面#{cp.page_id}): {e}")
                continue

        print("  ⚠️ 无法检查登录状态（所有页面忙），假定已登录")
        return True

    async def re_login(self) -> bool:
        """重新注入 Cookie 并刷新所有页面以恢复登录状态"""
        print("\n🔄 ========== 开始重新登录流程 ==========")

        target_cp = None
        is_temp = False

        for cp in self._pages:
            if not cp.busy:
                try:
                    if not cp.page.is_closed():
                        target_cp = cp
                        break
                except Exception:
                    continue

        if not target_cp:
            print("  ⚠️ 所有页面都忙，尝试新建临时页面...")
            try:
                temp_page = await self.context.new_page()
                target_cp = ChatPage(temp_page, 99)
                is_temp = True
            except Exception as e:
                print(f"  ❌ 无法创建临时页面: {e}")
                return False

        try:
            auth = AuthHandler(target_cp.page, context=self.context)
            success = await auth.login(self.email, self.password)

            if success:
                self.logged_in = True
                print("  ✅ Cookie 重新注入成功！")

                await asyncio.sleep(1)
                for cp in self._pages:
                    if cp.page_id == target_cp.page_id or cp.busy:
                        continue
                    try:
                        if cp.page.is_closed():
                            continue
                        await cp.page.reload(
                            wait_until="domcontentloaded", timeout=30000
                        )
                        await asyncio.sleep(2)
                        cp._hook_installed = False
                        await cp.ensure_clipboard_hook()
                        print(f"  ✅ 页面#{cp.page_id} 已刷新")
                    except Exception as e:
                        print(f"  ⚠️ 刷新页面#{cp.page_id} 失败: {e}")

                if not is_temp:
                    target_cp._hook_installed = False
                    await target_cp.ensure_clipboard_hook()

                print("🔄 ========== 重新登录完成 ==========\n")
                return True
            else:
                self.logged_in = False
                print("  ❌ 重新登录失败，Cookie 可能已完全过期。")
                print("🔄 ========== 重新登录失败 ==========\n")
                return False

        except Exception as e:
            print(f"  ❌ 重新登录异常: {e}")
            import traceback
            traceback.print_exc()
            return False

        finally:
            if is_temp and target_cp:
                try:
                    await target_cp.page.close()
                except Exception:
                    pass

    async def refresh_idle_pages(self) -> int:
        """刷新所有空闲页面以保持前端 SPA 状态"""
        refreshed = 0
        for cp in self._pages:
            if cp.busy:
                continue
            try:
                if cp.page.is_closed():
                    continue

                current_url = cp.page.url or ""
                if "deepseek.com" not in current_url:
                    await cp.page.goto(
                        "https://chat.deepseek.com/",
                        wait_until="domcontentloaded", timeout=30000,
                    )
                else:
                    await cp.page.reload(
                        wait_until="domcontentloaded", timeout=30000
                    )
                await asyncio.sleep(2)
                cp._hook_installed = False
                await cp.ensure_clipboard_hook()
                refreshed += 1
            except Exception as e:
                print(f"  ⚠️ 刷新页面#{cp.page_id} 失败: {e}")
        return refreshed


    async def is_alive(self) -> bool:
        if not self._ready or not self._pages:
            return False
        for cp in self._pages:
            if await cp.is_alive():
                return True
        return False

    async def get_status(self) -> dict:
        alive_count = 0
        for cp in self._pages:
            try:
                if not cp.page.is_closed():
                    alive_count += 1
            except Exception:
                pass
        busy_count = sum(1 for cp in self._pages if cp.busy)
        return {
            "browser_alive": alive_count > 0,
            "logged_in": self.logged_in,
            "ready": self._ready,
            "engine": self._engine,
            "mode": "clipboard-first-v5-error-detect-stream-chunk",
            "has_token": True,
            "cookie_count": 0,
            "page_count": len(self._pages),
            "pages_alive": alive_count,
            "pages_busy": busy_count,
            "pages_idle": alive_count - busy_count,
            "uptime_seconds": time.time() - self.start_time,
            "heartbeat_count": self.heartbeat_count,
            "requests_handled": self.requests_handled,
            "total_requests": self.total_requests,
            "consecutive_errors": self._consecutive_errors,
            "timestamp": datetime.now().isoformat(),
        }

    async def take_screenshot_base64(self) -> Optional[str]:
        for cp in self._pages:
            try:
                if not cp.page.is_closed():
                    buf = await cp.page.screenshot(full_page=False)
                    return base64.b64encode(buf).decode("utf-8")
            except Exception:
                continue
        return None

    async def simulate_activity(self):
        """模拟用户活动：鼠标移动 + 可见性欺骗 + 轻量网络请求保持 session"""
        import random
        self.heartbeat_count += 1

        for cp in self._pages:
            try:
                if cp.page.is_closed() or cp.busy:
                    continue

                await cp.page.mouse.move(
                    random.randint(100, 1800),
                    random.randint(100, 900),
                )

                if self.heartbeat_count % 3 == 0:
                    await cp.page.evaluate("""
                        () => {
                            window.dispatchEvent(new Event('mousemove'));
                            window.dispatchEvent(new Event('focus'));
                            try {
                                Object.defineProperty(document, 'hidden', {
                                    get: () => false, configurable: true
                                });
                                Object.defineProperty(document, 'visibilityState', {
                                    get: () => 'visible', configurable: true
                                });
                                document.dispatchEvent(new Event('visibilitychange'));
                            } catch(e) {}
                        }
                    """)

                if self.heartbeat_count % 6 == 0:
                    await cp.page.evaluate("""
                        () => {
                            try {
                                fetch('/api/v0/users/current', {
                                    method: 'GET',
                                    credentials: 'include'
                                }).catch(() => {});
                            } catch(e) {}
                        }
                    """)

            except Exception:
                pass

        if self.heartbeat_count % 5 == 0:
            for cp in self._pages:
                if not cp.busy:
                    try:
                        await cp.ensure_clipboard_hook()
                    except Exception:
                        pass

        if self.heartbeat_count % 10 == 0:
            alive = 0
            for cp in self._pages:
                try:
                    if not cp.page.is_closed():
                        alive += 1
                except Exception:
                    pass
            busy = sum(1 for cp in self._pages if cp.busy)
            print(f"💓 #{self.heartbeat_count} ({alive}活/{busy}忙) 登录={self.logged_in}")

    async def shutdown(self):
        try:
            self._save_camoufox_cache()
            if self.context:
                await self.context.close()
            if self._camoufox_ctx:
                await self._camoufox_ctx.__aexit__(None, None, None)
            elif self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            print("🔒 已关闭")
        except Exception as e:
            print(f"⚠️ {e}")

