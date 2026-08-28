# CapCut/剪映内置 BGM、音效资源 ID

`add_audios` 现在支持两种音频来源：

- `audio_url`：原有方式，下载实体音频后写入草稿。
- `resource_id`：直接引用剪映内置 BGM 或音效，不下载音频文件。

## BGM ID 示例

```json
{
  "draft_url": "http://127.0.0.1:30000/openapi/capcut-mate/v1/get_draft?draft_id=YOUR_DRAFT_ID",
  "audio_infos": "[{\"source_type\":\"capcut_resource\",\"resource_id\":\"YOUR_MUSIC_ID\",\"resource_kind\":\"music\",\"resource_name\":\"BGM 名称\",\"duration\":30000000,\"start\":0,\"end\":15000000,\"volume\":0.35}]"
}
```

也可以用简写字段 `music_id`：

```json
[{"music_id":"YOUR_MUSIC_ID","duration":30000000,"start":0,"end":15000000,"volume":0.35}]
```

## 音效 ID 示例

```json
[{"effect_id":"YOUR_EFFECT_ID","duration":2000000,"start":3500000,"end":4500000,"volume":1.0}]
```

`music_id` 自动识别为 `music`，`effect_id` 自动识别为 `sound_effect`。所有时间单位均为微秒，ID 模式必须传素材总时长 `duration`。

## 兼容不同剪映版本

剪映不同版本的 `materials.audios` 可能有附加字段。接口支持把真实草稿中的单条音频素材对象原样放进 `resource_metadata`：

```json
[
  {
    "resource_id": "YOUR_MUSIC_ID",
    "resource_kind": "music",
    "duration": 30000000,
    "start": 0,
    "end": 15000000,
    "resource_metadata": {
      "category_id": "...",
      "category_name": "...",
      "name": "BGM 名称"
    }
  }
]
```

CapCut Mate 会保留扩展字段，只重建当前草稿必须唯一的本地素材 ID，并强制写入正确的 `music_id`/`effect_id`、类型和时长。

## Windows 首次验收

先在项目目录安装并启动：

```powershell
uv sync
uv pip install -e ".[windows]"
uv run main.py
```

1. Windows 安装并登录与资源 ID 所属区域一致的剪映版本。
2. 本地启动 CapCut Mate，创建草稿并调用 `add_audios`。
3. 在剪映中打开草稿，确认轨道存在、可播放且没有资源丢失提示。
4. 导出 5～10 秒视频，确认 BGM/音效被写入成片。
5. 如果某个版本只传 ID 无法解析，从一份人工添加过该资源的成功草稿中复制对应 `materials.audios` 项，作为 `resource_metadata` 再测。

实体音频 URL 模式仍然保留，可作为资源下架、账号无权限或版本不兼容时的降级方案。
