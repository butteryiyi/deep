"""
心跳保活服务：
- 定期模拟用户活动，防止会话过期
- 定期检查登录状态，失效时自动重新注入 Cookie
- 定期刷新页面，防止前端 SPA 状态丢失
"""

import time
import asyncio
from browser_manager import BrowserManager


class KeepaliveService:
    def __init__(
        self,
        browser_mgr: BrowserManager,
        interval: int = 30,
        login_check_interval: int = 180,   # 每 3 分钟检查一次登录状态
        page_refresh_interval: int = 1800,  # 每 30 分钟强制刷新一次空闲页面
    ):
        self.browser_mgr = browser_mgr
        self.interval = interval
        self.login_check_interval = login_check_interval
        self.page_refresh_interval = page_refresh_interval
        self._task: asyncio.Task = None
        self._running = False
        self.is_running = False

        self._last_login_check = 0.0
        self._last_page_refresh = 0.0
        self._login_check_failures = 0

    async def start(self):
        if self.is_running:
            return
        self._running = True
        self.is_running = True
        self._last_login_check = time.time()
        self._last_page_refresh = time.time()
        self._task = asyncio.create_task(self._heartbeat_loop())
        print(f"💓 心跳服务已启动（活动间隔: {self.interval}s, "
              f"登录检查间隔: {self.login_check_interval}s, "
              f"页面刷新间隔: {self.page_refresh_interval}s）")

    async def stop(self):
        self._running = False
        self.is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        print("💔 心跳服务已停止。")

    async def _heartbeat_loop(self):
        while self._running:
            try:
                await asyncio.sleep(self.interval)

                if not self._running or not self.browser_mgr:
                    continue

                if not await self.browser_mgr.is_alive():
                    continue

                # ── 1. 常规模拟活动（每次心跳都做） ──
                await self.browser_mgr.simulate_activity()

                now = time.time()

                # ── 2. 定期检查登录状态 ──
                if now - self._last_login_check >= self.login_check_interval:
                    self._last_login_check = now
                    await self._check_and_fix_login()

                # ── 3. 定期刷新空闲页面（防止前端 SPA 状态腐化） ──
                if now - self._last_page_refresh >= self.page_refresh_interval:
                    self._last_page_refresh = now
                    await self._refresh_idle_pages()

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"⚠️ 心跳循环异常: {e}")
                await asyncio.sleep(5)

    async def _check_and_fix_login(self):
        """检查登录状态，如果失效则自动重新登录"""
        try:
            is_logged_in = await self.browser_mgr.check_login_status()

            if is_logged_in:
                if self._login_check_failures > 0:
                    print(f"💓 登录状态已恢复正常")
                self._login_check_failures = 0
                return

            self._login_check_failures += 1
            print(f"⚠️ 检测到登录状态失效 "
                  f"(连续 {self._login_check_failures} 次)")

            # 第一次检测失败：先尝试简单刷新页面
            if self._login_check_failures == 1:
                print("  🔄 尝试刷新页面恢复...")
                refreshed = await self.browser_mgr.refresh_idle_pages()
                if refreshed:
                    await asyncio.sleep(3)
                    recheck = await self.browser_mgr.check_login_status()
                    if recheck:
                        print("  ✅ 刷新页面后登录状态恢复！")
                        self._login_check_failures = 0
                        return

            # 第二次及之后：重新注入 Cookie
            print("  🔄 刷新无效，开始重新注入 Cookie...")
            success = await self.browser_mgr.re_login()
            if success:
                self._login_check_failures = 0
                print("  ✅ 重新登录成功！")
            else:
                print(f"  ❌ 重新登录失败（第 {self._login_check_failures} 次），"
                      f"下次心跳将继续尝试")

        except Exception as e:
            print(f"⚠️ 登录状态检查异常: {e}")

    async def _refresh_idle_pages(self):
        """定期刷新空闲页面，防止前端状态腐化"""
        try:
            count = await self.browser_mgr.refresh_idle_pages()
            if count > 0:
                print(f"💓 已刷新 {count} 个空闲页面")
        except Exception as e:
            print(f"⚠️ 刷新空闲页面异常: {e}")
