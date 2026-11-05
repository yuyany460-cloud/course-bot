# course-bot

超星（学习通）章节自动播放 + 章节测验的小工具。接管你**已经登录**的 Edge，不重启浏览器、不复制配置、不碰账号密码。

```
装一次扩展 → python course_auto.py play / quiz / fill / next
```

---

## 它解决的三件事

### 1. 鼠标一离开就暂停

课程页在 video iframe 的 `window` / `document` 上挂了**原生** `mouseleave` / `mouseout`，
指针一离开播放器就暂停，并提示"观看时不可离开"。

删不掉：jQuery 那边只挂了 `resize`（`$._data` 里翻不到），拿不到函数引用就没法
`removeEventListener`。所以改成**在 `window` 的捕获阶段抢先接住事件**：

```js
win.addEventListener(t, e => e.stopImmediatePropagation(), true);
```

捕获阶段在 window 上是最先执行的，事件传不到页面自己的监听器 —— 效果等于删掉，
但不需要 DevTools，也不需要每次手动点。

> 同一 window 内幂等（`__apGuard` 标记），换章节 iframe 重建后要重装，
> 所以 `play` 每轮轮询都会调一次 `install_guard()`。

### 2. 静音

`muted` 是 `<video>` 元素自己的属性，**换一章就是一个新元素**。
所以它和上面的监听不是一回事：监听装一次管到底，静音必须**每轮都设**。
代码里这两件事是分开的，别合并。

### 3. 播放器停在"海报页"

新章节打开时是 `preload="none"` / `duration=NaN` / `readyState=0`，
进度永远是 0 —— 必须点一下大播放按钮它才去拉流。脚本会自动点。

### 另外：别碰倍速

`video.playbackRate = 2` 会被立刻暂停并把速度打回 1（这门课禁倍速）。
脚本完全不碰 `playbackRate`，老老实实 1 倍速。

---

## 用法

```bash
# 1) 播放当前章，直到任务点达标（默认 90.5%，页面写的是 ≥90%）
python course_auto.py play

# 2) 切到"章节测验"并把题目抓出来（写 var/quiz.json）
python course_auto.py quiz

# 3) 填答案并提交
python course_auto.py fill --answers '{"405887060":"D","405887062":"true"}'

# 4) 进入下一节
python course_auto.py next

# 5) 全自动跑多章（测验答案要你自己给，见下）
python course_auto.py loop --answers '{"...":"D"}' --max-chapters 10
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--target` | 任务点比例，默认 `0.905`（页面写 ≥90%） |
| `--interval` | 轮询秒数，默认 10 |
| `--max-minutes` | 单个视频最多等多久，默认 60 |
| `--no-submit` | 只填不交 |

---

## 已知限制：测验题是反爬字体

测验页的题干和选项用了字体混淆，`innerText` 出来是这样的：

```
党的十八大悓来，习近平总书记创造性地提出了（）的系统悑想
```

肉眼看得懂，程序读出来是乱码。所以 `quiz` 子命令**只负责把结构和选项的 `data` 值抓出来**，
题干要靠截图人读（或者接一个视觉模型）。

答案的写法就是选项的 `data` 属性：

| 题型 | 取值 |
| --- | --- |
| 单选 | `A` / `B` / `C` / `D` |
| 判断 | `true` / `false` |

`loop` 里没传 `--answers` 会停在测验页，等人工补答 —— 这就是留给视觉模型的接口位置，
在 `cmd_loop()` 里把 `plan = ...` 换成模型返回即可。

---

## 环境（换电脑照这个装）

| 需要 | 说明 |
| --- | --- |
| Windows | `solver.py` 用 `user32` 找窗口、`PIL.ImageGrab` 截屏，只支持 Windows |
| Python 3.10+ | |
| `pip install pillow` | 只有自动答题（截图）需要；只播视频不用装 |
| Edge（已登录超星） | 或 Chrome，扩展通用 |

装扩展：

1. `edge://extensions` → 打开「开发人员模式」
2. 「加载解压缩的扩展」→ 选仓库里的 `extension/` 目录
3. 装好后会弹一次「AutoPilot Bridge 已开始调试此浏览器」，正常

扩展和 Python 之间靠 WebSocket（默认 `127.0.0.1:8849`）通信，扩展用 `chrome.debugger`
拿完整 CDP 能力，所以能操作你**已经登录**的那个浏览器窗口，不用重新登录。

### 自动答题要配一个视觉模型

`solver.py` 会自动选后端：

1. 本机装了 autopilot → 用它的 `vision`（读它自己的配置），什么都不用设；
2. 没装 autopilot → 读这三个环境变量，任何 OpenAI 兼容接口都行：

```bat
set VISION_API_BASE=https://api.openai.com/v1
set VISION_API_KEY=sk-xxxx
set VISION_MODEL=gpt-4o-mini
```

不配也能跑，只是 `loop` 会跳过测验（视频任务点照样完成），测验你自己在网页上做。

---

## 目录

```
course_auto.py     主脚本（五个子命令）
extbridge.py       扩展桥服务端（标准库实现，425 行）
extension/         浏览器扩展（MV3，background.js + keepalive.js）
var/               运行产物：quiz.json / answers.json / course_state.json
```

---

## 为什么不用 CDP 调试端口

CDP 只能在浏览器**启动那一刻**开端口，而 Edge 136+ 禁止对默认配置目录开这个端口
（实测 Edge 155 显式传 `--remote-debugging-port` 也被拒绝）。
"接管用户已经打开的那个浏览器"只剩装扩展这一条路 —— 扩展跑在浏览器内部，
`chrome.debugger` 不受这个限制，也不用重新登录。

---

## 风险提示

能检测鼠标离开、能禁倍速，说明这门课的反作弊是开着的。脚本只是把"人在电脑前"
这件事自动化，异常行为（连续挂机、深夜刷课）平台是能看到的，自己掂量。
