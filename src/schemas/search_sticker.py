from typing import Any, List, Literal

from pydantic import BaseModel, Field, model_validator


class StickerPackage(BaseModel):
    """贴纸包信息"""
    height_per_frame: int = Field(..., description="每帧高度")
    size: int = Field(..., description="贴纸包大小")
    width_per_frame: int = Field(..., description="每帧宽度")


class LargeImage(BaseModel):
    """大图信息"""
    image_url: str = Field(..., description="图片URL")


class StickerInfo(BaseModel):
    """贴纸信息"""
    large_image: LargeImage = Field(..., description="大图信息")
    preview_cover: str = Field(..., description="预览封面")
    sticker_package: StickerPackage = Field(..., description="贴纸包信息")
    sticker_type: int = Field(..., description="贴纸类型")
    track_thumbnail: str = Field(..., description="轨道缩略图")


class StickerItem(BaseModel):
    """贴纸项"""
    sticker: StickerInfo = Field(..., description="贴纸信息")
    sticker_id: str = Field(..., description="贴纸ID")
    title: str = Field(..., description="贴纸标题")
    search_aliases: List[str] = Field(default_factory=list, description="补录检索别名")
    catalog: dict[str, Any] = Field(default_factory=dict, description="资产验证元数据")


class SearchStickerRequest(BaseModel):
    """搜索贴纸请求参数"""
    keyword: str = Field(default="", description="兼容旧客户端的单关键词")
    keywords: List[str] = Field(default_factory=list, description="组合检索词")
    match_mode: Literal["all", "any"] = Field(default="all", description="组合词匹配模式")
    limit: int = Field(default=50, ge=1, le=200, description="最大返回数量")

    @model_validator(mode="after")
    def require_search_term(self):
        if not self.keyword.strip() and not any(item.strip() for item in self.keywords):
            raise ValueError("keyword 或 keywords 至少提供一个")
        return self


class SearchStickerResponse(BaseModel):
    """搜索贴纸响应参数"""
    data: List[StickerItem] = Field(..., description="贴纸数据列表")
