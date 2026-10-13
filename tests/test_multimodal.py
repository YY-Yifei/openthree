"""多模态：图片能进上下文、能跨轮保留、不撑爆预算、不写进日志。

这是用户诉求（「支持下多模态」）的回归测试。全部不联网、不调真实模型。
"""
from __future__ import annotations

import base64
import json

import pytest

from openthree.agent import Agent, _estimate_chars
from openthree.llm import LLM, LLMConfig, _redact
from openthree.multimodal import (ImageError, ImageResult, build_content,
                                  content_text, count_images, image_result,
                                  sniff_mime, to_data_url)
from openthree.recorder import Recorder
from openthree.runtime import Runtime
from openthree.workspace import Workspace

# 1x1 像素的合法 PNG
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture
def png(tmp_path):
    p = tmp_path / "pic.png"
    p.write_bytes(PNG_BYTES)
    return p


# ---------------------------------------------------------------- 基础编解码
def test_sniff_mime_recognises_png(png):
    assert sniff_mime(png.read_bytes()) == "image/png"


def test_to_data_url_encodes_local_file(png):
    url = to_data_url(png)
    assert url.startswith("data:image/png;base64,")
    raw = base64.b64decode(url.split("base64,", 1)[1])
    assert raw == PNG_BYTES


def test_to_data_url_passes_http_through():
    assert to_data_url("https://example.com/a.png") == "https://example.com/a.png"


def test_to_data_url_missing_file_raises(tmp_path):
    with pytest.raises(ImageError):
        to_data_url(tmp_path / "nope.png")


def test_to_data_url_rejects_oversized(tmp_path, monkeypatch):
    big = tmp_path / "big.png"
    big.write_bytes(PNG_BYTES + b"\x00" * 5000)
    monkeypatch.setenv("OPENTHREE_MAX_IMAGE_BYTES", "100")
    with pytest.raises(ImageError):
        to_data_url(big)


# ---------------------------------------------------------------- content 组装
def test_build_content_without_images_stays_string():
    assert build_content("你好") == "你好"
    assert isinstance(build_content("你好", []), str)


def test_build_content_with_image_is_parts(png):
    c = build_content("看这张", [png])
    assert isinstance(c, list)
    assert c[0] == {"type": "text", "text": "看这张"}
    assert c[1]["type"] == "image_url"
    assert c[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_build_content_degrades_on_bad_image():
    c = build_content("看", ["/definitely/not/here.png"])
    assert isinstance(c, list)
    assert "图片加载失败" in c[-1]["text"]      # 单张坏图不拖垮整条消息


def test_content_text_and_count(png):
    c = build_content("两句话", [png, png])
    assert count_images(c) == 2
    assert "两句话" in content_text(c) and "2 张图片" in content_text(c)
    assert content_text("普通字符串") == "普通字符串"


def test_image_result_str_never_leaks_base64(png):
    r = image_result("已加载", [png])
    assert base64.b64encode(PNG_BYTES).decode() not in str(r)
    assert "1 张图片" in str(r)


# ---------------------------------------------------------------- 预算估算
def test_estimate_counts_image_as_fixed_cost(png):
    """图片按固定成本计，绝不按 base64 长度 —— 否则一张图就撑爆预算。"""
    hist = [{"role": "user", "content": build_content("图", [png])}]
    n = _estimate_chars(hist)
    assert n < 5000                              # 远小于 base64 的实际长度
    assert n > 100


def test_estimate_image_not_proportional_to_size():
    small = [{"role": "user", "content": build_content("x", ["http://a/1.png"])}]
    a = _estimate_chars(small)
    big = [{"role": "user", "content": build_content("x", ["http://a/1.png"] * 3)}]
    b = _estimate_chars(big)
    assert b > a                                  # 图多确实更贵，但是线性、有界


# ---------------------------------------------------------------- Agent 注入
def _agent(ws, registry, **kw) -> Agent:
    rec = Recorder(ws, user_input="t", model="stub")
    return Agent(ws, registry, LLM(LLMConfig(api_key="k", base_url="u")), rec, **kw)


def test_inject_images_appends_current_turn_message(ws, registry, png):
    ag = _agent(ws, registry)
    ag.history.append({"role": "user", "content": "旧问题"})
    ag.history.append({"role": "assistant", "content": "旧回答"})
    ag.task_text = "描述一下"
    ag.current_images = [str(png)]
    ag._inject_images()
    # 新起一条 user 消息，而不是污染旧的跨轮消息
    assert count_images(ag.history[-1]["content"]) == 1
    assert "描述一下" in content_text(ag.history[-1]["content"])
    assert ag.history[0]["content"] == "旧问题"


def test_inject_images_is_idempotent(ws, registry, png):
    ag = _agent(ws, registry)
    ag.current_images = [str(png)]
    ag._inject_images()
    ag._inject_images()                          # 再来一次
    assert sum(count_images(m.get("content")) for m in ag.history) == 1


def test_inject_images_not_attached_to_seeded_turns(ws, registry, png):
    """跨轮播种的旧轮次不能被本轮图片污染。"""
    ag = _agent(ws, registry, seed_turns=[
        {"user": "上一轮的问题", "answer": "上一轮的回答"}])
    ag.task_text = "这一轮的问题"
    ag.current_images = [str(png)]
    ag._inject_images()
    assert count_images(ag.history[0]["content"]) == 0      # 旧轮次干净
    assert count_images(ag.history[-1]["content"]) == 1     # 本轮带图


def test_tool_image_result_becomes_user_message(ws, registry, png):
    """工具返回 ImageResult → tool 消息是文本，图片另走一条 user 消息。"""
    ag = _agent(ws, registry)

    def fake(**kwargs):
        return image_result("生成了一张图", [str(png)])

    ag.reg.register_builtin("fake_view", fake)
    ag._run_tool_calls([{"id": "c1", "function": {"name": "fake_view", "arguments": "{}"}}])

    roles = [m["role"] for m in ag.history]
    assert roles == ["tool", "user"]             # 序列合法：tool 先补齐，user 后附
    assert count_images(ag.history[1]["content"]) == 1
    assert "生成了一张图" in content_text(ag.history[1]["content"])


# ---------------------------------------------------------------- 跨轮记忆
def test_transcript_roundtrips_images(ws, png):
    ws.append_turn("看图", "看到了", images=[str(png)])
    turns = ws.read_turns(1)
    assert turns[0]["images"] == [str(png)]
    # 档案里绝不能出现 base64
    assert "base64" not in ws.transcript_file.read_text(encoding="utf-8")


def test_agent_seeds_prior_turn_images(ws, registry, png):
    ag = _agent(ws, registry, seed_turns=[
        {"user": "先看这张", "answer": "好", "images": [str(png)]}])
    assert count_images(ag.history[0]["content"]) == 1
    assert "先看这张" in content_text(ag.history[0]["content"])


def test_turns_without_images_still_plain_string(ws, registry):
    ag = _agent(ws, registry, seed_turns=[{"user": "没有图", "answer": "好的"}])
    assert ag.history[0]["content"] == "没有图"   # 行为与从前逐字节一致


# ---------------------------------------------------------------- user-prompts 标记
def test_submit_writes_and_parses_image_marker(ws, png):
    ws.submit("分析这张图", images=[str(png)])
    text = ws.read_user_prompt()
    assert ws.parse_images_from_prompt(text) == [str(png)]
    assert ws.strip_images_marker(text).startswith("# 当前任务")
    assert "openthree:images" not in ws.strip_images_marker(text)


def test_submit_append_drops_stale_image_marker(ws, png):
    ws.submit("第一件", images=["http://a/old.png"])
    ws.submit("第二件", append=True, images=[str(png)])
    imgs = ws.parse_images_from_prompt(ws.read_user_prompt())
    assert imgs == [str(png)]                    # 旧图标记被剥掉，只留本轮


def test_scan_image_paths_from_free_text():
    ws = Workspace(__import__("pathlib").Path("."))
    got = ws.scan_image_paths("看看 ./a/b.png 和 http://x/y.jpg 以及 notes.txt")
    assert "./a/b.png" in got
    assert "http://x/y.jpg" in got
    assert "notes.txt" not in got


# ---------------------------------------------------------------- 日志脱敏
def test_redact_strips_base64_from_logs(png):
    payload = {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": to_data_url(png)}}]}]}
    safe = _redact(payload)
    blob = json.dumps(safe, ensure_ascii=False)
    assert base64.b64encode(PNG_BYTES).decode() not in blob
    assert "省略" in blob


def test_redact_keeps_api_key_out():
    assert "api_key" not in _redact({"api_key": "sk-x", "model": "m"})


def test_record_llm_redacts_base64_in_disk_log(ws, png):
    """纵深防御：即便有人绕过 LLM._redact 直接调 record_llm，日志也不留 base64。"""
    rec = Recorder(ws, user_input="t", model="stub")
    payload = {"model": "stub", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": to_data_url(png)}}]}]}
    rec.record_llm(payload, {"choices": []}, 1.0)
    logs = list((ws.logs_dir / "llm").glob("llm-*.json"))
    blob = logs[-1].read_text(encoding="utf-8")
    assert base64.b64encode(PNG_BYTES).decode() not in blob
    assert "省略" in blob


# ---------------------------------------------------------------- 工具
def test_view_image_builtin_returns_image_result(png):
    from openthree.builtins import view_image
    r = view_image(str(png))
    assert isinstance(r, ImageResult) and r.images == [str(png)]
    assert isinstance(view_image(""), ImageResult)   # 空路径不抛，返回错误说明


# ---------------------------------------------------------------- Runtime 端到端
class _EchoLLM:
    """记录收到的 messages，然后 finish。"""

    def __init__(self, cfg):
        self.config = cfg
        self.seen = []

    def chat(self, messages, tools=None, tool_choice="auto", **kwargs):
        self.seen = messages
        msg = {"role": "assistant", "content": "",
               "tool_calls": [{"id": "c1", "type": "function",
                               "function": {"name": "finish",
                                            "arguments": json.dumps({"answer": "看图完成"})}}]}
        return {"message": msg, "usage": {}, "elapsed_ms": 1.0,
                "raw_request": {"model": "stub", "messages": messages},
                "raw_response": {"choices": [{"message": msg}]}, "error": None}


def test_runtime_run_with_images_reaches_model(tmp_path, png):
    rt = Runtime(tmp_path)
    rt.config.api_key = rt.config.base_url = "stub"
    rt.config.model = "stub-model"
    stub = _EchoLLM(rt.config)
    rt.llm = stub

    doc = rt.run("这是什么", images=[str(png)])
    assert doc["answer"] == "看图完成"

    # 送进模型的消息里，确实有一条带图片的 user 消息
    assert any(m["role"] == "user" and count_images(m.get("content")) == 1
               for m in stub.seen)
    # 图片被归档进 assets
    assert any((rt.ws.assets_dir).glob("*.png"))
    # transcript 记下了图片路径
    turns = rt.ws.read_turns(1)
    assert turns[0]["images"]
    assert "base64" not in rt.ws.transcript_file.read_text(encoding="utf-8")


def test_runtime_images_persist_via_user_prompts(tmp_path, png):
    """不显式传 images 时，也能从 user-prompts.md 的标记还原图片。"""
    rt = Runtime(tmp_path)
    rt.config.api_key = rt.config.base_url = "stub"
    rt.config.model = "stub-model"
    rt.llm = _EchoLLM(rt.config)
    rt.run("带图任务", images=[str(png)])

    rt2 = Runtime(tmp_path)
    rt2.config.api_key = rt2.config.base_url = "stub"
    rt2.config.model = "stub-model"
    stub = _EchoLLM(rt2.config)
    rt2.llm = stub
    rt2.run(None)                                 # 沿用 user-prompts.md
    assert any(m["role"] == "user" and count_images(m.get("content")) == 1
               for m in stub.seen)
