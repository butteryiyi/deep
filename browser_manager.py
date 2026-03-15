# browser_manager.py
"""
浏览器生命周期管理器（修复版）：
核心修复：
1. Camoufox AsyncCamoufox 上下文管理器生命周期正确处理
2. 增加启动超时保护
3. 浏览器崩溃自动恢复
"""

import os
import sys
import time
import json
import asyncio
import base64
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
        self._lock = asyncio.Lock()
        self._camoufox_cm = None  # 保持上下文管理器引用，防止被 GC

        self.email = os.getenv("DEEPSEEK_EMAIL", "")
        self.password = os.getenv("DEEPSEEK_PASSWORD", "")
        self.headless = os.getenv("HEADLESS", "true").lower() == "true"
        self._engine = "unknown"

    def _check_camoufox_installed(self) -> bool:
        """检查 Camoufox 是否已预装"""
        try:
            from camoufox.pkgman import get_path
            path = get_path("camoufox")
            if path and Path(path).exists():
                print(f"  ✅ Camoufox 已预装: {path}")
                return True
        except Exception as e:
            print(f"  ⚠️ 检查 Camoufox 路径时出错: {e}")

        home_cache = Path.home() / ".cache" / "camoufox"
        if home_cache.exists() and any(home_cache.iterdir()):
            print(f"  ✅ 发现 Camoufox 缓存: {home_cache}")
            return True

        return False

    async def initialize(self):
        """初始化浏览器，带完整的错误处理和回退逻辑"""
        print("🔧 正在初始化浏览器...")

        os.environ['CAMOUFOX_NO_UPDATE_CHECK'] = '1'

        # 尝试 Camoufox
        camoufox_ok = False
        if self._check_camoufox_installed():
            print("  📦 使用预装的 Camoufox（跳过下载）")
            try:
                await asyncio.wait_for(self._start_with_camoufox(), timeout=60)
                camoufox_ok = True
                self._engine = "camoufox"
                print("  ✅ Camoufox 启动成功")
            except asyncio.TimeoutError:
                print("  ⚠️ Camoufox 启动超时（60秒），回退到 Playwright")
                await self._cleanup_camoufox()
            except Exception as e:
                print(f"  ⚠️ Camoufox 启动失败: {e}")
                await self._cleanup_camoufox()

        # 回退到 Playwright Firefox
        if not camoufox_ok:
            try:
                await asyncio.wait_for(self._start_with_playwright(), timeout=60)
                self._engine = "playwright-firefox"
                print("  ✅ Playwright Firefox 启动成功")
            except asyncio.TimeoutError:
                raise RuntimeError("Playwright Firefox 启动超时（60秒）")
            except Exception as e:
                raise RuntimeError(f"Playwright Firefox 启动失败: {e}")

        # 注入反检测脚本
        await self._inject_stealth_scripts()

        # Cookie 注入登录
        auth = AuthHandler(self.page, context=self.context)
        self.logged_in = await auth.login(self.email, self.password)

        if self.logged_in:
            print("🎉 登录成功！浏览器会话已建立。")
        else:
            print("⚠️ 登录可能未完成，请检查 /screenshot 端点。")

    async def _cleanup_camoufox(self):
        """安全清理 Camoufox 资源"""
        try:
            if self.page and not self.page.is_closed():
                await self.page.close()
        except Exception:
            pass
        try:
            if self.context:
                await self.context.close()
        except Exception:
            pass
        try:
            if self._camoufox_cm:
                await self._camoufox_cm.__aexit__(None, None, None)
        except Exception:
            pass
        self.page = None
        self.context = None
        self.browser = None
        self._camoufox_cm = None

    async def _start_with_camoufox(self):
        """使用 Camoufox 启动浏览器 —— 修复生命周期管理"""
        print("  → 使用 Camoufox 反指纹浏览器...")
        from camoufox.async_api import AsyncCamoufox

        # 创建上下文管理器并保持引用
        self._camoufox_cm = AsyncCamoufox(
            headless=self.headless,
            geoip=False,
        )

        # 进入上下文，获取 browser
        self.browser = await self._camoufox_cm.__aenter__()

        # 等一下让浏览器完全就绪
        await asyncio.sleep(2)

        # 检查浏览器是否真的活着
        if not self.browser.is_connected():
            raise RuntimeError("Camoufox browser 启动后立即断开连接")

        # 创建上下文和页面
        try:
            self.context = await self.browser.new_context(
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
            await asyncio.sleep(1)
            self.page = await self.context.new_page()
        except Exception as e:
            # 如果 new_context 或 new_page 失败，说明浏览器已经崩溃
            raise RuntimeError(f"Camoufox 创建页面失败: {e}")

        print("  ✅ Camoufox 浏览器已启动。")

    async def _start_with_playwright(self):
        """使用 Playwright Firefox 启动浏览器"""
        print("  → 回退到 Playwright Firefox...")
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()

        try:
            self.browser = await self.playwright.firefox.launch(
                headless=self.headless,
                args=["--no-sandbox"],
            )
        except Exception as launch_error:
            print(f"  ⚠️ Firefox 启动失败: {launch_error}，尝试安装...")
            import subprocess
            result = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "firefox"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Playwright Firefox 安装失败: {result.stderr}")
            self.browser = await self.playwright.firefox.launch(
                headless=self.headless,
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
        print("  ✅ Playwright Firefox 已启动。")

    async def _inject_stealth_scripts(self):
        """注入反检测脚本"""
        if self._engine == "camoufox":
            minimal_js = """
            if (navigator.webdriver !== undefined) {
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            }
            """
            await self.context.add_init_script(minimal_js)
            print("  🛡️ Camoufox 模式：使用最小化反检测脚本")
        else:
            firefox_stealth_js = """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { 
                get: () => ['zh-CN', 'zh', 'en-US', 'en'] 
            });
            const originalQuery = window.navigator.permissions.query;
            if (originalQuery) {
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({ state: Notification.permission }) :
                        originalQuery(parameters)
                );
            }
            """
            await self.context.add_init_script(firefox_stealth_js)
            print("  🛡️ Firefox 模式：注入兼容的反检测脚本")

    async def is_alive(self) -> bool:
        """检查浏览器是否存活"""
        try:
            if not self.page or self.page.is_closed():
                return False
            if not self.browser or not self.browser.is_connected():
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
            "engine": self._engine,
            "uptime_seconds": time.time() - self.start_time,
            "heartbeat_count": self.heartbeat_count,
            "requests_handled": self.requests_handled,
            "current_url": self.page.url if self.page and not self.page.is_closed() else "N/A",
            "timestamp": datetime.now().isoformat(),
        }

    async def take_screenshot_base64(self) -> Optional[str]:
        try:
            if not self.page or self.page.is_closed():
                return None
            screenshot_bytes = await self.page.screenshot(full_page=False)
            return base64.b64encode(screenshot_bytes).decode("utf-8")
        except Exception as e:
            print(f"❌ 截图失败: {e}")
            return None

    async def send_message(self, message: str) -> str:
        full_response = ""
        async for chunk in self.send_message_stream(message):
            full_response += chunk
        return full_response

    async def send_message_stream(self, message: str) -> AsyncGenerator[str, None]:
        async with self._lock:
            self.requests_handled += 1
            print(f"📨 处理第 {self.requests_handled} 个请求 (长度: {len(message)} 字符)")
            print(f"  → 内容预览: {message[:100]}...")

            try:
                # 确保在正确的页面上
                current_url = self.page.url if self.page else ""
                if "chat.deepseek.com" not in current_url:
                    await self.page.goto(
                        "https://chat.deepseek.com/",
                        wait_until="networkidle",
                        timeout=30000,
                    )
                    await asyncio.sleep(2)

                # 开启新对话
                print("  → 正在开启新的对话以保证上下文干净...")
                try:
                    new_chat_btn = self.page.locator(
                        "xpath=//*[contains(text(), '开启新对话')]"
                    ).first
                    await new_chat_btn.wait_for(state="visible", timeout=5000)
                    await new_chat_btn.click()
                    await asyncio.sleep(1)
                    print("  ✅ 已开启新对话")
                except Exception:
                    try:
                        icon_btn = self.page.locator(
                            "div.ds-icon-button, [class*='new-chat']"
                        ).first
                        if await icon_btn.is_visible(timeout=2000):
                            await icon_btn.click()
                            await asyncio.sleep(1)
                    except Exception:
                        pass

                # 输入消息
                textarea = self.page.locator(
                    "textarea[placeholder*='DeepSeek'], "
                    "textarea[placeholder*='发送消息'], "
                    "textarea, "
                    "[contenteditable='true']"
                ).first
                await textarea.wait_for(state="visible", timeout=10000)
                await textarea.click()
                await asyncio.sleep(0.3)

                await textarea.fill("")
                await asyncio.sleep(0.2)
                await textarea.fill(message)
                await asyncio.sleep(0.5)
                print(f"  → 已输入消息，长度: {len(message)}")

                # 发送
                await textarea.press("Enter")
                print("  → 消息已发送，等待模型响应...")
                await asyncio.sleep(2)

                # 流式读取响应
                last_text = ""
                stable_count = 0
                max_wait_seconds = 600
                response_started = False

                for tick in range(max_wait_seconds * 2):
                    await asyncio.sleep(0.5)

                    result = await self.page.evaluate("""
                        () => {
                            const items = document.querySelectorAll('div[data-virtual-list-item-key]');
                            if (items.length === 0) {
                                return { text: '', done: false, itemCount: 0, hasButton: false };
                            }
                            const lastItem = items[items.length - 1];
                            const mdEls = lastItem.querySelectorAll('[class*="ds-markdown"]');
                            let text = '';
                            if (mdEls.length > 0) {
                                text = mdEls[mdEls.length - 1].textContent || '';
                            }
                            const buttons = lastItem.querySelectorAll('div[role="button"]');
                            const hasButton = buttons.length > 0;
                            const stopBtn = document.querySelector('[class*="stop"], [class*="square"]');
                            const isGenerating = !!stopBtn;
                            return { 
                                text: text, 
                                done: hasButton && !isGenerating,
                                itemCount: items.length, 
                                hasButton: hasButton,
                                isGenerating: isGenerating
                            };
                        }
                    """)

                    current_text = result.get("text", "").strip()
                    is_done = result.get("done", False)
                    is_generating = result.get("isGenerating", False)

                    if current_text and not response_started:
                        response_started = True
                        print(f"  → 检测到回复开始（消息数: {result.get('itemCount')}）")

                    if response_started and current_text:
                        if len(current_text) > len(last_text):
                            new_part = current_text[len(last_text):]
                            last_text = current_text
                            stable_count = 0
                            yield new_part
                        elif current_text == last_text:
                            stable_count += 1

                        if is_done and stable_count >= 3:
                            print("  ✅ 响应完成（检测到复制按钮可用）。")
                            break

                    if tick > 0 and tick % 20 == 0:
                        print(
                            f"  ⏳ 等待中... tick={tick}, "
                            f"textLen={len(current_text)}, "
                            f"generating={is_generating}, "
                            f"hasButton={result.get('hasButton')}, "
                            f"stable={stable_count}"
                        )

                    if stable_count >= 60:
                        print("  ⏹️ 响应超时（文本 30 秒无变化）。")
                        break

                # 兜底
                if not last_text:
                    fallback_text = await self.page.evaluate("""
                        () => {
                            const allMd = document.querySelectorAll('[class*="ds-markdown"]');
                            if (allMd.length > 0) {
                                return allMd[allMd.length - 1].textContent || '';
                            }
                            return '';
                        }
                    """)
                    if fallback_text and fallback_text.strip():
                        print(f"  ⚠️ 使用兜底方式获取到回复，长度: {len(fallback_text)}")
                        yield fallback_text.strip()
                    else:
                        print("  ❌ 完全未能获取到响应")
                        yield "抱歉，未能获取到响应。请稍后重试。"

                print(f"  📊 最终回复长度: {len(last_text)} 字符")

            except Exception as e:
                error_msg = f"发送消息时出错: {str(e)}"
                print(f"  ❌ {error_msg}")
                import traceback
                traceback.print_exc()
                yield f"[错误] {error_msg}"

    async def simulate_activity(self):
        """心跳模拟活动"""
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
                print(f"💓 心跳 #{self.heartbeat_count} - 页面: {self.page.url[:60]}...")
        except Exception as e:
            print(f"⚠️ 心跳异常: {e}")

    async def shutdown(self):
        """安全关闭所有资源"""
        try:
            if self.page and not self.page.is_closed():
                await self.page.close()
        except Exception:
            pass

        try:
            if self.context:
                await self.context.close()
        except Exception:
            pass

        try:
            if self._camoufox_cm:
                await self._camoufox_cm.__aexit__(None, None, None)
                self._camoufox_cm = None
            elif self.browser:
                await self.browser.close()
        except Exception:
            pass

        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass

        print("🔒 浏览器已安全关闭。")
