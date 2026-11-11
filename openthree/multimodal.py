"""多模态：让 openthree 能「看见」图片（以及将来更多模态）。

极简的坚持（与整个项目一致）：

  * **不引入框架。** OpenAI 协议里的 content parts 就是事实标准，直接用它。
  * **不改 ReAct。** 一条 user 消息的 `content` 允许是
    `[{"type": "text", ...}, {"type": "image_url", ...}, ...]`，
    其余的观/思/行/观完全不变。
  * **不把 base64 当文本算。** history 估算时图片按固定成本折算，
    否则一张 500KB 的图会让上下文预算瞬间爆掉、触发无谓折叠。

图片从哪来、到哪去，一共四条路：

  1. 用户输入    CLI `--image` / 交互 `:img`  →  本轮 history 的 user 消息
  2. 工具返回    工具返回 `ImageResult`        →  Agent 补一条带图片的 user 消息
                 （tool 消息本身只能承载字符串，这是协议限制，不是我们的取舍）
  3. 会话记忆    transcript.jsonl 记图片路径   →  同会话下一轮播种时还原成图片
  4. 频道进来    A2A 图片 part（base64）       →  本轮 history 的 user 消息
                 （落盘走 `save_image_bytes`，与 1 共用同一条命名规则）

工具里要用，只要：

    from openthree import ImageResult

    def look(path: str) -> ImageResult:
        return ImageResult(text="这是一张截图", images=[path])
"""
from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

# 认识的图片后缀（未知后缀也会按 magic bytes 兜底判断）。
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# 原子写的临时文件名标记：`<digest>.<ext>.tmp-<8位>`。它只是写盘中途的形态，
# 任何「按目录算体量」的地方都该跳过它（残留只可能是崩溃留下的碎片）。
TMP_MARK = ".tmp-"

log = logging.getLogger(__name__)

# 传给模型的 detail 参数：auto / low / high。low 更省 token。
DEFAULT_DETAIL = "auto"

# 单张图片的字节上限。data URL 会比原图大约 1.33 倍（base64），故留足余量。
# 可用环境变量 OPENTHREE_MAX_IMAGE_BYTES 覆盖；设为 0 表示不限制。
DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024

# 估算 history 体量时，一张图片折算多少「字符」。
# 不按 base64 实际长度算 —— 那会把上下文预算直接撑爆。
IMAGE_CHAR_COST = 2000

_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


class ImageError(ValueError):
    """图片无法装载：不存在 / 是目录 / 太大 / 读不动。"""


def max_image_bytes() -> int:
    """单张图片的字节上限（0 = 不限制）。"""
    raw = os.getenv("OPENTHREE_MAX_IMAGE_BYTES", "").strip()
    if raw:
        try:
            n = int(float(raw))
            if n >= 0:
                return n
        except ValueError:
            pass
    return DEFAULT_MAX_IMAGE_BYTES


def is_url(src: Any) -> bool:
    """http(s) 链接（直接交给模型，不下载）。"""
    return isinstance(src, str) and src.startswith(("http://", "https://"))


def is_data_url(src: Any) -> bool:
    """已经是 data: URL。"""
    return isinstance(src, str) and src.startswith("data:")


def is_remote(src: Any) -> bool:
    """不需要落到本地读取的引用。"""
    return is_url(src) or is_data_url(src)


def resolve_path(src: Any, base_dir: Optional[Union[str, Path]] = None) -> Path:
    """把（可能相对的）路径解析成绝对路径。"""
    p = Path(str(src)).expanduser()
    if not p.is_absolute():
        p = Path(base_dir or Path.cwd()) / p
    return p


def sniff_mime(data: bytes) -> Optional[str]:
    """按 magic bytes 判断图片类型；认不出返回 None。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return None


def guess_mime(path: Union[str, Path]) -> str:
    """按后缀猜 MIME；猜不出用 mimetypes 再兜一层。"""
    ext = Path(str(path)).suffix.lower()
    if ext in _MIME_BY_EXT:
        return _MIME_BY_EXT[ext]
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def to_data_url(src: Any, *, base_dir: Optional[Union[str, Path]] = None) -> str:
    """把图片变成可直接发给模型的 URL（data: 或原样的 http(s)）。

    Raises:
        ImageError: 本地文件不存在 / 读取失败 / 超过大小上限。
    """
    if is_remote(src):
        return str(src)
    path = resolve_path(src, base_dir)
    if not path.exists():
        raise ImageError(f"图片不存在: {path}")
    if path.is_dir():
        raise ImageError(f"这是目录不是图片: {path}")
    try:
        data = path.read_bytes()
    except Exception as exc:  # noqa: BLE001
        raise ImageError(f"读取失败 {path}: {type(exc).__name__}: {exc}") from exc
    limit = max_image_bytes()
    if limit and len(data) > limit:
        raise ImageError(
            f"图片过大 {path}: {len(data)} 字节 > 上限 {limit}"
            f"（可用 OPENTHREE_MAX_IMAGE_BYTES 调大，或先压缩）"
        )
    mime = sniff_mime(data) or guess_mime(path)
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def image_part(src: Any, *, detail: str = DEFAULT_DETAIL,
               base_dir: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """构造 OpenAI 协议的 image_url part。"""
    part: Dict[str, Any] = {"type": "image_url", "image_url": {"url": to_data_url(src, base_dir=base_dir)}}
    if detail:
        part["image_url"]["detail"] = detail
    return part


def text_part(text: Any) -> Dict[str, Any]:
    """构造 OpenAI 协议的 text part。"""
    return {"type": "text", "text": str(text)}


def build_content(text: Any = "", images: Optional[Sequence[Any]] = None, *,
                  detail: str = DEFAULT_DETAIL,
                  base_dir: Optional[Union[str, Path]] = None) -> Union[str, List[Dict[str, Any]]]:
    """组装一条消息的 content。

    没有图片时返回 str（与从前逐字节一致，不惊动任何老代码）；
    有图片时返回 parts 列表。单张图片装不动不会拖垮整条消息 ——
    就地换成一句说明文本。
    """
    imgs = [i for i in (images or []) if i]
    if not imgs:
        return str(text or "")
    parts: List[Dict[str, Any]] = []
    if text:
        parts.append(text_part(text))
    for src in imgs:
        try:
            parts.append(image_part(src, detail=detail, base_dir=base_dir))
        except ImageError as exc:
            parts.append(text_part(f"[图片加载失败] {src}: {exc}"))
    return parts


@dataclass
class ImageResult:
    """工具返回的「带图片的结果」。

    Agent 收到它会把结果拆成两条消息：一条 tool（文本），
    一条 user（图片 parts）—— 后者才是模型真能「看见」的那一半。
    """

    text: str = ""
    images: List[str] = field(default_factory=list)
    detail: str = DEFAULT_DETAIL

    def brief(self) -> str:
        n = len(self.images)
        return f"{self.text}（+{n} 张图片）" if n else str(self.text)

    def __str__(self) -> str:  # 让 str() 安全：绝不吐出 base64
        return self.brief()


def image_result(text: str = "", images: Optional[Sequence[Any]] = None,
                 detail: str = DEFAULT_DETAIL) -> ImageResult:
    """构造 ImageResult（便捷函数，用户自造工具可直接用）。"""
    return ImageResult(text=str(text or ""),
                       images=[str(i) for i in (images or []) if i],
                       detail=detail or DEFAULT_DETAIL)


def content_text(content: Any) -> str:
    """从 str | parts 里抽出纯文本；图片用 `[N 张图片]` 占位。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: List[str] = []
        n_img = 0
        for part in content:
            if isinstance(part, dict):
                t = part.get("type")
                if t == "text":
                    out.append(str(part.get("text", "")))
                elif t in ("image_url", "image", "input_image"):
                    n_img += 1
            elif part:
                out.append(str(part))
        if n_img:
            out.append(f"[{n_img} 张图片]")
        return " ".join(x for x in out if x)
    return "" if content is None else str(content)


def count_images(content: Any) -> int:
    """一条消息的 content 里含几张图片。"""
    if not isinstance(content, list):
        return 0
    return sum(1 for p in content
               if isinstance(p, dict) and p.get("type") in ("image_url", "image", "input_image"))


def estimate_content_chars(content: Any) -> int:
    """估算一条 content 的字符体量；图片按 IMAGE_CHAR_COST 折算，不看 base64 长度。"""
    if isinstance(content, str):
        return len(content) + 20
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                t = part.get("type")
                if t in ("image_url", "image", "input_image"):
                    total += IMAGE_CHAR_COST
                elif t == "text":
                    total += len(str(part.get("text", ""))) + 20
                else:
                    total += len(str(part))
            else:
                total += len(str(part))
        return total
    if content:
        return len(str(content))
    return 0


def sanitize_for_log(obj: Any) -> Any:
    """递归地把 data:...;base64,XXXX 换成占位符 —— **只用于日志**。

    图片的真实 base64 绝不能进 logs/：一是体积，二是隐私。
    LLM 请求日志与 Recorder 都过这道关。不改动将要发出的真实请求。
    """
    if isinstance(obj, dict):
        return {k: sanitize_for_log(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_for_log(v) for v in obj]
    if isinstance(obj, str) and obj.startswith("data:") and "base64," in obj:
        head, _, b64 = obj.partition("base64,")
        return f"{head}base64,<省略 {len(b64)} 字节图片>"
    return obj


def _usable_ext(ext: str) -> bool:
    """`.png` 这种形状能不能当文件名后缀：`.` + 1~8 位 **ASCII** 字母数字。

    文件名的唯一来源是内容哈希 + 这个后缀，而频道收的是**不可信对端**的字节：
    `../x`、`a/b`、空格都不许进来。`str.isalnum()` 是 Unicode-aware 的（`.图片` 也能过），
    所以还要 `isascii()` —— 判据要与注释说的「字母数字」真的对得上。
    """
    return (bool(ext) and ext.startswith(".") and 1 <= len(ext) - 1 <= 8
            and ext[1:].isascii() and ext[1:].isalnum())


def _is_written(dest: Path, size: int) -> bool:
    """`dest` 是否已经是一份写完的文件（且大小与这次一致）。

    并发场景里 `exists()` 与 `stat()` 之间那个文件可能被别的线程删掉 —— 那要当成
    「没写好」，而不是让 `FileNotFoundError` 顶掉调用方本来要处理的 `OSError`。
    """
    try:
        return dest.exists() and dest.stat().st_size == size
    except OSError:
        return False


def asset_dest(assets_dir: Union[str, Path], data: bytes, *,
               fallback_ext: str = "", sniff: bool = True) -> Path:
    """这份内容会落在哪个文件名上（**纯计算，不写盘**）。

    命名规则的唯一真源：`save_image_bytes` 用它写、频道用它判「这份内容是不是
    已经在目录里」（去重命中不该再占一次预算）。判据见 `save_image_bytes`。

    Raises:
        ImageError: 空内容 / 认不出类型且没给兜底后缀 / 后缀形状不合法。
    """
    if not data:
        raise ImageError("图片内容是空的")
    mime = sniff_mime(data) if sniff else None
    ext = _EXT_BY_MIME.get(mime or "") or (fallback_ext or "").strip().lower()
    if not ext:
        raise ImageError("认不出图片类型（只认 png / jpeg / gif / webp / bmp 的 magic bytes）")
    if not ext.startswith("."):
        ext = "." + ext
    # 后缀是文件名的一部分，而 `save_image_bytes` 承诺「名字里不放调用方给的任何字符串」
    # —— 那就得真的拒绝 `.` + 8 位以内字母数字以外的形状（`/`、`..`、空格都不行），
    # 否则一个未来的调用方传进 `../x` 就是往 `assets/` 外面写。
    if not _usable_ext(ext):
        raise ImageError(f"非法的图片后缀：{ext!r}")
    digest = hashlib.sha1(data).hexdigest()[:16]
    return Path(assets_dir) / f"{digest}{ext}"


def save_image_bytes(assets_dir: Union[str, Path], data: bytes, *,
                     fallback_ext: str = "", sniff: bool = True) -> Path:
    """把一段图片字节落进 assets 目录（按内容 sha1 去重），返回落盘路径。

    **命名规则只许有这一处**：CLI `--image`、工具返回、频道入站都走它。名字只由
    内容哈希 + magic bytes 决定的类型后缀组成 —— 不放调用方给的任何字符串，所以
    同一张图不会有两个名字，`Workspace.stage_images()` 的「已存在就不写」才真的
    去重。（收图时文件名来自一个不可信的对端，这一点不只是洁癖。）

    Args:
        assets_dir: 落盘目录（一般是 `Workspace.assets_dir`）。
        data: 原始字节。
        fallback_ext: 认不出类型时用的后缀（带点，如 `".png"`）；留空 = 认不出就报错。
        sniff: 是否按 magic bytes 判类型；False 时一律用 `fallback_ext`。

    Raises:
        ImageError: 空内容 / 超过 `max_image_bytes()` / 认不出类型且没给兜底后缀。

    写字是**原子**的（临时文件 + `os.replace`）：收图是并发入口
    （`ThreadingHTTPServer` 每条连接一个线程），两个线程同时收到同一张图时，
    「先 exists 再 write_bytes」有一个「文件已存在但还没写完」的窗口 —— 读到它的
    那一个会拿到半张图。
    """
    if not data:
        raise ImageError("图片内容是空的")
    limit = max_image_bytes()
    if limit and len(data) > limit:
        raise ImageError(f"图片过大：{len(data)} 字节 > 上限 {limit}")
    assets_dir = Path(assets_dir)
    dest = asset_dest(assets_dir, data, fallback_ext=fallback_ext, sniff=sniff)
    try:
        assets_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ImageError(f"图片目录建不出来 {assets_dir}: {type(exc).__name__}: {exc}") from exc
    # 大小也一起比：同名的半截图（老版本没有原子写、进程在写盘中途被杀）不该被
    # 当成「已经有了」—— 那样模型从此收到的都是坏图，而目录里看不出异常。
    if _is_written(dest, len(data)):
        return dest
    tmp = assets_dir / f"{dest.name}{TMP_MARK}{uuid.uuid4().hex[:8]}"
    try:
        try:
            tmp.write_bytes(data)
        except OSError as exc:
            # 磁盘满 / 只读 / 权限一律归到 ImageError：`Raises:` 只承诺这一种，
            # 一个裸 OSError 会穿过频道的 `except ImageError` 变成 500。
            raise ImageError(
                f"图片写入失败 {assets_dir}: {type(exc).__name__}: {exc}") from exc
        try:
            os.replace(tmp, dest)
        except OSError as exc:
            # Windows：另一个线程正在把同一个 dest 换进去时，`MoveFileEx` 回
            # WinError 5（拒绝访问）—— 那不是失败，是「别人刚刚写好了同一个文件」。
            # 只要 dest 已经存在且大小与这次一致，就当成功；否则才是真错误。
            if not _is_written(dest, len(data)):
                raise ImageError(
                    f"图片写入失败 {assets_dir}: {type(exc).__name__}: {exc}") from exc
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError as exc:
                # 清不掉就留一句日志：这种碎片**不占上传预算**（`A2aChannel._assets_usage`
                # 跳过 `TMP_MARK`），但也不该凭空消失 —— 否则运维会在 `assets/` 里看见
                # 一个没人认识的文件，而日志里什么都没有。
                log.warning("临时文件清不掉（已忽略）：%s：%s", tmp, exc)
    return dest


def save_asset(assets_dir: Union[str, Path], src: Any) -> Path:
    """把本地图片复制进 assets 目录（按内容 sha1 去重），返回落盘路径。

    URL / data: 不下载 —— 那是调用方的事。

    认不出 magic bytes 的本地文件**仍会被收下**（后缀按扩展名 / `mimetypes` 兜底）：
    这条路径处理的是自己磁盘上的文件，宽松一点是对的。频道入站则相反 —— 那里
    `fallback_ext` 留空，认不出就拒（内容是不可信输入）。
    """
    if is_remote(src):
        raise ImageError("save_asset 只处理本地文件；URL / data: 请直接引用")
    path = resolve_path(src)
    if not path.exists():
        raise ImageError(f"图片不存在: {path}")
    if path.is_dir():
        raise ImageError(f"这是目录不是图片: {path}")
    try:
        data = path.read_bytes()
    except Exception as exc:  # noqa: BLE001
        raise ImageError(f"读取失败 {path}: {type(exc).__name__}: {exc}") from exc
    fallback = _EXT_BY_MIME.get(guess_mime(path)) or path.suffix.lower() or ".bin"
    if not _usable_ext(fallback):
        # 自己的文件不该因为一个怪后缀被拒（`.png~`、`.image_1`、`.screenshot`）：
        # 真正的类型判定只认 magic bytes，后缀只是给人和 `guess_mime` 一个提示。
        # 这条路径处理的是**本地可信**文件，宽松是对的；频道那边相反（认不出就拒）。
        fallback = ".bin"
    try:
        return save_image_bytes(assets_dir, data, fallback_ext=fallback)
    except ImageError as exc:
        # 出错时把路径带上：CLI 一次带多张 `--image` 时，`stage_images()` 把
        # `str(exc)` 记进 `_last_image_error` 给人看 —— 少了路径就分不清是哪一张。
        raise ImageError(f"{path}: {exc}") from exc
