# -*- coding: utf-8 -*-
"""阿里云百炼 TokenPlan 文生图素材源测试。

与 openai_image 测试同一口径：全部用 unittest.mock 替换 requests 和
time.sleep，CI 不依赖真实网络、真实 API key 和真实计费。百炼走同步
multimodal-generation 协议，返回一次性 OSS 图片直链，下载/落盘/按需生成/
计费安全语义与 openai_image 完全共用。
"""
import io
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests
from PIL import Image

from app.config import config
from app.services import material


def _png_bytes(width=64, height=96, color=(120, 40, 200)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _bailian_response(image_url, status_code=200):
    """构造 multimodal-generation 成功响应体。"""
    return SimpleNamespace(
        json=lambda: {
            "request_id": "req-1",
            "output": {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": [{"image": image_url}],
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
            "usage": {"image_count": 1},
        },
        status_code=status_code,
    )


def _error_response(message, status_code):
    return SimpleNamespace(
        json=lambda: {"code": "Error", "message": message},
        status_code=status_code,
        text=message,
    )


def _download_response(content, status_code=200):
    return SimpleNamespace(
        status_code=status_code,
        content=content,
        headers={"Content-Length": str(len(content))},
        iter_content=lambda chunk_size: iter((content,)),
        close=lambda: None,
    )


_NATIVE_HOST = "https://token-plan.cn-beijing.maas.aliyuncs.com"
_COMPAT_BASE = f"{_NATIVE_HOST}/compatible-mode/v1"
_ENDPOINT = f"{_NATIVE_HOST}/api/v1/services/aigc/multimodal-generation/generation"


class TestBailianImageProvider(unittest.TestCase):
    def setUp(self):
        self.original_app_config = dict(config.app)
        self.original_proxy_config = dict(config.proxy)
        self.save_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.save_dir, ignore_errors=True)
        config.app["bailian_tokenplan_api_key"] = "sk-test-tokenplan"
        config.app["bailian_tokenplan_base_url"] = _COMPAT_BASE
        config.app.pop("bailian_image_model", None)
        config.app.pop("bailian_image_size", None)
        config.app.pop("bailian_image_prompt_template", None)
        config.app.pop("bailian_native_base_url", None)
        config.app.pop("tls_verify", None)
        config.proxy.clear()

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        config.proxy.clear()
        config.proxy.update(self.original_proxy_config)

    @staticmethod
    def _generated_item(term, image_path, duration=5):
        item = material.MaterialInfo()
        item.provider = "bailian_image"
        item.url = image_path
        item.duration = duration
        item.source_info = {
            "provider": "bailian_image",
            "search_term": term,
            "rendition": {"id": None, "width": 928, "height": 1664},
        }
        return item

    # ------------------------------------------------------------------
    # 配置解析
    # ------------------------------------------------------------------

    def test_is_bailian_image_enabled_requires_api_key(self):
        self.assertTrue(material.is_bailian_image_enabled())
        config.app["bailian_tokenplan_api_key"] = ""
        self.assertFalse(material.is_bailian_image_enabled())

    def test_native_base_url_strips_compatible_mode_suffix(self):
        self.assertEqual(material._bailian_native_base_url(), _NATIVE_HOST)
        config.app["bailian_native_base_url"] = "https://dashscope.aliyuncs.com/"
        self.assertEqual(
            material._bailian_native_base_url(), "https://dashscope.aliyuncs.com"
        )

    def test_endpoint_and_model_defaults(self):
        endpoint, model = material._bailian_image_endpoint()
        self.assertEqual(endpoint, _ENDPOINT)
        self.assertEqual(model, "qwen-image-2.0")
        config.app["bailian_image_model"] = "wan2.7-image"
        self.assertEqual(material._bailian_image_endpoint()[1], "wan2.7-image")

    def test_endpoint_raises_without_api_key(self):
        config.app["bailian_tokenplan_api_key"] = ""
        with self.assertRaises(ValueError):
            material._bailian_image_endpoint()

    def test_size_defaults_and_override(self):
        self.assertEqual(
            material._bailian_image_size(material.VideoAspect.portrait), "928*1664"
        )
        self.assertEqual(
            material._bailian_image_size(material.VideoAspect.landscape), "1664*928"
        )
        self.assertEqual(
            material._bailian_image_size(material.VideoAspect.square), "1328*1328"
        )
        config.app["bailian_image_size"] = "2048*2048"
        self.assertEqual(
            material._bailian_image_size(material.VideoAspect.portrait), "2048*2048"
        )

    # ------------------------------------------------------------------
    # 成功路径
    # ------------------------------------------------------------------

    def test_generate_images_bailian_success(self):
        response = _bailian_response("https://oss.example.com/gen/abc.png?sig=1")
        download = _download_response(_png_bytes(width=928, height=1664))

        with (
            patch("app.services.material.requests.post", return_value=response) as post,
            patch("app.services.material.requests.get", return_value=download) as get,
        ):
            results = material.generate_images_bailian(
                "sunrise over mountains",
                minimum_duration=5,
                video_aspect=material.VideoAspect.portrait,
                save_dir=self.save_dir,
            )

        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.provider, "bailian_image")
        self.assertEqual(item.duration, 5)
        # 同步 multimodal-generation 端点、鉴权头与请求体
        self.assertEqual(post.call_args.args[0], _ENDPOINT)
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer sk-test-tokenplan",
        )
        self.assertEqual(
            post.call_args.kwargs["json"],
            {
                "model": "qwen-image-2.0",
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"text": "sunrise over mountains"}],
                        }
                    ]
                },
                "parameters": {"size": "928*1664", "n": 1},
            },
        )
        # 临时直链原样下载，签名参数不被剥离
        self.assertEqual(get.call_args.args[0], "https://oss.example.com/gen/abc.png?sig=1")
        self.assertTrue(item.url.endswith(".png"))
        self.assertTrue(os.path.isfile(item.url))
        with Image.open(item.url) as saved:
            self.assertEqual(saved.size, (928, 1664))
        self.assertEqual(
            item.source_info["rendition"], {"id": None, "width": 928, "height": 1664}
        )

    def test_generate_images_bailian_applies_prompt_template(self):
        config.app["bailian_image_prompt_template"] = (
            "cinematic photo of {term}, photorealistic"
        )
        response = _bailian_response("https://oss.example.com/x.png")
        download = _download_response(_png_bytes())
        with (
            patch("app.services.material.requests.post", return_value=response) as post,
            patch("app.services.material.requests.get", return_value=download),
        ):
            material.generate_images_bailian(
                "晨光中的玻璃杯", minimum_duration=5, save_dir=self.save_dir
            )
        self.assertEqual(
            post.call_args.kwargs["json"]["input"]["messages"][0]["content"][0]["text"],
            "cinematic photo of 晨光中的玻璃杯, photorealistic",
        )

    def test_generate_images_bailian_returns_empty_when_no_image_url(self):
        response = SimpleNamespace(
            json=lambda: {"output": {"choices": [{"message": {"content": []}}]}},
            status_code=200,
        )
        with patch("app.services.material.requests.post", return_value=response):
            results = material.generate_images_bailian(
                "term", minimum_duration=5, save_dir=self.save_dir
            )
        self.assertEqual(results, [])
        self.assertEqual(os.listdir(self.save_dir), [])

    # ------------------------------------------------------------------
    # 重试与计费安全
    # ------------------------------------------------------------------

    def test_retries_5xx_with_backoff(self):
        responses = [
            _error_response("server busy", 503),
            _bailian_response("https://oss.example.com/x.png"),
        ]
        download = _download_response(_png_bytes())
        with (
            patch("app.services.material.requests.post", side_effect=responses) as post,
            patch("app.services.material.requests.get", return_value=download),
            patch("app.services.material.time.sleep") as sleep,
        ):
            results = material.generate_images_bailian(
                "ocean", minimum_duration=5, save_dir=self.save_dir
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(sleep.call_count, 1)

    def test_fails_fast_on_400(self):
        response = _error_response("content policy violation", 400)
        with (
            patch("app.services.material.requests.post", return_value=response) as post,
            patch("app.services.material.time.sleep") as sleep,
        ):
            results = material.generate_images_bailian(
                "blocked", minimum_duration=5, save_dir=self.save_dir
            )
        self.assertEqual(results, [])
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_retries_connect_timeout(self):
        responses = [
            requests.exceptions.ConnectTimeout("connect timed out"),
            _bailian_response("https://oss.example.com/x.png"),
        ]
        download = _download_response(_png_bytes())
        with (
            patch("app.services.material.requests.post", side_effect=responses) as post,
            patch("app.services.material.requests.get", return_value=download),
            patch("app.services.material.time.sleep"),
        ):
            results = material.generate_images_bailian(
                "term", minimum_duration=5, save_dir=self.save_dir
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(post.call_count, 2)

    def test_does_not_retry_unconfirmed_read_timeout(self):
        with (
            patch(
                "app.services.material.requests.post",
                side_effect=requests.exceptions.ReadTimeout("read timed out"),
            ) as post,
            patch("app.services.material.time.sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
                material.generate_images_bailian(
                    "term", minimum_duration=5, save_dir=self.save_dir
                )
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_redacts_api_key_in_failure_detail(self):
        config.app["bailian_tokenplan_api_key"] = "sk-secret-abc"
        response = _error_response("invalid key sk-secret-abc", 401)
        with (
            patch("app.services.material.requests.post", return_value=response),
            patch("app.services.material.logger") as logger,
        ):
            results = material.generate_images_bailian(
                "term", minimum_duration=5, save_dir=self.save_dir
            )
        self.assertEqual(results, [])
        for message in [str(c) for c in logger.error.call_args_list]:
            self.assertNotIn("sk-secret-abc", message)

    # ------------------------------------------------------------------
    # download_videos 分发与按需生成
    # ------------------------------------------------------------------

    def test_download_videos_bailian_image_generates_on_demand_and_stops(self):
        generated = {
            "term-1": [self._generated_item("term-1", "/tmp/img-1.png")],
            "term-2": [self._generated_item("term-2", "/tmp/img-2.png")],
            "term-3": [self._generated_item("term-3", "/tmp/img-3.png")],
        }

        def fake_generate(search_term, minimum_duration, video_aspect, save_dir=""):
            return generated[search_term]

        with (
            patch(
                "app.services.material.generate_images_bailian",
                side_effect=fake_generate,
            ) as generate,
            patch(
                "app.services.material._render_openai_image_video",
                side_effect=lambda p, d: f"{p}.mp4",
            ),
        ):
            result = material.download_videos(
                task_id="test-bailian-image-lazy",
                search_terms=["term-1", "term-2", "term-3"],
                source="bailian_image",
                audio_duration=8,
                max_clip_duration=5,
            )

        # 5s + 5s > 8s，第三个关键词不能再触发付费生成
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(result, ["/tmp/img-1.png.mp4", "/tmp/img-2.png.mp4"])

    def test_download_videos_bailian_image_bypasses_search_cache(self):
        with (
            patch(
                "app.services.material.generate_images_bailian",
                return_value=[self._generated_item("sunrise", "/tmp/img-1.png")],
            ) as generate,
            patch("app.services.material._search_videos_with_cache") as cached_search,
            patch(
                "app.services.material._render_openai_image_video",
                return_value="/tmp/img-1.png.mp4",
            ),
        ):
            result = material.download_videos(
                task_id="test-bailian-image-cache-bypass",
                search_terms=["sunrise"],
                source="bailian_image",
                audio_duration=5,
                max_clip_duration=5,
            )
        self.assertEqual(generate.call_count, 1)
        cached_search.assert_not_called()
        self.assertEqual(result, ["/tmp/img-1.png.mp4"])

    def test_download_videos_bailian_image_skips_generation_without_audio(self):
        with patch("app.services.material.generate_images_bailian") as generate:
            result = material.download_videos(
                task_id="test-bailian-image-no-audio",
                search_terms=["term-1"],
                source="bailian_image",
                audio_duration=0,
                max_clip_duration=5,
            )
        generate.assert_not_called()
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
