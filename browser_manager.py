# browser_manager.py

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

# 审查替换的固定提示语列表
CENSORSHIP_PHRASES = [
    "这个问题我暂时无法回答",
    "让我们换个话题再聊聊吧",
    "我无法回答这个问题",
    "抱歉，我无法",
    "这个话题不太适合讨论",
    "我没法对此进行回答",
    "作为AI助手，我无法",
    "你好，这个问题我暂时无法回答，让我们换个话题再聊聊吧",
    "很抱歉，这个问题",
]

# ============================================================
# 精简后的 JS：合并读取文本 + 状态检测为一次调用
# ============================================================
CHECK_STATE_JS = """
() => {
    const result = {
        hasButton: false,
        isGenerating: false,
        text: '',
        itemCount: 0,
        hasThinking: false,
        thinkingDone: false,
    };

    const items = document.querySelectorAll('div[data-virtual-list-item-key]');
    result.itemCount = items.length;
    if (items.length === 0) return result;

    const lastItem = items[items.length - 1];

    // —— 提取回复正文（排除思考区域）——
    const allMd = lastItem.querySelectorAll('[class*="ds-markdown"]');
    if (allMd.length > 0) {
        // 收集 think 容器
        const thinkSels = [
            '[class*="think"]', '[class*="Think"]', '[class*="thought"]',
            '[class*="reasoning"]', '[class*="collapse"]', '[class*="Collapse"]',
            'details'
        ];
        const thinkSet = new Set();
        for (const sel of thinkSels) {
            lastItem.querySelectorAll(sel).forEach(el => thinkSet.add(el));
        }

        if (thinkSet.size > 0) {
            result.hasThinking = true;
            // 检查思考区域是否已收起/完成（有 summary 或者有折叠标记）
            for (const tc of thinkSet) {
                if (tc.querySelector('summary') ||
                    tc.getAttribute('aria-expanded') === 'false' ||
                    tc.classList.toString().includes('ollaps')) {
                    result.thinkingDone = true;
                    break;
                }
            }
        }

        const replyMds = [];
        for (const md of allMd) {
            let insideThink = false;
            for (const tc of thinkSet) {
                if (tc.contains(md) && tc !== md) {
                    insideThink = true;
                    break;
                }
            }
            if (!insideThink) replyMds.push(md);
        }

        if (replyMds.length > 0) {
            result.text = replyMds[replyMds.length - 1].innerText || '';
        } else if (allMd.length > 0) {
            result.text = allMd[allMd.length - 1].innerText || '';
        }
    }

    // —— 复制/重试按钮 ——
    const buttons = lastItem.querySelectorAll('div[role="button"]');
    result.hasButton = buttons.length > 0;

    // —— 是否正在生成：查找停止按钮（更精确的选择器）——
    // DeepSeek 的停止按钮通常有特定的 SVG 或者 aria-label
    const stopSelectors = [
        'button[aria-label*="stop"]',
        'button[aria-label*="Stop"]',
        'button[aria-label*="停止"]',
        '[class*="stop-button"]',
        '[class*="stopButton"]',
    ];

    let foundStop = false;
    for (const sel of stopSelectors) {
        const el = document.querySelector(sel);
        if (el && el.offsetParent !== null) {
            foundStop = true;
            break;
        }
    }

    // 备用检测：通过动画元素判断（加载动画/光标闪烁）
    if (!foundStop) {
        const loadingIndicators = lastItem.querySelectorAll(
            '[class*="loading"], [class*="cursor"], [class*="blink"], ' +
            '[class*="typing"], [class*="generating"]'
        );
        for (const el of loadingIndicators) {
            if (el.offsetParent !== null) {
                foundStop = true;
                break;
            }
        }
    }

    // 再备用：检查整个页面底部的停止/方块按钮区域
    if (!foundStop) {
        // 只在输入框附近查找，避免误匹配
        const inputArea = document.querySelector('textarea')?.parentElement?.parentElement?.parentElement;
        if (inputArea) {
            const btns = inputArea.querySelectorAll('div[role="button"], button');
            for (const btn of btns) {
                const svg = btn.querySelector('svg');
                if (svg && btn.offsetParent !== null) {
                    const rect = svg.querySelector('rect');
                    if (rect) {
                        foundStop = true;
                        break;
                    }
                }
            }
        }
    }

    result.isGenerating = foundStop;
    return result;
}
"""


class ChatPage:
    def __init__(self, page, page_id: int):
        self.page = page
        self.page_id = page_id
        self.busy = False
        self.request_count = 0
        self.last_used = 0.0

    async def start_new_chat(self):
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
                    return
            except Exception:
                continue

        await self.page.goto(
            "https://chat.deepseek.com/",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await asyncio.sleep(3)

    async def get_conversation_item_count(self) -> int:
        try:
            return await self.page.evaluate("""
                () => document.querySelectorAll(
                    'div[data-virtual-list-item-key]'
                ).length
            """)
        except Exception:
            return 0

    async def type_and_send(self, message: str):
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
        await asyncio.sleep(1)

    async def check_generation_state(self) -> dict:
        return await self.page.evaluate(CHECK_STATE_JS)

    async def read_response_text(self) -> str:
        try:
            state = await self.check_generation_state()
            return (state.get("text") or "").strip()
        except Exception:
            return ""

    async def is_alive(self) -> bool:
        try:
            if self.page.is_closed():
                return False
            await self.page.evaluate("() => document.title")
            return True
        except Exception:
            return False


def _is_censored(text: str) -> bool:
    text = text.strip()
    if not text:
        return False
    for phrase in CENSORSHIP_PHRASES:
        if phrase in text:
            if len(text) < 100:
                return True
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

        first_page = await self.context.new_page()
        auth = AuthHandler(first_page, context=self.context)
        self.logged_in = await auth.login(self.email, self.password)

        if not self.logged_in:
            print("⚠️ 登录可能未完成")
            await first_page.close()
        else:
            print("🎉 登录成功！")
            self._pages.append(ChatPage(first_page, 0))
            print(f"  📄 页面 #0 就绪")

        for i in range(1, self._page_count):
            try:
                page = await self.context.new_page()
                await page.goto(
                    "https://chat.deepseek.com/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await asyncio.sleep(2)
                self._pages.append(ChatPage(page, i))
                print(f"  📄 页面 #{i} 就绪")
            except Exception as e:
                print(f"  ⚠️ 页面 #{i} 创建失败: {e}")

        actual_count = len(self._pages)
        self._page_semaphore = asyncio.Semaphore(actual_count)

        self._ready = True
        self._ready_event.set()
        print(f"✅ 就绪（引擎: {self._engine}，{actual_count} 个并发页面）")

    async def _start_with_camoufox(self):
        from camoufox.async_api import AsyncCamoufox
        self._camoufox = AsyncCamoufox(headless=self.headless, geoip=False)
        self.browser = await self._camoufox.__aenter__()
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
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
        print(f"📨 请求 #{req_id} (长度: {len(message)} 字符)")

        cp = None
        try:
            cp = await asyncio.wait_for(self._acquire_page(), timeout=300)
        except asyncio.TimeoutError:
            yield "[错误] 所有页面忙碌，请稍后重试"
            return
        except Exception as e:
            yield f"[错误] {e}"
            return

        print(f"  [{req_id}] 分配到页面 #{cp.page_id}")

        try:
            cp.request_count += 1

            if not await cp.is_alive():
                print(f"  [{req_id}] 页面 #{cp.page_id} 已死，恢复中...")
                try:
                    new_page = await self.context.new_page()
                    await new_page.goto(
                        "https://chat.deepseek.com/",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    await asyncio.sleep(2)
                    cp.page = new_page
                except Exception as e:
                    yield f"[错误] 页面恢复失败: {e}"
                    return

            # ========== 新对话 & 记录基线 ==========
            await cp.start_new_chat()
            await asyncio.sleep(1)

            baseline_item_count = await cp.get_conversation_item_count()
            print(f"  [{req_id}] 基线对话项数: {baseline_item_count}")

            await cp.type_and_send(message)
            print(f"  [{req_id}] 等待回复...")

            # ═══════════════════════════════════════════════════
            # 核心改进：
            # 1. 无论任何状态，只要有新文本就立即 yield（不再受按钮/生成状态门控）
            # 2. 用"文本稳定 + 非生成中"作为主要完成判据
            # 3. 按钮出现作为辅助完成确认
            # 4. 最终读取用额外等待确保拿到完整文本
            # ═══════════════════════════════════════════════════

            max_wait_seconds = 600
            poll_interval = 0.5  # 从 0.3 调大到 0.5，减少 DOM 压力

            yielded_length = 0
            best_snapshot = ""
            last_text = ""
            generation_started = False
            new_response_appeared = False
            stable_ticks = 0          # 文本不再变化的连续 tick 数
            stable_threshold = 6      # 3 秒 (6 * 0.5) 文本稳定就认为完成
            finished = False
            resend_attempted = False

            total_ticks = int(max_wait_seconds / poll_interval)

            for tick in range(total_ticks):
                await asyncio.sleep(poll_interval)

                try:
                    state = await cp.check_generation_state()
                except Exception:
                    continue

                has_button = state.get("hasButton", False)
                is_generating = state.get("isGenerating", False)
                current_text = (state.get("text") or "").strip()
                item_count = state.get("itemCount", 0)
                elapsed = (tick + 1) * poll_interval

                # ———— 阶段 1：等待新回复出现 ————
                if not new_response_appeared:
                    if item_count > baseline_item_count:
                        new_response_appeared = True
                        print(f"  [{req_id}] 📬 新对话项出现 "
                              f"({baseline_item_count} -> {item_count})")
                    elif is_generating:
                        new_response_appeared = True
                        print(f"  [{req_id}] 📬 检测到生成开始")
                    else:
                        # 定期日志
                        if tick > 0 and tick % int(15 / poll_interval) == 0:
                            print(f"  [{req_id}] ⏳ {elapsed:.0f}s "
                                  f"等待新回复... items={item_count}")

                        # 30 秒没出现，尝试重发一次
                        if elapsed > 30 and not resend_attempted:
                            resend_attempted = True
                            print(f"  [{req_id}] ⚠️ 30秒未见新对话项，尝试重新发送")
                            try:
                                await cp.type_and_send(message)
                                baseline_item_count = item_count
                            except Exception as e:
                                print(f"  [{req_id}] ❌ 重发失败: {e}")

                        if elapsed > 60:
                            print(f"  [{req_id}] ❌ 60秒无新对话项，放弃")
                            break
                        continue

                # ———— 跟踪生成状态 ————
                if is_generating and not generation_started:
                    generation_started = True
                    print(f"  [{req_id}] 🚀 生成开始")

                # ———— 审查检测 ————
                if (current_text and
                    len(best_snapshot) > 50 and
                    len(current_text) < len(best_snapshot) * 0.5 and
                    _is_censored(current_text)):
                    print(f"  [{req_id}] 🛡️ 检测到审查替换！"
                          f"当前={len(current_text)} vs 快照={len(best_snapshot)}")
                    remaining = best_snapshot[yielded_length:]
                    if remaining:
                        yield remaining
                    finished = True
                    break

                # ———— 更新快照 ————
                if current_text and len(current_text) >= len(best_snapshot):
                    best_snapshot = current_text

                # ———— 【核心修复】无条件流式输出增量 ————
                # 不再检查 is_generating 或 has_button，有新文本就输出
                if current_text and len(current_text) > yielded_length:
                    new_content = current_text[yielded_length:]
                    yield new_content
                    yielded_length = len(current_text)
                    stable_ticks = 0  # 有新文本，重置稳定计数

                # ———— 文本稳定性跟踪 ————
                if current_text == last_text and current_text:
                    stable_ticks += 1
                else:
                    stable_ticks = 0
                last_text = current_text

                # ———— 完成检测（多条件综合判断）————

                # 条件 A：有按钮 + 非生成中 + 文本稳定 ≥ 1秒
                if (has_button and not is_generating and
                        stable_ticks >= int(1.0 / poll_interval) and
                        new_response_appeared and current_text):
                    # 最终确认：再读一次确保拿到完整文本
                    await asyncio.sleep(0.5)
                    try:
                        final_state = await cp.check_generation_state()
                        final_text = (final_state.get("text") or "").strip()
                        if final_text and len(final_text) > yielded_length:
                            yield final_text[yielded_length:]
                            yielded_length = len(final_text)
                        if len(final_text) > len(best_snapshot):
                            best_snapshot = final_text
                    except Exception:
                        pass

                    # 审查最终检查
                    final_check_text = best_snapshot if len(best_snapshot) > yielded_length else current_text
                    if (_is_censored(final_check_text) and
                            len(best_snapshot) > len(final_check_text) * 1.5):
                        remaining = best_snapshot[yielded_length:]
                        if remaining:
                            yield remaining
                        print(f"  [{req_id}] 🛡️ 完成时审查，使用快照")
                    
                    print(f"  [{req_id}] ✅ 正常完成 {max(yielded_length, len(best_snapshot))} 字符")
                    finished = True
                    break

                # 条件 B：文本稳定很久 + 非生成中（按钮可能没出现）
                if (stable_ticks >= stable_threshold and
                        not is_generating and
                        (generation_started or new_response_appeared) and
                        current_text):
                    # 额外等待一下，给按钮出现的时间，但不阻塞输出
                    await asyncio.sleep(1.0)
                    try:
                        final_state = await cp.check_generation_state()
                        final_text = (final_state.get("text") or "").strip()
                        still_generating = final_state.get("isGenerating", False)

                        if still_generating:
                            # 原来还在生成，重置稳定计数继续等
                            stable_ticks = 0
                            if final_text and len(final_text) > yielded_length:
                                yield final_text[yielded_length:]
                                yielded_length = len(final_text)
                            continue

                        if final_text and len(final_text) > yielded_length:
                            yield final_text[yielded_length:]
                            yielded_length = len(final_text)
                        if len(final_text) > len(best_snapshot):
                            best_snapshot = final_text
                    except Exception:
                        pass

                    print(f"  [{req_id}] ✅ 稳定完成 {max(yielded_length, len(best_snapshot))} 字符")
                    finished = True
                    break

                # ———— 进度日志 ————
                if tick > 0 and tick % int(20 / poll_interval) == 0:
                    print(
                        f"  [{req_id}] ⏳ {elapsed:.0f}s "
                        f"len={len(current_text)} yielded={yielded_length} "
                        f"gen={is_generating} btn={has_button} "
                        f"stable={stable_ticks} snapshot={len(best_snapshot)}"
                    )

                # ———— 硬超时 ————
                if (elapsed > 60 and
                        not generation_started and
                        not current_text and
                        new_response_appeared):
                    print(f"  [{req_id}] ❌ 60秒无回复内容")
                    break

            # ———— 最终兜底 ————
            if not finished:
                # 再做一次最终读取
                try:
                    await asyncio.sleep(1)
                    final_text = await cp.read_response_text()
                    if final_text and len(final_text) > yielded_length:
                        yield final_text[yielded_length:]
                        yielded_length = len(final_text)
                        print(f"  [{req_id}] 📋 兜底最终读取: {len(final_text)} 字符")
                except Exception:
                    pass

                if best_snapshot and yielded_length < len(best_snapshot):
                    remaining = best_snapshot[yielded_length:]
                    if remaining:
                        yield remaining
                    print(f"  [{req_id}] 📋 兜底快照: {len(best_snapshot)} 字符")
                elif yielded_length == 0:
                    fallback = await cp.read_response_text()
                    if fallback:
                        yield fallback
                        print(f"  [{req_id}] 📋 兜底读取: {len(fallback)} 字符")
                    else:
                        yield "抱歉，未能获取到响应。请稍后重试。"
                        print(f"  [{req_id}] ❌ 完全无响应")

            print(f"  [{req_id}] 📊 页面#{cp.page_id} 请求完成")

        except Exception as e:
            print(f"  [{req_id}] ❌ {e}")
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
        alive_count = 0
        busy_count = 0
        for cp in self._pages:
            if await cp.is_alive():
                alive_count += 1
            if cp.busy:
                busy_count += 1

        return {
            "browser_alive": alive_count > 0,
            "logged_in": self.logged_in,
            "ready": self._ready,
            "engine": self._engine,
            "mode": "stream-with-anti-censorship",
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
                    await cp.page.evaluate("""
                        () => {
                            document.dispatchEvent(new MouseEvent(
                                'mousemove', {
                                    clientX: Math.random() * window.innerWidth,
                                    clientY: Math.random() * window.innerHeight
                                }
                            ));
                            window.scrollBy(0, Math.random() > 0.5 ? 1 : -1);
                        }
                    """)
            except Exception:
                pass
        if self.heartbeat_count % 10 == 0:
            alive = sum(1 for cp in self._pages if not cp.page.is_closed())
            busy = sum(1 for cp in self._pages if cp.busy)
            print(f"💓 心跳 #{self.heartbeat_count} ({alive}存活/{busy}忙碌)")

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
