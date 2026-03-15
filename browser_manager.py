# browser_manager.py
"""
DeepSeek 反代（纯 API 版，无 UI 回退）
- Playwright 登录拿 Token
- 浏览器内 fetch 探测真实 API 路径
- 全部走 HTTP，释放浏览器
"""

import os
import sys
import json
import time
import asyncio
import base64
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

    async def initialize(self):
        print("🔧 正在初始化...")

        if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
            if os.path.isdir("/opt/browsers"):
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/browsers"

        await self._login_with_browser()

        if not self.logged_in:
            raise RuntimeError("❌ 登录失败，无法继续。")

        # 在浏览器同源环境下探测 API
        await self._probe_api_in_browser()
        # 提取凭据
        await self._extract_credentials()

        if not self._token:
            raise RuntimeError("❌ 无法提取 Token，无法继续。")
        if not self._api_base:
            raise RuntimeError("❌ 无法确定 API 路径，无法继续。")

        # 关闭浏览器
        await self._close_browser()

        # 创建 HTTP 客户端
        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=10.0),
            headers=self._build_headers(),
            cookies=self._cookies,
        )

        ok = await self._verify_api()
        if ok:
            print("🎉 API 验证通过，纯 HTTP 模式已就绪。")
        else:
            raise RuntimeError("❌ API 验证失败。请检查日志。")

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

    async def _probe_api_in_browser(self):
        """
        在浏览器里用 fetch 探测真实 API 路径。
        因为浏览器已经登录，fetch 自动带 Cookie，不会有 CORS 问题。
        """
        print("  → 在浏览器内探测 API 路径...")

        result = await self.page.evaluate("""
            async () => {
                const results = {};
                
                // 候选 API base 路径
                const bases = [
                    '/api/v0',
                    '/api/v1', 
                    '/api',
                    '/v0',
                    '/v1',
                ];
                
                // 候选端点（GET 类）
                const getEndpoints = [
                    '/chat/list_session?count=1&offset=0',
                    '/chat/session/list?count=1&offset=0',
                    '/session/list?count=1&offset=0',
                    '/user/info',
                    '/user/current',
                    '/chat/history',
                ];
                
                // 候选端点（POST 类）
                const postEndpoints = [
                    {path: '/chat/create_session', body: {agent: 'chat'}},
                    {path: '/chat/session/create', body: {agent: 'chat'}},
                ];
                
                // 先尝试 GET
                for (const base of bases) {
                    for (const ep of getEndpoints) {
                        const url = base + ep;
                        try {
                            const resp = await fetch(url, {
                                method: 'GET',
                                credentials: 'include',
                            });
                            const status = resp.status;
                            let body = '';
                            try { body = await resp.text(); } catch(e) {}
                            
                            results[`GET ${url}`] = {status, body: body.substring(0, 300)};
                            
                            if (status === 200) {
                                return {
                                    success: true, 
                                    api_base: base, 
                                    method: 'GET',
                                    endpoint: url, 
                                    status, 
                                    body: body.substring(0, 500),
                                    all: results
                                };
                            }
                        } catch(e) {
                            results[`GET ${url}`] = {error: e.message};
                        }
                    }
                }
                
                // 再尝试 POST
                for (const base of bases) {
                    for (const ep of postEndpoints) {
                        const url = base + ep.path;
                        try {
                            const resp = await fetch(url, {
                                method: 'POST',
                                credentials: 'include',
                                headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify(ep.body),
                            });
                            const status = resp.status;
                            let body = '';
                            try { body = await resp.text(); } catch(e) {}
                            
                            results[`POST ${url}`] = {status, body: body.substring(0, 300)};
                            
                            if (status === 200) {
                                return {
                                    success: true, 
                                    api_base: base, 
                                    method: 'POST',
                                    endpoint: url, 
                                    status, 
                                    body: body.substring(0, 500),
                                    all: results
                                };
                            }
                        } catch(e) {
                            results[`POST ${url}`] = {error: e.message};
                        }
                    }
                }
                
                return {success: false, all: results};
            }
        """)

        print(f"  → 探测结果:")
        if result.get("success"):
            self._api_base = f"https://chat.deepseek.com{result['api_base']}"
            print(f"  ✅ 找到有效 API base: {self._api_base}")
            print(f"     命中: {result['method']} {result['endpoint']} → {result['status']}")
            print(f"     响应: {result['body'][:200]}")
        else:
            # 打印所有尝试结果用于调试
            all_results = result.get("all", {})
            for url_key, res in all_results.items():
                status = res.get("status", "ERR")
                body = res.get("body", res.get("error", ""))[:100]
                print(f"    {url_key}: {status} → {body}")

            # 最后一招：拦截页面上按钮触发的请求
            print("  → 所有常规路径都失败，尝试拦截页面真实请求...")
            await self._sniff_by_interaction()

    async def _sniff_by_interaction(self):
        """在页面上触发操作，拦截实际发出的 API 请求"""
        captured = []

        async def capture(request):
            url = request.url
            if "deepseek.com" in url and request.resource_type in ("fetch", "xhr"):
                captured.append({
                    "url": url,
                    "method": request.method,
                    "headers": dict(request.headers),
                })

        self.page.on("request", capture)

        # 尝试点击新对话按钮或任何能触发 API 的操作
        try:
            # 等一下让页面自然加载触发请求
            await asyncio.sleep(3)

            # 尝试点击侧边栏之类的
            try:
                btn = self.page.locator("xpath=//*[contains(text(), '开启新对话')]").first
                if await btn.is_visible(timeout=3000):
                    await btn.click()
                    await asyncio.sleep(2)
            except Exception:
                pass

            # 再等等
            await asyncio.sleep(2)
        finally:
            self.page.remove_listener("request", capture)

        print(f"  → 拦截到 {len(captured)} 个请求:")
        for req in captured:
            print(f"    {req['method']} {req['url']}")
            auth_h = req["headers"].get("authorization", "")
            if auth_h:
                print(f"      Auth: {auth_h[:60]}...")
                if auth_h.startswith("Bearer ") and not self._token:
                    self._token = auth_h[7:]

            # 提取 API base
            if "/api/" in req["url"] and not self._api_base:
                import re
                m = re.search(r"(https://[^/]+/api/v\d+)", req["url"])
                if m:
                    self._api_base = m.group(1)
                    print(f"  ✅ 从拦截请求中提取到 API base: {self._api_base}")
                else:
                    m2 = re.search(r"(https://[^/]+/api)", req["url"])
                    if m2:
                        self._api_base = m2.group(1)
                        print(f"  ✅ 从拦截请求中提取到 API base: {self._api_base}")

            # 提取额外 headers
            for key in ["x-app-version", "x-client-locale", "x-client-platform",
                         "x-client-version", "x-ds-pow-response", "x-request-id"]:
                if key in req["headers"] and key not in self._extra_headers:
                    self._extra_headers[key] = req["headers"][key]

    async def _extract_credentials(self):
        print("  → 提取登录凭据...")

        # Cookies
        raw_cookies = await self.context.cookies()
        for c in raw_cookies:
            self._cookies[c["name"]] = c["value"]
        print(f"  ✅ 提取到 {len(self._cookies)} 个 Cookie")

        # 如果嗅探已经拿到 token 就跳过
        if self._token:
            print(f"  ✅ Token 已通过请求拦截获得: {self._token[:30]}...")
            return

        # 从 localStorage 正确解析 token
        try:
            token = await self.page.evaluate("""
                () => {
                    function extractValue(raw) {
                        if (!raw) return null;
                        try {
                            const parsed = JSON.parse(raw);
                            if (parsed && typeof parsed.value === 'string') {
                                return parsed.value;
                            }
                            if (typeof parsed === 'string') {
                                return parsed;
                            }
                        } catch(e) {}
                        return raw;
                    }
                    
                    // 按优先级查找
                    const keys = ['userToken', 'ds_token', 'token', '_token', 'auth_token'];
                    for (const key of keys) {
                        const raw = localStorage.getItem(key);
                        if (raw) {
                            const val = extractValue(raw);
                            if (val && val.length > 20) return val;
                        }
                    }
                    
                    // 列出所有 localStorage keys 用于调试
                    const allKeys = [];
                    for (let i = 0; i < localStorage.length; i++) {
                        const k = localStorage.key(i);
                        const v = localStorage.getItem(k);
                        allKeys.push({key: k, preview: (v||'').substring(0, 80)});
                    }
                    return JSON.stringify({_debug_all_keys: allKeys});
                }
            """)

            if token and not token.startswith('{"_debug'):
                self._token = token.strip()
                print(f"  ✅ 从 localStorage 提取到 Token: {self._token[:30]}...")
            else:
                # 打印调试信息
                print(f"  ⚠️ localStorage 调试信息:")
                try:
                    debug = json.loads(token)
                    for item in debug.get("_debug_all_keys", []):
                        print(f"    {item['key']}: {item['preview']}")
                except Exception:
                    print(f"    {token[:300]}")
        except Exception as e:
            print(f"  ⚠️ Token 提取异常: {e}")

        # 从 cookies 找 token
        if not self._token:
            for name in ["ds_token", "token", "sessionToken", "ds_session_id"]:
                if name in self._cookies:
                    self._token = self._cookies[name]
                    print(f"  ✅ 使用 Cookie '{name}' 作为 Token: {self._token[:30]}...")
                    break

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

        # 合并拦截到的真实请求头
        headers.update(self._extra_headers)
        return headers

    async def _verify_api(self) -> bool:
        """验证 API 是否可用"""
        # 用探测到的 base 尝试一个简单请求
        endpoints_to_try = []
        if self._api_base:
            endpoints_to_try.append(f"{self._api_base}/chat/create_session")

        for endpoint in endpoints_to_try:
            try:
                resp = await self._http_client.post(
                    endpoint, json={"agent": "chat"}
                )
                print(f"  → 验证 POST {endpoint}: {resp.status_code}")
                if resp.status_code == 200:
                    data = resp.json()
                    print(f"    响应: {json.dumps(data, ensure_ascii=False)[:200]}")
                    # 如果创建成功，删掉测试会话
                    try:
                        sid = data.get("data", {}).get("biz_data", {}).get("id")
                        if sid:
                            await self._http_client.post(
                                f"{self._api_base}/chat/delete_session",
                                json={"chat_session_id": sid}
                            )
                    except Exception:
                        pass
                    return data.get("code") == 0
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
            print("  🔒 浏览器已关闭，内存已释放。")
        except Exception as e:
            print(f"  ⚠️ 关闭浏览器出错: {e}")

    # ─── 对外接口 ───────────────────────────────────────

    async def is_alive(self) -> bool:
        if not self._http_client:
            return False
        try:
            resp = await self._http_client.post(
                f"{self._api_base}/chat/create_session",
                json={"agent": "chat"}
            )
            if resp.status_code == 200:
                data = resp.json()
                try:
                    sid = data.get("data", {}).get("biz_data", {}).get("id")
                    if sid:
                        await self._http_client.post(
                            f"{self._api_base}/chat/delete_session",
                            json={"chat_session_id": sid}
                        )
                except Exception:
                    pass
                return data.get("code") == 0
            return False
        except Exception:
            return False

    async def get_status(self) -> dict:
        return {
            "logged_in": self.logged_in,
            "mode": "http-api",
            "api_base": self._api_base,
            "has_token": bool(self._token),
            "cookie_count": len(self._cookies),
            "uptime_seconds": time.time() - self.start_time,
            "total_requests": self.total_requests,
            "timestamp": datetime.now().isoformat(),
        }

    async def take_screenshot_base64(self) -> Optional[str]:
        return None  # 纯 API 模式无截图

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
            # 创建会话
            create_resp = await self._http_client.post(
                f"{self._api_base}/chat/create_session",
                json={"agent": "chat"},
            )
            if create_resp.status_code != 200:
                yield f"[错误] 创建会话失败: {create_resp.status_code} {create_resp.text[:200]}"
                return

            session_data = create_resp.json()
            if session_data.get("code") != 0:
                yield f"[错误] {json.dumps(session_data, ensure_ascii=False)[:200]}"
                return

            chat_session_id = session_data["data"]["biz_data"]["id"]
            print(f"  [{req_id}] 会话: {chat_session_id}")

            payload = {
                "chat_session_id": chat_session_id,
                "parent_message_id": 0,
                "prompt": message,
                "ref_file_ids": [],
                "thinking_enabled": False,
                "search_enabled": False,
            }

            full_text = ""
            async with self._http_client.stream(
                "POST", f"{self._api_base}/chat/completion", json=payload,
            ) as resp:
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

            # 清理会话
            try:
                await self._http_client.post(
                    f"{self._api_base}/chat/delete_session",
                    json={"chat_session_id": chat_session_id},
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
        self.heartbeat_count += 1
        # 纯 API 模式不需要频繁心跳

    async def shutdown(self):
        try:
            if self._http_client:
                await self._http_client.aclose()
            print("🔒 已安全关闭。")
        except Exception as e:
            print(f"⚠️ 关闭出错: {e}")
