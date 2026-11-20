"""测试：接口调用与返回解析（全部离线，用假 opener）。"""

from __future__ import annotations

import io
import json
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

from cursorqa.client import (
    ANSWER_PROMPT,
    ANSWER_PROMPT_FULL,
    ApiError,
    VisionClient,
    build_payload,
    describe_http_error,
    extract_reply,
    is_full_request,
    is_skip_reply,
    normalize_images,
    resolve_endpoint,
)


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def http_error(code: int, body: object, headers: dict[str, str] | None = None) -> HTTPError:
    payload = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
    return HTTPError("https://example.invalid", code, "err", {}, io.BytesIO(payload))


class ResolveEndpointTest(unittest.TestCase):
    def test_deepseek_uses_responses(self) -> None:
        style, url = resolve_endpoint("https://api.deepseek.com")
        self.assertEqual(style, "responses")
        self.assertEqual(url, "https://api.deepseek.com/responses")

    def test_openai_compatible_uses_chat(self) -> None:
        style, url = resolve_endpoint("https://api.openai.com/v1")
        self.assertEqual(style, "chat")
        self.assertEqual(url, "https://api.openai.com/v1/chat/completions")

    def test_bare_host_gets_v1(self) -> None:
        style, url = resolve_endpoint("dashscope.aliyuncs.com/compatible-mode/v1")
        self.assertEqual(style, "chat")
        self.assertTrue(url.startswith("https://"))
        self.assertTrue(url.endswith("/v1/chat/completions"))

    def test_full_urls_are_kept(self) -> None:
        self.assertEqual(
            resolve_endpoint("https://x.test/v1/chat/completions/"),
            ("chat", "https://x.test/v1/chat/completions"),
        )
        self.assertEqual(
            resolve_endpoint("https://x.test/responses/"),
            ("responses", "https://x.test/responses"),
        )

    def test_empty_raises(self) -> None:
        with self.assertRaises(ApiError):
            resolve_endpoint("  ")


class PayloadTest(unittest.TestCase):
    def test_responses_payload_shape(self) -> None:
        payload = build_payload("responses", "deepseek-flash", "看题", "AAAA")
        self.assertEqual(payload["model"], "deepseek-flash")
        content = payload["input"][0]["content"]
        self.assertEqual(content[0]["type"], "input_text")
        self.assertEqual(content[1]["type"], "input_image")
        self.assertTrue(content[1]["image_url"].startswith("data:image/png;base64,"))

    def test_text_only_payloads(self) -> None:
        chat = build_payload("chat", "deepseek-chat", "只看文字", None)
        self.assertEqual(chat["messages"][1]["content"], "只看文字")
        self.assertNotIn("image_url", str(chat))

        responses = build_payload("responses", "deepseek-chat", "只看文字", None)
        content = responses["input"][0]["content"]
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]["type"], "input_text")

    def test_chat_payload_shape(self) -> None:
        payload = build_payload("chat", "gpt-4o", "看题", "AAAA")
        self.assertEqual(payload["messages"][0]["role"], "system")
        parts = payload["messages"][1]["content"]
        self.assertEqual(parts[1]["type"], "image_url")
        self.assertIn("base64", parts[1]["image_url"]["url"])


class ImagesTest(unittest.TestCase):
    def test_normalize(self) -> None:
        self.assertEqual(normalize_images(None), [])
        self.assertEqual(normalize_images(""), [])
        self.assertEqual(normalize_images("AAA"), ["AAA"])
        self.assertEqual(normalize_images(["A", "", "B"]), ["A", "B"])

    def test_two_images_chat(self) -> None:
        payload = build_payload("chat", "gpt-4o", "看题", ["A", "B"])
        parts = payload["messages"][1]["content"]
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[0]["type"], "text")
        self.assertEqual(parts[1]["type"], "image_url")
        self.assertEqual(parts[2]["type"], "image_url")

    def test_two_images_responses(self) -> None:
        payload = build_payload("responses", "deepseek-flash", "看题", ["A", "B"])
        content = payload["input"][0]["content"]
        self.assertEqual([item["type"] for item in content], ["input_text", "input_image", "input_image"])

    def test_ask_images_sends_both(self) -> None:
        captured: dict[str, object] = {}

        def opener(request: object, **_kwargs: object) -> FakeResponse:
            captured["body"] = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
            return FakeResponse({"output_text": "答案：B"})

        client = VisionClient("sk-test", opener=opener)
        reply = client.ask_images(["AAA", "BBB"], prompt="看整屏")
        self.assertEqual(reply.text, "答案：B")
        body = captured["body"]
        assert isinstance(body, dict)
        self.assertEqual(len(body["input"][0]["content"]), 3)

    def test_ask_images_requires_images(self) -> None:
        client = VisionClient("sk-test", opener=lambda *a, **k: FakeResponse({}))
        with self.assertRaises(ApiError):
            client.ask_images([])

    def test_full_prompt_mentions_two_images(self) -> None:
        self.assertIn("第二张图是整屏", ANSWER_PROMPT_FULL)
        self.assertNotIn("只回复 FULL", ANSWER_PROMPT_FULL)


class ReplyMarkersTest(unittest.TestCase):
    def test_skip(self) -> None:
        self.assertTrue(is_skip_reply("SKIP"))
        self.assertFalse(is_skip_reply("答案：A"))

    def test_full(self) -> None:
        self.assertTrue(is_full_request("FULL"))
        self.assertFalse(is_full_request("答案：B。"))


class ExtractReplyTest(unittest.TestCase):
    def test_responses_api(self) -> None:
        data = {
            "output": [
                {"type": "reasoning", "summary": []},
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "答案是 B。"}],
                },
            ]
        }
        self.assertEqual(extract_reply(data), "答案是 B。")

    def test_responses_flat_text(self) -> None:
        self.assertEqual(extract_reply({"output_text": "  直接给答案  "}), "直接给答案")

    def test_chat_string(self) -> None:
        data = {"choices": [{"message": {"role": "assistant", "content": "选 C"}}]}
        self.assertEqual(extract_reply(data), "选 C")

    def test_chat_parts(self) -> None:
        data = {"choices": [{"message": {"content": [{"type": "text", "text": "四十二"}]}}]}
        self.assertEqual(extract_reply(data), "四十二")

    def test_multiple_parts_joined(self) -> None:
        data = {
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "第一段"}]},
                {"type": "message", "content": [{"type": "output_text", "text": "第二段"}]},
            ]
        }
        self.assertEqual(extract_reply(data), "第一段\n第二段")

    def test_empty(self) -> None:
        self.assertEqual(extract_reply({}), "")
        self.assertEqual(extract_reply({"output": []}), "")


class DescribeErrorTest(unittest.TestCase):
    def test_401_hint(self) -> None:
        error = describe_http_error(401, json.dumps({"error": {"message": "bad key"}}))
        self.assertIn("密钥", error.hint)
        self.assertIn("bad key", str(error))
        self.assertFalse(error.retryable)

    def test_image_rejection_mentions_multimodal(self) -> None:
        body = json.dumps({"error": {"message": "this model does not support image input"}})
        error = describe_http_error(400, body)
        self.assertIn("截图", str(error))
        self.assertIn("多模态", error.hint)

    def test_server_error_is_retryable(self) -> None:
        self.assertTrue(describe_http_error(503, "").retryable)

    def test_non_json_body_kept(self) -> None:
        error = describe_http_error(500, "<html>bad gateway</html>")
        self.assertIn("bad gateway", str(error))


class AskTest(unittest.TestCase):
    def test_success_responses(self) -> None:
        payload = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "答案 A"}]}]}
        client = VisionClient("sk-test", base_url="https://api.deepseek.com", opener=lambda *a, **k: FakeResponse(payload))
        reply = client.ask("AAAA")
        self.assertEqual(reply.text, "答案 A")
        self.assertFalse(reply.is_skip)
        self.assertGreaterEqual(reply.latency, 0)

    def test_skip_flag(self) -> None:
        payload = {"choices": [{"message": {"content": " skip "}}]}
        client = VisionClient("sk-test", base_url="https://api.openai.com/v1", opener=lambda *a, **k: FakeResponse(payload))
        self.assertTrue(client.ask("AAAA").is_skip)

    def test_missing_key_fails_fast(self) -> None:
        client = VisionClient("", opener=lambda *a, **k: FakeResponse({}))
        with self.assertRaises(ApiError) as ctx:
            client.ask("AAAA")
        self.assertIn("密钥", str(ctx.exception))

    def test_missing_image_fails(self) -> None:
        client = VisionClient("sk-test", opener=lambda *a, **k: FakeResponse({}))
        with self.assertRaises(ApiError):
            client.ask("")

    def test_empty_reply_raises(self) -> None:
        client = VisionClient("sk-test", opener=lambda *a, **k: FakeResponse({"output": []}))
        with self.assertRaises(ApiError) as ctx:
            client.ask("AAAA")
        self.assertIn("没有返回文字", str(ctx.exception))

    def test_retries_then_succeeds(self) -> None:
        calls: list[int] = []
        payload = {"output_text": "OK"}

        def opener(*_args: object, **_kwargs: object) -> FakeResponse:
            calls.append(1)
            if len(calls) == 1:
                raise http_error(503, {"error": {"message": "busy"}})
            return FakeResponse(payload)

        client = VisionClient("sk-test", retries=2, opener=opener)
        with mock.patch("cursorqa.client.time.sleep") as sleep:
            reply = client.ask("AAAA")
        self.assertEqual(reply.text, "OK")
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once()

    def test_retry_exhausted(self) -> None:
        def opener(*_args: object, **_kwargs: object) -> FakeResponse:
            raise http_error(500, "")

        client = VisionClient("sk-test", retries=1, opener=opener)
        with mock.patch("cursorqa.client.time.sleep"), self.assertRaises(ApiError) as ctx:
            client.ask("AAAA")
        self.assertEqual(ctx.exception.status, 500)

    def test_4xx_not_retried(self) -> None:
        calls: list[int] = []

        def opener(*_args: object, **_kwargs: object) -> FakeResponse:
            calls.append(1)
            raise http_error(401, {"error": {"message": "invalid key"}})

        client = VisionClient("sk-test", retries=3, opener=opener)
        with self.assertRaises(ApiError):
            client.ask("AAAA")
        self.assertEqual(len(calls), 1)

    def test_network_error_message(self) -> None:
        def opener(*_args: object, **_kwargs: object) -> FakeResponse:
            raise URLError("timed out")

        client = VisionClient("sk-test", retries=0, opener=opener)
        with self.assertRaises(ApiError) as ctx:
            client.ask("AAAA")
        self.assertIn("网络", str(ctx.exception))

    def test_non_json_response(self) -> None:
        class TextResponse(FakeResponse):
            def __init__(self) -> None:
                self._body = b"<html>login</html>"

        client = VisionClient("sk-test", opener=lambda *a, **k: TextResponse())
        with self.assertRaises(ApiError) as ctx:
            client.ask("AAAA")
        self.assertIn("JSON", str(ctx.exception))

    def test_body_level_error(self) -> None:
        client = VisionClient(
            "sk-test",
            opener=lambda *a, **k: FakeResponse({"error": {"message": "image too large"}}),
        )
        with self.assertRaises(ApiError) as ctx:
            client.ask("AAAA")
        self.assertIn("image too large", str(ctx.exception))

    def test_ask_text_needs_prompt(self) -> None:
        client = VisionClient("sk-test", opener=lambda *a, **k: FakeResponse({}))
        with self.assertRaises(ApiError):
            client.ask_text("   ")

    def test_ask_text_sends_no_image(self) -> None:
        captured: dict[str, object] = {}

        def opener(request: object, **_kwargs: object) -> FakeResponse:
            captured["body"] = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
            return FakeResponse({"choices": [{"message": {"content": "答案：B"}}]})

        client = VisionClient(
            "sk-test", base_url="https://api.openai.com/v1", model="deepseek-chat", opener=opener
        )
        reply = client.ask_text("▶ 题目一行")
        self.assertEqual(reply.text, "答案：B")
        body = captured["body"]
        assert isinstance(body, dict)
        self.assertEqual(body["messages"][1]["content"], "▶ 题目一行")

    def test_test_text_uses_text_prompt(self) -> None:
        captured: dict[str, object] = {}

        def opener(request: object, **_kwargs: object) -> FakeResponse:
            captured["body"] = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
            return FakeResponse({"output_text": "OK"})

        client = VisionClient("sk-test", opener=opener)
        self.assertEqual(client.test_text().text, "OK")
        body = captured["body"]
        assert isinstance(body, dict)
        self.assertNotIn("input_image", str(body))

    def test_test_uses_builtin_image(self) -> None:
        captured: dict[str, object] = {}

        def opener(request: object, **_kwargs: object) -> FakeResponse:
            captured["body"] = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
            return FakeResponse({"output_text": "OK"})

        client = VisionClient("sk-test", opener=opener)
        reply = client.test()
        self.assertEqual(reply.text, "OK")
        body = captured["body"]
        assert isinstance(body, dict)
        image = body["input"][0]["content"][1]["image_url"]
        self.assertTrue(image.startswith("data:image/png;base64,iVBOR"))

    def test_prompt_mentions_skip(self) -> None:
        self.assertIn("SKIP", ANSWER_PROMPT)


if __name__ == "__main__":
    unittest.main()
