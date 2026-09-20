"""
api_client.py —— HTTP 请求封装
"""
import requests
from pathlib import Path


def upload_asset(base, file_path, asset_type="image"):
    with open(file_path, "rb") as f:
        r = requests.post(f"{base}/assets", params={"asset_type": asset_type},
                          files={"file": f}, timeout=300)
    r.raise_for_status()
    return r.json()["asset_id"]


def submit_job(base, payload):
    r = requests.post(f"{base}/jobs", json=payload, timeout=60)
    if r.status_code >= 400:
        return None, f"HTTP {r.status_code}: {r.text[:300]}"
    return r.json(), None


def query_job(base, job_id):
    r = requests.get(f"{base}/jobs/{job_id}", timeout=30)
    r.raise_for_status()
    return r.json()


def get_outputs(base, job_id):
    r = requests.get(f"{base}/jobs/{job_id}/outputs", timeout=30)
    r.raise_for_status()
    return r.json()


def cancel_job(base, job_id):
    r = requests.post(f"{base}/jobs/{job_id}/cancel", timeout=30)
    r.raise_for_status()
    return r.json()


def health(base):
    r = requests.get(f"{base}/health", timeout=15)
    r.raise_for_status()
    return r.json()


def list_jobs(base, limit=100):
    r = requests.get(f"{base}/jobs", params={"limit": limit}, timeout=30)
    r.raise_for_status()
    return r.json().get("items", [])


def download_to(url, save_path, chunk_mb=1, timeout=600):
    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(save_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=chunk_mb * 1024 * 1024):
                if chunk:
                    f.write(chunk)


def extract_script(base, prompt):
    """
    从提示词提取口播文案
    返回：(口播文案，错误信息)
    """
    try:
        r = requests.post(f"{base}/scripts/extract",
                         json={"prompt": prompt},
                         timeout=60)
        r.raise_for_status()
        result = r.json()
        return result.get("script", ""), result.get("error")
    except Exception as e:
        return None, str(e)
