from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.security import require_admin_token
from app.core.push import PushServiceError
from app.core import push as push_service

router = APIRouter(
    prefix="/push",
    tags=["ntfy"],
    dependencies=[Depends(require_admin_token)],
)


class PushMessageRequest(BaseModel):
    """发送推送的请求体。"""

    title: str
    body: str
    topic: str = Field(
        default="spider",
        description="ntfy topic 名称（不含前缀），如 nintendo / monitor / spider",
    )
    priority: Optional[str] = Field(
        default=None,
        description="通知优先级: min / low / default / high / urgent",
    )
    tags: Optional[str] = Field(
        default=None,
        description="逗号分隔的标签，支持 emoji 短码，如 'warning,loudspeaker'",
    )
    click_url: Optional[str] = Field(
        default=None,
        description="点击通知时打开的链接",
    )


@router.post("/send", summary="推送 ntfy 消息")
def send_push_message(payload: PushMessageRequest):
    try:
        tag_list = [t.strip() for t in payload.tags.split(",")] if payload.tags else None
        result = push_service.send_note(
            title=payload.title,
            body=payload.body,
            topic=payload.topic,
            priority=payload.priority,
            tags=tag_list,
            click_url=payload.click_url,
        )
        return {"message": "推送成功", "response": result}
    except PushServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
