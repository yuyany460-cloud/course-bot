"""看截图答题：截 Edge 窗口 -> 视觉模型读题 -> 返回 {题号: 'D'|'true'}

超星的题干用了字体混淆，DOM 里是"悓来""悑想"这种乱码，只能靠看图。
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import sys
from ctypes import wintypes
from pathlib import Path

from PIL import ImageGrab

AUTOPILOT_ROOT = os.environ.get("AUTOPILOT_ROOT", r"D:\code\autopilot")
sys.path.insert(0, AUTOPILOT_ROOT)
from autopilot import vision  # noqa: E402

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


def capture_window(keyword: str, out: Path) -> Path:
    """截 Edge 窗口。

    注意：窗口标题会随**前台标签页**变化（`xxx 和另外 3 个页面 - Microsoft Edge`），
    所以别用具体页面名当关键字，用 "Edge" 就够稳。
    """
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
    # 兜底：抠 "1. ABD" / "1、对"
    pairs = re.findall(r"(?:^|\n)\s*(\d+)\s*[.、:：]\s*([A-Da-d]{1,4}|对|错)", raw)
    return {int(n): a for n, a in pairs}


def solve(images, workspace: str = r"D:\code\autopilot") -> dict[int, str]:
    """images 可以是单张路径，也可以是按顺序的多张（上屏/下屏）。返回 {题号: 答案}。"""
    if isinstance(images, (str, Path)):
        result = vision.read_image(images, prompt=PROMPT, workspace=workspace,
                                   max_tokens=900, detail="original")
    else:
        pairs = [(f"第{i + 1}屏", p) for i, p in enumerate(images)]
        result = vision.read_images(pairs, prompt=PROMPT, workspace=workspace,
                                    max_tokens=900, detail="original")
    return _parse(result.get("text") or "")


def to_data(answer: str, qtype: str) -> str:
    """把"对/错/ABCD"翻译成页面里 <span data="..."> 的值（多选原样返回字母串）。"""
    a = answer.strip().upper().replace(" ", "")
    if qtype == "判断":
        if a.startswith(("对", "TRUE", "T", "√")):
            return "true"
        if a.startswith(("错", "FALSE", "F", "×")):
            return "false"
    if qtype == "多选":
        letters = "".join(ch for ch in a if ch in "ABCD")
        return letters
    return a if a in ("A", "B", "C", "D") else ""


def solve_quiz(questions: list[dict], shots, workspace: str = r"D:\code\autopilot") -> dict:
    """questions: course_auto 抓出来的题目列表 -> {qid: data值}

    **按题号对应**，不按数组顺序：截图里题号是 1/2/3，题目列表也是从上到下，
    对不上号就宁可留空，也不要把答案填到别的题上。
    """
    by_no = solve(shots, workspace)
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
    kw = sys.argv[1] if len(sys.argv) > 1 else "学生学习"
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("var/shots/quiz.png")
    p = capture_window(kw, out)
    print("截图:", p, vision.read_image(p, prompt=PROMPT, max_tokens=800).get("text", "")[:400])
