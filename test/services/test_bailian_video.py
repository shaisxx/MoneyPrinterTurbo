import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests

from app.config import config
from app.models.schema import VideoAspect
from app.services import material
from app.services import bailian_video as bv


_NATIVE = "https://token-plan.cn-beijing.maas.aliyuncs.com"
_SUBMIT_URL = f"{_NATIVE}/api/v1/services/aigc/video-generation/video-synthesis"
_TASKS_URL = f"{_NATIVE}/api/v1/tasks"


def _response(payload, status_code=200):
    return SimpleNamespace(status_code=status_code, json=lambda: payload)


def _submit_response(task_id="task-1", status="PENDING"):
    return _response({"output": {"task_id": task_id, "task_status": status},
                      "request_id": "req-1"})


def _poll_response(status, video_url=None, code=None, message=None):
    output = {"task_id": "task-1", "task_status": status}
    if video_url is not None:
        output["video_url"] = video_url
    if code is not None:
        output["code"] = code
    if message is not None:
        output["message"] = message
    return _response({"output": output, "request_id": "req-2"})


class TestBailianVideoService(unittest.TestCase):
    def setUp(self):
        self.original_app = dict(config.app)
        self.original_proxy = dict(config.proxy)
        config.app.update(
            {
                "bailian_tokenplan_api_key": "sk-test-tokenplan",
                "bailian_tokenplan_base_url": f"{_NATIVE}/compatible-mode/v1",
            }
        )
        for key in (
            "bailian_video_api_key",
            "bailian_video_model",
            "bailian_video_resolution",
            "bailian_video_watermark",
            "bailian_native_base_url",
            "bailian_video_min_duration",
            "bailian_video_max_duration",
        ):
            config.app.pop(key, None)
        config.proxy.clear()

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app)
        config.proxy.clear()
        config.proxy.update(self.original_proxy)

    # ------------------------------------------------------------------
    # 配置解析
    # ------------------------------------------------------------------

    def test_api_key_reuses_tokenplan_and_allows_override(self):
        self.assertEqual(bv.get_api_key(), "sk-test-tokenplan")
        self.assertTrue(bv.is_enabled())
        config.app["bailian_video_api_key"] = "sk-dedicated"
        self.assertEqual(bv.get_api_key(), "sk-dedicated")
        config.app["bailian_video_api_key"] = ""
        config.app["bailian_tokenplan_api_key"] = ""
        with patch.dict(os.environ, {"BAILIAN_API_KEY": "env-key"}, clear=False):
            self.assertEqual(bv.get_api_key(), "env-key")
        with patch.dict(os.environ, {"BAILIAN_API_KEY": ""}, clear=False):
            self.assertEqual(bv.get_api_key(), "")
            self.assertFalse(bv.is_enabled())

    def test_native_base_url_strips_compatible_mode(self):
        self.assertEqual(bv._native_base_url(), _NATIVE)
        config.app["bailian_native_base_url"] = "https://dashscope.aliyuncs.com/"
        self.assertEqual(bv._native_base_url(), "https://dashscope.aliyuncs.com")

    def test_model_and_resolution_defaults(self):
        self.assertEqual(bv._model_id(), "happyhorse-1.1-t2v")
        self.assertEqual(bv._resolution(), "1080P")
        config.app["bailian_video_resolution"] = "720p"
        self.assertEqual(bv._resolution(), "720P")

    def test_invalid_resolution_rejected(self):
        config.app["bailian_video_resolution"] = "4K"
        with self.assertRaises(bv.BailianVideoError):
            bv._resolution()

    def test_missing_api_key_fails_before_submission(self):
        config.app["bailian_tokenplan_api_key"] = ""
        with (
            patch.dict(os.environ, {"BAILIAN_API_KEY": ""}, clear=False),
            patch.object(bv.requests, "post") as post,
        ):
            with self.assertRaises(bv.BailianVideoError):
                bv.generate_videos("sunrise", 5)
        post.assert_not_called()

    def test_empty_search_term_fails_before_paid_submission(self):
        for invalid in ("", "   ", None, 0, False):
            with self.subTest(invalid=invalid):
                with patch.object(bv.requests, "post") as post:
                    with self.assertRaises(bv.BailianVideoError):
                        bv.generate_videos(invalid, 5)
                post.assert_not_called()

    # ------------------------------------------------------------------
    # 成功路径
    # ------------------------------------------------------------------

    def test_generate_videos_success(self):
        with (
            patch.object(
                bv.requests, "post", return_value=_submit_response("task-9")
            ) as post,
            patch.object(
                bv.requests,
                "get",
                return_value=_poll_response(
                    "SUCCEEDED", video_url="https://oss.example.com/v.mp4?sig=1"
                ),
            ) as get,
            patch.object(bv.time, "sleep"),
        ):
            items = bv.generate_videos(
                "a cat", minimum_duration=5, video_aspect=VideoAspect.portrait
            )

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.provider, "bailian_video")
        self.assertEqual(item.url, "https://oss.example.com/v.mp4?sig=1")
        self.assertEqual(item.duration, 5)
        self.assertEqual(item.source_info["asset_id"], "task-9")
        # 提交端点、异步头与请求体
        self.assertEqual(post.call_args.args[0], _SUBMIT_URL)
        self.assertEqual(post.call_args.kwargs["headers"]["X-DashScope-Async"], "enable")
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"], "Bearer sk-test-tokenplan"
        )
        self.assertEqual(
            post.call_args.kwargs["json"],
            {
                "model": "happyhorse-1.1-t2v",
                "input": {"prompt": "a cat"},
                "parameters": {
                    "resolution": "1080P",
                    "ratio": "9:16",
                    "duration": 5,
                    "watermark": False,
                },
            },
        )
        # 轮询打在 /api/v1/tasks/{id}
        self.assertTrue(get.call_args.args[0].startswith(_TASKS_URL + "/"))

    def test_duration_clamped_to_model_range(self):
        with (
            patch.object(bv.requests, "post", return_value=_submit_response()) as post,
            patch.object(
                bv.requests, "get", return_value=_poll_response("SUCCEEDED", "https://x/y.mp4")
            ),
            patch.object(bv.time, "sleep"),
        ):
            items = bv.generate_videos("term", minimum_duration=99)
        self.assertEqual(items[0].duration, 15)
        self.assertEqual(post.call_args.kwargs["json"]["parameters"]["duration"], 15)

    # ------------------------------------------------------------------
    # 计费安全
    # ------------------------------------------------------------------

    def test_submit_exception_is_unconfirmed(self):
        with patch.object(
            bv.requests, "post", side_effect=requests.exceptions.Timeout("timeout")
        ):
            with self.assertRaises(bv.BailianVideoUnconfirmedTaskError):
                bv.generate_videos("term", 5)

    def test_submit_5xx_is_unconfirmed(self):
        with patch.object(bv.requests, "post", return_value=_response({}, 503)):
            with self.assertRaises(bv.BailianVideoUnconfirmedTaskError):
                bv.generate_videos("term", 5)

    def test_submit_redirect_is_unconfirmed(self):
        with patch.object(bv.requests, "post", return_value=_response({}, 302)):
            with self.assertRaises(bv.BailianVideoUnconfirmedTaskError):
                bv.generate_videos("term", 5)

    def test_submit_4xx_is_deterministic_error(self):
        resp = _response({"code": "InvalidApiKey", "message": "bad key sk-test-tokenplan"}, 401)
        with (
            patch.object(bv.requests, "post", return_value=resp),
            patch.object(bv.logger, "error"),
        ):
            with self.assertRaises(bv.BailianVideoError) as raised:
                bv.generate_videos("term", 5)
        # 错误信息不能泄漏 API key
        self.assertNotIn("sk-test-tokenplan", str(raised.exception))

    def test_submit_without_task_id_is_unconfirmed(self):
        with patch.object(bv.requests, "post", return_value=_response({"output": {}}, 200)):
            with self.assertRaises(bv.BailianVideoUnconfirmedTaskError):
                bv.generate_videos("term", 5)

    def test_poll_terminal_failure_raises(self):
        with (
            patch.object(bv.requests, "post", return_value=_submit_response()),
            patch.object(
                bv.requests,
                "get",
                return_value=_poll_response("FAILED", code="InvalidParameter", message="bad"),
            ),
            patch.object(bv.time, "sleep"),
        ):
            with self.assertRaises(bv.BailianVideoError):
                bv.generate_videos("term", 5)

    def test_poll_unknown_status_is_unconfirmed(self):
        with (
            patch.object(bv.requests, "post", return_value=_submit_response()),
            patch.object(bv.requests, "get", return_value=_poll_response("WEIRD")),
            patch.object(bv.time, "sleep"),
        ):
            with self.assertRaises(bv.BailianVideoUnconfirmedTaskError):
                bv.generate_videos("term", 5)

    def test_succeeded_without_video_url_raises(self):
        with (
            patch.object(bv.requests, "post", return_value=_submit_response()),
            patch.object(bv.requests, "get", return_value=_poll_response("SUCCEEDED")),
            patch.object(bv.time, "sleep"),
        ):
            with self.assertRaises(bv.BailianVideoError):
                bv.generate_videos("term", 5)

    def test_poll_waits_through_pending_then_succeeds(self):
        polls = [
            _poll_response("PENDING"),
            _poll_response("RUNNING"),
            _poll_response("SUCCEEDED", "https://oss.example.com/v.mp4"),
        ]
        with (
            patch.object(bv.requests, "post", return_value=_submit_response()),
            patch.object(bv.requests, "get", side_effect=polls) as get,
            patch.object(bv.time, "sleep") as sleep,
        ):
            items = bv.generate_videos("term", 5)
        self.assertEqual(len(items), 1)
        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)


class TestBailianVideoOnDemand(unittest.TestCase):
    def setUp(self):
        self.original_app = dict(config.app)
        config.app.update({"bailian_tokenplan_api_key": "sk-test-tokenplan"})

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app)

    @staticmethod
    def _item(term, url, duration=5):
        return material.MaterialInfo(
            provider="bailian_video",
            url=url,
            duration=duration,
            source_info={"provider": "bailian_video", "search_term": term,
                         "asset_id": "task-1",
                         "rendition": {"id": "task-1", "width": 1080, "height": 1920}},
        )

    def test_on_demand_stops_after_duration_covered(self):
        generated = {
            "t1": [self._item("t1", "https://x/1.mp4")],
            "t2": [self._item("t2", "https://x/2.mp4")],
            "t3": [self._item("t3", "https://x/3.mp4")],
        }
        with (
            patch.object(
                bv, "generate_videos", side_effect=lambda search_term, **kw: generated[search_term]
            ) as gen,
            patch.object(
                material,
                "_save_generated_video_with_retry",
                side_effect=lambda url, directory, provider: f"/local/{url.rsplit('/', 1)[-1]}",
            ),
            patch.object(material, "_persist_material_sources"),
        ):
            result = material.download_videos(
                task_id="t-bailian-video",
                search_terms=["t1", "t2", "t3"],
                source="bailian_video",
                audio_duration=8,
                max_clip_duration=5,
            )
        # 5+5 >= 8，第三个关键词不再下单
        self.assertEqual(gen.call_count, 2)
        self.assertEqual(result, ["/local/1.mp4", "/local/2.mp4"])

    def test_on_demand_skips_without_audio(self):
        with patch.object(bv, "generate_videos") as gen:
            result = material.download_videos(
                task_id="t-bailian-no-audio",
                search_terms=["t1"],
                source="bailian_video",
                audio_duration=0,
                max_clip_duration=5,
            )
        gen.assert_not_called()
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
