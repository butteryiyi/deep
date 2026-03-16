# browser_manager.py
# 基于探针 probe_20260316_111934.json 精确数据编写
# 核心策略：DOM 轮询 + 复制按钮(剪贴板拦截) + 审查快照

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


def _is_censored(text: str) -> bool:
    text = text.strip()
    if not text:
        return False
    for phrase in CENSORSHIP_PHRASES:
        if phrase in text:
            if len(text) < 150:
                return True
    return False


# ═══════════════════════════════════════════════════════════════
# 注入脚本：只做剪贴板拦截（探针证实 SSE 拦截无效）
# ═══════════════════════════════════════════════════════════════
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

# ═══════════════════════════════════════════════════════════════
# 一次性读取所有状态（基于探针确认的精确结构）
#
# 探针确认的 DOM 结构:
#   div[data-virtual-list-item-key]
#     └ div.ds-message._63c77b1
#         ├ div._74c0879 (折叠容器)
#         │   └ div.ds-think-content._767406f
#         │       └ div.ds-markdown  ← 思考文本(排除)
#         └ div.ds-markdown  ← 正式回复(目标)
#
# 完成标志: item className 含 "_43c05b5" (生成中没有这个类)
# 按钮位置: item 内 div.ds-flex._965abe9 > div.ds-icon-button
# 复制按钮: 第一个 ds-icon-button, SVG 含 "M6.14923"
# ═══════════════════════════════════════════════════════════════
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
        clipText: '',
        clipLen: 0,
    };

    const items = document.querySelectorAll('div[data-virtual-list-item-key]');
    R.itemCount = items.length;
    if (items.length === 0) return R;

    const lastItem = items[items.length - 1];
    const itemClass = lastItem.className || '';

    // ══ 完成检测 ══
    // 探针发现: 生成中 className="_4f9bf79 d7dc56a8"
    //           完成后 className="_4f9bf79 d7dc56a8 _43c05b5"
    R.isComplete = itemClass.includes('_43c05b5');

    // ══ 消息容器 ══
    const msgDiv = lastItem.querySelector('div.ds-message');
    if (!msgDiv) return R;

    // ══ 正式回复: ds-message 的直接子代 ds-markdown ══
    // 探针确认: 正式回复 = div.ds-message > div.ds-markdown
    //          思考区域 = div.ds-message > div._74c0879 > div.ds-think-content > div.ds-markdown
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

    // ══ 思考区域 ══
    const thinkDiv = msgDiv.querySelector('div.ds-think-content');
    if (thinkDiv) {
        const thinkMd = thinkDiv.querySelector('div.ds-markdown');
        if (thinkMd) {
            R.thinkLen = (thinkMd.innerText || '').length;
        }
    }

    // ══ 按钮 ══
    // 探针确认: 按钮在 div.ds-flex._965abe9 > div.ds-icon-button
    // 完成后有5个按钮（复制、重试、点赞、点踩、分享）
    const btnContainer = lastItem.querySelector('div._965abe9');
    if (btnContainer) {
        const btns = btnContainer.querySelectorAll('div.ds-icon-button');
        R.buttonCount = btns.length;
        R.hasButton = btns.length > 0;
    } else {
        // 备选: 直接找
        const btns = lastItem.querySelectorAll('div.ds-icon-button');
        R.buttonCount = btns.length;
        R.hasButton = btns.length >= 3;  // 至少3个才算有功能按钮（排除用户消息的2个）
    }

    // ══ 生成中检测 ══
    // 方法1: _43c05b5 类名检测（最可靠）
    R.isGenerating = !R.isComplete && R.itemCount >= 2;

    // 方法2: 检查是否有"正在思考"动画
    if (!R.isGenerating) {
        const thinkAnim = lastItem.querySelector('span.e4b3a110');
        if (thinkAnim) {
            const style = thinkAnim.getAttribute('style') || '';
            if (style.includes('running')) {
                R.isGenerating = true;
            }
        }
    }

    // ══ 剪贴板 ══
    if (window.__clipData) {
        R.clipText = window.__clipData.text || '';
        R.clipLen = R.clipText.length;
    }

    return R;
}
"""

# 点击复制按钮
# 探针确认: 复制按钮 = _965abe9 容器内第一个 ds-icon-button
# SVG path 以 "M6.14923" 开头
CLICK_COPY_JS = """
() => {
    const items = document.querySelectorAll('div[data-virtual-list-item-key]');
    if (items.length === 0) return 'no-items';
    const lastItem = items[items.length - 1];

    // 清空之前的剪贴板
    if (window.__clipData) {
        window.__clipData = { text: '', time: 0 };
    }

    // 在 _965abe9 容器内找第一个按钮
    const btnContainer = lastItem.querySelector('div._965abe9');
    if (btnContainer) {
        const firstBtn = btnContainer.querySelector('div.ds-icon-button');
        if (firstBtn) {
            firstBtn.click();
            return 'clicked-965';
        }
    }

    // 备选: 通过 SVG path 找复制按钮
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

    // 最后备选: 找父级是 _965abe9 或 _54866f7 的按钮
    for (const btn of allBtns) {
        const parentClass = btn.parentElement?.className || '';
        if (parentClass.includes('_965abe9') || parentClass.includes('_54866f7')) {
            btn.click();
            return 'clicked-parent';
        }
    }

    return 'not-found';
}
"""

# 滚动到底部
SCROLL_BOTTOM_JS = """
() => {
    // 探针确认: 滚动容器是 div._765a5cd.ds-scroll-area
    const sa = document.querySelector('.ds-scroll-area');
    if (sa) { sa.scrollTop = sa.scrollHeight; return true; }
    return false;
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
        """安装/重装剪贴板拦截"""
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
            print(f"  ⚠️ P#{self.page_id} 剪贴板 hook 失败: {e}")

    async def reset_clip(self):
        try:
            await self.page.evaluate("() => { if(window.__clipData) window.__clipData = {text:'',time:0}; }")
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
                "itemCount": 0, "clipText": "", "clipLen": 0,
                "error": str(e),
            }

    async def click_copy_and_wait(self, timeout: float = 3.0) -> str:
        try:
            result = await self.page.evaluate(CLICK_COPY_JS)
            if result == 'not-found' or result == 'no-items':
                return ""

            deadline = time.time() + timeout
            while time.time() < deadline:
                await asyncio.sleep(0.2)
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

    async def start_new_chat(self):
        self._hook_installed = False
        if "chat.deepseek.com" not in (self.page.url or ""):
            await self.page.goto(
                "https://chat.deepseek.com/",
                wait_until="domcontentloaded", timeout=30000,
            )
            await asyncio.sleep(2)

        # 探针确认: 新对话按钮
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
        # 探针确认: textarea placeholder="给 DeepSeek 发送消息 "
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

        # 登录
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

        # 创建其余页面
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

        self.total_requests += 1
        self.requests_handled += 1
        req_id = self.total_requests
        print(f"📨 #{req_id} ({len(message)} 字符)")

        cp = None
        try:
            cp = await asyncio.wait_for(self._acquire_page(), timeout=300)
        except (asyncio.TimeoutError, RuntimeError) as e:
            yield f"[错误] {e}"
            return

        print(f"  [#{req_id}] → 页面#{cp.page_id}")

        try:
            cp.request_count += 1

            # 检查存活
            if not await cp.is_alive():
                print(f"  [#{req_id}] 页面死亡，恢复中...")
                try:
                    new_page = await self.context.new_page()
                    await new_page.goto(
                        "https://chat.deepseek.com/",
                        wait_until="domcontentloaded", timeout=30000,
                    )
                    await asyncio.sleep(2)
                    cp.page = new_page
                    cp._hook_installed = False
                except Exception as e:
                    yield f"[错误] 页面恢复失败: {e}"
                    return

            # 新对话
            await cp.start_new_chat()
            await asyncio.sleep(0.5)

            # 安装 hook + 重置
            await cp.ensure_clipboard_hook()
            await cp.reset_clip()

            # 发送
            await cp.type_and_send(message)
            print(f"  [#{req_id}] 已发送")

            # ═══════════════════════════════════════════
            # 流式读取主循环
            # ═══════════════════════════════════════════
            max_wait = 600
            poll_interval = 0.4
            yielded_len = 0
            best_text = ""        # 抗审查快照
            gen_started = False
            no_change_count = 0
            prev_len = 0
            start_ts = time.time()
            scroll_counter = 0

            # 等 AI 开始生成（第二个 item 出现）
            for _ in range(60):
                await asyncio.sleep(0.5)
                st = await cp.read_state()
                if st.get("itemCount", 0) >= 2:
                    break
                if time.time() - start_ts > 30:
                    break

            while True:
                elapsed = time.time() - start_ts
                if elapsed > max_wait:
                    print(f"  [#{req_id}] ⏰ 超时 {max_wait}s")
                    break

                await asyncio.sleep(poll_interval)
                scroll_counter += 1

                # 每 12 次（~5秒）滚动一下
                if scroll_counter % 12 == 0:
                    await cp.scroll_to_bottom()

                # 读状态
                state = await cp.read_state()
                dom_text = state.get("domText", "")
                dom_len = state.get("domLen", 0)
                think_len = state.get("thinkLen", 0)
                is_complete = state.get("isComplete", False)
                has_button = state.get("hasButton", False)
                is_gen = state.get("isGenerating", False)
                btn_count = state.get("buttonCount", 0)

                # 检测生成开始
                if not gen_started and (dom_len > 0 or think_len > 0 or is_gen):
                    gen_started = True
                    no_change_count = 0
                    print(f"  [#{req_id}] 🚀 开始 (think={think_len} reply={dom_len} gen={is_gen})")

                # 更新快照
                if dom_len > len(best_text):
                    best_text = dom_text

                # ── 审查检测 ──
                if (gen_started and len(best_text) > 80
                    and dom_text and dom_len < len(best_text) * 0.4
                    and _is_censored(dom_text)):
                    print(f"  [#{req_id}] 🛡️ 审查! dom={dom_len} snap={len(best_text)}")
                    remaining = best_text[yielded_len:]
                    if remaining:
                        yield remaining
                    break

                # ── 流式增量输出 ──
                if dom_text and dom_len > yielded_len:
                    new_part = dom_text[yielded_len:]
                    if not is_complete:
                        # 生成中: 直接输出
                        yield new_part
                        yielded_len = dom_len
                        no_change_count = 0
                    elif not _is_censored(dom_text):
                        # 已完成且非审查
                        yield new_part
                        yielded_len = dom_len

                # ── 完成检测 ──
                # 探针发现: is_complete = className 含 _43c05b5
                # 同时 has_button = 有5个功能按钮
                if gen_started and is_complete and has_button and btn_count >= 3:
                    # 再等一下确认
                    await asyncio.sleep(0.5)
                    confirm = await cp.read_state()
                    if confirm.get("isComplete", False) and confirm.get("hasButton", False):
                        # 滚到底再读一次
                        await cp.scroll_to_bottom()
                        await asyncio.sleep(0.3)
                        final = await cp.read_state()
                        final_text = final.get("domText", "")
                        final_len = final.get("domLen", 0)

                        if final_len > len(best_text):
                            best_text = final_text

                        # 输出剩余
                        use_text = best_text if final_len < len(best_text) else final_text
                        if len(use_text) > yielded_len:
                            if not _is_censored(use_text):
                                yield use_text[yielded_len:]
                                yielded_len = len(use_text)
                            else:
                                remaining = best_text[yielded_len:]
                                if remaining:
                                    yield remaining
                                    yielded_len = len(best_text)
                                print(f"  [#{req_id}] 🛡️ 完成时审查")

                        # ══ 点复制按钮获取完整 markdown ══
                        clip_text = await cp.click_copy_and_wait(timeout=3.0)
                        if clip_text:
                            print(f"  [#{req_id}] 📋 clip={len(clip_text)} dom={yielded_len}")

                        print(f"  [#{req_id}] ✅ 完成: {yielded_len} 字 "
                              f"(dom={final_len} clip={len(clip_text) if clip_text else 0})")
                        break

                # ── 无进展检测 ──
                if dom_len == prev_len:
                    no_change_count += 1
                else:
                    no_change_count = 0
                    prev_len = dom_len

                if no_change_count > int(90 / poll_interval):
                    if gen_started and best_text:
                        if len(best_text) > yielded_len:
                            yield best_text[yielded_len:]
                        print(f"  [#{req_id}] ⏰ 90s 无进展: {len(best_text)} 字")
                        break
                    elif not gen_started and elapsed > 120:
                        print(f"  [#{req_id}] ❌ 120s 无响应")
                        break

                # ── 日志 ──
                if scroll_counter % 37 == 0:  # ~15s
                    print(f"  [#{req_id}] ⏳ {elapsed:.0f}s dom={dom_len} "
                          f"think={think_len} out={yielded_len} "
                          f"comp={is_complete} btn={btn_count}")

            # ═══════════════════════════════════════════
            # 兜底
            # ═══════════════════════════════════════════
            if yielded_len == 0:
                # 1) 点复制按钮
                clip = await cp.click_copy_and_wait(timeout=5.0)
                if clip and not _is_censored(clip):
                    yield clip
                    print(f"  [#{req_id}] 📋 兜底复制: {len(clip)} 字")
                    return

                # 2) 再读 DOM
                await cp.scroll_to_bottom()
                await asyncio.sleep(1)
                st = await cp.read_state()
                dt = st.get("domText", "")
                if dt and not _is_censored(dt):
                    yield dt
                    print(f"  [#{req_id}] 📋 兜底DOM: {len(dt)} 字")
                    return

                # 3) 快照
                if best_text:
                    yield best_text
                    print(f"  [#{req_id}] 📋 兜底快照: {len(best_text)} 字")
                    return

                yield "抱歉，未能获取到响应。请稍后重试。"
                print(f"  [#{req_id}] ❌ 完全无响应")

        except Exception as e:
            print(f"  [#{req_id}] ❌ {e}")
            import traceback
            traceback.print_exc()
            yield f"[错误] {str(e)}"

        finally:
            if cp:
                self._release_page(cp)

    async def is_alive(self) -> bool:
        if not self._ready or not self._pages:
            return False
        for cp in self._pages:
            if await cp.is_alive():
                return True
        return False

    async def get_status(self) -> dict:
        alive_count = sum(1 for cp in self._pages if not cp.page.is_closed())
        busy_count = sum(1 for cp in self._pages if cp.busy)
        return {
            "browser_alive": alive_count > 0,
            "logged_in": self.logged_in,
            "ready": self._ready,
            "engine": self._engine,
            "mode": "dom-poll+clipboard-v3",
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
        self.heartbeat_count += 1
        for cp in self._pages:
            try:
                if not cp.page.is_closed() and not cp.busy:
                    import random
                    await cp.page.mouse.move(
                        random.randint(100, 1800),
                        random.randint(100, 900),
                    )
            except Exception:
                pass

        # 定期重装 hook
        if self.heartbeat_count % 5 == 0:
            for cp in self._pages:
                if not cp.busy:
                    try:
                        await cp.ensure_clipboard_hook()
                    except Exception:
                        pass

        if self.heartbeat_count % 10 == 0:
            alive = sum(1 for cp in self._pages if not cp.page.is_closed())
            busy = sum(1 for cp in self._pages if cp.busy)
            print(f"💓 #{self.heartbeat_count} ({alive}活/{busy}忙)")

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
