# Seedream 5.5 官方 API 参数差异 & 本插件现状对比

> 官方来源：
> - 图片生成 API（字段全集，更新于 2026-11-13）：https://docs.volcengine.com/docs/ark/image-generation-api?lang=zh
> - Doubao Seedream 5.5 教程（能力差异）：https://docs.volcengine.com/docs/ark/seedream-5-5?lang=zh
>
> 模型 ID：`doubao-seedream-5-5-261028`
> 端点：`POST https://ark.cn-beijing.volces.com/api/v3/images/generations`

---

## 1. v5.5 新增字段（官方文档标注 new）

| 字段 | 类型 / 取值 | 默认 | 说明 |
| --- | --- | --- | --- |
| `background` | `auto` / `transparent` / `opaque` | `auto`（5.5） | 透明通道模式。`transparent` 直接输出原生 RGBA。5.5 文生图也支持（5.0 pro/flash 只支持图生图、且必须输入带 alpha 的 PNG）。`transparent` + `output_format=jpeg` 会**直接报错**。图层拆分场景传入该字段会报错。 |
| `layer_decomposition` | boolean | `false` | 图层拆分开关。5.5 最多 1 张底图 + **99 个图层**（5.0 pro/flash 为 16）。开启时 `image` 必填且只能传 1 张。 |
| `layer_decomposition_options.max_images` | int `[2,100]` | `30` | 本次最多输出图数（含底图）。按该值**预扣 IPM**，不足则返还。 |
| `layer_decomposition_options.text_recognition` | boolean | `false` | 富文本 OCR 开关，规整文字图层额外返回文本 + 样式（字体/颜色/粗体/斜体/下划线/删除线/阴影/半透明）。 |
| `safety_identifier` | string ≤64 | — | 终端用户标识，用于平台侧违规检测。5.5 / 5.0 pro / 5.0 flash 支持。 |

### 响应新增字段
- 公共：`data[].output_format`、`data[].background`（实际透明度类型，可能与请求值不同）、`usage.input_images`
- 图层拆分专用：`data[].z_index`（底图=0）、`name`、`description`、`bounding_box.{absolute,normalized}`（normalized 为 0–1000 整数）
- 富文本 OCR：`data[].text_recognition.{text, lines[].bounding_box, lines[].runs[].style}`

---

## 2. 与 5.0 pro / 5.0 flash 的同名字段行为差异

| 字段 | Seedream 5.5 | Seedream 5.0 pro / flash |
| --- | --- | --- |
| `output_format` 默认值 | **png** | jpeg |
| `background` 默认值 | auto（三值全支持） | opaque（仅 transparent / opaque） |
| `optimize_prompt_options.mode` | **仅 standard**（传 fast 会报错） | pro 支持 standard / fast |
| `size`（图片生成） | 1K / 1.5K / 2K，**默认 2K** | 同左 |
| `size`（图层拆分） | 1K / 1.5K / 2K / **auto（默认）** | 同左 |
| 参考图上限 | 10 张 | 10 张 |
| 组图 `sequential_image_generation` | **不支持** | 不支持（仅 5.0 lite / 4.5 / 4.0） |
| 流式 `stream` | **不支持** | 不支持（仅 5.0 lite / 4.5 / 4.0） |
| 联网 `tools:[{type:web_search}]` | **不支持** | 不支持（仅 5.0 lite） |
| 总像素区间（方式 2） | [921600, 4624220] = [1280x720, 2048x2048x1.1025] | 同左 |
| 宽高比区间 | [1/16, 16] | 同左 |

**5.5 尺寸映射表（官方，与本插件 `SEEDREAM_SIZE_OPTIONS` 完全一致）**

| 档位 | 1:1 | 4:3 | 3:4 | 16:9 | 9:16 | 3:2 | 2:3 | 21:9 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1K | 1024x1024 | 1152x864 | 864x1152 | 1424x800 | 800x1424 | 1248x832 | 832x1248 | 1568x672 |
| 1.5K | 1536x1536 | 1792x1344 | 1344x1792 | 2048x1152 | 1152x2048 | 1872x1248 | 1248x1872 | 2352x1008 |
| 2K | 2048x2048 | 2368x1776 | 1776x2368 | 2816x1584 | 1584x2816 | 2496x1664 | 1664x2496 | 3136x1344 |

---

## 3. 其他要点

- **官方没有 `aspect_ratio` 字段**。比例只能通过 `size`（宽x高）或在 prompt 里用自然语言描述。全文检索官方 API 文档，`aspect_ratio` 出现 0 次。
- 提示词：官方建议中文 ≤300 字、英文 ≤600 词。5.5 额外支持俄/阿/菲/泰/土/韩/马来/西/葡/印尼/法/德/越/日等语言。
- 多轮编辑一致性：官方建议优先用不带透明通道的 RGB 图，并设 `background=opaque`。
- `watermark` 默认 true，本插件默认 false（需显式传 false 才能去水印，插件已做）。
- 图层拆分任一图层失败 → 整体请求报错，不支持部分成功。

---

## 4. 本插件现状 vs 官方 v5.5

### 已覆盖（字段名正确）

| 插件配置 | 实际发送字段 | 结论 |
| --- | --- | --- |
| `seedream_watermark` | `watermark` | ✅ |
| `seedream_send_output_format` / `seedream_output_format` | `output_format` | ✅ 默认 png 正好符合 5.5 |
| `seedream_optimize_prompt_mode` | `optimize_prompt_options.mode` | ⚠️ 配置项含 `fast`，5.5 会 400 |
| `seedream_web_search` | `tools: [{type: "web_search"}]` | ❌ 5.5 不支持联网 |
| `seedream_send_detailed_resolution` + 自适应尺寸 | `size`（1K/1.5K/2K 或 WxH） | ✅ 尺寸表与官方 5.5 完全对齐 |
| `seedream_send_aspect_ratio` | `aspect_ratio` | ❌ 非官方字段，官方 Ark 可能 400，仅部分中转支持 |

### 完全缺失

| 官方字段 | 影响 |
| --- | --- |
| `background` | 拿不到 5.5 最核心的新能力：原生透明背景（贴纸/图标/商品素材） |
| `layer_decomposition` + `layer_decomposition_options` | 拿不到图层拆分（1+99）与富文本 OCR |
| `safety_identifier` | 合规相关，可选项 |

### 代码层面阻碍（若要支持图层拆分）

- `_extract_image_url_from_response()` 只取 `urls[0]`（main.py:6844），多图响应会被截断成 1 张。
- `_extract_image_urls_from_response()` 未解析 `z_index` / `bounding_box` / `name` / `description` / `text_recognition`，也没有"底图 + 图层"的输出组装逻辑。
- `_get_seedream_request_parameters()`（main.py:5638）目前只组装 watermark / output_format / optimize_prompt_options / tools / aspect_ratio / size。

---

## 5. 建议改动（按优先级）

**P0 —— 修正确性（不修就会在 5.5 上报错）**
1. `seedream_web_search`：在 5.5 模型下强制不发送 `tools`（或对模型名做版本判断，仅 5.0 lite 才发）。
2. `seedream_optimize_prompt_mode`：5.5 只允许 `standard`，`fast` 时自动降级并 warn。

**P1 —— 补齐 5.5 核心新能力**
3. 新增 `seedream_background`（`auto` / `transparent` / `opaque`，默认 `auto`）+ `seedream_send_background` 开关；
   校验：`transparent` + `jpeg` → 自动改 png 或直接拒绝并提示。
4. `seedream_send_aspect_ratio` 的 hint 说明"非官方字段，仅中转支持；官方 Ark 请用 size 或提示词表达比例"。

**P2 —— 图层拆分（工作量大，涉及多图链路）**
5. 新增 `seedream_layer_decomposition` 开关 + `max_images`(2–100，默认 30) + `text_recognition` 开关。
6. 需要配套：多图返回、z_index 排序、底图/图层打包（zip 或逐张发送）、OCR 结果可选输出；以及 `max_images` 的预扣费逻辑。

**P3 —— 可选**
7. `safety_identifier`（可复用用户 ID 哈希）。
8. 模型条目里加 `doubao-seedream-5-5-261028` 的预设模板（分辨率默认 2K、output_format png、background auto）。
