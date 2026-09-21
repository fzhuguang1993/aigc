# video_text_tools
"""
视频 / 文字处理工具包（纯功能代码，无 UI 依赖，可整体移植）

模块一览
--------
- config         : 全部可配置常量（FFmpeg、SMB、溯源、数据库）
- file_utils     : 视频文件扫描、溯源码文件名解析
- ffmpeg_utils   : ffprobe 取视频信息、缩略图、水印/格式化命令构建
- watermark      : 批量水印/格式化处理（回调式，可在任意线程调用）
- renamer        : 批量重命名引擎（数字/大小写字母/罗马数字/希腊字母规则）
- batch_input    : 批量粘贴录入（pyautogui + pyperclip 键鼠自动化）
- smb_utils      : SMB 远程上传
- trace_utils    : 视频溯源（溯源码池取码、按规则重命名、入库，带事务回滚）

依赖
----
必选: 无（标准库 + ffmpeg 可执行程序）
可选: smbclient（SMB 上传）、pymysql（溯源入库）、pypinyin（中文首拼）

用法示例
--------
    from video_text_tools import get_video_info, process_videos, RenameEngine

    info = get_video_info("a.mp4")
    process_videos(["a.mp4"], watermark_path="wm.png",
                   log_callback=print)
"""

from .config import (
    VIDEO_EXTENSIONS,
    FFMPEG_DEFAULT_VIDEO_BITRATE,
    FFMPEG_DEFAULT_AUDIO_BITRATE,
    FFMPEG_DEFAULT_FPS,
    FFMPEG_WATERMARK_SCALE,
    SMB_CONFIG,
    TRACE_CONFIG,
    DB_CFG,
)
from .file_utils import (
    get_base_dir,
    is_video_file,
    get_sorted_video_files,
    get_video_files,
    extract_trace_code,
    has_trace_code,
)
from .ffmpeg_utils import (
    get_ffmpeg_path,
    get_ffprobe_path,
    get_video_info,
    get_video_thumbnail,
    build_format_command,
    build_watermark_command,
    generate_output_filename,
)
from .watermark import process_videos
from .renamer import RenameEngine

# 以下模块依赖可选第三方库，缺失时跳过导出（模块本身仍可延迟导入）
try:
    from .batch_input import BatchInput, read_clipboard_lines, get_mod_key
except ImportError:
    pass

try:
    from .smb_utils import SMBUtils
except ImportError:
    pass

try:
    from .trace_utils import (
        TraceUtils, get_trace_owner_map, get_user_initials,
        bind_videos, unbind_videos,
    )
except ImportError:
    pass
