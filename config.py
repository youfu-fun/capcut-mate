# 项目常量定义
import os


# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# 保存剪映草稿的目录
DRAFT_DIR = os.path.join(PROJECT_ROOT, "output", "draft")

# 日志目录
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")

# 临时文件目录
TEMP_DIR = os.path.join(PROJECT_ROOT, "temp")

# 本地部署地址。云端部署时通过 SERVER_BASE_URL 覆盖，不再默认依赖作者官网。
SERVER_BASE_URL = os.getenv("SERVER_BASE_URL", "http://127.0.0.1:30000").rstrip("/")

# 视频生成任务完成结果（SQLite 持久化）
VIDEO_GEN_TASK_DB_PATH = os.path.join(PROJECT_ROOT, "db", "video_gen_tasks.sqlite3")

# 视频生成任务：生成视频在 COS 上的可访问保留天数（预签名下载 URL 有效期，环境变量覆盖）
VIDEO_GEN_RETENTION_DAYS = max(1, int(os.getenv("VIDEO_GEN_RETENTION_DAYS", "7")))

# 剪映草稿的下载路径
DRAFT_URL = os.getenv(
    "DRAFT_URL",
    f"{SERVER_BASE_URL}/openapi/capcut-mate/v1/get_draft",
)

# 本地文件的 HTTP 下载地址前缀
DOWNLOAD_URL = os.getenv("DOWNLOAD_URL", SERVER_BASE_URL)

# 草稿提示URL
TIP_URL = os.getenv("TIP_URL", f"{SERVER_BASE_URL}/docs")

# 贴纸配置文件路径
STICKER_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "sticker.json")

# 花字配置文件路径
HUAZI_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "huazi.json")

# 模板目录路径
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "template")

# 剪映草稿保存路径（下载剪映草稿保存位置）-- 云渲染必需配置
_local_app_data = os.getenv("LOCALAPPDATA", "")
_default_draft_save_path = (
    os.path.join(
        _local_app_data,
        "JianyingPro",
        "User Data",
        "Projects",
        "com.lveditor.draft",
    )
    if _local_app_data
    else os.path.join(PROJECT_ROOT, "output", "jianying_drafts")
)
DRAFT_SAVE_PATH = os.getenv("DRAFT_SAVE_PATH", _default_draft_save_path)

# 腾讯云对象存储配置（优先）
COS_SECRET_ID = os.getenv("COS_SECRET_ID", "")
COS_SECRET_KEY = os.getenv("COS_SECRET_KEY", "")
COS_BUCKET_NAME = os.getenv("COS_BUCKET_NAME", "")
COS_REGION = os.getenv("COS_REGION", "")

# 阿里云对象存储配置（COS 未配置时作为兜底）
OSS_ACCESS_KEY_ID = os.getenv("OSS_ACCESS_KEY_ID", "")
OSS_ACCESS_KEY_SECRET = os.getenv("OSS_ACCESS_KEY_SECRET", "")
OSS_BUCKET_NAME = os.getenv("OSS_BUCKET_NAME", "")
OSS_ENDPOINT = os.getenv("OSS_ENDPOINT", "")

# 火山引擎 TOS AccessKeyId（COS、OSS 均未配置完整时作为兜底）
TOS_ACCESS_KEY_ID = os.getenv("TOS_ACCESS_KEY_ID", "")
# 火山引擎 TOS AccessKeySecret
TOS_ACCESS_KEY_SECRET = os.getenv("TOS_ACCESS_KEY_SECRET", "")
# TOS 存储桶名称
TOS_BUCKET_NAME = os.getenv("TOS_BUCKET_NAME", "")
# TOS 地域，例如 cn-beijing、cn-shanghai
TOS_REGION = os.getenv("TOS_REGION", "")
# TOS 访问域名（可选，未设置时按地域自动生成，例如 tos-cn-beijing.volces.com）
TOS_ENDPOINT = os.getenv("TOS_ENDPOINT", "")

# 对象存储上传目录前缀（根目录），COS / OSS / TOS 共用。
# 最终 object key 格式：[前缀/]yyyy-MM-dd/文件名；前缀为空时文件落在桶根目录下的日期目录中。
# 首尾多余的 / 会自动去除。
#
# 配置示例（环境变量 STORAGE_UPLOAD_PREFIX）：
#   未设置或留空     -> 2026-06-15/video.mp4
#   capcut-mate       -> capcut-mate/2026-06-15/video.mp4
#   prod/capcut-mate  -> prod/capcut-mate/2026-06-15/video.mp4
#   /capcut-mate/     -> capcut-mate/2026-06-15/video.mp4（与上相同）
STORAGE_UPLOAD_PREFIX = os.getenv("STORAGE_UPLOAD_PREFIX", "")

# 官网计费开关；本地部署默认关闭，托管运营时显式设为 true。
ENABLE_APIKEY = os.getenv("ENABLE_APIKEY", "false").strip().lower() == "true"

# 文件下载大小限制（字节），默认200MB
DOWNLOAD_FILE_SIZE_LIMIT = int(os.getenv("DOWNLOAD_FILE_SIZE_LIMIT", str(200 * 1024 * 1024)))
