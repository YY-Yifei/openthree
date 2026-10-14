"""三维报告：把工作区里所有的 json 记录渲染成一个自包含的 HTML。

`.openthree/three-report.html` 直接双击打开即可看到智能体工作的情况：
思考、工具调用、结果、自我演化、token 消耗，一页看全。

无外部依赖（不引 CDN、不引 css 文件），纯内联，离线可看。
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from .workspace import DEFAULT_AGENT_NAME, Workspace

KIND_STYLE = {
    "llm": ("#6366f1", "LLM"),
    "tool": ("#0ea5e9", "工具"),
    "evolution": ("#f59e0b", "演化"),
    "note": ("#8b5cf6", "思考"),
    "finish": ("#10b981", "完成"),
    "error": ("#ef4444", "错误"),
    "system": ("#64748b", "系统"),
}


def _load_sessions(ws: Workspace) -> List[Dict[str, Any]]:
    out = []
    for p in sorted(ws.logs_dir.glob("session-*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            continue
    out.sort(key=lambda d: d.get("started_at", ""), reverse=True)
    return out


def _esc(x: Any) -> str:
    return html.escape(str(x if x is not None else ""))


def _pre(x: Any, limit: int = 4000) -> str:
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, indent=2)
    if len(s) > limit:
        s = s[:limit] + f"\n...（截断，共 {len(s)} 字符）"
    return f"<pre>{_esc(s)}</pre>"


def build_html(ws: Workspace) -> str:
    sessions = _load_sessions(ws)
    evolution = _load_evolution(ws)
    memory = _load_memory(ws)
    funcs = _list_functions(ws)
    identity = ws.read_identity()
    label = identity.get("name") or f"{DEFAULT_AGENT_NAME}（未命名）"
    motto = identity.get("motto") or ""

    total_events = sum(s.get("stats", {}).get("events", 0) for s in sessions)
    total_tools = sum(s.get("stats", {}).get("tool_calls", 0) for s in sessions)
    total_llm = sum(s.get("stats", {}).get("llm_calls", 0) for s in sessions)
    total_evo = sum(s.get("stats", {}).get("evolutions", 0) for s in sessions)

    parts: List[str] = []
    parts.append(_head(label))

    # 概览
    parts.append(f"""
<section class="hero">
  <h1>{_esc(label)}</h1>
  <p class="sub">{_esc(motto) if motto else 'openthree 智能体工作记录'}</p>
  <div class="cards">
    {_card('会话', len(sessions))}
    {_card('事件', total_events)}
    {_card('模型调用', total_llm)}
    {_card('工具调用', total_tools)}
    {_card('自我演化', total_evo)}
    {_card('functions 文件', len(funcs))}
  </div>
  <p class="meta">工作区 <code>{_esc(ws.dir)}</code> · 生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
</section>""")

    if not sessions:
        parts.append('<section><p class="empty">还没有会话记录。运行 <code>openthree "你的任务"</code> 开始。</p></section>')

    # 会话
    for idx, s in enumerate(sessions):
        parts.append(_render_session(s, idx == 0))

    # 演化史
    parts.append(_render_evolution(evolution))
    # functions
    parts.append(_render_functions(funcs))
    # 记忆
    if memory:
        parts.append(_render_memory(memory))

    parts.append(_tail())
    return "\n".join(parts)


def _card(label: str, value: Any) -> str:
    return f'<div class="card"><div class="v">{_esc(value)}</div><div class="l">{_esc(label)}</div></div>'


def _render_session(s: Dict[str, Any], open_by_default: bool) -> str:
    stats = s.get("stats", {})
    sid = s.get("session_id", "session")
    op = " open" if open_by_default else ""
    out = [f"""
<section class="session">
  <details{op}>
    <summary>
      <span class="badge">{_esc(s.get('status', '?'))}</span>
      <b>{_esc(sid)}</b>
      <span class="dim">{_esc(s.get('started_at', '')[:19])} · {_esc(s.get('model', ''))}</span>
      <span class="dim right">{stats.get('steps', 0)} 步 / {stats.get('tool_calls', 0)} 工具 / {stats.get('llm_calls', 0)} 模型</span>
    </summary>
    <div class="body">
      <div class="task"><b>任务</b>{_pre(s.get('user_input', ''))}</div>"""]

    if s.get("answer"):
        out.append(f'<div class="answer"><b>最终答复</b>{_pre(s["answer"])}</div>')

    out.append('<ol class="timeline">')
    for ev in s.get("events", []):
        out.append(_render_event(ev))
    out.append("</ol></div></details></section>")
    return "\n".join(out)


def _render_event(ev: Dict[str, Any]) -> str:
    kind = ev.get("kind", "system")
    color, label = KIND_STYLE.get(kind, ("#64748b", kind))
    data = ev.get("data", {}) or {}
    ms = ev.get("elapsed_ms")
    time_s = ev.get("ts", "")[11:19]
    badge = f'<span class="k" style="--c:{color}">{_esc(label)}</span>'
    meta = f'<span class="dim">#{ev.get("seq")} · {time_s}' + (f' · {ms:.0f}ms' if ms else "") + "</span>"

    inner = [f'<div class="ev-head">{badge}{meta}</div>']
    summary = ev.get("summary", "")
    if summary:
        inner.append(f'<div class="ev-sum">{_esc(summary)}</div>')

    # 按 kind 展开细节
    if kind == "tool":
        if data.get("arguments"):
            inner.append(f'<div class="detail"><b>参数</b>{_pre(data["arguments"], 1500)}</div>')
        if data.get("output"):
            inner.append(f'<div class="detail"><b>结果</b>{_pre(data["output"], 3000)}</div>')
        if data.get("error"):
            inner.append(f'<div class="detail err"><b>错误</b>{_pre(data["error"])}</div>')
    elif kind == "llm":
        inner.append(f'<div class="detail"><b>用量</b>{_pre(data.get("usage", {}), 600)}'
                     f'<span class="dim">请求 {data.get("messages", "?")} 条消息 · '
                     f'{data.get("tools", 0)} 个工具 · 原文 logs/llm/{_esc(data.get("file", ""))}</span></div>')
    elif kind == "evolution":
        action = data.get("action", "")
        inner.append(f'<div class="detail"><b>动作</b> {_esc(action)}</div>')
        if data.get("code"):
            inner.append(f'<div class="detail"><b>代码</b>{_pre(data["code"], 4000)}</div>')
        if data.get("content"):
            inner.append(f'<div class="detail"><b>内容</b>{_pre(data["content"], 4000)}</div>')
        if data.get("summary") and action == "compact_applied":
            inner.append(f'<div class="detail"><b>摘要</b>{_pre(data["summary"], 1500)}</div>')
        if data.get("added") is not None:
            inner.append(f'<div class="detail"><b>新增</b> {_esc(", ".join(data.get("added") or []) or "无")}'
                         f'　<b>移除</b> {_esc(", ".join(data.get("removed") or []) or "无")}</div>')
    elif kind == "note":
        if data.get("thought") and data["thought"] != summary:
            inner.append(f'<div class="detail">{_pre(data["thought"], 3000)}</div>')
        if data.get("tags"):
            inner.append(f'<div class="dim">标签: {_esc(", ".join(data["tags"]))}</div>')
    elif kind == "finish":
        inner.append(f'<div class="detail"><b>答复</b>{_pre(data.get("answer", ""))}</div>')
    elif kind == "error":
        inner.append(f'<div class="detail err">{_pre(data)}</div>')

    return f'<li class="ev ev-{_esc(kind)}">{"".join(inner)}</li>'


def _load_evolution(ws: Workspace) -> List[Dict[str, Any]]:
    path = ws.logs_dir / "evolution.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return out


def _load_memory(ws: Workspace) -> List[Dict[str, Any]]:
    """长期记忆条目 —— 复用 workspace 的唯一解析口径（容错、跳过坏行）。"""
    return ws.read_memory()


def _list_functions(ws: Workspace) -> List[Dict[str, str]]:
    out = []
    for p in ws.list_function_files():
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            text = ""
        out.append({"name": p.name, "code": text, "size": f"{len(text)}B"})
    return out


def _render_evolution(evolution: List[Dict[str, Any]]) -> str:
    if not evolution:
        return '<section><h2>自我演化史</h2><p class="empty">还没有演化记录。当模型写出新 function 或改写提示词时，会出现在这里。</p></section>'
    rows = []
    for ev in evolution:
        data = ev.get("data", {}) or {}
        rows.append(f"""<li><span class="dim">{_esc(ev.get('ts','')[:19])}</span>
        <b>{_esc(data.get('action',''))}</b> — {_esc(ev.get('summary',''))}</li>""")
    return f'<section><h2>自我演化史 <span class="dim">({len(evolution)})</span></h2><ol class="plain">{"".join(rows)}</ol></section>'


def _render_functions(funcs: List[Dict[str, str]]) -> str:
    if not funcs:
        return ('<section><h2>functions</h2><p class="empty">'
                '<code>.openthree/functions/</code> 还是空的。模型的自造能力从这里生长。</p></section>')
    blocks = []
    for f in funcs:
        blocks.append(f"""<details><summary><b>{_esc(f['name'])}</b> <span class="dim">{_esc(f['size'])}</span></summary>
        {_pre(f['code'], 6000)}</details>""")
    return f'<section><h2>functions <span class="dim">({len(funcs)})</span></h2>{"".join(blocks)}</section>'


def _render_memory(memory: List[Dict[str, Any]]) -> str:
    rows = []
    for m in memory[-50:]:
        tags = " ".join(f'<span class="tag">{_esc(t)}</span>' for t in (m.get("tags") or []))
        rows.append(f'<li><span class="dim">{_esc(m.get("ts",""))}</span> {_esc(m.get("content",""))} {tags}</li>')
    return f'<section><h2>长期记忆 <span class="dim">({len(memory)})</span></h2><ol class="plain">{"".join(rows)}</ol></section>'


def _head(label: str = DEFAULT_AGENT_NAME) -> str:
    return ("""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__OPENTHREE_TITLE__</title>""".replace("__OPENTHREE_TITLE__", f"{_esc(label)} · openthree")
            + """
<style>
:root{--bg:#0b1020;--fg:#e6e9f5;--dim:#8b93b0;--line:#232a44;--card:#141a30;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.65 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
section{max-width:1120px;margin:0 auto 28px;padding:0 20px}
h1{font-size:44px;margin:0 0 6px;letter-spacing:6px;background:linear-gradient(90deg,#6366f1,#0ea5e9,#10b981);-webkit-background-clip:text;background-clip:text;color:transparent}
h2{font-size:19px;margin:28px 0 12px;border-left:3px solid #6366f1;padding-left:10px}
.hero{padding:48px 20px 8px}
.sub{color:var(--dim);margin:0 0 22px}
.cards{display:flex;gap:12px;flex-wrap:wrap}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 20px;min-width:104px}
.card .v{font-size:26px;font-weight:700}
.card .l{color:var(--dim);font-size:12px;margin-top:2px}
.meta{color:var(--dim);font-size:12px;margin-top:16px}
code{background:#1b2340;padding:1px 6px;border-radius:5px;font-size:12px}
.session details{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.session summary{cursor:pointer;padding:14px 18px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.session summary b{font-family:ui-monospace,Consolas,monospace}
.body{padding:0 18px 18px}
.dim{color:var(--dim);font-size:12px}
.right{margin-left:auto}
.badge{background:#10b98122;color:#10b981;border:1px solid #10b98155;border-radius:20px;padding:1px 10px;font-size:12px}
.timeline{list-style:none;padding:0;margin:12px 0 0;border-left:1px solid var(--line)}
.ev{position:relative;padding:8px 0 8px 18px;margin-left:8px}
.ev:before{content:"";position:absolute;left:-5px;top:16px;width:9px;height:9px;border-radius:50%;background:var(--c,#64748b)}
.ev-head{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.k{color:var(--c);border:1px solid var(--c);border-radius:20px;padding:0 9px;font-size:11px}
.ev-sum{margin:3px 0 0;white-space:pre-wrap;word-break:break-word}
.ev-note .ev-sum,.ev-finish .ev-sum{color:#c7cdf0}
.detail{margin-top:6px;background:#0f1528;border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:13px}
.detail b{color:var(--dim);font-weight:600;margin-right:6px;display:inline-block}
.detail.err{border-color:#ef444455;background:#2a1015}
pre{margin:6px 0 0;white-space:pre-wrap;word-break:break-word;font:12px/1.55 ui-monospace,Consolas,monospace;max-height:420px;overflow:auto}
.task,.answer{background:#0f1528;border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:12px 0}
.plain{list-style:none;padding:0}
.plain li{padding:7px 0;border-bottom:1px solid var(--line)}
.tag{background:#6366f133;color:#a5b4fc;border-radius:5px;padding:0 6px;font-size:11px;margin-left:4px}
.empty{color:var(--dim)}
footer{text-align:center;color:var(--dim);padding:28px;font-size:12px}
</style></head><body>""")


def _tail() -> str:
    return '<footer>openthree 智能体工作记录 · 由 openthree 生成</footer></body></html>'


def write_report(ws: Workspace) -> Path:
    ws.report_file.write_text(build_html(ws), encoding="utf-8")
    return ws.report_file
