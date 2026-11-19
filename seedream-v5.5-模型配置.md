# Seedream 5.5 适配与图层拆分 · 配置说明

> 官方文档：[图片生成 API](https://docs.volcengine.com/docs/ark/image-generation-api?lang=zh)（2026-11-13）、[Doubao Seedream 5.5 教程](https://docs.volcengine.com/docs/ark/seedream-5-5?lang=zh)
> 模型 ID：`doubao-seedream-5-5-261028`

## 1. 本次代码改动

### 1.1 按模型名自动裁剪参数（只适配 5.5 与 5.0 pro）

`_seedream_model_family()` 只识别两类版本关键词，其余一律视为 legacy（不发送任何新参数）：

| 模型名关键词 | 版本 | 发送策略 |
| --- | --- | --- |
| `5.5` / `5-5` / `5_5` | 5.5 | background(auto/transparent/opaque)、safety_identifier、layer_decomposition、text_recognition；fast→standard |
| `5-pro` / `5.0-pro` / `5-0-pro` | 5.0 pro | background(transparent/opaque)、safety_identifier、layer_decomposition；支持 fast |
| 其它（4.x、5.0 lite/flash、无版本词） | legacy | 只发 watermark / size / output_format / optimize(standard)，不发送 background、safety_identifier、tools、layer 参数 |

固定规则：
- `tools=[{type:web_search}]`：5.5 与 5.0 pro 均不支持，**任何模型名都不再发送**。
- `optimize_prompt_options.mode=fast`：5.5 自动转 `standard`；legacy 也回落 `standard`。
- `background=transparent`：自动锁定 `output_format=png`（官方 “transparent + jpeg” 会直接报错）。
- `background=auto`：只有 5.5 会真正发送（5.0 pro 无 auto 取值，legacy 不发送）。

### 1.2 新增「Seedream 图层拆分」参数模式

参数模式下拉新增 `seedream_layer`（Seedream 图层拆分），与 `seedream`（生图）互斥、各有一套参数：

| 配置项 | 发送字段 | 默认 | 说明 |
| --- | --- | --- | --- |
| 图层拆分分辨率 | `size` | auto | 只接受 1K / 1.5K / 2K / auto，不会发“宽x高” |
| 图层拆分最大输出图数 | `layer_decomposition_options.max_images` | 30 | 官方范围 2–100，按该值预扣 IPM |
| 图层拆分富文本OCR | `layer_decomposition_options.text_recognition` | false | 仅 5.5 支持 |
| 图层拆分输出格式 | `output_format` | png | 只控制底图，图层恒为 png |
| 图层拆分水印 | `watermark` | false | |
| 图层拆分显示图层信息 | 本地 | false | 关闭时合并转发只发图；开启后附带 z_index/名称/描述/坐标/OCR |

请求恒为 `layer_decomposition: true`，只允许 1 张输入图（多余参考图自动截断），且强制走 Images Generations 端点、不发送 `background`。

### 1.3 响应解析与发送

- 解析 `data[]` 全部条目：`url`/`b64_json`、`z_index`（底图 0，按序排列）、`name`、`description`、`bounding_box.absolute/normalized`、`text_recognition.text/lines[].runs[].style`、`output_format`、`background`、`size`、`error`。
- 全部图片下载完成后打包为 `LayerImageBundle`（本身是底图 bytes，兼容原有成功判定/计费）。
- 默认以**合并转发**（`Nodes`）一次性发出全部图层，**不带 Reply 引用段**；合并转发失败时回退为逐张发送（同样不引用）。
- 元数据无论是否显示都会被完整解析，只是默认不输出到聊天。

## 2. 给 bot 配置 seedream-v5.5

需求：触发词 `s5.5`、每次 0.175 元、参考图 ≤2 张（超过每张 +0.05）、透明参数 auto、能自动的都自动、分辨率档 2K、任何分辨率同价 0.175。

### 2.1 需要写入 `data/config/astrbot_plugin_shoubanhua_config.json` 的 4 处

```jsonc
// ① model_list 追加
"seedream-v5.5"

// ② images_generations_model_list 追加（必须，否则不会走 /v1/images/generations）
"seedream-v5.5"

// ③ command_model_list 追加（触发词）
{ "__template_key": "binding", "command": "s5.5", "model": "seedream-v5.5" }

// ④ model_parameter_list 追加
{
  "__template_key": "model_parameters",
  "model": "seedream-v5.5",
  "parameter_mode": "seedream",
  "api_key_tag": "默认",
  "request_model_name": "",
  "reference_image_limit": 2,
  "extra_reference_image_quota": 1,
  "extra_reference_image_charge": 0.05,
  "charge_amount": 0.175,
  "charge_amount_2k": 0.175,
  "charge_amount_4k": 0.175,
  "deduct_on_violation": false,
  "max_output_tokens": 0,
  "default_resolution": "2K",
  "send_default_size": false,
  "enable_seedream_parameters": true,
  "seedream_web_search": false,
  "seedream_send_output_format": true,
  "seedream_output_format": "png",
  "seedream_watermark": false,
  "seedream_resolution": "2K",
  "seedream_send_aspect_ratio": false,
  "seedream_send_detailed_resolution": false,
  "seedream_pixel_limit": 0,
  "seedream_adaptive_aspect_ratio": false,
  "seedream_max_side_2000": true,
  "seedream_side_over_2000_auto_2k": true,
  "seedream_optimize_prompt_mode": "standard",
  "seedream_background": "auto",
  "seedream_safety_identifier": ""
}
```

计费验算（`extra_reference_image_quota=1` → 每超 1 张算 1 阶梯）：
| 参考图 | 扣费 |
| --- | --- |
| 0–2 张 | 0.175 |
| 3 张 | 0.225 |
| 4 张 | 0.275 |

### 2.2 部署状态（已执行）

两个 AstrBot 实例都已推送代码并写入配置，容器已重启并验证插件加载正常：

| 实例 | 容器 | 插件目录 | 备份目录 |
| --- | --- | --- | --- |
| 主 3058627484 | `astrbot-3058627484-port` | `/www/wwwroot/astrbot/3058627484/data/plugins/astrbot_plugin_shoubanhua` | `/www/wwwroot/astrbot/3058627484/backups/` |
| 次 1878863124 | `astrbot-1878863124-port` | `/www/wwwroot/astrbot/1878863124/data/plugins/astrbot_plugin_shoubanhua` | `/www/wwwroot/astrbot/1878863124/backups/` |

实际写入的触发绑定：

- `s5.5` → `seedream-v5.5`（生图：0.175 元/次，参考图 >2 张每张 +0.05，2K 档，background=auto，output_format=png）
- `s5.5c` → `seedream-v5.5-layer`（图层拆分：request_model_name=`seedream-v5.5`，0.175 元/次，size=auto，max_images=30，只发拆分图）

容器内实测（**未调用任何生成接口**）：

```json
// s5.5 实际请求体
{"model":"seedream-v5.5","watermark":false,"output_format":"png","optimize_prompt_options":{"mode":"standard"},"background":"auto","size":"2K"}

// s5.5c 实际请求体
{"model":"seedream-v5.5","layer_decomposition":true,"layer_decomposition_options":{"max_images":30},"size":"auto","output_format":"png","watermark":false}
```

扣费实测：0 张 / 2 张参考图 = 0.175 元；3 张 = 0.225 元；4 张 = 0.275 元。

### 2.3 后续手动调整

- 价格/参考图阶梯：插件配置 → 模型参数设置 → 对应模型条目（`charge_amount`、`extra_reference_image_quota`、`extra_reference_image_charge`）。
- 想给 `s5.5c` 显示图层元数据：把该条目的「图层拆分显示图层信息」打开；想返回 OCR 文本，再打开「图层拆分富文本OCR」（仅 5.5 支持）。
- 想换触发词：改「触发指令绑定」里的 `command` 即可，模型名不用动。


## 4. 为什么图层拆分只回了一张图（New API 网关丢字段）

**现象**：`s5.5c` 只返回一张底图，插件日志为 `Seedream 图层拆分完成: images=1, z_index=[0]`，请求体里 `layer_decomposition: true` 确实发出去了。

**原因**：对接的网关是 **New API v1.0.0-rc.51**。它的 `relaykit/dto.ImageRequest` 只声明了 `model`/`prompt`/`n`/`size`/`response_format` 等少数字段，未声明的字段会被塞进 `Extra`，而 `MarshalJSON` **故意不把 Extra 合并回请求体**（官方 issue #7340 明确说明）。因此 `layer_decomposition` / `layer_decomposition_options` 在网关就被丢掉，火山方舟收到的是一次普通生图，自然只回一张。

**可选解决路径**（按可靠性排序）：

1. **直连火山方舟**（最稳）：给图层拆分模型绑定一个 `api_url = https://ark.cn-beijing.volces.com/api/v3` 的 API Key，绕过 New API。
2. **网关侧开启请求体透传**：New API 渠道设置里有 `pass_through_body_enabled`（请求体透传），开启后原始请求体会直接转发。
   注意：这只解决请求方向；New API 还会把出图转存到自己的存储（`webstatic.aiproxy.vip`），响应侧是否保留多图取决于其响应转换实现，可能仍只回一张。
3. **等 New API 支持**：需要在其 `dto.ImageRequest` 里声明 `layer_decomposition` / `layer_decomposition_options`（同 #7340 的修法，提 issue 或 PR）。

**插件侧的兜底**：现在如果检测到「只有底图、没有任何图层」，回复里会自动追加提示：

> ⚠️ 上游只返回了底图（没有任何图层）：对接的中转网关很可能丢弃了 layer_decomposition 参数，需要在网关侧给该渠道开启「请求体透传」，或改用直连火山方舟的 Key。

同时日志会打印 `图层拆分响应结构: top=[...], items=[[...]]`，可据此判断网关到底返回了什么字段。



## 5. 换到 apilio 网关（图层拆分已可用）

`https://api.apilio.ai/` 把图层拆分做成了**独立模型 ID**，而不是请求体参数：

| 模型 ID | 说明 |
| --- | --- |
| `seedream-v5.5/layer-decomposition` | Seedream 5.5 图层拆分 |
| `seedream-v5-pro/layer-decomposition` | 5.0 pro 图层拆分 |
| `seedream-v5-flash/layer-decomposition` | 5.0 flash 图层拆分 |

实测（1 张海报 → 拆分）：

- `POST /v1/images/generations`，model=`seedream-v5.5/layer-decomposition`，`size` 传 1K 或 auto 均可，约 39 秒返回；
- 响应 `data` **9 项**（1 张底图 + 8 个图层），每项带 `url`、`size`、`name`、`description`、`boundingBox`、`zindex`；
- 字段是 **camelCase**（`zindex` / `boundingBox` / `outputType`），插件已做双向兼容；
- 该模型的请求体里**不需要** `layer_decomposition` 参数（传了会被网关忽略）。

### 已回退（当前线上状态）

apilio 只是用来验证"图层拆分模型变体"这条路可行，验证完已按要求回退：

| 配置 | 当前值 |
| --- | --- |
| API Key 列表 | **apilio 已删除**，只剩默认等原有 Key |
| 模型 `seedream-v5.5-layer` | `api_key_tag=默认`、`request_model_name=seedream-v5.5/layer-decomposition` |
| 触发词 | `s5.5c`（不变） |

回退后原网关（10.10.10.89）实测：**HTTP 200 / 40.8 秒 / data 9 项（1 底图 + 8 图层）**，字段含 `zindex`、`name`、`boundingBox` —— 前提是 New API 后台已给 `seedream-v5.5/layer-decomposition` 配好价格（未配时会直接 400：`Model ... price not configured`）。

生图模型 `s5.5` 仍走原网关（10.10.10.89），只有图层拆分切到 apilio。

> 提示：apilio 的 `/v1/models` 里也有 `seedream-v5.5`（非 layer 版），如果要整体迁移生图模型，把 `s5.5` 的 `api_key_tag` 也改成 `apilio` 即可。



## 6. 精确比例模式（不吸附 8 档）

**开关**：模型参数 → Seedream → 「Seedream精确比例」（`seedream_exact_aspect_ratio`），默认关；当前 `s5.5` 与 `s5.5c` 已开启。

**开启后**：不再把源比例吸附到 1:1/4:3/3:4/16:9/9:16/3:2/2:3/21:9 八档，而是直接按源比例反算 `宽x高`：

- 像素预算取所选档位：1K=1048576、1.5K=2359296、2K=4194304；
- 自动夹到官方区间：总像素 [1280x720=921600, 2048x2048x1.1025=4624220]、宽高比 [1/16, 16]；
- 「像素数上限 (K)」「宽高均不超过2000」「边长超2000自动升2K」仍按原规则参与。

实测（容器内，1400x1000 的参考图，2K 档）：

| 模式 | 发送的 size | 实际比例 |
| --- | --- | --- |
| 关闭（8 档吸附） | `2368x1776` | 1.3333（4:3） |
| **开启（精确）** | `2423x1731` | **1.3998** |
| 开启 + 命令 `=16:9` | `2731x1536` | 1.7780 |

## 7. 比例来源日志

现在日志会明确写出"比例从哪来、原图多大"：

```
Seedream 精确比例: model=seedream-v5.5, 来源=第1张参考图 1400x1000, source_ratio=1.4000, resolution=2K, size=2423x1731 (实际比例=1.3998)
Seedream 自适应尺寸: model=seedream-v5.5, 来源=命令比例 16:9, source_ratio=1.7778, resolution=2K, aspect_ratio=16:9, size=2816x1584
```

- `来源=命令比例 16:9` → 用了消息里 `=宽:高`；
- `来源=第1张参考图 1400x1000` → 用了第 1 张图的原始像素；
- 读图失败会逐张顺延并打 WARNING（`读取 Seedream 第N张参考图尺寸失败`）。

> 注意：素材顺序是 **引用链 → 当前消息 → @头像**，所以"第1张参考图"不一定是你最后发的那张；想强制指定比例就在命令里写 `s5.5=16:9`。



## 8. 计费规则（2026-11-19 调整）

| 模型 / 触发词 | 计费方式 | 当前价格 |
| --- | --- | --- |
| `seedream-v5.5`（`s5.5`） | 每次生成固定价，任何分辨率同价 | **0.26 元/次** |
| `seedream-v5.5-layer`（`s5.5c`） | **返回张数 × 单张价格**（底图算 1 张） | **0.175 元/张** |

图层拆分实扣示例：

| 返回张数 | 扣费 |
| --- | --- |
| 1 张（只有底图） | 0.175 元 |
| 2 张 | 0.350 元 |
| 5 张 | 0.875 元 |
| 9 张（实测常见） | 1.575 元 |
| 30 张（max_images 上限） | 5.250 元 |

实现要点：

- 扣费在**拿到响应后**按实际张数结算（`_get_seedream_layer_cost`），不是调用前；
- 调用前的余额校验只按 **1 张** 的价格算，所以余额接近 0 时仍可能触发拆分，最终按可用余额扣减（账本记录实际扣费）；
- 成功消息里会多一段 `图层: N 张`，自定义模板可用 `{layer_count}` 占位符；
- 「该模型单次生成扣费」这一项在图层模式下即**单张价格**。

