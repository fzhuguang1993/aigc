"""
api_client.py —— HTTP 请求封装（统一异常约定）

错误处理约定：
- 所有公开函数成功时返回解析后的结果（dict / list / 无返回值）
- HTTP 状态码 >= 400 或网络异常，统一抛出 ApiError
- 调用方只需 `except ApiError`（或更大的 Exception），不再逐函数记忆
  "返回 (resp, err) 元组" 还是 "直接 raise" 两套风格
"""
import requests
from pathlib import Path


class ApiError(Exception):
    """API 调用统一异常。status_code 为 HTTP 状态码（网络层异常时为 None）。"""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _request(method, url, timeout=30, **kwargs):
    """所有请求的统一出口：发请求 → 校验状态码 → 返回 Response。"""
    try:
        r = requests.request(method, url, timeout=timeout, **kwargs)
    except requests.RequestException as e:
        raise ApiError(f"{type(e).__name__}: {e}") from e
    if r.status_code >= 400:
        raise ApiError(f"HTTP {r.status_code}: {r.text[:300]}", r.status_code)
    return r


def upload_asset(base, file_path, asset_type="image"):
    # 元组超时：连接 10 秒（地址错时快速失败），大文件读取仍给 300 秒
    with open(file_path, "rb") as f:
        r = _request("POST", f"{base}/assets", timeout=(10, 300),
                     params={"asset_type": asset_type}, files={"file": f})
    return r.json()["asset_id"]


def submit_job(base, payload):
    """提交生成任务，返回任务 JSON（含 job_id）。失败抛 ApiError。"""
    return _request("POST", f"{base}/jobs", timeout=(10, 60), json=payload).json()


def query_job(base, job_id):
    return _request("GET", f"{base}/jobs/{job_id}").json()


def get_outputs(base, job_id):
    return _request("GET", f"{base}/jobs/{job_id}/outputs").json()


def cancel_job(base, job_id):
    return _request("POST", f"{base}/jobs/{job_id}/cancel").json()


def health(base):
    return _request("GET", f"{base}/health", timeout=15).json()


def list_jobs(base, limit=100):
    return _request("GET", f"{base}/jobs", params={"limit": limit}).json().get("items", [])


def download_to(url, save_path, chunk_mb=1, timeout=600):
    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(url, stream=True, timeout=timeout) as r:
            if r.status_code >= 400:
                raise ApiError(f"HTTP {r.status_code}: 下载失败", r.status_code)
            with open(save_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=chunk_mb * 1024 * 1024):
                    if chunk:
                        f.write(chunk)
    except requests.RequestException as e:
        raise ApiError(f"{type(e).__name__}: {e}") from e


def extract_script(base, prompt):
    """从提示词提取口播文案，返回文案字符串（可能为空串）。失败抛 ApiError。"""
    return _request("POST", f"{base}/scripts/extract", timeout=60,
                    json={"prompt": prompt}).json().get("script", "")
