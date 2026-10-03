import math
import os
import time
from typing import Any, Mapping
from urllib.parse import quote_plus

import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect


# 阿里云百炼 TokenPlan 文生视频（HappyHorse）。与 LLM/文生图共用同一把
# TokenPlan Key 和同一网关 host，但走 DashScope 原生的异步任务接口：
#   submit: POST {host}/api/v1/services/aigc/video-generation/video-synthesis
#   poll:   GET  {host}/api/v1/tasks/{task_id}
# 提交必须带 X-DashScope-Async: enable；成功后结果地址在 output.video_url。
DEFAULT_NATIVE_BASE_URL = "https://token-plan.cn-beijing.maas.aliyuncs.com"
VIDEO_SYNTHESIS_PATH = "api/v1/services/aigc/video-generation/video-synthesis"
TASKS_PATH = "api/v1/tasks"
DEFAULT_MODEL_ID = "happyhorse-1.1-t2v"
DEFAULT_RESOLUTION = "1080P"
DEFAULT_MIN_DURATION_SECONDS = 3
DEFAULT_MAX_DURATION_SECONDS = 15
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_RUN_TIMEOUT_SECONDS = 1800.0
MAX_POLL_RETRIES = 5
RETRY_BASE_SECONDS = 1.0
MAX_ERROR_TEXT_LENGTH = 500
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
SUPPORTED_RESOLUTIONS = frozenset({"480P", "720P", "1080P"})
# 百炼任务状态为大写枚举：PENDING/RUNNING/SUCCEEDED/FAILED/CANCELED/UNKNOWN。
TERMINAL_FAILURE_STATUSES = frozenset({"FAILED", "CANCELED", "CANCELLED", "UNKNOWN"})
ACTIVE_STATUSES = frozenset({"PENDING", "RUNNING"})
SUCCESS_STATUS = "SUCCEEDED"


class BailianVideoError(RuntimeError):
    """确定性的配置、请求或响应错误。"""

    def __init__(self, message: str, task_id: str = ""):
        super().__init__(message)
        # 只要远端任务已经创建，所有错误类型都统一携带任务 ID，方便上层稳定
        # 展示排障依据，并让用户能去百炼控制台按 task_id 找回结果。
        self.task_id = task_id


class BailianVideoUnconfirmedTaskError(BailianVideoError):
    """远端可能已创建付费任务，但本机无法确认其最终状态。"""

    def __init__(self, message: str, task_id: str = ""):
        super().__init__(message, task_id=task_id)


class BailianVideoDownloadError(BailianVideoError):
    """远端付费任务已成功，但生成的视频未能下载到本机。"""

    def __init__(self, message: str, task_id: str):
        super().__init__(message, task_id=task_id)


def get_api_key(settings: Mapping[str, Any] | None = None) -> str:
    """读取百炼凭据。

    优先使用文生视频专用 ``bailian_video_api_key``；缺省时复用 LLM/文生图的
    ``bailian_tokenplan_api_key``（TokenPlan 一把 Key 通用），最后回退到语义
    明确的环境变量 ``BAILIAN_API_KEY``。
    """
    settings = config.app if settings is None else settings
    dedicated = str(settings.get("bailian_video_api_key", "") or "").strip()
    shared = str(settings.get("bailian_tokenplan_api_key", "") or "").strip()
    environment_key = os.getenv("BAILIAN_API_KEY", "").strip()
    return dedicated or shared or environment_key


def is_enabled(settings: Mapping[str, Any] | None = None) -> bool:
    return bool(get_api_key(settings))


def _native_base_url() -> str:
    """推导 DashScope 原生 host。

    优先 ``bailian_native_base_url``；否则从 LLM 的 ``bailian_tokenplan_base_url``
    剥离 ``/compatible-mode/v1`` 得到原生 host；全部缺省时回退到百炼北京网关。
    """
    configured_native = str(config.app.get("bailian_native_base_url", "") or "").strip()
    if configured_native:
        return configured_native.rstrip("/")
    base = str(config.app.get("bailian_tokenplan_base_url", "") or "").strip().rstrip("/")
    if not base:
        return DEFAULT_NATIVE_BASE_URL
    for suffix in ("/compatible-mode/v1", "/compatible-mode", "/v1"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base.rstrip("/") or DEFAULT_NATIVE_BASE_URL


def _model_id() -> str:
    return str(
        config.app.get("bailian_video_model", DEFAULT_MODEL_ID) or DEFAULT_MODEL_ID
    ).strip()


def _resolution() -> str:
    configured = config.app.get("bailian_video_resolution", DEFAULT_RESOLUTION)
    value = str(configured).strip().upper()
    if value not in SUPPORTED_RESOLUTIONS:
        # 分辨率直接影响付费任务规格，非法值不能静默回退到最高默认分辨率，
        # 否则可能产生超出预期的费用。
        supported = ", ".join(sorted(SUPPORTED_RESOLUTIONS))
        raise BailianVideoError(
            f"Unsupported Bailian video resolution {value!r}; expected one of: {supported}"
        )
    return value


def _config_bool(key: str, default: bool) -> bool:
    value = config.app.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def _bounded_float(key: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(config.app.get(key, default))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return min(max(value, minimum), maximum)


def _duration_bounds() -> tuple[int, int]:
    def read(key: str, default: int) -> int:
        try:
            value = int(config.app.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if value >= 1 else default

    minimum = read("bailian_video_min_duration", DEFAULT_MIN_DURATION_SECONDS)
    maximum = read("bailian_video_max_duration", DEFAULT_MAX_DURATION_SECONDS)
    return minimum, max(minimum, maximum)


def _tls_verify() -> bool:
    return _config_bool("tls_verify", True)


def _status_code(response: Any) -> int:
    try:
        return int(getattr(response, "status_code", 200))
    except (TypeError, ValueError):
        return 200


def _redact_secret(value: Any, secret: str) -> str:
    text = str(value or "")
    if secret:
        text = text.replace(secret, "***")
        encoded = quote_plus(secret)
        if encoded != secret:
            text = text.replace(encoded, "***")
    for proxy_url in config.proxy.values():
        proxy_secret = str(proxy_url or "")
        if proxy_secret:
            text = text.replace(proxy_secret, "***")
    return text[:MAX_ERROR_TEXT_LENGTH]


def _response_error(response: Any, api_key: str) -> str:
    try:
        payload = response.json()
    except Exception:
        return f"HTTP {_status_code(response)}"
    if not isinstance(payload, dict):
        return f"HTTP {_status_code(response)}"
    # 百炼错误既可能是顶层 {"code","message"}，也可能包在 output 里。
    output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
    code = payload.get("code") or output.get("code")
    message = payload.get("message") or output.get("message")
    error = payload.get("error")
    if isinstance(error, dict):
        code = code or error.get("code")
        message = message or error.get("message")
    detail = ": ".join(str(item) for item in (code, message) if item not in (None, ""))
    return _redact_secret(detail or f"HTTP {_status_code(response)}", api_key)


def _is_retryable_error(error: Exception) -> bool:
    if isinstance(
        error,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ),
    ):
        return True
    response = getattr(error, "response", None)
    return response is not None and _status_code(response) in RETRYABLE_STATUS_CODES


def _rendition_size(aspect: VideoAspect, resolution: str) -> tuple[int, int]:
    """按分辨率与画幅估算产物尺寸，仅用于素材来源记录（元数据）。"""
    short_edge = {"480P": 480, "720P": 720, "1080P": 1080}[resolution]
    long_edge = {"480P": 854, "720P": 1280, "1080P": 1920}[resolution]
    if aspect == VideoAspect.portrait:
        return short_edge, long_edge
    if aspect == VideoAspect.square:
        return short_edge, short_edge
    return long_edge, short_edge


def generate_videos(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> list[MaterialInfo]:
    """提交一个百炼 HappyHorse 文生视频任务，并等待可下载的结果地址。"""
    api_key = get_api_key()
    if not api_key:
        raise BailianVideoError(
            "Alibaba Cloud Bailian video requires an API key "
            "(bailian_tokenplan_api_key or bailian_video_api_key)"
        )

    term = str(search_term or "").strip()
    if not term:
        # 空提示词可能来自上游脚本拆分异常。付费生成源不能把它提交到远端，
        # 否则即使接口接受请求，也只会得到无法使用且已经计费的视频。
        raise BailianVideoError("Bailian video search term must not be empty")

    aspect = VideoAspect(video_aspect)
    try:
        requested_duration = max(int(minimum_duration), 1)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BailianVideoError(
            "Bailian video clip duration must be a positive integer"
        ) from exc
    minimum, maximum = _duration_bounds()
    duration = min(max(requested_duration, minimum), maximum)
    if duration != requested_duration:
        logger.info(
            "Bailian clip duration clamped to the configured model range: "
            f"requested={requested_duration}s, using={duration}s "
            f"(configured {minimum}-{maximum}s)"
        )
    resolution = _resolution()
    native_base = _native_base_url()
    payload = {
        "model": _model_id(),
        "input": {"prompt": term},
        "parameters": {
            "resolution": resolution,
            # HappyHorse 的 ratio 取值与 VideoAspect 一致（9:16 / 16:9 / 1:1）。
            "ratio": aspect.value,
            "duration": duration,
            "watermark": _config_bool("bailian_video_watermark", False),
        },
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # 百炼 HTTP 接口只支持异步，必须显式声明。
        "X-DashScope-Async": "enable",
    }
    submit_url = f"{native_base}/{VIDEO_SYNTHESIS_PATH}"
    logger.info(
        "generating video with Alibaba Cloud Bailian (HappyHorse): "
        f"model={payload['model']}, term={term!r}, duration={duration}s, "
        f"resolution={resolution}, ratio={aspect.value}"
    )

    # 提交接口不做自动重试：超时或 5xx 可能发生在付费任务已经创建之后，盲目
    # 重试会造成重复扣费。只有拿到明确拒绝响应时才判定为确定性失败。
    try:
        response = requests.post(
            submit_url,
            json=payload,
            headers=headers,
            proxies=config.proxy,
            verify=_tls_verify(),
            timeout=(30, 60),
            allow_redirects=False,
        )
    except Exception as exc:
        raise BailianVideoUnconfirmedTaskError(
            "Bailian video submission returned no response; a paid task may "
            "already exist remotely: "
            f"error={type(exc).__name__}, detail={_redact_secret(exc, api_key)}"
        ) from exc

    status_code = _status_code(response)
    if 300 <= status_code < 400:
        raise BailianVideoUnconfirmedTaskError(
            "Bailian video submission returned a redirect; the paid task state "
            "is unknown and the request was not replayed"
        )
    if status_code >= 500:
        raise BailianVideoUnconfirmedTaskError(
            f"Bailian video submission failed with HTTP {status_code}; a paid "
            "task may already exist remotely"
        )
    if not 200 <= status_code < 300:
        raise BailianVideoError(
            "Bailian video generation request rejected: "
            f"HTTP {status_code}, {_response_error(response, api_key)}"
        )
    try:
        body = response.json()
    except Exception as exc:
        raise BailianVideoUnconfirmedTaskError(
            "Bailian video submission returned an unreadable response; a paid "
            f"task may already exist remotely: error={type(exc).__name__}"
        ) from exc

    output = body.get("output") if isinstance(body, dict) else None
    task_id = ""
    if isinstance(output, dict):
        task_id = str(output.get("task_id") or "").strip()
    if not task_id:
        raise BailianVideoUnconfirmedTaskError(
            "Bailian accepted the submission without returning a task id"
        )
    logger.info(f"Alibaba Cloud Bailian video task created: id={task_id}")

    task = _wait_for_task(
        task_id=task_id,
        tasks_url=f"{native_base}/{TASKS_PATH}",
        api_key=api_key,
    )
    if task is None:
        return []
    task_output = task.get("output") if isinstance(task, dict) else None
    video_url = task_output.get("video_url") if isinstance(task_output, dict) else None
    if not isinstance(video_url, str) or not video_url.startswith(("http://", "https://")):
        raise BailianVideoError(
            f"Bailian task succeeded without a downloadable video: id={task_id}",
            task_id=task_id,
        )

    width, height = _rendition_size(aspect, resolution)
    return [
        MaterialInfo(
            provider="bailian_video",
            url=video_url,
            duration=duration,
            source_info={
                "provider": "bailian_video",
                "search_term": term,
                "asset_id": task_id,
                "rendition": {"id": task_id, "width": width, "height": height},
            },
        )
    ]


def _wait_for_task(
    *,
    task_id: str,
    tasks_url: str,
    api_key: str,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + _bounded_float(
        "bailian_video_run_timeout", DEFAULT_RUN_TIMEOUT_SECONDS, 60.0, 7200.0
    )
    poll_interval = _bounded_float(
        "bailian_video_poll_interval", DEFAULT_POLL_INTERVAL_SECONDS, 0.5, 60.0
    )
    headers = {"Authorization": f"Bearer {api_key}"}
    consecutive_failures = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BailianVideoUnconfirmedTaskError(
                "Bailian task is still running after the configured local wait "
                f"timeout: id={task_id}",
                task_id=task_id,
            )

        phase_timeout = max(min(remaining / 2.0, 30.0), 0.001)
        try:
            response = requests.get(
                f"{tasks_url}/{quote_plus(task_id)}",
                headers=headers,
                proxies=config.proxy,
                verify=_tls_verify(),
                timeout=(phase_timeout, phase_timeout),
                allow_redirects=False,
            )
            status_code = _status_code(response)
            if status_code in RETRYABLE_STATUS_CODES:
                raise requests.exceptions.HTTPError(
                    f"HTTP {status_code}", response=response
                )
            if not 200 <= status_code < 300:
                raise BailianVideoUnconfirmedTaskError(
                    "Bailian task status is unknown: "
                    f"http_status={status_code}, detail={_response_error(response, api_key)}",
                    task_id=task_id,
                )
            body = response.json()
            if not isinstance(body, dict):
                raise BailianVideoUnconfirmedTaskError(
                    "Bailian task status response is malformed", task_id=task_id
                )
        except BailianVideoUnconfirmedTaskError:
            raise
        except Exception as exc:
            if not _is_retryable_error(exc):
                raise BailianVideoUnconfirmedTaskError(
                    "Bailian polling failed and the paid task state is unknown: "
                    f"error={type(exc).__name__}, detail={_redact_secret(exc, api_key)}",
                    task_id=task_id,
                ) from exc

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BailianVideoUnconfirmedTaskError(
                    "Bailian task is still running after the configured local wait "
                    f"timeout: id={task_id}",
                    task_id=task_id,
                ) from exc
            consecutive_failures += 1
            if consecutive_failures > MAX_POLL_RETRIES:
                raise BailianVideoUnconfirmedTaskError(
                    "Bailian polling failed after retries; the paid task may still "
                    f"be running remotely: id={task_id}",
                    task_id=task_id,
                ) from exc
            delay = min(RETRY_BASE_SECONDS * consecutive_failures, remaining)
            logger.warning(
                "Bailian polling hit a transient error; retrying the same task: "
                f"id={task_id}, attempt={consecutive_failures}/{MAX_POLL_RETRIES}, "
                f"retry_in={delay:.1f}s"
            )
            time.sleep(delay)
            continue

        consecutive_failures = 0
        output = body.get("output") if isinstance(body.get("output"), dict) else {}
        status = str(output.get("task_status") or "").strip().upper()
        if status == SUCCESS_STATUS:
            return body
        if status in TERMINAL_FAILURE_STATUSES:
            raise BailianVideoError(
                "Bailian task did not produce a video: "
                f"id={task_id}, status={status}, detail={_response_error(response, api_key)}",
                task_id=task_id,
            )
        if status not in ACTIVE_STATUSES:
            raise BailianVideoUnconfirmedTaskError(
                f"Bailian returned an unknown task status: id={task_id}, status={status!r}",
                task_id=task_id,
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BailianVideoUnconfirmedTaskError(
                "Bailian task is still running after the configured local wait "
                f"timeout: id={task_id}",
                task_id=task_id,
            )
        time.sleep(min(poll_interval, remaining))
