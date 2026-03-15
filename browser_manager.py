"""
浏览器生命周期管理器：
- 使用 Camoufox (反指纹 Firefox) 通过 Playwright 启动
- 管理登录、会话保持、消息发送
- 提供截图、状态查询等调试功能
"""

import os
import sys
import time
import json
import asyncio
import base64
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
        self._lock = asyncio.Lock()  # 防止并发操作浏览器

        # 凭据
        self.email = os.getenv("DEEPSEEK_EMAIL", "")
        self.password = os.getenv("DEEPSEEK_PASSWORD", "")

    async def initialize(self):
        """初始化浏览器并完成登录。"""
        print("🔧 正在初始化 Camoufox 浏览器...")

        # 确保 Playwright 浏览器路径正确（与 Dockerfile 中一致）
        if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
            # 检查共享路径是否存在
            if os.path.isdir("/opt/browsers"):
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/browsers"
                print("  → 使用共享浏览器路径: /opt/browsers")

        # 先尝试使用 Camoufox
        camoufox_succeeded = False
        try:
            # 设置环境变量禁用 Camoufox 的更新检查（避免 GitHub API 限流）
            os.environ['CAMOUFOX_NO_UPDATE_CHECK'] = '1'
            from camoufox.async_api import AsyncCamoufox
            self._camoufox_cls = AsyncCamoufox
            await self._start_with_camoufox()
            camoufox_succeeded = True
        except Exception as e:
            print(f"⚠️ Camoufox 启动失败: {e}")
            print("⚠️ 将回退到 Playwright Firefox...")
            # 清理可能的部分资源
            if hasattr(self, '_camoufox'):
                try:
                    await self._camoufox.__aexit__(None, None, None)
                except:
                    pass
            camoufox_succeeded = False

        # 如果 Camoufox 失败，回退到 Playwright
        if not camoufox_succeeded:
            await self._start_with_playwright()

        # 执行登录
        auth = AuthHandler(self.page)
        self.logged_in = await auth.login(self.email, self.password)

        if self.logged_in:
            print("🎉 登录成功！浏览器会话已建立。")
        else:
            print("⚠️ 登录可能未完成，但浏览器保持运行。请检查 /screenshot 端点。")

    async def _start_with_camoufox(self):
        """使用 Camoufox 启动浏览器。"""
        print("  → 使用 Camoufox 反指纹浏览器...")

        from camoufox.async_api import AsyncCamoufox

        # Camoufox 启动参数
        self._camoufox = AsyncCamoufox(
            headless=True,
            geoip=False,  # 云环境中禁用 GeoIP
        )
        self.browser = await self._camoufox.__aenter__()

        # 创建上下文和页面
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        self.page = await self.context.new_page()

        # 注入反检测脚本
        await self._inject_stealth_scripts()
        print("  ✅ Camoufox 浏览器已启动。")

    async def _start_with_playwright(self):
        """回退方案：使用标准 Playwright Firefox。"""
        print("  → 回退到 Playwright Firefox...")

        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()

        # 检查 Firefox 可执行文件是否存在
        try:
            # 尝试直接启动
            self.browser = await self.playwright.firefox.launch(
                headless=True,
                args=["--no-sandbox"],
            )
        except Exception as launch_error:
            print(f"  ⚠️ Firefox 启动失败: {launch_error}")
            print("  🔄 尝试自动安装 Firefox...")

            # 自动安装 Playwright Firefox
            import subprocess
            env = os.environ.copy()
            result = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "firefox"],
                capture_output=True,
                text=True,
                env=env,
                timeout=120
            )
            print(f"  安装输出: {result.stdout}")
            if result.stderr:
                print(f"  安装错误: {result.stderr}")

            if result.returncode != 0:
                raise RuntimeError(f"Playwright Firefox 安装失败: {result.stderr}")

            # 安装完成后重试启动
            self.browser = await self.playwright.firefox.launch(
                headless=True,
                args=["--no-sandbox"],
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
        await self._inject_stealth_scripts()
        print("  ✅ Playwright Firefox 已启动。")

    async def _inject_stealth_scripts(self):
        """注入反检测 JavaScript 脚本。"""
        stealth_js = """
        // 隐藏 webdriver 标志
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        
        // 伪造 plugins
        Object.defineProperty(navigator, 'plugins', {
            get: () => [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                { name: 'Native Client', filename: 'internal-nacl-plugin' }
            ]
        });
        
        // 伪造 languages
        Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] });
        
        // 隐藏自动化相关属性
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
        delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
        
        // 伪造 chrome 对象
        if (!window.chrome) {
            window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
        }
        
        // 覆盖 permissions query
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
        );

        console.log('[Stealth] 反检测脚本已注入');
        """
        await self.context.add_init_script(stealth_js)

    async def is_alive(self) -> bool:
        """检查浏览器是否仍然存活。"""
        try:
            if not self.page or self.page.is_closed():
                return False
            await self.page.evaluate("() => document.title")
            return True
        except Exception:
            return False

    async def get_status(self) -> dict:
        """获取当前状态信息。"""
        alive = await self.is_alive()
        return {
            "browser_alive": alive,
            "logged_in": self.logged_in,
            "uptime_seconds": time.time() - self.start_time,
            "heartbeat_count": self.heartbeat_count,
            "requests_handled": self.requests_handled,
            "current_url": self.page.url if self.page and not self.page.is_closed() else "N/A",
            "timestamp": datetime.now().isoformat(),
        }

    async def take_screenshot_base64(self) -> Optional[str]:
        """截取当前页面截图，返回 Base64 字符串。"""
        try:
            if not self.page or self.page.is_closed():
                return None
            screenshot_bytes = await self.page.screenshot(full_page=False)
            return base64.b64encode(screenshot_bytes).decode("utf-8")
        except Exception as e:
            print(f"❌ 截图失败: {e}")
            return None

    async def send_message(self, message: str) -> str:
        """发送消息并等待完整响应（非流式）。"""
        full_response = ""
        async for chunk in self.send_message_stream(message):
            full_response += chunk
        return full_response

    async def send_message_stream(self, message: str) -> AsyncGenerator[str, None]:
        """
        发送消息并流式返回响应。
        """
        async with self._lock:
            self.requests_handled += 1
            print(f"📨 处理第 {self.requests_handled} 个请求: {message[:50]}...")

            try:
                if "chat.deepseek.com" not in self.page.url:
                    await self.page.goto("https://chat.deepseek.com/", wait_until="networkidle", timeout=30000)
                    await asyncio.sleep(2)

                try:
                    new_chat_btn = self.page.locator("div.ds-icon-button, [class*='new-chat']").first
                    if await new_chat_btn.is_visible(timeout=2000):
                        await new_chat_btn.click()
                        await asyncio.sleep(1)
                except Exception:
                    pass

                textarea = self.page.locator("textarea, [contenteditable='true'], #chat-input").first
                await textarea.wait_for(state="visible", timeout=10000)
                await textarea.click()
                await asyncio.sleep(0.3)

                await textarea.fill(message)
                await asyncio.sleep(0.5)

                send_btn = self.page.locator(
                    "div[class*='send'], button[class*='send'], "
                    "[data-testid='send-button'], "
                    "div.ds-icon-button[role='button']"
                ).last

                if await send_btn.is_visible(timeout=3000):
                    await send_btn.click()
                else:
                    await textarea.press("Enter")

                print("  → 消息已发送，等待响应...")
                await asyncio.sleep(1)

                last_text = ""
                stable_count = 0
                max_wait_seconds = 120

                for _ in range(max_wait_seconds * 2):
                    await asyncio.sleep(0.5)

                    current_text = await self.page.evaluate("""
                        () => {
                            const selectors = [
                                '.ds-markdown.ds-markdown--block',
                                '[class*="message-content"]',
                                '[class*="assistant"]',
                                '.markdown-body'
                            ];
                            for (const sel of selectors) {
                                const elements = document.querySelectorAll(sel);
                                if (elements.length > 0) {
                                    const lastEl = elements[elements.length - 1];
                                    return lastEl.textContent || '';
                                }
                            }
                            return '';
                        }
                    """)

                    if current_text and len(current_text) > len(last_text):
                        new_part = current_text[len(last_text):]
                        last_text = current_text
                        stable_count = 0
                        yield new_part
                    elif current_text and current_text == last_text:
                        stable_count += 1
                        is_generating = await self.page.evaluate("""
                            () => {
                                const loadingEls = document.querySelectorAll(
                                    '[class*="loading"], [class*="generating"], ' +
                                    '[class*="thinking"], .ds-loading'
                                );
                                return loadingEls.length > 0;
                            }
                        """)
                        if not is_generating and stable_count >= 6:
                            print("  ✅ 响应完成。")
                            break

                    if stable_count >= 20:
                        print("  ⏹️ 响应超时（文本无变化）。")
                        break

                if not last_text:
                    yield "抱歉，未能获取到响应。请稍后重试。"

            except Exception as e:
                error_msg = f"发送消息时出错: {str(e)}"
                print(f"  ❌ {error_msg}")
                screenshot = await self.take_screenshot_base64()
                if screenshot:
                    print(f"  📸 错误截图已保存（可通过 /screenshot 端点查看）")
                yield f"[错误] {error_msg}"

    async def simulate_activity(self):
        """模拟用户活动，保持会话活跃。"""
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
                    document.dispatchEvent(new Event('visibilitychange'));
                    console.log('[Keepalive] 心跳 - ' + new Date().toISOString());
                }
            """)

            if self.heartbeat_count % 10 == 0:
                print(f"💓 心跳 #{self.heartbeat_count} - 页面: {self.page.url[:60]}...")

        except Exception as e:
            print(f"⚠️ 心跳异常: {e}")

    async def shutdown(self):
        """安全关闭浏览器。"""
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            print("🔒 浏览器已安全关闭。")
        except Exception as e:
            print(f"⚠️ 关闭浏览器时出错: {e}")
