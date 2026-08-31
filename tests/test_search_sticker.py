#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
测试 search_sticker 功能
"""

import sys
import os

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.service.search_sticker import search_sticker


def test_search_sticker():
    """测试搜索贴纸功能"""
    
    print("测试搜索贴纸功能")
    
    # 测试正常搜索
    print("\n1. 测试正常搜索 '梦幻':")
    result = search_sticker("梦幻")
    print(f"   找到 {len(result)} 条记录")
    if result:
        print(f"   第一条记录标题: {result[0]['title']}")
    
    # 测试搜索不到内容的情况
    print("\n2. 测试搜索不存在的关键词 '不存在的关键词':")
    result = search_sticker("不存在的关键词")
    print(f"   找到 {len(result)} 条记录（应该为0条，不再随机回退）")
    assert result == []
    
    # 测试空关键词
    print("\n3. 测试空关键词:")
    result = search_sticker("")
    assert result == []

    print("\n4. 测试组合关键词:")
    result = search_sticker(keywords=["猫", "跳舞"], match_mode="all", limit=200)
    assert result
    assert all("猫" in item["title"] and "跳舞" in item["title"] for item in result)
    
    print("\n测试完成!")


if __name__ == "__main__":
    test_search_sticker()
