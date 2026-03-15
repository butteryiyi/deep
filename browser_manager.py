# browser_manager.py
"""
DeepSeek 反代（DOM 交互模式）
- 合并旧版（DOM 交互 + Camoufox + 请求锁）与新版（就绪控制 + 辅助脚本预注入）
- 不涉及 PoW，所有反爬由浏览器自己处理
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

        # 请求锁：保证同时只有一个对话在进行
        self._lock = asyncio.Lock()

        # 就绪控制
        self._ready = False
        self._ready_event = asyncio.Event()

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

    # ── Camoufox 缓存管理 ──

    def _prepare_camoufox_cache(self):
        home_cache = Path.home() / ".cache"
        store_dir = home_cache / "camoufox_store"
        cache_dir = home_cache / "camoufox"

        if store_dir.exists() and any(store_dir.iterdir()):
            print(f"  📦 从持久存储恢复 Camoufox 缓存...")
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                shutil.copytree(store_dir, cache_dir, dirs_exist_ok=True)
                print(f"  ✅ 缓存已恢复，大小: {self._dir_size(cache_dir):.0f} MB")
            except Exception as e:
                print(f"  ⚠️ 恢复缓存失败: {e}")
        else:
            print(f"  📦 未找到持久缓存，Camoufox 将首次下载...")
            store_dir.mkdir(parents=True, exist_ok=True)

    def _save_camoufox_cache(self):
        home_cache = Path.home() / ".cache"
        store_dir = home_cache / "camoufox_store"
        cache_dir = home_cache / "camoufox"

        if cache_dir.exists() and any(cache_dir.iterdir()):
            try:
                store_dir.mkdir(parents=True, exist_ok=True)
                shutil.copytree(cache_dir, store_dir, dirs_exist_ok=True)
                print(f"  💾 Camoufox 缓存已保存 ({self._dir_size(store_dir):.0f} MB)")
            except Exception as e:
                print(f"  ⚠️ 保存缓存失败（非致命）: {e}")

    @staticmethod
    def _dir_size(path: Path) -> float:
        total = 0
        try:
            for f in path.rglob("*"):
                if f.is_file():
                    total += f.stat().st_size
        except Exception:
            pass
        return total / (1024 * 1024)

    # ── 初始化 ──

    async def initialize(self):
        print("🔧 正在初始化浏览器...")

        if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
            if os.path.isdir("/opt/browsers"):
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/browsers"

        # 优先尝试 Camoufox，失败则回退 Playwright Firefox
        camoufox_ok = False
        try:
            os.environ['CAMOUFOX_NO_UPDATE_CHECK'] = '1'
            self._prepare_camoufox_cache()
            from camoufox.async_api import AsyncCamoufox
            self._camoufox_cls = AsyncCamoufox
            await self._start_with_camoufox()
            camoufox_ok = True
            self._engine = "camoufox"
            self._save_camoufox_cache()
        except Exception as e:
            print(f"⚠️ Camoufox 启动失败: {e}")
            print("⚠️ 回退到 Playwright Firefox...")
            if hasattr(self, '_camoufox'):
                try:
                    await self._camoufox.__aexit__(None, None, None)
                except Exception:
                    pass
            camoufox_ok = False

        if not camoufox_ok:
            await self._start_with_playwright()
            self._engine = "playwright-firefox"

        await self._inject_stealth_scripts()

        # 登录
        auth = AuthHandler(self.page, context=self.context)
        self.logged_in = await auth.login(self.email, self.password)

        if not self.logged_in:
            print("⚠️ 登录可能未完成，请检查 /screenshot 端点。")
        else:
            print("🎉 登录成功！")

        # 注入 DOM 辅助脚本
        await self._inject_helper_scripts()

        # 标记就绪
        self._ready = True
        self._ready_event.set()
        print(f"✅ 浏览器就绪（引擎: {self._engine}，模式: DOM 交互，无 PoW）")

    async def _start_with_camoufox(self):
        print("  → 使用 Camoufox 反指纹浏览器...")
        from camoufox.async_api import AsyncCamoufox

        self._camoufox = AsyncCamoufox(headless=self.headless, geoip=False)
        self.browser = await self._camoufox.__aenter__()
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        self.page = await self.context.new_page()
        print("  ✅ Camoufox 浏览器已启动。")

    async def _start_with_playwright(self):
        print("  → 使用 Playwright Firefox...")
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()

        try:
            self.browser = await self.playwright.firefox.launch(
                headless=self.headless, args=["--no-sandbox"]
            )
        except Exception:
            import subprocess
            env = os.environ.copy()
            result = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "firefox"],
                capture_output=True, text=True, env=env, timeout=120,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Playwright Firefox 安装失败: {result.stderr}")
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
        print("  ✅ Playwright Firefox 已启动。")

    async def _inject_stealth_scripts(self):
        if self._engine == "camoufox":
            await self.context.add_init_script(
                "if (navigator.webdriver !== undefined) {"
                "  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
                "}"
            )
            print("  🛡️ Camoufox: 最小化反检测脚本")
        else:
            await self.context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['zh-CN', 'zh', 'en-US', 'en']
                });
                const _origQuery = window.navigator.permissions.query;
                if (_origQuery) {
                    window.navigator.permissions.query = (p) => (
                        p.name === 'notifications'
                            ? Promise.resolve({ state: Notification.permission })
                            : _origQuery(p)
                    );
                }
            """)
            print("  🛡️ Firefox: 注入反检测脚本")

    async def _inject_helper_scripts(self):
        """注入 DOM 辅助脚本，用于高效提取回复和检测状态"""
        await self.page.evaluate("""
            () => {
                window.__ds_injected = true;

                // 获取最后一条回复的文本
                window.__ds_get_last_reply = () => {
                    // 方法1：通过 data-virtual-list-item-key 定位（DeepSeek 特有）
                    const items = document.querySelectorAll('div[data-virtual-list-item-key]');
                    if (items.length > 0) {
                        const lastItem = items[items.length - 1];
                        const mdEls = lastItem.querySelectorAll('[class*="ds-markdown"]');
                        if (mdEls.length > 0) {
                            return mdEls[mdEls.length - 1].textContent || '';
                        }
                    }

                    // 方法2：通用 markdown 容器
                    const allMd = document.querySelectorAll(
                        '.ds-markdown--block, .ds-markdown, [class*="markdown"]'
                    );
                    if (allMd.length > 0) {
                        return allMd[allMd.length - 1].textContent || '';
                    }
                    return '';
                };

                // 综合检测是否正在生成
                window.__ds_is_generating = () => {
                    // 停止按钮存在
                    const stops = document.querySelectorAll(
                        '[class*="stop"], [class*="square"], [aria-label*="stop"], [aria-label*="Stop"]'
                    );
                    for (const el of stops) {
                        if (el.offsetParent !== null) return true;
                    }

                    // loading 动画
                    const loaders = document.querySelectorAll(
                        '[class*="loading"], [class*="typing"], [class*="generating"]'
                    );
                    for (const el of loaders) {
                        if (el.offsetParent !== null) return true;
                    }

                    return false;
                };

                // 检查复制按钮是否出现（回复完成的可靠标志）
                window.__ds_has_copy_button = () => {
                    const items = document.querySelectorAll('div[data-virtual-list-item-key]');
                    if (items.length === 0) return false;
                    const lastItem = items[items.length - 1];
                    const buttons = lastItem.querySelectorAll('div[role="button"]');
                    return buttons.length > 0;
                };

                // 回复数量
                window.__ds_reply_count = () => {
                    return document.querySelectorAll('div[data-virtual-list-item-key]').length;
                };

                console.log('[DS Helper] 辅助脚本注入完成');
            }
        """)
        print("  📜 DOM 辅助脚本已注入")

    async def _ensure_helper_injected(self):
        """确保辅助脚本仍然有效（页面刷新后会丢失）"""
        try:
            injected = await self.page.evaluate("() => window.__ds_injected === true")
            if not injected:
                await self._inject_helper_scripts()
        except Exception:
            await self._inject_helper_scripts()

    # ── 新对话 ──

    async def _start_new_chat(self):
        """开启新对话，确保上下文干净"""
        print("  → 开启新对话...")

        # 确保在 DeepSeek 页面
        if "chat.deepseek.com" not in self.page.url:
            await self.page.goto(
                "https://chat.deepseek.com/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(2)

        # 尝试点击"开启新对话"
        selectors = [
            "xpath=//*[contains(text(), '开启新对话')]",
            "xpath=//*[contains(text(), '新对话')]",
            "xpath=//*[contains(text(), 'New chat')]",
            "xpath=//*[contains(text(), 'New Chat')]",
            "div.ds-icon-button",
            "[class*='new-chat']",
            "[class*='new_chat']",
        ]

        for sel in selectors:
            try:
                btn = self.page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await asyncio.sleep(1)
                    print("  ✅ 已开启新对话")
                    return True
            except Exception:
                continue

        # 都失败则导航到主页
        await self.page.goto(
            "https://chat.deepseek.com/",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await asyncio.sleep(3)
        print("  ✅ 已导航到新对话页面")
        return True

    # ── 输入并发送 ──

    async def _type_and_send(self, message: str):
        """定位输入框，输入消息，发送"""
        # 等待输入框
        textarea = self.page.locator(
            "textarea[placeholder*='DeepSeek'], "
            "textarea[placeholder*='发送消息'], "
            "textarea, "
            "[contenteditable='true']"
        ).first
        await textarea.wait_for(state="visible", timeout=10000)
        await textarea.click()
        await asyncio.sleep(0.3)

        # 先尝试 fill（模拟粘贴，速度快），失败则用 JS 注入
        try:
            await textarea.fill("")
            await asyncio.sleep(0.2)
            await textarea.fill(message)
        except Exception:
            # fill 失败，用 JS 直接设值
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
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    } else {
                        el.innerText = text;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                }
            """, message)

        await asyncio.sleep(0.5)
        print(f"  → 已输入消息，长度: {len(message)}")

        # 发送：先尝试 Enter，因为 DeepSeek 默认 Enter 发送
        await textarea.press("Enter")
        print("  → 消息已发送")
        await asyncio.sleep(2)

    # ── 核心发送方法 ──

    async def send_message(self, message: str) -> str:
        full = ""
        async for chunk in self.send_message_stream(message):
            full += chunk
        return full

    async def send_message_stream(self, message: str) -> AsyncGenerator[str, None]:
        """
        发送消息并流式返回响应。
        
        合并两版优点：
        - 旧版的完成检测（复制按钮 + 停止按钮 + stable_count）
        - 新版的就绪控制和辅助脚本预注入
        """
        # 等待就绪
        if not self._ready:
            print("  ⏳ 请求等待浏览器初始化完成...")
            ok = await self.wait_until_ready(timeout=180)
            if not ok:
                yield "[错误] 浏览器初始化超时，请稍后重试"
                return

        # 加锁
        async with self._lock:
            self.total_requests += 1
            self.requests_handled += 1
            req_id = self.total_requests
            print(f"📨 请求 #{req_id} (长度: {len(message)} 字符)")
            print(f"  → 预览: {message[:100]}...")

            if not self.page:
                yield "[错误] 浏览器未就绪"
                return

            try:
                # 确保辅助脚本有效
                await self._ensure_helper_injected()

                # 开启新对话
                await self._start_new_chat()
                await asyncio.sleep(1)

                # 再次确保辅助脚本（新对话可能导致页面变化）
                await self._ensure_helper_injected()

                # 记录发送前的回复数量
                reply_count_before = await self.page.evaluate(
                    "() => window.__ds_reply_count()"
                )

                # 输入并发送
                await self._type_and_send(message)

                # ── 等待回复出现 ──
                print(f"  [{req_id}] 等待回复...")
                last_text = ""
                stable_count = 0
                max_wait_seconds = 600  # 最多等10分钟
                response_started = False

                for tick in range(max_wait_seconds * 2):  # 每0.5秒一次
                    await asyncio.sleep(0.5)

                    try:
                        current_text = await self.page.evaluate(
                            "() => window.__ds_get_last_reply()"
                        )
                        is_generating = await self.page.evaluate(
                            "() => window.__ds_is_generating()"
                        )
                        has_copy_btn = await self.page.evaluate(
                            "() => window.__ds_has_copy_button()"
                        )
                        current_count = await self.page.evaluate(
                            "() => window.__ds_reply_count()"
                        )
                    except Exception:
                        # 页面可能刷新了，重新注入
                        await self._ensure_helper_injected()
                        continue

                    current_text = (current_text or "").strip()

                    # 检测回复开始
                    if not response_started:
                        if current_text and current_count > reply_count_before:
                            response_started = True
                            print(f"  [{req_id}] 回复开始")
                        elif is_generating:
                            response_started = True
                            print(f"  [{req_id}] 检测到生成中")
                        elif tick > 120:  # 60秒还没开始
                            print(f"  [{req_id}] ❌ 等待回复超时（60秒）")
                            yield "[错误] 等待回复超时"
                            return
                        continue

                    # 回复已开始，流式输出
                    if current_text and len(current_text) > len(last_text):
                        new_part = current_text[len(last_text):]
                        last_text = current_text
                        stable_count = 0
                        yield new_part
                    elif current_text == last_text:
                        stable_count += 1

                    # 完成检测（三重保障）
                    # 1. 复制按钮出现 + 不在生成 + 文本稳定
                    if has_copy_btn and not is_generating and stable_count >= 3:
                        print(f"  [{req_id}] ✅ 完成（复制按钮可用）")
                        break

                    # 2. 不在生成 + 有内容 + 文本稳定较长时间
                    if not is_generating and current_text and stable_count >= 10:
                        print(f"  [{req_id}] ✅ 完成（生成停止 + 文本稳定）")
                        break

                    # 3. 文本超长时间无变化
                    if stable_count >= 60:  # 30秒无变化
                        print(f"  [{req_id}] ⏹️ 超时（文本30秒无变化）")
                        break

                    # 进度日志
                    if tick > 0 and tick % 20 == 0:
                        print(
                            f"  [{req_id}] ⏳ tick={tick}, "
                            f"len={len(current_text)}, "
                            f"gen={is_generating}, "
                            f"copy={has_copy_btn}, "
                            f"stable={stable_count}"
                        )

                # ── 兜底：如果完全没有获取到内容 ──
                if not last_text:
                    fallback = await self.page.evaluate("""
                        () => {
                            const allMd = document.querySelectorAll('[class*="ds-markdown"]');
                            if (allMd.length > 0) {
                                return allMd[allMd.length - 1].textContent || '';
                            }
                            return '';
                        }
                    """)
                    if fallback and fallback.strip():
                        print(f"  [{req_id}] ⚠️ 兜底获取回复，长度: {len(fallback)}")
                        yield fallback.strip()
                    else:
                        print(f"  [{req_id}] ❌ 完全未获取到响应")
                        try:
                            ss = await self.take_screenshot_base64()
                            if ss:
                                print(f"  [{req_id}] 📸 调试截图已生成")
                        except Exception:
                            pass
                        yield "抱歉，未能获取到响应。请稍后重试。"

                print(f"  [{req_id}] 📊 最终长度: {len(last_text)} 字符")

            except Exception as e:
                print(f"  [{req_id}] ❌ {e}")
                import traceback
                traceback.print_exc()
                yield f"[错误] {str(e)}"

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
            "mode": "dom-interaction",
            "uptime_seconds": time.time() - self.start_time,
            "heartbeat_count": self.heartbeat_count,
            "requests_handled": self.requests_handled,
            "total_requests": self.total_requests,
            "has_token": True,
            "cookie_count": 0,
            "current_url": self.page.url if self.page and not self.page.is_closed() else "N/A",
            "timestamp": datetime.now().isoformat(),
        }

    async def take_screenshot_base64(self) -> Optional[str]:
        try:
            if not self.page or self.page.is_closed():
                return None
            buf = await self.page.screenshot(full_page=False)
            return base64.b64encode(buf).decode("utf-8")
        except Exception as e:
            print(f"❌ 截图失败: {e}")
            return None

    async def simulate_activity(self):
        if not self.page or self.page.is_closed():
            return
        try:
            self.heartbeat_count += 1
            import random
            x = random.randint(100, 1800)
            y = random.randint(100, 900)
            await self.page.mouse.move(x, y)
            await self.page.evaluate("""
                () => {
                    document.dispatchEvent(new MouseEvent('mousemove', {
                        clientX: Math.random() * window.innerWidth,
                        clientY: Math.random() * window.innerHeight
                    }));
                    window.scrollBy(0, Math.random() > 0.5 ? 1 : -1);
                    window.dispatchEvent(new Event('focus'));
                }
            """)
            if self.heartbeat_count % 10 == 0:
                print(f"💓 心跳 #{self.heartbeat_count}")
        except Exception as e:
            print(f"⚠️ 心跳异常: {e}")

    async def shutdown(self):
        try:
            if hasattr(self, '_save_camoufox_cache'):
                self._save_camoufox_cache()
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            print("🔒 浏览器已安全关闭。")
        except Exception as e:
            print(f"⚠️ 关闭浏览器时出错: {e}")
