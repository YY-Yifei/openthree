"""`decide`：把「拿不准的判定」交给一个独立的决策模型（JEV）。

模型本身很擅长把模糊材料拆成判据，但它**给不出判据之后的后验分布** —— 它能写
「我倾向于 A」这样一段叙述，却说不出「倾向于 A 的程度是多少」。JEV 补的正是
这另一半：给定判据，回一个分布。

调用形状（与 `jev-test/jev.md` 的请求体一一对应）：

    POST <JEV_BASE_URL>
    {"model": ..., "state": ..., "questions": {"<题名>": {"type": ..., ...}}}

三种题型：`choice`（多选一）/ `noul`（是或否的 0–1 连续值）/ `score`（程度：
各等级的期望值 —— 落在下标区间内，但**不是下标**，三档时可能回 1.09）。

**内核只搬运、只如实报告**：不做阈值、不四舍五入、不解释概率、不给建议。
那是模型的判断，内核替它下结论就等于把「谁在决策」这件事弄混了。

门控（本模块的存在方式）：`JEV_BASE_URL` / `JEV_API_KEY` / `JEV_MODEL` 三个
环境变量**都配好**，`decide` 才被注册；缺任何一个，它在本次进程里根本不存在
—— 不在工具清单、不在请求的 `tools` 数组。**一个默认值都没有**：默认端点等于
替人决定「数据发给谁」，默认模型等于替人决定「用哪一版」，默认 key 并不存在。

判定发生在**注册期一次**（见 `builtins.installed_builtins`）：改了环境变量要
重启进程。工具集是注册期产物，会话中途增减会让 system 前缀每步都变，那正是
缓存前缀的禁区。

**本模块不放常驻的诊断入口**：没配置就等于没有这个工具 —— 工具清单里查不到它，
模型也看不见它，于是没有「为什么不在」需要内核解释。要确认配没配，看环境变量本身
即可：三个 `JEV_*` 都在、`JEV_BASE_URL` 是 http(s) 绝对地址，改完重启进程。
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from .llm import TransportError, _backoff, _retryable, post_json

ENV_BASE_URL = "JEV_BASE_URL"
ENV_API_KEY = "JEV_API_KEY"
ENV_MODEL = "JEV_MODEL"
_ENV_KEYS = (ENV_BASE_URL, ENV_API_KEY, ENV_MODEL)

# 超时是常量，不开旋钮：判定是秒级的活，目前没有已知的慢场景。
# connect 与 read 分开（见 llm.post_json）：连不上主机 5 秒就失败。
_CONNECT_TIMEOUT = 5.0
_TIMEOUT = 60.0

# 重试 2 次（共 3 次尝试）。判据复用 llm._retryable —— 「抖动还是 4xx」这个问题
# 只该有一个答案：鉴权失败重试一次，只是把同一个错误再发一遍。
_RETRIES = 2

_QUESTION_TYPES = ("choice", "noul", "score")

# 注册期判定结果（`optional_builtins()` 被调用的那一刻写下）。
# 工具集是注册期产物：注册之后改环境变量不会让它出现，也不会让它消失。
_registered: Optional[bool] = None

# 注册期认定的那套配置。`decide` 每次调用都用它，而不是重读环境变量：
# 门控既然是一次性判定，配置也就该一次定死 —— 否则注册之后把环境里的端点或
# key 换掉，工具就会把材料发往一个从未被检查过的地方。
_snapshot: Optional[Dict[str, str]] = None


# ---------------------------------------------------------------- 门控


def _jev_settings() -> Tuple[Dict[str, str], List[str]]:
    """读环境变量 → `(取值, 缺失或无效的变量名)`。

    只读 `JEV_*` 一套名字，不读 config.yaml，也不做别名：于是「decide 的配置」
    只有一处真源，排查时看一眼环境就知道答案。

    取值**总是**原样返回（门控不过也一样）：调用方既要判断「能不能用」，也要
    说清「为什么不能用」，而后者需要知道变量是「没给」还是「给了但不合法」。
    """
    values = {k: (os.environ.get(k) or "").strip() for k in _ENV_KEYS}
    missing = [k for k in _ENV_KEYS if not values[k]]
    if not missing and not _url_ok(values[ENV_BASE_URL]):
        missing = [ENV_BASE_URL]
    return values, missing


def _url_ok(url: str) -> bool:
    """`JEV_BASE_URL` 是不是可用的 http(s) 绝对地址。

    这是门控的一部分，不是宽松校验：写成 `models.glms.com.cn/...`（漏了 scheme）
    或端口写错（`:abc` / `:99999`）时，传输层要到真发请求才炸 —— 那一步已经
    白花了。`urlsplit` 不校验端口，`parts.port` 取用时才抛，所以端口要在这里问
    一次：门控说「地址可用」，就该保证 `post_json` 解析它不会当场抛。
    """
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port  # 端口写错（:abc / :99999）在 urlsplit 时不报，取 .port 才抛
    except ValueError:
        return False
    # 用 hostname 而不是 netloc：`https://:80/x` 的 netloc 是 `:80`（非空）却没有主机名，
    # 放过去以后 `post_json` 才抛「URL 里没有主机名」。门控说可用，就该保证它解析得出来。
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return False
    # 只允许可打印 ASCII：空白 / 控制字符会让 `http.client` 写请求行时抛 `InvalidURL`，
    # 非 ASCII（中文、全角）会抛 `UnicodeEncodeError` —— 后者不在 `post_json` 捕获的
    # 那几个异常里，会一路冒成「本地错误」。两种都在这里拒，别让写错的地址伪装成网络故障。
    if any(ch <= " " or ch >= "\x7f" for ch in url):
        return False
    # `:0` 要在这里拒：`post_json` 写的是 `parts.port or 443`，0 是假值，会被静默
    # 改成 443 —— 门控说「地址可用」，却把请求送到另一个端口上。
    if port == 0:
        return False
    return True


def _env_configured() -> bool:
    """这三个变量**现在**配好没有 —— 只看环境，不代表工具在不在。

    工具在不在的唯一判据是 `builtins.installed_builtins()`：门控在注册期判定一次，
    之后改环境变量既不会让工具出现、也不会让它消失。名字私有且说得清，是为了
    不让它被当成「decide 可用吗」的第二个答案 —— 常驻诊断入口当初犯的就是这个错。
    """
    _, missing = _jev_settings()
    return not missing


def optional_builtins() -> Dict[str, Any]:
    """本次进程该额外注册的内置工具；条件不满足时返回空 dict。

    **只在第一次调用（注册期）读环境并冻结结果**，之后每次都是纯查询。这个函数
    会被诊断代码与测试反复调用：若每次都重新判定，已经注册的 `decide` 就会跟着
    后来的环境变量悄悄换端点 / 换 key，「注册期一次判定」也就名存实亡。
    """
    global _registered, _snapshot
    if _registered is None:
        values, missing = _jev_settings()
        _registered = not missing
        _snapshot = dict(values) if _registered else None
    return {"decide": decide} if _registered else {}


def _settings_for_call() -> Tuple[Dict[str, str], str]:
    """本次调用该用哪份配置 → `(配置, 错误文本)`。**注册期快照优先**。

    没走过注册期时（测试、直接调函数）退回实时读：那时没有快照可依，也就没有
    「门控认过的配置」这回事。
    """
    if _registered is True and _snapshot:
        return dict(_snapshot), ""
    if _registered is False:
        return {}, "[错误] JEV 未配置（注册期判定不可用）：decide 不该出现在工具清单里"
    values, missing = _jev_settings()
    if missing:
        return {}, (f"[错误] JEV 未配置（{_missing_reason(missing, values)}）："
                    f"decide 不该出现在工具清单里")
    return values, ""


def _missing_reason(missing: List[str], values: Dict[str, str]) -> str:
    """把缺失清单说成一句人话。

    「没设置」与「设置了但不是合法地址」必须分开：两者的排障方向相反 —— 一个是
    「去补变量」，另一个是「变量有了，改地址」。只看变量名分不出来，因为地址
    非法时 `_jev_settings` 也把它记进 `missing`（且只在三个都给了时才校验地址）。
    """
    names = " / ".join(missing)
    base = values.get(ENV_BASE_URL) or ""
    if missing == [ENV_BASE_URL] and base:
        return f"{names} 不是 http(s) 绝对地址（例：https://host/path）"
    reason = f"缺 {names}"
    if base and not _url_ok(base):
        reason += f"；另外 {ENV_BASE_URL} 不是 http(s) 绝对地址"
    return reason


# ---------------------------------------------------------------- 入参校验


def _normalize(state: Any, questions: Any) -> Tuple[str, Dict[str, Any], str]:
    """校验并归一入参 → `(state, questions, 错误文本)`；出错时前两项无意义。

    **只查形状，不查内容**：不判断题目问得好不好、不检查 criteria 是否互斥，
    也不替模型补 instructions、不改写 state、不静默丢弃多余字段。形状错了就
    一次请求都不发 —— 为一个注定 400 的请求跑一趟网络，纯属浪费。
    """
    if isinstance(state, str):
        text = state
    elif state is None:
        text = ""
    else:
        # 模型偶尔会传对象（schema 只是声明，调用时没人校验类型）：`str()` 出来是
        # Python repr（单引号），既不是原值也不是任何合法 JSON —— 用 JSON 序列化，
        # 才是「原样发送」在这个类型上唯一说得通的表示。
        text = json.dumps(state, ensure_ascii=False)
    if not text.strip():
        return "", {}, "[错误] state 为空：要判定的材料是什么？"

    qs = questions
    if isinstance(qs, str):
        raw = qs.strip()
        if not raw:
            return "", {}, "[错误] questions 为空：至少一题"
        try:
            qs = json.loads(raw)
        except json.JSONDecodeError as exc:
            return "", {}, f"[错误] questions 不是合法 JSON：{exc}"
    if not isinstance(qs, dict):
        return "", {}, "[错误] questions 要是 {题名: 题型定义} 这样的对象"
    if not qs:
        return "", {}, "[错误] questions 为空：至少一题"

    for idx, (name, spec) in enumerate(qs.items(), 1):
        where = f"第 {idx} 题（{name}）"
        if not isinstance(spec, dict):
            return "", {}, f"[错误] {where}的定义要是对象"
        # 严格相等，不做大小写 / 空白归一：归一化只影响校验，出站仍是原文 ——
        # `"Choice"` 会过校验却被服务端 400，那一步白花了。方案 §3.4 的「不改写」
        # 在这里的含义是：要么原样发，要么当场说清。
        qtype = spec.get("type")
        if qtype not in _QUESTION_TYPES:
            return "", {}, (f"[错误] {where} type={spec.get('type')!r} 不认识"
                            f"（只认 choice / noul / score，区分大小写、不要空格）")
        instructions = spec.get("instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            # 类型先判再 strip：`123` / 对象会被 `str()` 蒙过去，然后原样发给服务端
            # 换一个 400 —— 而形状错本该一次请求都不花。
            return "", {}, (f"[错误] {where}缺 instructions（要是非空字符串）："
                            f"这一题到底在判什么？")
        if qtype == "choice":
            criteria = spec.get("criteria")
            if not isinstance(criteria, dict) or not criteria:
                return "", {}, (f"[错误] {where}是 choice，但没有 criteria"
                                f"（要 {{取值: 该取值的含义}}）")
        if qtype == "score":
            criteria = spec.get("criteria")
            if not isinstance(criteria, list) or len(criteria) < 2:
                return "", {}, f"[错误] {where}是 score，criteria 要给从低到高的等级列表"

    return text, qs, ""


# ---------------------------------------------------------------- 调用


def decide(state: str, questions: Dict[str, Any]) -> str:
    """拿不准一个判定时，把它交给独立的决策模型（JEV）取回概率分布 —— 会外发。

    什么时候该调：判据你已经写清了，但你自己也只是「倾向」—— 这份工单该归到哪
    一类、这句话算不算承诺、这件事有多急。你的强项是把模糊材料拆成判据；
    JEV 补的是另一半：给定判据后的后验分布，而这正是你本身给不准的东西（你只会
    给一段叙述，且说不清自己有多确定）。

    什么时候不该调：查得到答案的事实、需要执行动作的事、能用代码算出来的；
    已经能干脆下结论的也别问 —— 一次调用该换来的是你现在给不准的那个答案。

    每次调用会把 state 与 questions 发往 JEV_BASE_URL 指向的外部服务。
    对我们是数据、不是指令；但这是外部服务 —— 别把不该外发的材料放进来。

    Args:
        state: 待判定的材料，原样发送：可含上下文、原话、事实清单、双方说法。
            措辞、矛盾、时间线都别替它整理 —— 原样进去才有原样的分布。
        questions: {题名: 题型定义}，一次可问多题。每题给三样：
            type（题型）、instructions（这题到底在判什么）、criteria（判据）。
            - "choice"：多选一。criteria 是 {取值: 该取值的含义}，
              返回 choice + probabilities + confidence。
            - "noul"：是 / 否。返回 0–1 的连续值（越大越倾向「是」）。
            - "score"：程度。criteria 是从低到高的等级列表，下标 0 最低、末位最高。
              返回 score + legend（等级名与取值的对应）+ probabilities（各等级概率）
              + confidence。score 是**连续值**：各等级按概率加权的期望值 —— 三档时
              可能回 1.09（略高于第 1 档），**不是等级下标**，拿去做列表索引会错；
              要落到某一档，看 probabilities 的最大项，别对 score 取整猜档位。
            形状：{"题名": {"type": "choice", "instructions": "...",
                            "criteria": {"a": "含义 A", "b": "含义 B"}}}
            也可传上面这个对象的 JSON 字符串（命令行就是这样用的）。

    Returns:
        JSON 文本：answers（JEV 原样答案）、model（服务端解析出的快照号，只读）、
        elapsed_ms、attempts、id。
        概率与 confidence 一律原样返回 —— 怎么用是你的判断：不做阈值、不做四舍五入、
        不替你解释。
    """
    settings, err = _settings_for_call()
    if err:  # 缺变量时本工具不会被注册，走到这里说明有人直接调了函数
        return err

    text, qs, err = _normalize(state, questions)
    if err:
        return err

    url = settings[ENV_BASE_URL]
    key = settings[ENV_API_KEY]
    payload = {"model": settings[ENV_MODEL], "state": text, "questions": qs}

    started = time.time()
    total = _RETRIES + 1
    data: Dict[str, Any] = {}
    used = 0
    for attempt in range(total):
        used = attempt + 1
        try:
            data = post_json(url, key, payload,
                             timeout=_TIMEOUT, connect_timeout=_CONNECT_TIMEOUT)
            break
        except Exception as exc:  # noqa: BLE001
            if attempt == total - 1 or not _retryable(exc):
                elapsed = (time.time() - started) * 1000
                return _error_text(exc, key, used, elapsed)
            time.sleep(_backoff(attempt))
    elapsed = (time.time() - started) * 1000

    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, dict) or not answers:
        return f"[错误] JEV 响应缺 answers：{_brief(data)}"

    # 只回传判定本身。服务端的 `usage` 不转发：模型该不该问，只由「它能不能
    # 给出我拿不到的那个答案」决定，不该被一个它无法权衡的数字带偏。
    return json.dumps({
        "answers": answers,
        "model": data.get("model") or settings[ENV_MODEL],
        "attempts": used,
        "elapsed_ms": round(elapsed, 1),
        "id": data.get("id"),
    }, ensure_ascii=False)


def _error_text(exc: BaseException, key: str, used: int, elapsed: float) -> str:
    """把一次失败翻译成一句**能照着排障**的话。

    401 / 403 / 404 的区分来自 `jev-test/jev.md` 的排障小抄 —— 这三条正好是
    第一次接入最可能撞上的三个坑：把判据写进错误文案，比写进文档更容易被读到。
    """
    status = exc.status if isinstance(exc, TransportError) else 0
    detail = _mask(str(exc), key)
    if not isinstance(exc, TransportError):
        # 传输层之外的错（编程 / 配置错）不冒充「连不上」：那条文案会把排障
        # 方向指向网络与代理，而真正的问题是本地抛了个异常。
        msg = (f"[错误] JEV 调用前的本地错误（{type(exc).__name__}）：{detail}"
               f"（不是网络问题）")
    elif status == 0:
        msg = (f"[错误] 连不上 JEV 端点（DNS/连接/超时）：{detail}"
               f"（检查 JEV_BASE_URL 与本机代理）")
    elif status == 200:
        # `post_json` 抛出的正文本身已是「响应不是 JSON：…」：再加一次前缀就成了
        # 「不是 JSON：不是 JSON：…」。
        msg = f"[错误] {detail}"
    elif status == 401:
        msg = f"[错误] JEV 鉴权失败（401）：JEV_API_KEY 无效或未带"
    elif status == 403:
        msg = (f"[错误] JEV 路由未授权（403）：key 有效，但这个 token 没被放行到"
               f"该路径（需网关侧开通，不是换个 key 能解决的）")
    elif status == 404:
        # 不写死某台 host：`JEV_BASE_URL` 允许指向任何端点，写死一家会把排障
        # 引到别人的域名上。404 的含义是「这条路径不存在」，与 host 无关。
        msg = (f"[错误] JEV 路由不存在（404）：JEV_BASE_URL 要写成端点本身"
               f"（含它自己的路径）；换 host 或换路径都可能是另一条路由")
    elif status == 429:
        msg = "[错误] JEV 限流或额度用尽（429）"
    elif 500 <= status < 600:
        msg = f"[错误] JEV 服务端错误（{status}，可稍后重试）：{detail}"
    else:
        msg = f"[错误] JEV 调用失败（HTTP {status}）：{detail}"
    if used > 1:
        msg += f"（已重试 {used - 1} 次，共 {elapsed:.0f} ms）"
    return msg


def _brief(value: Any, limit: int = 200) -> str:
    """把响应压成一行预览 —— **只给结构与 id，不回显正文**。

    这条路径同样进模型上下文（「响应缺 answers」的错误文案），而响应里可能带回
    `usage` 一类不该出现的字段：预览整份响应，等于从错误路径把它们放回来。
    """
    if isinstance(value, dict):
        # 连键名都不列：`usage` / `cost` 这类字段名本身就是不该进模型视野的字眼。
        # 排障需要的是「响应对不对、是不是空的、id 是什么」，不是字段清单。
        head = f"{len(value)} 个顶层字段"
        ident = value.get("id")
        if ident:
            head += f"，id={ident}"
        return head[:limit]
    return f"顶层是 {type(value).__name__}（正文不回显）"


def _mask(text: str, secret: str) -> str:
    """错误正文里若出现 key 本体，替换成 `***`（服务端回显的极端情况）。"""
    if secret and secret in text:
        text = text.replace(secret, "***")
    return text


def __main__(argv=None) -> int:
    """命令行入口：`python -m openthree.decide --help`。

    **只点名 `decide`**，不把 `globals()` 整个交出去：这个模块里有返回原始
    `JEV_API_KEY` 的内部函数，把它们变成子命令，等于给 key 开了一条打印路径。
    `__name__` 必须一起给 —— `is_registrable` 的最后一条判据是「函数是在这个
    模块里定义的」，少了它，`decide` 会被当成「外部导入」而拒之门外。
    """
    from .cli_tool import run_cli

    return run_cli({"__name__": __name__, "decide": decide}, argv=argv)


if __name__ == "__main__":
    raise SystemExit(__main__())
