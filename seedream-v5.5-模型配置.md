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
