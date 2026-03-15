# browser_manager.py
"""
DeepSeek 反代（纯 API 版）
- Playwright 登录
- 拦截浏览器真实 API 请求，提取路径/Token/Headers
- 全部走 HTTP，释放浏览器
"""

import os
import sys
import json
import time
import asyncio
import re
import httpx
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
        self.total_requests = 0

        self.email = os.getenv("DEEPSEEK_EMAIL", "")
        self.password = os.getenv("DEEPSEEK_PASSWORD", "")
        self.headless = os.getenv("HEADLESS", "true").lower() == "true"

        self._cookies: dict = {}
        self._token: str = ""
        self._api_base: str = ""
        self._http_client: Optional[httpx.AsyncClient] = None
        self._extra_headers: dict = {}
        # 存储完整的请求模板
        self._create_session_template: dict = {}
        self._completion_template: dict = {}

    async def initialize(self):
        print("🔧 正在初始化...")

        if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
            if os.path.isdir("/opt/browsers"):
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/browsers"

        await self._login_with_browser()

        if not self.logged_in:
            raise RuntimeError("❌ 登录失败。")

        # 核心：拦截浏览器真实请求
        await self._capture_real_api()
        await self._extract_credentials()

        if not self._api_base:
            raise RuntimeError("❌ 无法确定 API 路径。查看上方日志。")

        await self._close_browser()

        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=10.0),
            headers=self._build_headers(),
            cookies=self._cookies,
        )

        ok = await self._verify_api()
        if ok:
            print("🎉 API 验证通过，纯 HTTP 模式就绪。")
        else:
            raise RuntimeError("❌ API 验证失败。")

    async def _login_with_browser(self):
        print("  → 启动浏览器进行登录...")
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()

        try:
            self.browser = await self.playwright.firefox.launch(
                headless=self.headless, args=["--no-sandbox"]
            )
        except Exception:
            import subprocess
            env = os.environ.copy()
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "firefox"],
                capture_output=True, text=True, env=env, timeout=120,
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
        await self.context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        self.page = await self.context.new_page()

        auth = AuthHandler(self.page, context=self.context)
        self.logged_in = await auth.login(self.email, self.password)

    async def _capture_real_api(self):
        """
        拦截浏览器实际发出的所有 XHR/Fetch 请求。
        分三步：
        1. 监听页面自然加载时发的请求
        2. 在页面上发一条消息，捕获 create_session + completion 请求
        3. 从捕获结果中提取 API base、Token、Headers
        """
        print("  → 拦截浏览器真实 API 请求...")

        all_captured = []

        async def on_request(request):
            url = request.url
            # 只关注 XHR/Fetch 请求（排除静态资源）
            if request.resource_type in ("fetch", "xhr"):
                entry = {
                    "url": url,
                    "method": request.method,
                    "headers": dict(request.headers),
                    "post_data": request.post_data,
                }
                all_captured.append(entry)

        # 也监听 response 来获取更多信息
        responses = []

        async def on_response(response):
            url = response.url
            if response.request.resource_type in ("fetch", "xhr"):
                ct = response.headers.get("content-type", "")
                entry = {
                    "url": url,
                    "status": response.status,
                    "content_type": ct,
                    "method": response.request.method,
                }
                # 尝试获取响应体（不超时的情况下）
                try:
                    if "json" in ct and response.status == 200:
                        body = await response.text()
                        entry["body"] = body[:500]
                except Exception:
                    pass
                responses.append(entry)

        self.page.on("request", on_request)
        self.page.on("response", on_response)

        # ── 第一阶段：等待页面自然请求（加载会话列表等）──
        print("  → [1/2] 等待页面自然 API 请求...")
        await asyncio.sleep(5)

        print(f"  → 第一阶段捕获到 {len(all_captured)} 个请求, {len(responses)} 个响应")
        for r in responses:
            print(f"    {r['method']} {r['status']} {r['url'][:120]}")
            if r.get("body"):
                print(f"      body: {r['body'][:150]}")

        # 检查是否已经拿到了需要的信息
        self._analyze_captured(all_captured, responses)

        if self._api_base:
            print(f"  ✅ 第一阶段就找到了 API base: {self._api_base}")
            self.page.remove_listener("request", on_request)
            self.page.remove_listener("response", on_response)
            return

        # ── 第二阶段：主动发一条消息 ──
        print("  → [2/2] 在浏览器中发送测试消息...")
        try:
            # 先尝试点新对话按钮
            try:
                new_chat_selectors = [
                    "xpath=//*[contains(text(), '开启新对话')]",
                    "xpath=//*[contains(text(), 'New chat')]",
                    "xpath=//*[contains(text(), '新对话')]",
                    "[class*='new-chat']",
                    "[class*='newChat']",
                ]
                for sel in new_chat_selectors:
                    try:
                        btn = self.page.locator(sel).first
                        if await btn.is_visible(timeout=2000):
                            await btn.click()
                            await asyncio.sleep(1)
                            print(f"    点击了: {sel}")
                            break
                    except Exception:
                        continue
            except Exception:
                pass

            # 找到输入框
            textarea = None
            for sel in ["textarea", "[contenteditable='true']", "input[type='text']"]:
                try:
                    el = self.page.locator(sel).first
                    if await el.is_visible(timeout=3000):
                        textarea = el
                        break
                except Exception:
                    continue

            if textarea:
                await textarea.click()
                await textarea.fill("hi")
                await asyncio.sleep(0.3)
                await textarea.press("Enter")
                print("    ✅ 已发送 'hi'")

                # 等待响应完成
                await asyncio.sleep(8)
            else:
                print("    ⚠️ 未找到输入框")

        except Exception as e:
            print(f"    ⚠️ 发送消息失败: {e}")

        self.page.remove_listener("request", on_request)
        self.page.remove_listener("response", on_response)

        print(f"  → 总计捕获 {len(all_captured)} 个请求, {len(responses)} 个响应")
        print("  → 全部捕获的请求:")
        for req in all_captured:
            url = req["url"]
            method = req["method"]
            has_auth = "authorization" in req["headers"]
            pd = (req.get("post_data") or "")[:80]
            print(f"    {method} {url[:150]} auth={has_auth}")
            if pd:
                print(f"      body: {pd}")

        print("  → 全部捕获的响应:")
        for r in responses:
            print(f"    {r['method']} {r['status']} {r['url'][:150]}")
            if r.get("body"):
                print(f"      body: {r['body'][:200]}")

        # 分析
        self._analyze_captured(all_captured, responses)

    def _analyze_captured(self, requests: list, responses: list):
        """从捕获的请求/响应中提取 API 信息"""
        for req in requests:
            url = req["url"]
            method = req["method"]
            headers = req["headers"]

            # 提取 Authorization Token
            auth_h = headers.get("authorization", "")
            if auth_h.startswith("Bearer ") and not self._token:
                self._token = auth_h[7:]
                print(f"  ✅ 捕获到 Token: {self._token[:30]}...")

            # 提取 extra headers
            for key in ["x-app-version", "x-client-locale", "x-client-platform",
                         "x-client-version", "x-ds-pow-response", "x-request-id",
                         "x-client-timezone"]:
                if key in headers and key not in self._extra_headers:
                    self._extra_headers[key] = headers[key]

            # 提取 API base
            if "deepseek.com" in url:
                # 尝试从 URL 中提取 API base
                for pattern in [
                    r"(https://[^/]+/api/v\d+)",
                    r"(https://[^/]+/api)",
                ]:
                    m = re.search(pattern, url)
                    if m and not self._api_base:
                        self._api_base = m.group(1)
                        print(f"  ✅ API base (from URL pattern): {self._api_base}")

                # 保存特定端点的请求模板
                if "create_session" in url or "session" in url:
                    self._create_session_template = {
                        "url": url,
                        "method": method,
                        "headers": headers,
                        "post_data": req.get("post_data"),
                    }
                    print(f"  ✅ 保存 session 请求模板: {method} {url}")

                if "completion" in url:
                    self._completion_template = {
                        "url": url,
                        "method": method,
                        "headers": headers,
                        "post_data": req.get("post_data"),
                    }
                    print(f"  ✅ 保存 completion 请求模板: {method} {url}")

    async def _extract_credentials(self):
        print("  → 提取登录凭据...")

        raw_cookies = await self.context.cookies()
        for c in raw_cookies:
            self._cookies[c["name"]] = c["value"]
        print(f"  ✅ 提取到 {len(self._cookies)} 个 Cookie")

        if self._token:
            print(f"  ✅ Token 已有: {self._token[:30]}...")
            return

        try:
            token = await self.page.evaluate("""
                () => {
                    function extract(raw) {
                        if (!raw) return null;
                        try {
                            const p = JSON.parse(raw);
                            if (p && typeof p.value === 'string') return p.value;
                            if (typeof p === 'string') return p;
                        } catch(e) {}
                        return raw;
                    }
                    const keys = ['userToken', 'ds_token', 'token', '_token', 'auth_token'];
                    for (const k of keys) {
                        const r = localStorage.getItem(k);
                        if (r) {
                            const v = extract(r);
                            if (v && v.length > 20 && !v.startsWith('{') && !v.startsWith('<')) return v;
                        }
                    }
                    return null;
                }
            """)
            if token:
                self._token = token.strip()
                print(f"  ✅ Token: {self._token[:30]}...")
        except Exception as e:
            print(f"  ⚠️ Token 提取异常: {e}")

    def _build_headers(self) -> dict:
        headers = {
            "Accept": "text/event-stream",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://chat.deepseek.com",
            "Referer": "https://chat.deepseek.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
                "Gecko/20100101 Firefox/126.0"
            ),
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        headers.update(self._extra_headers)
        return headers

    async def _verify_api(self) -> bool:
        """用捕获到的真实请求模板来验证"""

        # 如果有 create_session 模板，直接用
        if self._create_session_template:
            tmpl = self._create_session_template
            url = tmpl["url"]
            method = tmpl["method"]
            print(f"  → 验证(模板): {method} {url}")

            try:
                if method == "POST":
                    body = tmpl.get("post_data")
                    if body and isinstance(body, str):
                        body = json.loads(body)
                    resp = await self._http_client.post(url, json=body or {"agent": "chat"})
                else:
                    resp = await self._http_client.request(method, url)

                print(f"    状态: {resp.status_code}")
                if resp.status_code == 200:
                    ct = resp.headers.get("content-type", "")
                    if "json" in ct:
                        data = resp.json()
                        print(f"    响应: {json.dumps(data, ensure_ascii=False)[:200]}")
                        return True
                else:
                    print(f"    响应: {resp.text[:200]}")
            except Exception as e:
                print(f"    异常: {e}")

        # 尝试用 api_base + 常规端点
        if self._api_base:
            for path in ["/chat/create_session", "/chat/session/create"]:
                url = f"{self._api_base}{path}"
                try:
                    resp = await self._http_client.post(url, json={"agent": "chat"})
                    print(f"  → 验证 POST {url}: {resp.status_code}")
                    if resp.status_code == 200:
                        ct = resp.headers.get("content-type", "")
                        if "json" in ct:
                            data = resp.json()
                            print(f"    响应: {json.dumps(data, ensure_ascii=False)[:200]}")
                            return True
                    else:
                        print(f"    响应: {resp.text[:200]}")
                except Exception as e:
                    print(f"    异常: {e}")

        return False

    async def _close_browser(self):
        try:
            if self.context:
                await self.context.close()
                self.context = None
            if self.browser:
                await self.browser.close()
                self.browser = None
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None
            self.page = None
            print("  🔒 浏览器已关闭。")
        except Exception as e:
            print(f"  ⚠️ 关闭浏览器出错: {e}")

    async def is_alive(self) -> bool:
        if not self._http_client:
            return False
        try:
            if self._create_session_template:
                url = self._create_session_template["url"]
                resp = await self._http_client.post(url, json={"agent": "chat"})
                return resp.status_code == 200
            return False
        except Exception:
            return False

    async def get_status(self) -> dict:
        return {
            "logged_in": self.logged_in,
            "mode": "http-api",
            "api_base": self._api_base,
            "has_token": bool(self._token),
            "has_session_template": bool(self._create_session_template),
            "has_completion_template": bool(self._completion_template),
            "cookie_count": len(self._cookies),
            "uptime_seconds": time.time() - self.start_time,
            "total_requests": self.total_requests,
            "timestamp": datetime.now().isoformat(),
        }

    async def take_screenshot_base64(self) -> Optional[str]:
        return None

    async def send_message(self, message: str) -> str:
        full = ""
        async for chunk in self.send_message_stream(message):
            full += chunk
        return full

    async def send_message_stream(self, message: str) -> AsyncGenerator[str, None]:
        self.total_requests += 1
        req_id = self.total_requests
        print(f"📨 请求 #{req_id} (长度: {len(message)} 字符)")

        if not self._http_client:
            yield "[错误] HTTP 客户端未初始化"
            return

        try:
            # ── 创建会话 ──
            if self._create_session_template:
                url = self._create_session_template["url"]
            else:
                url = f"{self._api_base}/chat/create_session"

            create_resp = await self._http_client.post(url, json={"agent": "chat"})
            if create_resp.status_code != 200:
                yield f"[错误] 创建会话失败: {create_resp.status_code} {create_resp.text[:200]}"
                return

            session_data = create_resp.json()
            if session_data.get("code") != 0:
                yield f"[错误] {json.dumps(session_data, ensure_ascii=False)[:200]}"
                return

            # 兼容不同响应结构
            biz = session_data.get("data", {})
            if "biz_data" in biz:
                chat_session_id = biz["biz_data"]["id"]
            elif "id" in biz:
                chat_session_id = biz["id"]
            else:
                chat_session_id = biz.get("chat_session_id", "")

            print(f"  [{req_id}] 会话: {chat_session_id}")

            # ── 发送消息 ──
            if self._completion_template:
                comp_url = self._completion_template["url"]
            else:
                comp_url = f"{self._api_base}/chat/completion"

            payload = {
                "chat_session_id": chat_session_id,
                "parent_message_id": 0,
                "prompt": message,
                "ref_file_ids": [],
                "thinking_enabled": False,
                "search_enabled": False,
            }

            full_text = ""
            async with self._http_client.stream("POST", comp_url, json=payload) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield f"[错误] {resp.status_code}: {body.decode()[:200]}"
                    return

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = data.get("choices", [])
                    if choices:
                        content = choices[0].get("delta", {}).get("content", "")
                        if content:
                            full_text += content
                            yield content

            # ── 清理 ──
            try:
                del_url = comp_url.replace("completion", "delete_session")
                await self._http_client.post(
                    del_url, json={"chat_session_id": chat_session_id}
                )
            except Exception:
                pass

            print(f"  [{req_id}] ✅ 完成，长度: {len(full_text)}")

        except httpx.ReadTimeout:
            yield "[错误] 响应超时"
        except Exception as e:
            print(f"  [{req_id}] ❌ {e}")
            import traceback
            traceback.print_exc()
            yield f"[错误] {str(e)}"

    async def simulate_activity(self):
        pass

    async def shutdown(self):
        try:
            if self._http_client:
                await self._http_client.aclose()
            print("🔒 已关闭。")
        except Exception as e:
            print(f"⚠️ {e}")
