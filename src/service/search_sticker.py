from src.utils.logger import logger
from typing import Any, Dict, List, Literal
import re
import unicodedata
from src.utils.sticker_catalog import load_sticker_catalog


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", normalized)


def search_sticker(
    keyword: str = "",
    *,
    keywords: List[str] | None = None,
    match_mode: Literal["all", "any"] = "all",
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    搜索贴纸的业务逻辑
    
    Args:
        keyword: 搜索关键词
        
    Returns:
        List[Dict[str, Any]]: 贴纸数据列表，最多返回50条记录
    """
    # keywords 是新版组合查询的权威输入；keyword 仅用于兼容旧客户端。不能把
    # 展示用的 "猫 跳舞" 再作为第三个 AND 条件，否则会错误要求标题连续出现。
    raw_terms = list(keywords) if keywords else [keyword]
    terms = list(dict.fromkeys(_normalize(item) for item in raw_terms if _normalize(item)))
    logger.info(f"Searching stickers with terms={terms}, mode={match_mode}, limit={limit}")
    if not terms:
        return []
    
    # 搜索与草稿写入共用同一目录，避免“能搜到但无法落到草稿”。
    try:
        sticker_data = load_sticker_catalog()
    except Exception as e:
        logger.error(f"Failed to read sticker config file: {e}")
        return []
    
    scored: List[tuple[int, int, int, Dict[str, Any]]] = []
    for position, item in enumerate(sticker_data):
        aliases = item.get("search_aliases", [])
        searchable = " ".join(
            [str(item.get("title", "")), *(str(alias) for alias in aliases)]
        )
        title = _normalize(searchable)
        hits = sum(term in title for term in terms)
        matched = hits == len(terms) if match_mode == "all" else hits > 0
        if not matched:
            continue
        # 命中词越多越优先；同分时短标题通常语义更聚焦，最后保持原始稳定顺序。
        scored.append((-hits, len(title), position, item))

    scored.sort(key=lambda item: item[:3])
    result = [item[-1] for item in scored[: max(1, min(limit, 200))]]
    logger.info(f"Found {len(result)} stickers matching terms={terms}")
    return result
