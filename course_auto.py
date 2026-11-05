"""超星章节自动播放 + 章节测验 —— 单文件可运行版。

设计上切成五个互不依赖的子命令，方便单步调试，也方便你换掉其中任一环：

    python course_auto.py play    # 播放当前章视频，直到"任务点"达标（默认 90.5%）
    python course_auto.py quiz    # 切到"章节测验"并把题目抓出来（含截图）
    python course_auto.py fill --answers '{"405887060":"D","405887062":"true"}'
    python course_auto.py next    # 点"下一节"，等到新章加载完
    python course_auto.py loop    # 全自动：play -> quiz -> fill -> submit -> next

三个关键约束（都是实测踩出来的，改代码前先看）：

1. **反挂机**：超星在 video iframe 的 window/document 上挂了**原生** mouseleave/mouseout，
   鼠标一离开就暂停。jQuery 那边只有 resize，`$._data` 找不到，所以删不掉引用；
   这里改成在 window 的**捕获阶段**先接住并 stopImmediatePropagation —— 事件到不了
   页面自己的监听器，效果等于删掉。装一次即可（同一 window 内幂等），换章节 iframe
   重建后要重装，所以 play 每轮都会调一次 install_guard。

2. **静音**：跟监听不一样，`muted` 是 <video> 元素自己的属性，**换一章就是一个新元素**，
   所以必须每轮都设一遍。这是两件事，别合并。

3. **禁倍速**：改 playbackRate 会被立刻暂停并打回 1。本脚本完全不碰它。

不去动"进度/时间"：超星按 currentTime 上报，跳转会被判无效，只能老实播。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

# autopilot 里的扩展桥（同一台机器上，Edge 装着 AutoPilot Bridge 扩展）
# 这台机器的 Python 开了 safe-path：脚本目录不会自动进 sys.path，
# 所以 `import solver` 会失败。自己补一条，放在最前面。
sys.path.insert(0, str(Path(__file__).resolve().parent))
# 扩展桥：优先用仓库里自带的副本；在本机 autopilot 里也能直接跑
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from extbridge import bridge as ext_bridge                # noqa: E402
except ImportError:                                           # 退回本机安装
    from autopilot.extbridge import bridge as ext_bridge      # noqa: E402

HERE = Path(__file__).resolve().parent
STATE = HERE / "var" / "course_state.json"
SHOTS = HERE / "var" / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)

# 课程页的标签页 id。**必须钉死**：不传 tabId 时扩展挑"当前活动标签"，
# 用户随手点开 edge:// 页面或 F12，attach 就会失败：
# "无法附加调试器：Cannot access chrome:// and edge:// URLs"。
TAB_ID: int | None = None


# ---------------------------------------------------------------- 基础通道

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def connect(timeout: float = 30.0):
    global TAB_ID
    bridge = ext_bridge()
    end = time.time() + timeout
    while time.time() < end:
        st = bridge.status()
        if st.get("listening") and st.get("connected"):
            break
        time.sleep(1.0)
    else:
        raise SystemExit("扩展桥没连上：确认 Edge 里 AutoPilot Bridge 扩展是开着的")

    if TAB_ID is None:
        tabs = (bridge.call("tabs", {}, timeout=15) or {}).get("tabs", [])
        page = next((t for t in tabs if "studentstudy" in str(t.get("url"))), None)
        if not page:
            raise SystemExit("没找到学习页面（studentstudy），先把课程页打开")
        TAB_ID = page["id"]
        log(f"锁定标签页 [{TAB_ID}] {page.get('title')}")
    return bridge


def ev(bridge, script: str, timeout: int = 30) -> Any:
    raw = bridge.call("eval", {"script": script, "tabId": TAB_ID}, timeout=timeout)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return raw
    return raw


def wait_until(bridge, script: str, ok, timeout: float = 8.0,
               interval: float = 0.3, label: str = "") -> Any:
    """轮询到条件成立为止，最长等 timeout 秒。

    以前这里全是 `time.sleep(5)` 这种死等：页面 0.5 秒就绪也要等满 5 秒，
    一章白扔十几秒。改成条件轮询后，状态一变 300ms 内就往下走。
    """
    end = time.time() + timeout
    last = None
    while True:
        last = ev(bridge, script, timeout=15)
        try:
            if ok(last):
                return last
        except Exception:
            pass
        if time.time() >= end:
            if label:
                log(f"等待「{label}」超时（{timeout:.0f}s），按当前状态继续")
            return last
        time.sleep(interval)


# JS 片段：从顶层一路下钻到"含题目"或"含视频"的那一层文档
DESCEND = r"""
  const docs = [document];
  let doc = document;
  for (let i = 0; i < 4; i++) {
    const f = doc.querySelector('iframe');
    if (!f || !f.contentDocument) break;
    doc = f.contentDocument;
    docs.push(doc);
    if (doc.querySelector('[id^=question]') || doc.querySelector('video')) break;
  }
"""


def _with(body: str) -> str:
    """把公共的下钻逻辑拼进一段 IIFE。"""
    return "(() => {" + DESCEND + body + "})()"


# ---------------------------------------------------------------- 反挂机拦截

GUARD = _with(r"""
  const install = (win, label) => {
    if (!win) return label + ':null';
    if (win.__apGuard) return label + ':cached';
    const kill = e => { try { e.stopImmediatePropagation(); } catch (_) {} };
    ['mouseleave', 'mouseout'].forEach(t => win.addEventListener(t, kill, true));
    win.__apGuard = true;
    return label + ':ok';
  };
  const res = [];
  docs.forEach((d, i) => res.push(install(d.defaultView, i === 0 ? 'top' : 'lvl' + i)));
  return JSON.stringify(res);
""")


def install_guard(bridge) -> list[str]:
    """在每一层 window 的捕获阶段掐掉 mouseleave/mouseout（幂等）。"""
    return ev(bridge, GUARD) or []


# ---------------------------------------------------------------- 视频

# 新章节的播放器停在"海报页"：preload=none、duration=NaN、readyState=0，
# 必须点一下大播放按钮它才去拉流。合成 click 就够（video.js 不校验 isTrusted）。
START_VIDEO = _with(r"""
  const vf = [...docs].reverse().find(d => d.querySelector('video'));
  if (!vf) return JSON.stringify({stage: 'no-video'});
  const v = vf.querySelector('video');
  const seq = ['.vjs-big-play-button', '.vjs-poster', '.prism-big-play-btn', '.vjs-play-control'];
  const tried = [];
  for (const sel of seq) {
    const el = vf.querySelector(sel);
    if (!el) continue;
    try { el.click(); tried.push(sel); } catch (e) {}
    break;
  }
  try { v.muted = true; } catch (e) {}
  // 千万别调 v.load()：它会把 currentTime 重置回 0（实测 270 秒直接归零），
  // 看起来像"从头重播"，超星那边的心跳也就白报了。
  const p = v.play(); if (p && p.catch) p.catch(() => {});
  return JSON.stringify({tried, ready: v.readyState, dur: String(v.duration),
                         cur: Math.round(v.currentTime || 0)});
""")

VIDEO_TICK = _with(r"""  const vf = [...docs].reverse().find(d => d.querySelector('video'));
  if (!vf) return JSON.stringify({stage: 'no-video'});
  const v = vf.querySelector('video');
  const out = {
    cur: Math.round(v.currentTime || 0),
    dur: Math.round(v.duration || 0),
    ready: v.readyState,
    paused: v.paused,
    muted: v.muted,
    rate: v.playbackRate,
  };
  // 静音：新章节 = 新 video 元素，所以每次都设
  if (!v.muted) { try { v.muted = true; } catch (e) {} out.muted = v.muted; }
  // 续播：暂停就拉起来（播到末尾时别硬拉，否则会从头再来）
  if (v.paused && !(out.dur && out.cur >= out.dur - 1)) {
    const p = v.play();
    if (p && p.catch) p.catch(() => {});
    out.replayed = true;
  }
  // 任务点状态：cards 层那行"完成条件"，以及是否出现已完成标记
  const cards = docs.find(d => d.body && /完成条件|章节测验/.test(d.body.innerText || ''));
  if (cards) {
    const t = cards.body.innerText.replace(/\s+/g, ' ');
    out.done_mark = /已完成|任务点已完成/.test(t);
  }
  out.hidden = document.hidden;
  return JSON.stringify(out);
""")


def play_until_done(bridge, target: float = 0.995, interval: float = 3.0,
                    max_minutes: float = 60.0) -> bool:
    """播到"任务点真的完成"为止。

    注意：页面上写的"观看时长 ≥ 总时长的 90%"只是提示，实测播到 94% 任务点
    仍然是"未完成"——服务端按累计观看秒数算，得让它跑到结尾。
    所以默认 target=0.995，并且以页面上的"已完成"标记为准。
    """
    install_guard(bridge)
    deadline = time.time() + max_minutes * 60
    stuck, last = 0, None
    no_video = 0
    while time.time() < deadline:
        s = ev(bridge, VIDEO_TICK, timeout=25)
        if not isinstance(s, dict):
            log(f"读状态失败：{s!r}")
            time.sleep(interval)
            continue
        if s.get("stage") == "no-video":
            # 播完后超星会把播放器换成"已完成"面板，这时也会没有 video；
            # 但也可能只是换章加载中，所以连续 3 次才算过关。
            no_video += 1
            log(f"当前层没有 video（第 {no_video} 次）")
            if no_video >= 3:
                log("连续 3 次没有视频，视为本章无视频/已完成")
                return True
            time.sleep(interval)
            continue
        no_video = 0

        # 新章节的播放器是「未初始化」状态：preload=none、duration=NaN，
        # 必须点一下大播放按钮才会去拉流。这一步不做，进度永远是 0。
        if s.get("ready") == 0 or not s.get("dur"):
            log("播放器未初始化，点大播放按钮触发加载")
            ev(bridge, START_VIDEO, timeout=25)
            time.sleep(0.8)      # 点完就复查，别干等 3 秒
            continue

        cur, dur = s.get("cur", 0), s.get("dur", 0)
        pct = (cur / dur) if dur else 0.0
        log(f"进度 {cur}/{dur}s = {pct*100:.1f}%  paused={s.get('paused')} "
            f"muted={s.get('muted')} 任务点={'已完成' if s.get('done_mark') else '未完成'}")
        _save_state(cur=cur, dur=dur, pct=round(pct, 4), paused=s.get("paused"))

        if pct >= target or s.get("done_mark"):
            log(f"达到 {target*100:.1f}%，本视频视为完成")
            return True

        stuck = stuck + 1 if cur == last else 0
        last = cur
        if stuck >= 6:
            log("连续 60 秒没动，重装拦截并再拉一次")
            install_guard(bridge)
            stuck = 0
        time.sleep(interval)

    log("超时，放弃本视频")
    return False


def _save_state(**kw) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    try:
        STATE.write_text(json.dumps(kw, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- 章节测验

OPEN_QUIZ = r"""
(() => {
  const li = [...document.querySelectorAll('li[title]')]
    .find(e => /章节测验|测验/.test(e.getAttribute('title') || ''));
  if (!li) return JSON.stringify({err: '没有"章节测验"这个 tab（本章可能没有测验）'});
  li.click();
  return JSON.stringify({ok: true, tab: li.getAttribute('title')});
})()
"""

DUMP_QUIZ = _with(r"""
  const out = {questions: [], text: ''};
  const quiz = [...docs].reverse().find(d => d.querySelector('[id^=question]'));
  if (!quiz) { out.err = '没找到题目容器'; return JSON.stringify(out); }
  out.text = (quiz.body ? quiz.body.innerText : '').replace(/\s+/g, ' ').slice(0, 400);
  // 已经交过卷的页面会显示"已完成/本次成绩 N 分"，这种不用再答，直接下一节
  out.done = /已完成|本次成绩/.test(out.text);
  quiz.querySelectorAll('[id^=question]').forEach(q => {
    if (!/^\d+$/.test(q.id.replace('question', ''))) return;
    const item = {id: q.id, type: '', stem: '', options: []};
    const clone = q.cloneNode(true);
    clone.querySelectorAll('.Zy_ulTop, ul, .stem_answer').forEach(e => e.remove());
    item.stem = (clone.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 600);
    q.querySelectorAll('li[onclick]').forEach(li => {
      // 单选/判断用 .num_option，多选用 .num_option_dx —— 两个都要抓
      const sp = li.querySelector('span.num_option, span.num_option_dx');
      if (!sp) return;
      item.options.push({data: sp.getAttribute('data') || '',
                         text: (li.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 160)});
    });
    const multi = !!q.querySelector('li.before-after-checkbox')
               || !!q.querySelector('span.num_option_dx');
    item.type = multi ? '多选'
      : (item.options.some(o => o.data === 'true' || o.data === 'false') ? '判断' : '单选');
    const inp = q.querySelector('input[id^=answer]');
    item.answer = inp ? inp.value : '';
    out.questions.push(item);
  });
  return JSON.stringify(out);
""")


def _real_click(bridge, selector: str) -> bool:
    """用 CDP 派发真实鼠标事件点某个元素。

    为什么不能用 `el.click()`：超星这套页面是 Vue 绑的，合成 click 会被当成
    非用户操作（`isTrusted=false`）——`下一节` 点了不跳、任务卡片点了不打开，
    全是这个原因。真实点击才有用。
    """
    try:
        r = bridge.call("click", {"target": selector, "by": "selector", "tabId": TAB_ID},
                        timeout=20)
        return bool(r)
    except Exception:
        return False


OPEN_VIDEO = r"""
(() => {
  const li = [...document.querySelectorAll('li[title]')]
    .find(e => /视频/.test(e.getAttribute('title') || ''));
  if (!li) return JSON.stringify({err: '没有"视频"这个 tab'});
  li.click();
  return JSON.stringify({ok: true});
})()
"""


# 就绪探针：用它们替代死等
CNT_Q = _with(r"""
  const quiz = [...docs].reverse().find(d => d.querySelector('[id^=question]'));
  return quiz ? quiz.querySelectorAll('[id^=question]').length : 0;
""")

VIDEO_READY = _with(r"""
  // 只要 <video> 元素在就算就绪：新章节的播放器停在"海报页"，
  // readyState 一直是 0，要等 play_until_done 去点大播放按钮才会加载。
  const vf = [...docs].reverse().find(d => d.querySelector('video'));
  const v = vf && vf.querySelector('video');
  return JSON.stringify({ready: !!v, loaded: !!(v && v.readyState > 0),
                         dur: v ? Math.round(v.duration || 0) : 0});
""")

DIALOG_READY = r"""
(() => {
  const e = document.getElementById('popok');
  if (!e) return 'no';
  const r = e.getBoundingClientRect();
  return r.width > 8 ? 'yes' : 'no';
})()
"""

RESULT_READY = _with(r"""
  const quiz = [...docs].reverse().find(d => d.querySelector('[id^=question]'));
  const t = quiz && quiz.body ? quiz.body.innerText : '';
  return JSON.stringify({ready: /已完成|本次成绩/.test(t)});
""")


def open_video(bridge, wait: float = 8.0) -> bool:
    """切回"视频"tab，并等到 <video> 真的就绪。"""
    _real_click(bridge, 'li[title="视频"]')
    r = wait_until(bridge, VIDEO_READY,
                   lambda x: isinstance(x, dict) and x.get("ready"),
                   timeout=wait, label="视频加载")
    return isinstance(r, dict) and bool(r.get("ready"))


def open_quiz(bridge, wait: float = 8.0) -> dict:
    """切到"章节测验"tab，并等到题目出现。"""
    _real_click(bridge, 'li[title="章节测验"]')   # 同样是 Vue，得真实点击
    wait_until(bridge, CNT_Q, lambda n: isinstance(n, int) and n > 0,
               timeout=wait, label="测验题目")
    data = ev(bridge, DUMP_QUIZ, timeout=40)
    return data if isinstance(data, dict) else {"err": "抽取失败"}


def fill(bridge, answers: dict[str, str | list[str]]) -> dict:
    """answers: {questionId: 'A'} 或 {'ABCD'} 或 ['A','B','C','D']

    多选要一个一个点，页面用 addMultipleChoice 累加；单选/判断只点第一个。
    """
    js = _with("""
      const plan = %s;
      const quiz = [...docs].reverse().find(d => d.querySelector('[id^=question]'));
      if (!quiz) return JSON.stringify({err: '没找到题目'});
      const log = [];
      for (const [qid, raw] of Object.entries(plan)) {
        const q = quiz.getElementById(qid);
        if (!q) { log.push([qid, '题目不存在']); continue; }
        // "ABCD" / "A,B,C" / ["A","B"] 都接受：纯大写字母串按字符拆
        const sRaw = String(raw).trim();
        const wants = Array.isArray(raw) ? raw
          : (/^[A-Da-d]{2,}$/.test(sRaw) ? sRaw.toUpperCase().split('')
                                          : sRaw.split(/[,;、\\s]+/))
              .map(s => String(s).trim()).filter(Boolean);
        const multi = !!q.querySelector('li.before-after-checkbox');
        const picked = [];
        for (const want of (multi ? wants : wants.slice(0, 1))) {
          let hit = null;
          for (const li of q.querySelectorAll('li[onclick]')) {
            const sp = li.querySelector('span.num_option, span.num_option_dx');
            if (sp && (sp.getAttribute('data') || '') === want) { hit = li; break; }
          }
          if (!hit) { log.push([qid, '没有 data=' + want]); continue; }
          hit.click();
          picked.push(want);
        }
        log.push([qid, picked.join(''), 'ok']);
      }
      const check = Object.keys(plan).map(qid => {
        const q = quiz.getElementById(qid);
        const inp = q && q.querySelector('input[id^=answer]');
        return [qid, inp ? inp.value : null];
      });
      return JSON.stringify({log, check});
    """ % json.dumps(answers, ensure_ascii=False))
    r = ev(bridge, js, timeout=30)
    return r if isinstance(r, dict) else {"err": str(r)}


SUBMIT = r"""
(() => {
  let doc = document;
  for (let i = 0; i < 4; i++) {
    const f = doc.querySelector('iframe');
    if (!f || !f.contentDocument) break;
    doc = f.contentDocument;
    if (doc.querySelector('.btnSubmit')) break;
  }
  const btn = doc.querySelector('.btnSubmit');
  if (!btn) return JSON.stringify({err: '没有提交按钮（可能已经提交过）'});
  btn.click();
  return JSON.stringify({ok: true});
})()
"""

CONFIRM = r"""
(() => {
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 8 && r.height > 6 && s.display !== 'none' && s.visibility !== 'hidden'; };
  const ok = document.getElementById('popok');
  if (ok && vis(ok)) { ok.click(); return JSON.stringify({ok: true, id: 'popok'}); }
  const alt = [...document.querySelectorAll('a,button')]
    .find(e => vis(e) && /^(确定|确认)$/.test((e.innerText || '').trim()));
  if (alt) { alt.click(); return JSON.stringify({ok: true, text: alt.innerText}); }
  return JSON.stringify({err: '没等到确认框'});
})()
"""

RESULT = _with(r"""
  const quiz = [...docs].reverse().find(d => d.querySelector('[id^=question]'));
  const t = (quiz && quiz.body ? quiz.body.innerText : '').replace(/\s+/g, ' ');
  const score = (t.match(/本次成绩\s*([\d.]+)\s*分/) || [])[1] || '';
  return JSON.stringify({done: /已完成/.test(t), score, text: t.slice(0, 200)});
""")


def submit(bridge) -> dict:
    a = ev(bridge, SUBMIT, timeout=25)
    if not isinstance(a, dict) or a.get("err"):
        return a if isinstance(a, dict) else {"err": str(a)}

    # 等确认框出现（通常 0.3~0.8 秒），别死等 2 秒
    d = wait_until(bridge, DIALOG_READY, lambda v: v == "yes", timeout=6, label="确认框")
    if d != "yes":
        return {"submit": a, "confirm": {"err": "没等到确认框"}}
    b = ev(bridge, CONFIRM, timeout=20)

    # 等成绩出来（一般 1~2 秒）
    wait_until(bridge, RESULT_READY,
               lambda x: isinstance(x, dict) and x.get("ready"),
               timeout=10, label="成绩")
    r = ev(bridge, RESULT, timeout=25)
    return {"submit": a, "confirm": b, "result": r}


# ---------------------------------------------------------------- 章节切换

NEXT = r"""
(() => {
  const el = document.getElementById('prevNextFocusNext');
  if (!el) return JSON.stringify({err: '没有"下一节"按钮，可能已经是最后一节'});
  const before = location.href;
  el.click();
  return JSON.stringify({ok: true, before});
})()
"""

CHAPTER_INFO = r"""
(() => {
  const cards = document.querySelectorAll('iframe')[0] &&
                document.querySelectorAll('iframe')[0].contentDocument;
  const vf = cards && cards.querySelector('iframe') && cards.querySelector('iframe').contentDocument;
  const v = vf && vf.querySelector('video');
  // 优先用标题元素：正则匹配"章节详情"后面那串，会在弹出"目录"面板时抓错
  const titleEl = document.querySelector('.prev_title') ||
                  document.querySelector('.prev_title_pos');
  let name = titleEl ? (titleEl.innerText || '').trim() : '';
  if (!name) {
    const m = (document.body.innerText || '').match(/章节详情\s*\n?\s*([^\n]{2,40})/);
    name = m ? m[1].trim() : '';
  }
  return JSON.stringify({
    chapter: name.slice(0, 40),
    url: location.href.slice(0, 190),
    tabs: [...document.querySelectorAll('li[title]')].map(li => li.getAttribute('title')),
    video: v ? {cur: Math.round(v.currentTime), dur: Math.round(v.duration || 0)} : null
  });
})()
"""


CATALOG = r"""
(() => {
  const items = [...document.querySelectorAll('.posCatalog_name')].map(e => {
    const oc = e.getAttribute('onclick') || '';
    const m = oc.match(/getTeacherAjax\('([^']*)','([^']*)','([^']*)'/);
    return {name: (e.getAttribute('title') || e.innerText || '').trim().slice(0, 40),
            kid: m ? m[3] : ''};
  }).filter(x => x.kid);
  return JSON.stringify(items);
})()
"""

GOTO_CHAPTER = r"""
(() => {
  const before = location.href;
  const after = before.replace(/chapterId=\d+/, 'chapterId=__KID__');
  if (after === before) return JSON.stringify({err: 'URL 里没有 chapterId'});
  location.href = after;
  return JSON.stringify({ok: true});
})()
"""


def next_chapter(bridge, wait: float = 10.0) -> dict:
    """下一节：优先按目录里的 chapterId 直接跳转。

    为什么不点"下一节"按钮：那个按钮在大页面里位于 y≈1500，真实点击要先
    scrollIntoView，而页面头尾都有固定栏，落点经常被盖住——表现为"点了没反应"。
    目录项带 `getTeacherAjax(...,'1218604026',...)`，第三个参数就是 chapterId，
    直接换 URL 既快又不会点歪。
    """
    cur = ev(bridge, CHAPTER_INFO) or {}
    name0 = (cur.get("chapter") or "").strip()
    items = ev(bridge, CATALOG) or []

    idx = next((i for i, it in enumerate(items)
                if it["name"] and (it["name"] in name0 or name0 in it["name"])), None)
    if idx is not None and idx + 1 < len(items):
        nxt = items[idx + 1]
        ev(bridge, GOTO_CHAPTER.replace("__KID__", nxt["kid"]), timeout=20)
        info = wait_until(bridge, CHAPTER_INFO,
                          lambda x: isinstance(x, dict) and (x.get("chapter") or "") != name0,
                          timeout=wait, label="下一章(直跳)")
        if isinstance(info, dict) and (info.get("chapter") or "") != name0:
            return info

    # 目录不可用时退回点击
    _real_click(bridge, "#prevNextFocusNext")
    changed = lambda x: isinstance(x, dict) and (x.get("chapter") or "") != name0
    info = wait_until(bridge, CHAPTER_INFO, changed, timeout=wait, label="下一章(点击)")
    if not changed(info):
        ev(bridge, NEXT, timeout=20)
        info = wait_until(bridge, CHAPTER_INFO, changed, timeout=wait, label="下一章(兜底)")
    return info if isinstance(info, dict) else {"err": "没跳过去"}


# ---------------------------------------------------------------- 编排

def cmd_play(args) -> int:
    bridge = connect()
    info = ev(bridge, CHAPTER_INFO)
    log(f"当前章节：{info.get('chapter')}  tabs={info.get('tabs')}")
    ok = play_until_done(bridge, args.target, args.interval, args.max_minutes)
    log("视频完成" if ok else "视频未完成")
    return 0 if ok else 4


def cmd_quiz(args) -> int:
    bridge = connect()
    data = open_quiz(bridge)
    if data.get("err"):
        log(f"打开测验失败：{data['err']}")
        return 4
    log(f"抓到 {len(data.get('questions') or [])} 题")
    for q in data["questions"]:
        log(f"  {q['id']} [{q['type']}] {q['stem'][:80]}")
        for o in q["options"]:
            log(f"      data={o['data']:5} {o['text'][:60]}")
    (HERE / "var" / "quiz.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"已写 {HERE / 'var' / 'quiz.json'}")
    return 0


def cmd_fill(args) -> int:
    bridge = connect()
    answers = json.loads(args.answers) if args.answers else json.loads(
        Path(args.answers_file).read_text(encoding="utf-8"))
    r = fill(bridge, answers)
    log(json.dumps(r, ensure_ascii=False))
    if not args.no_submit:
        res = submit(bridge)
        log(json.dumps(res, ensure_ascii=False))
        score = ((res.get("result") or {}).get("score") or "")
        if score:
            log(f"得分：{score}")
    return 0


def cmd_next(args) -> int:
    bridge = connect()
    log(json.dumps(next_chapter(bridge), ensure_ascii=False))
    return 0


def cmd_loop(args) -> int:
    """每章：先做测验（已交卷就跳）→ 回视频 tab 播到任务点完成 → 下一节。

    为什么把测验放在前面：视频播完时超星可能自动跳到下一节，这时再找上一章的
    测验就找不到了（实测 7.1 播完直接跳到 7.3，7.2 整章被跳过）。
    先做测验、再播视频，就不会漏。
    """
    bridge = connect()
    for n in range(1, args.max_chapters + 1):
        info = ev(bridge, CHAPTER_INFO)
        start_chapter = (info or {}).get("chapter")
        log(f"===== 第 {n} 轮：{start_chapter} =====")

        data = open_quiz(bridge)
        qs = (data or {}).get("questions") or []
        if not qs:
            log(f"本章没有测验或打不开：{(data or {}).get('err')}")
        elif (data or {}).get("done"):
            log("本章测验已提交过，跳过（不管分数）")
        else:
            log(f"测验 {len(qs)} 题")
            plan = args.answers and json.loads(args.answers) or None
            if not plan and args.auto_solve:
                try:
                    from solver import capture_window, solve_quiz
                    shots = []
                    ev(bridge, "(() => { window.scrollTo(0, 0); return 'ok'; })()")
                    time.sleep(0.25)
                    shots.append(capture_window(
                        "Edge", HERE / "var" / "shots" / f"quiz_{n}_top.png"))
                    # 只有真的能滚（内容比窗口长）才截第二屏，省一次截图 + 0.3 秒
                    moved = ev(bridge, "(() => { window.scrollTo(0, 99999);"
                                       " return JSON.stringify({y: Math.round(window.scrollY)}); })()")
                    if isinstance(moved, dict) and moved.get("y", 0) > 50:
                        time.sleep(0.25)
                        shots.append(capture_window(
                            "Edge", HERE / "var" / "shots" / f"quiz_{n}_bottom.png"))
                    log(f"截图 {len(shots)} 屏")
                    plan = solve_quiz(qs, shots)
                    log(f"模型答案：{json.dumps(plan, ensure_ascii=False)}")
                except Exception as exc:
                    log(f"自动答题失败：{type(exc).__name__}: {exc}")
            if plan:
                log(json.dumps(fill(bridge, plan), ensure_ascii=False))
                res = submit(bridge)
                log(json.dumps((res.get("result") or {}), ensure_ascii=False))
                score = ((res.get("result") or {}).get("score") or "")
                if score:
                    log(f"得分：{score}")
            else:
                # 拿不到答案就跳过测验，别让整轮停死（视频任务点照样能完成）
                log("没有答案，跳过本章测验")

        # 回到"视频"tab 再播
        if not open_video(bridge):
            log("切不回视频 tab，跳过本章")
        else:
            if not play_until_done(bridge, args.target, args.interval, args.max_minutes):
                log("视频没完成，停在原地")
                return 4

        now = ev(bridge, CHAPTER_INFO)
        if (now or {}).get("chapter") != start_chapter:
            log(f"页面已自动跳到：{now.get('chapter')}，跳过'下一节'")
            continue

        r = next_chapter(bridge)
        if r.get("err"):
            log(f"没有下一节了：{r['err']}")
            return 0
        log(f"已进入：{r.get('chapter')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="超星章节自动播放 + 测验")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn, helptext in (("play", cmd_play, "播放当前章视频到任务点"),
                               ("quiz", cmd_quiz, "切到章节测验并抓题"),
                               ("fill", cmd_fill, "填答案并提交"),
                               ("next", cmd_next, "进入下一节"),
                               ("loop", cmd_loop, "全自动跑多章")):
        p = sub.add_parser(name, help=helptext)
        p.set_defaults(func=fn)
        p.add_argument("--target", type=float, default=0.995, help="播到多少算完成，默认 0.995（要跑满）")
        p.add_argument("--interval", type=float, default=3.0, help="轮询秒数（默认 3，越小反应越快）")
        p.add_argument("--max-minutes", type=float, default=60.0, help="单个视频最多等多久")
        if name in ("fill", "loop"):
            p.add_argument("--answers", default="", help='JSON：{"题目id":"D"}，判断题用 true/false')
            p.add_argument("--answers-file", default="var/answers.json")
            p.add_argument("--no-submit", action="store_true")
        if name == "loop":
            p.add_argument("--max-chapters", type=int, default=50)
            p.add_argument("--auto-solve", action="store_true",
                           help="用视觉模型看截图作答（超星题干是反爬字体，只能看图）")

    args = ap.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
