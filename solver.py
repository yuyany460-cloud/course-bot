"""看截图答题：截 Edge 窗口 -> 视觉模型读题 -> 返回 {题号: 'D'|'true'}

两种后端，自动选：
1. **本机装了 autopilot**：用 autopilot.vision，读它自己的配置；
2. **换电脑时走这个**：任何 OpenAI 兼容接口，读三个环境变量

       VISION_API_BASE   例如 https://api.openai.com/v1
       VISION_API_KEY    你的 key
       VISION_MODEL      支持图片的模型名，例如 gpt-4o-mini / qwen-vl-max

题干的字体被混淆过（DOM 里是"悓来""悑想"这种乱码），所以只能看图。

依赖：Pillow（截屏）；Windows 限定（用 user32 找窗口）。
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import sys
import urllib.request
from ctypes import wintypes
from pathlib import Path

from PIL import ImageGrab

PROMPT = """这是超星"章节测验"的作答页面截图（同一页的上下两屏，内容有重叠）。

请逐题作答，并**带上题号**：

1. 题型看题干前的标签：【单选题】【多选题】【判断题】。
2. 单选：给一个字母，如 "C"。
3. **多选：把每个选项独立判断一遍，正确的全选上，通常有 2~4 个正确选项**，
   不要只挑一个。把字母连起来写，如 "ABD"。
4. 判断：给"对"或"错"。
5. 两屏重复出现的题**按题号去重**，一题只给一个答案。

严格只输出 JSON，不要解释、不要 markdown：
{"answers": [{"n": 1, "answer": "ABD"}, {"n": 2, "answer": "BCD"}, {"n": 3, "answer": "对"}]}
"""


# ---------------------------------------------------------------- 截图

def capture_window(keyword: str, out: Path) -> Path:
    """截 Edge 窗口。

    窗口标题会随**前台标签页**变化（`xxx 和另外 3 个页面 - Microsoft Edge`），
    所以别用具体页面名当关键字，用 "Edge" 最稳。
    """
    if not sys.platform.startswith("win"):
        raise RuntimeError("capture_window 只支持 Windows（用了 user32）")

    user32 = ctypes.windll.user32
    user32.SetProcessDPIAware()
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        title = buf.value
        if keyword in title or ("Chrome_WidgetWin" in cls.value and "Edge" in title):
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    if not found:
        raise RuntimeError(f"没找到标题含「{keyword}」的窗口")

    hwnd = found[0]
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


# ---------------------------------------------------------------- 模型后端

def _autopilot_root() -> str:
    """autopilot 装在哪：优先环境变量，其次同目录的 local_paths.py（不入库）。

    这样仓库里不会出现任何人的本机路径；本机想用 autopilot 就建一个
    local_paths.py 写 `AUTOPILOT_ROOT = r"D:\\code\\autopilot"`，它被 .gitignore 挡掉。
    """
    root = os.environ.get("AUTOPILOT_ROOT", "").strip()
    if root:
        return root
    try:
        from local_paths import AUTOPILOT_ROOT as local_root  # type: ignore
        return str(local_root).strip()
    except Exception:
        return ""


def _via_autopilot(images: list[Path], prompt: str) -> str | None:
    """本机装了 autopilot 就用它；没装/没配 key 返回 None。"""
    root = _autopilot_root()
    if root and root not in sys.path:
        sys.path.insert(0, root)
    try:
        from autopilot import vision  # type: ignore
    except Exception:
        return None
    try:
        if not vision.is_available():
            return None
        if len(images) == 1:
            return vision.read_image(images[0], prompt=prompt, max_tokens=900,
                                     detail="original").get("text") or ""
        pairs = [(f"第{i + 1}屏", p) for i, p in enumerate(images)]
        return vision.read_images(pairs, prompt=prompt, max_tokens=900,
                                  detail="original").get("text") or ""
    except Exception:
        return None


def _via_openai(images: list[Path], prompt: str) -> str:
    """任何 OpenAI 兼容的 /chat/completions 接口都能用。"""
    key = os.environ.get("VISION_API_KEY", "").strip()
    if not key:
        raise RuntimeError("没有可用的视觉模型：既没装 autopilot，也没配 VISION_API_KEY")
    base = os.environ.get("VISION_API_BASE", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("VISION_MODEL", "gpt-4o-mini")

    content: list[dict] = [{"type": "text", "text": prompt}]
    for p in images:
        b64 = base64.b64encode(Path(p).read_bytes()).decode("ascii")
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"}})

    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": content}],
                       "max_tokens": 900}).encode("utf-8")
    req = urllib.request.Request(base + "/chat/completions", data=body, method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as fh:
        data = json.loads(fh.read().decode("utf-8"))
    return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""


# ---------------------------------------------------------------- 解析

def _parse(text: str) -> dict[int, str]:
    """解析成 {题号: 答案}。缺少题号时按出现顺序兜底编号。"""
    raw = (text or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(raw[start:end + 1])
            ans = data.get("answers") or data.get("answer") or []
            if isinstance(ans, list) and ans:
                out: dict[int, str] = {}
                if isinstance(ans[0], dict):
                    for i, item in enumerate(ans, 1):
                        n = item.get("n") or item.get("no") or item.get("index") or i
                        try:
                            out[int(str(n).strip().rstrip(".、"))] = str(
                                item.get("answer") or item.get("ans") or "").strip()
                        except ValueError:
                            out[i] = str(item.get("answer") or "").strip()
                    return out
                return {i: str(a).strip() for i, a in enumerate(ans, 1)}
        except Exception:
            pass
    pairs = re.findall(r"(?:^|\n)\s*(\d+)\s*[.、:：]\s*([A-Da-d]{1,4}|对|错)", raw)
    return {int(n): a for n, a in pairs}


def solve(images) -> dict[int, str]:
    """images 可以是单张路径，也可以是多张（上屏/下屏）。返回 {题号: 答案}。"""
    paths = [images] if isinstance(images, (str, Path)) else list(images)
    paths = [Path(p) for p in paths]

    text = _via_autopilot(paths, PROMPT)
    if text is None:
        text = _via_openai(paths, PROMPT)
    return _parse(text)


def to_data(answer: str, qtype: str) -> str:
    """把"对/错/ABCD"翻译成页面里 <span data="..."> 的值（多选原样返回字母串）。"""
    a = answer.strip().upper().replace(" ", "")
    if qtype == "判断":
        if a.startswith(("对", "TRUE", "T", "√")):
            return "true"
        if a.startswith(("错", "FALSE", "F", "×")):
            return "false"
    if qtype == "多选":
        return "".join(ch for ch in a if ch in "ABCD")
    return a if a in ("A", "B", "C", "D") else ""


def solve_quiz(questions: list[dict], shots) -> dict:
    """questions: course_auto 抓出来的题目列表 -> {qid: data值}

    **按题号对应**，不按数组顺序：截图里题号是 1/2/3，题目列表也是从上到下，
    对不上号就宁可留空，也不要把答案填到别的题上。
    """
    by_no = solve(shots)
    plan = {}
    for i, q in enumerate(questions, 1):
        raw = by_no.get(i)
        if not raw:
            continue
        data = to_data(raw, q["type"])
        if data:
            plan[q["id"]] = data
    return plan


if __name__ == "__main__":
    kw = sys.argv[1] if len(sys.argv) > 1 else "Edge"
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("var/shots/quiz.png")
    p = capture_window(kw, out)
    print("截图:", p)
    print("答案:", solve(p))
