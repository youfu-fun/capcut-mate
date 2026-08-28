import json
from pydantic import BaseModel, Field, field_validator
from typing import List


class AddAudiosRequest(BaseModel):
    """批量添加音频请求参数"""
    draft_url: str = Field(..., description="草稿URL")
    audio_infos: str = Field(..., description="音频信息列表, 用JSON字符串表示")

    @field_validator("audio_infos")
    @classmethod
    def validate_audio_infos_sources(cls, value: str) -> str:
        """支持 HTTP 音频和剪映/CapCut 内置资源 ID 两种来源。"""
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"audio_infos JSON parse error: {exc.msg}") from exc

        if not isinstance(data, list):
            raise ValueError("audio_infos should be a list")

        for idx, item in enumerate(data):
            if not isinstance(item, dict):
                raise ValueError(f"audio_infos[{idx}] should be an object")

            audio_url = item.get("audio_url")
            metadata = item.get("resource_metadata")
            if metadata is not None and not isinstance(metadata, dict):
                raise ValueError(f"audio_infos[{idx}].resource_metadata should be an object")
            metadata = metadata or {}
            resource_id = (
                item.get("resource_id")
                or item.get("music_id")
                or item.get("effect_id")
                or metadata.get("music_id")
                or metadata.get("effect_id")
                or metadata.get("resource_id")
            )
            source_type = item.get("source_type")
            resource_mode = source_type in ("capcut_resource", "jianying_resource") or bool(resource_id)

            if resource_mode:
                if not isinstance(resource_id, str) or not resource_id.strip():
                    raise ValueError(f"audio_infos[{idx}].resource_id is required")
                duration = item.get("duration", metadata.get("duration"))
                if not isinstance(duration, (int, float)) or duration <= 0:
                    raise ValueError(f"audio_infos[{idx}].duration must be greater than 0")
            elif not isinstance(audio_url, str) or not audio_url.startswith(("http://", "https://")):
                raise ValueError(f"audio_infos[{idx}].audio_url must start with http:// or https://")
        return value


class AddAudiosResponse(BaseModel):
    """添加音频响应参数"""
    draft_url: str = Field(default="", description="草稿URL")
    track_id: str = Field(default="", description="音频轨道ID")
    audio_ids: List[str] = Field(default=[], description="音频ID列表")
