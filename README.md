# OCS 本地 AI 题库

给 [OCS 网课助手](https://docs.ocsjs.com/docs/other/api#AnswererWrapper) 用的本地题库程序。
不需要 GUI，纯 Python 标准库（**零第三方依赖**），填一个大模型 API Key 就能跑：

- **大模型答题**：接入任意 OpenAI 兼容接口（DeepSeek / 通义千问 / 智谱 / Kimi / OpenAI / SiliconFlow / 本地 Ollama）。
- **识图**：题干里的图片会自动抓取并交给视觉模型（qwen-vl、glm-4v、gpt-4o 等）。
- **联网搜索**：时事类题目自动检索最新资料再作答（Bing / DuckDuckGo 免 key，也支持 Tavily / Serper / 博查）。
- **Markdown 题库**：答过的题写进 `data/bank/*.md`，下次遇到同题直接命中，不再消耗 token。
- **兼容 OCS 解析规则**：单选 / 多选（`#` 分隔）/ 判断 / 填空四种题型都按 OCS 的匹配算法输出。

---

## 工作原理

```
OCS 脚本  ──POST /api/search──▶  本地服务
                                   │
                                   ├─1. 查 Markdown 题库 ──命中──▶ 立即返回（毫秒级）
                                   │
                                   ├─2. 大模型作答（可带图片）
                                   │
                                   ├─3. 判断题/时事题 ──▶ 联网搜索 ──▶ 带资料再问一次
                                   │
                                   └─4. 写回 .md 题库 ──▶ 返回答案
```

OCS 单次题库请求的超时是 **60 秒**，程序内置 55 秒总预算，超时前会主动降级（跳过联网、减少重试），避免整题失败。

---

## 快速开始

### 1. 准备配置并填 API Key

`config.toml` 里放的是你自己的 Key，已在 `.gitignore` 中，不会被提交。第一次使用先复制模板：

```bash
cp config.example.toml config.toml          # Windows: copy config.example.toml config.toml
```

然后打开 `config.toml`，找到 `[llm]`：

```toml
[llm]
provider = "deepseek"        # 用哪家模型

[llm.providers.deepseek]
api_key = "sk-你的密钥"       # 填这里
```

> 换别家只需改 `provider`，再填对应小节里的 `api_key`。配置里已经预置了 7 家服务商的地址和默认模型。

> 要求 OCS **4.7.21 及以上**（题库配置支持自定义字段 handler），图片题优化需要 5.x。

### 2. 启动

Windows 双击 `start.bat`，或者命令行：

```bash
python main.py
```

看到下面这段就成功了：

```
========================================================
OCS 本地题库已启动：http://127.0.0.1:8000
题库条目：0 条
状态页：http://127.0.0.1:8000/
OCS 题库配置：http://127.0.0.1:8000/api/ocs-config
========================================================
```

顺手自检一下（会真的调一次模型）：

```bash
python main.py check
```

### 3. 粘贴到 OCS

浏览器打开 <http://127.0.0.1:8000/>，复制页面上的 JSON，粘贴进 OCS 的「题库配置」，保存即可。

也可以在命令行打印：

```bash
python main.py config
```

生成的配置长这样：

```json
[
  {
    "name": "本地AI题库",
    "homepage": "http://127.0.0.1",
    "url": "http://127.0.0.1:8000/api/search",
    "method": "post",
    "contentType": "json",
    "type": "GM_xmlhttpRequest",
    "headers": { "Content-Type": "application/json" },
    "data": {
      "title": { "handler": "return (env) => (env.suggestion_title || env.title || '')" },
      "options": { "handler": "return (env) => (env.suggestion_options || env.options || '')" },
      "type": "${type}",
      "images": { "handler": "return (env) => ..." }
    },
    "handler": "return (res) => (res && res.code === 1 && res.answer) ? [res.question, res.answer] : undefined"
  }
]
```

三个要点：

- `type` 必须是 `GM_xmlhttpRequest`。课程页面通常是 https，直接 `fetch` 本地 http 会被浏览器的混合内容策略拦截。
- **`data` 里必须真的引用图片题字段**（`${images}` / `${suggestion_title}` / `${suggestion_options}`，或 handler 里读 `env.images`），否则 OCS 会提示「当前题库配置暂不支持图片、公式等题目处理」。OCS 的判定逻辑是扫 `data` 的值，只看有没有引用，不看实际效果。
- `title` / `options` 用 handler 而不是 `${title}`，是为了兼容没开图片题优化的场景：OCS 5.x 只在有图片时注入 `suggestion_*`，其它时候回退到原始 `title` / `options`。

如果脚本提示域名未授权，把脚本管理器里的 `@connect` 设为**宽松模式**，或使用 OCS 的[全域名通用版脚本](https://docs.ocsjs.com/docs/other/api#common-version)。

---

## 配置文件

所有配置都在 `config.toml`，带中文注释。常用的几项：

| 配置项 | 说明 | 默认 |
| --- | --- | --- |
| `server.port` | 本地监听端口 | `8000` |
| `llm.provider` | 使用哪家模型 | `deepseek` |
| `llm.timeout` | 单次模型调用超时（秒） | `40` |
| `llm.thinking` | 思考模式：`auto`（只在 DeepSeek 上发）/ `on` / `off` | `auto` |
| `llm.thinking_effort` | 思考强度：`low` / `high` / `max` | `low` |
| `vision.provider` | 识图用哪家，留空沿用 `llm.provider` | `""` |
| `search.mode` | `auto` 自动判断 / `always` 每题都搜 / `never` 关闭 | `auto` |
| `search.providers` | 搜索渠道，按顺序尝试 | `["bing","duckduckgo"]` |
| `bank.fuzzy_threshold` | 题干模糊匹配阈值（0~1） | `0.92` |
| `bank.verify_options` | 同题干但选项不同时是否复用旧答案 | `true` |
| `behavior.total_timeout` | 单题总预算（秒） | `55` |
| `behavior.save_to_bank` | 是否把新答案写入题库 | `true` |

改完配置不用重启也能生效（题库也会重新加载）：

```bash
curl http://127.0.0.1:8000/api/reload
```

### 思考模式（DeepSeek）

按 [DeepSeek 思考模式文档](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode) 接入，用的是 OpenAI 格式的两个参数：

```json
{"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
```

配置里两个开关：

```toml
[llm]
thinking = "auto"          # auto / on / off
thinking_effort = "low"    # low / high / max
```

- **`auto`（默认）**：只有在 `base_url` 指向 DeepSeek 官方接口时才发送这两个参数。别的厂商收到未知字段可能直接 400，所以默认不发给它们。
- **`on`**：强制发送，自建/中转的 DeepSeek 兼容接口用这个。
- **`off`**：发送 `{"thinking": {"type": "disabled"}}`，速度最快。
- 思考强度默认 **`low`**：DeepSeek 服务端默认是 `high`，答题场景 `low` 更快，实测 2 秒左右能出结果。要更稳就改 `high` / `max`。

两点实现细节：

- 思考模式不支持 `temperature`，程序在开启思考时会自动不发送这个字段。
- `python main.py check` 会额外打印一行「思考模式：开启，强度 low」，方便确认生效。

单个服务商可以覆盖全局设置，写在自己的 `[llm.providers.xxx]` 里即可：

```toml
[llm.providers.deepseek]
thinking = "off"
```

### 预置的模型服务商

| provider | base_url | 默认模型 | 识图模型 |
| --- | --- | --- | --- |
| `deepseek` | api.deepseek.com/v1 | deepseek-flash | —（flash 本身支持识图） |
| `qwen` | dashscope.aliyuncs.com/compatible-mode/v1 | qwen-plus | qwen-vl-max |
| `zhipu` | open.bigmodel.cn/api/paas/v4 | glm-4-flash | glm-4v-flash |
| `moonshot` | api.moonshot.cn/v1 | moonshot-v1-8k | moonshot-v1-8k-vision-preview |
| `openai` | api.openai.com/v1 | gpt-4o-mini | gpt-4o-mini |
| `siliconflow` | api.siliconflow.cn/v1 | Qwen2.5-7B-Instruct | Qwen2-VL-72B |
| `ollama` | 127.0.0.1:11434/v1 | qwen2.5:7b | llava |

**推荐组合**：主模型用 `deepseek-flash`（便宜、中文好、自带识图），复杂题想更稳可以换 `deepseek-v4-pro`。识图也可以单独指到 `qwen-vl`：

```toml
[vision]
provider = "qwen"
model = "qwen-vl-max"
```

---

## 识图

程序从三个地方找图片：

1. **OCS 5.x 图片题优化**：`env.images`（题干和选项里的图片，已由脚本转成 base64），对应题干里的 `[图片1]`、`[图片2]` 占位符。这条路最稳，不受防盗链和跨域影响；
2. **老版本 / 未开图片题优化**：默认 `images` handler 会退回 DOM 抓取题干区域的 `<img>`，把地址交给本地服务下载；
3. **文本里的图片链接**：`http(s)://...png`、`<img src="...">`、`data:image/...`，以及手动调接口时传的 `images` 参数。

抓到的图片会随题目一起发给视觉模型。默认单题最多 5 张、单张 8MB，GIF 会跳过（多数视觉模型不支持）。

发给模型的题干里 `[图片1]`、`[图片2]` 与图片顺序严格对应，每张图前面还会单独插一句「第 N 张图片」，避免模型把编号搞混。图片数量超过 `vision.max_images`、或某张图下载失败时，提示词里会明确写出「[图片6] 本次未能获取」，让模型降低置信度而不是硬猜。

**选项本身是图片**时（选项文本是 `[图片3]` 或图片链接），答案会返回**选项字母**而不是选项原文——因为 OCS 那侧记录的是图片 URL，回传原文匹配不上，只有字母能走「纯 ABCD 兜底」按序号选中。

如果目标平台的图片不在默认选择器范围内（路径 2），改 OCS 配置里 `images` handler 的后半段即可：

```js
return (env) => {
  if (env.images && env.images.length) return env.images;   // 保留这行，OCS 才认为配置支持图片
  const urls = [...document.querySelectorAll('.你的题干容器 img')].map(i => i.src);
  return urls.slice(0, 5);
}
```

---

## 联网搜索

以下任一条件成立就会联网：

- 模型自己标记 `need_search: true`（时事、赛事、人事、价格、版本发布等）；
- 题干命中时效关键词（最新、目前、今年、现任、夺冠、票房……）；
- 模型自评置信度低于 `behavior.low_confidence`。

搜索到资料后会**重新问一次模型**，并要求以资料为准。`search.mode = "never"` 可完全关闭。

免 key 的 `bing` / `duckduckgo` 走网页抓取，够用但不保证 100% 稳定；对稳定性要求高可以填 `tavily_api_key` / `serper_api_key` / `bocha_api_key`，把对应渠道放到 `providers` 最前面。

---

## Markdown 题库

### 位置与格式

`data/bank/` 下所有 `.md` 都会被读取，新题默认追加到 `default.md`：

```markdown
## 中国梦是什么？

- 类型: single
- 选项:
  - A. 实现中华民族伟大复兴
  - B. 实现世界和平
  - C. 人民幸福
- 答案: A
- 解析: 十九大报告中的表述。
- 来源: llm+web
- 更新时间: 2026-11-08 11:02:13
```

- `## ` 后面是题干，**这是匹配用的主键**；
- `- 答案:` 填字母，多选写 `A#C`；
- 填空题的「答案」直接写文本，多个空用 `#` 分隔；
- `- 答案内容:` 可选，是回传 OCS 的完整选项文本，一般不写，程序会自己还原；
- `- 图片指纹:` 由程序自动写入。题干只剩 `[图片N]` 占位符时（数学公式题常见），照片就是题面，必须靠指纹区分，**不要手动改**。

手工加题只要三四行也认：

```markdown
## 1+1=?

- 类型: single
- 答案: B
- 选项:
  - A. 1
  - B. 2
```

`- 类型:` 建议写上（`single` / `multiple` / `judgement` / `completion`）。不写表示「不限题型」，可能被其它题型命中。

### 匹配规则

1. 题干归一化后精确匹配（去掉【单选题】、题号、标点、空格、全半角差异）；
2. **题干是「[图片1] ，求 [图片2]」这类套话时，键里会带上图片指纹**——数学公式题的文字往往只有几个字，光看文字会把整页题都当成同一道；
3. 没命中就走模糊匹配（`bank.fuzzy_threshold`，默认 0.92，带图片指纹的键不参与）；
4. **题型必须一致**：单选题、多选题、判断题、填空题各自独立缓存。同一道题在不同题型下答案可能完全不同，多选绝不会复用单选的答案；
5. 命中后还会校验选项：**同题干但选项对不上时不会复用旧答案**，而是重新问模型（考试题库里同题干换选项很常见，这条能避免大量错答）。

答案形态也按这个思路走：文字题回传的是**选项原文**（相当于直接给答案内容，OCS 靠文本匹配，选项顺序变了也不怕），只有选项本身是图片（`[图片N]`、图片链接）时才回退到字母——那种情况下 OCS 侧只认序号。

> 选项里的空行会被丢掉。有些平台的某个选项文本带尾随换行，OCS 拼接后会多出一个空行，不丢掉的话后面选项会整体错位一位。

> 答案里本身带 `#`、`|`、`;` 这类分隔符时（比如程序输出 `2#3#5#7#`），程序会改用 **JSON 数组**回传（`["2#3#5#7#"]`）。OCS 的 `splitAnswer` 会先尝试 `JSON.parse`，这样内容不会被切开，填进去就是原样。

缓存命中时返回是毫秒级的，OCS 那边几乎无感。

> 自动更新答案时会重写该条目所在的 `.md` 文件。手写的笔记请放到 `data/bank/` 的子目录里（只读取该目录下的 `*.md`，不进子目录）。

---

## 命令行

```bash
python main.py                       # 启动服务（等价于 serve）
python main.py serve --port 8080     # 指定端口
python main.py check                 # 自检：配置 / 题库目录 / 模型连通性
python main.py config                # 打印 OCS 题库配置
python main.py ask "1+1=?" -o "A. 1
B. 2" -t single                      # 命令行直接问一道题
python main.py ask "看图作答" -i ./题目.png --no-cache
python main.py bank -q 网络 -n 10    # 查题库
python main.py bank --purge-stale    # 预览旧版无效条目（加 --yes 才删除，会先备份 .bak）
python main.py bank --forget 关键词   # 删掉匹配的缓存，让它重新作答（同样要 --yes）
```

`ask` 加 `--json` 可以看完整返回，方便排查。

---

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/search` | 答题，参数 `title` / `options` / `type` / `image` |
| GET | `/api/ocs-config` | 生成 OCS 题库配置 JSON |
| GET | `/api/stats` | 运行统计（请求数、命中数、题库条目） |
| GET | `/api/reload` | 重新加载配置与题库 |
| GET | `/api/bank?q=关键词` | 查询题库 |
| GET | `/health` | 健康检查 |

请求示例：

```bash
curl "http://127.0.0.1:8000/api/search?title=1%2B1%3D%3F&options=A.%201%0AB.%202&type=single"
```

返回：

```json
{
  "code": 1,
  "question": "1+1=?",
  "answer": "B. 2",
  "labels": "B",
  "type": "single",
  "from": "llm",
  "explain": "基础运算",
  "elapsed_ms": 1832,
  "cached": false,
  "images": 0,
  "search": false
}
```

`code` 为 1 表示成功，0 表示失败（`msg` 里是原因）。

---

## 常见问题

**Q：OCS 里显示「题库请求超时」。**
模型太慢或网络差。先把 `llm.timeout` 调小到 25~30，换更快的模型（`glm-4-flash`、`qwen-turbo`、`deepseek-flash`），`search.mode` 改 `auto` 或 `never`。已命中题库的题目不受影响。

**Q：脚本提示域名未授权 / 请求被拦截。**
把油猴 `@connect` 设为宽松模式，或换全域名通用版脚本。`type` 一定要是 `GM_xmlhttpRequest`。

**Q：OCS 提示「当前题库配置暂不支持图片、公式等题目处理」。**
说明配的题库配置没有引用图片题字段。OCS 只做静态检测：`method` 必须是 `post`，且 `data` 里要出现 `${images}` / `${suggestion_title}` / `${suggestion_options}` 这三个占位符，或者某个字段的 handler 源码里出现 `env.images` 这类引用。用状态页 / `python main.py config` **重新复制一份配置**即可（本程序 1.1 起已按新协议生成）。

**Q：日志里出现「模型没有返回合法 JSON」/「模型只返回了推理过程」。**
说明模型（通常是推理型，如 deepseek-reasoner、GLM-Z1、带思考开关的 Qwen3）把思考链当成了正文。程序现在会自动处理：先尝试从 `reasoning_content` 里找 JSON，找不到就把模型的原始输出作为上下文再要一次「只输出 JSON」；提示词末尾也强制要求以 `{` 开头。仍然频繁失败的话，换一个非推理模型，或把 `llm.provider` 指向同一家的普通对话模型。

**Q：数学/公式题互相串答案。**
两种旧数据会被自动跳过，不会再被命中：一是题干含 `[图片N]` 但没有图片指纹的图片题（键对不上），二是选项里含空项的条目（字母答案整体错位）。它们会留在 md 里当噪音，可以 `python main.py bank --purge-stale` 预览、加 `--yes` 清理（自动备份 .bak）。

**Q：多选题只返回了一个选项。**
提示词现在要求模型先在 `checks` 字段里按选项字母**逐个判断**，再由 checks 汇总出 answer；程序也只认 checks。这样模型即使习惯性只挑一个，逐项判断的结果也会被采纳。之前答错的题可以 `python main.py bank --forget 题干关键词 --yes` 删掉缓存让它重答。

**Q：多选题答成了单选题的答案。**
1.1 起题库按「题干 + 题型 + 选项」三重校验，不会再跨题型复用。若仍出现，检查 `data/bank/*.md` 里那条记录是不是手工写的、且没写 `- 类型:`；补上题型即可。

**Q：一直返回 code 0，日志里是 401 / 403。**
Key 不对或没充值。跑 `python main.py check` 看具体报错。

**Q：答案明显不对。**
先看 `data/logs/ocs.log` 里模型给的理由。常见原因是题干图片没抓到（平台把图片放在 iframe 或 canvas 里），可以调 `image` handler 的选择器；纯文本题出错可以换更强的模型。

**Q：能不能多台设备共用？**
把 `server.host` 改成 `0.0.0.0`，然后用局域网地址（如 `http://192.168.1.10:8000`）打开状态页——页面上的题库配置会自动使用你访问的那个地址，复制就能用，不用手改 IP。

**Q：题库会不会越写越大？**
每题一条，几千条也就几 MB，`default.md` 用编辑器直接搜也行。

---

## 目录结构

```
ocs-answer-llm/
├── main.py                 命令行入口
├── config.toml             配置（填 key 的地方）
├── start.bat               Windows 一键启动
├── ocs_answerer/
│   ├── config.py           配置加载与合并
│   ├── normalize.py        题干归一化、选项解析
│   ├── bank.py             Markdown 题库读写与匹配
│   ├── llm.py              OpenAI 兼容客户端
│   ├── vision.py           图片抓取与编码
│   ├── websearch.py        联网搜索渠道
│   ├── solver.py           答题主流程
│   ├── server.py           HTTP 服务与状态页
│   └── log.py              日志
├── data/
│   ├── bank/               题库（.md）
│   └── logs/               运行日志
└── tests/                  自动化测试
```

跑测试：

```bash
python -m unittest discover -s tests -v
```

其中 `tests/test_ocs_compat.py` 把 OCS 的答案匹配算法（`splitAnswer` / `answerNormalizedMatch` /
`resolveSingle` / `resolveMultiple` / `resolveJudgement` / `resolveCompletion`）移植成了 Python，
用来验证本程序输出的答案格式能被 OCS 正确识别；`tests/test_e2e.py` 则用假的模型服务跑通整条链路。

---

## 说明

本项目只负责「把大模型包装成 OCS 题库接口」，不修改、不注入任何学习平台页面。
模型给出的答案仍可能出错，请自行判断，不要用于需要诚信承诺的正式考试。
