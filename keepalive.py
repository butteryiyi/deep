"""
心跳保活服务：
定期模拟用户活动，防止会话过期或被检测为非活跃。
"""

import asyncio
from browser_manager import BrowserManager


class KeepaliveService:
    def __init__(self, browser_mgr: BrowserManager, interval: int = 30):
        """
        Args:
            browser_mgr: 浏览器管理器实例
            interval: 心跳间隔（秒），默认 30 秒
        """
        self.browser_mgr = browser_mgr
        self.interval = interval
        self._task: asyncio.Task = None
        self._running = False
        self.is_running = False  # 对外暴露的运行状态

    async def start(self):
        """启动心跳循环。如果已经在运行，则忽略。"""
        if self.is_running:
            return
        self._running = True
        self.is_running = True
        self._task = asyncio.create_task(self._heartbeat_loop())
        print(f"💓 心跳服务已启动（间隔: {self.interval}s）")

    async def stop(self):
        """停止心跳循环。"""
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
        """心跳主循环。"""
        while self._running:
            try:
                await asyncio.sleep(self.interval)
                # 只有浏览器就绪时才执行模拟活动
                if self._running and self.browser_mgr and await self.browser_mgr.is_alive():
                    await self.browser_mgr.simulate_activity()
                # 如果浏览器未就绪，则静默跳过
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"⚠️ 心跳循环异常: {e}")
                await asyncio.sleep(5)  # 出错后短暂等待再重试
