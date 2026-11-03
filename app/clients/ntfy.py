"""ntfy 推送客户端。

通过 ntfy (https://ntfy.sh) 发送通知到手机/桌面。
不同类型的通知使用不同的 topic，以 topic_prefix 为前缀。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import requests

from app.core.settings import settings


class NtfyError(Exception):
    """ntfy 调用统一异常。"""


class NtfyClient:
    """对 ntfy HTTP API 的轻量封装。"""

    def __init__(self, base_url: str, topic_prefix: str):
        if not base_url:
            raise NtfyError("未配置 ntfy 服务地址 (ntfy_base_url)")
        self.base_url = base_url.rstrip("/")
        self.topic_prefix = topic_prefix

    def _full_topic(self, topic: str) -> str:
        """拼接完整 topic 名称。"""
        if topic.startswith(self.topic_prefix):
            return topic
        return f"{self.topic_prefix}{topic}"

    @staticmethod
    def _priority_value(priority: str) -> int:
        """将优先级字符串映射为数值。"""
        mapping = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}
        return mapping.get(priority, 3)

    def send(
        self,
        topic: str,
        title: str,
        message: str,
        *,
        priority: Optional[str] = None,
        tags: Optional[list[str]] = None,
        click_url: Optional[str] = None,
        actions: Optional[list[dict]] = None,
    ) -> Dict[str, Any]:
        """发送通知到指定 topic。

        Args:
            topic: topic 名称（会自动加前缀）
            title: 通知标题
            message: 通知内容
            priority: 优先级 (min/low/default/high/urgent)
            tags: 标签列表，支持 emoji 短码
            click_url: 点击通知时打开的链接
            actions: 通知上的按钮，ntfy JSON 格式，如
                [{"action": "view", "label": "…", "url": "…", "clear": True}]
        """
        full_topic = self._full_topic(topic)
        url = f"{self.base_url}"

        payload: Dict[str, Any] = {
            "topic": full_topic,
            "title": title,
            "message": message,
        }
        if priority:
            payload["priority"] = self._priority_value(priority)
        if tags:
            payload["tags"] = tags
        if click_url:
            payload["click"] = click_url
        if actions:
            payload["actions"] = actions

        response = requests.post(
            url,
            json=payload,
            timeout=15,
        )
        if response.status_code >= 400:
            raise NtfyError(
                f"ntfy 推送失败: {response.status_code} - {response.text}"
            )
        return response.json()


_ntfy_client: NtfyClient | None = None


def get_ntfy_client() -> NtfyClient:
    """返回全局复用的 NtfyClient。"""

    global _ntfy_client
    if _ntfy_client is None:
        _ntfy_client = NtfyClient(settings.ntfy_base_url, settings.ntfy_topic_prefix)
    return _ntfy_client
