# 支持 gemini 路由
公开仓库：https://github.com/misaka-link/astrbot_plugin_shoubanhua

征求api请求格式 请提ls  看到了可以考虑融合在一起
# docker部署输出lm列表的时候全是符号 解决办法 请运行 
apt update 

apt install -y fonts-dejavu fonts-noto fonts-freefont-ttf

手动部署请什root权限运行（针对linux）
# 通过第三方柏拉图api调用 Banana的api使用
注册地址 [柏拉图1]( https://api.bltcy.ai/register?aff=dcc39044557)
# 原先bnn涨价原因
因为nano-banana涨价到0.08一次毫无性价比，上了自定义模型建议全部换成gemini-2.5-flash-image-preview、gemini-2.5-flash-image。
测试下来效果是一样的
## 功能特性
- **多风格转换**：内置十几种指令，如 `#手办化`、`#Q版化`、`#痛车化`、`#鬼图` 等，满足不同场景需求。
- **自定义生成**：使用 `#bnn <提示词>` 指令，可以完全自定义 Prompt 进行创作；也可用 `#bnn*2 <提示词>` 并发生成多次。
- **灵活的输入方式**：支持直接发送图片、回复图片、或`@用户`来使用其头像进行制作。
- **强大的管理功能 (管理员限定)**：
  - **Key 管理**：通过指令动态添加、查看、删除 API Key，支持配置多个 Key 并自动轮换使用。
  - **用户次数管理**：可为普通用户设置使用次数，并通过指令进行增加和查询，实现轻量级付费或激励机制。
- **高度可定制**：所有指令的默认提示词（Prompt）都在后台配置文件中开放，也可按模型指定最终提示词模板。
- **代理支持**：内置 HTTP / SOCKS5 代理支持，方便在特殊网络环境下部署。

## 安装与配置

### 安装

2.  将该文件夹放入 `astrbot/plugins` 目录下。
3.  重启 AstrBot。

### 配置

在 AstrBot 管理面板的 `插件管理` -> `手办工坊 Pro` 中进行配置。常用项说明：

| 配置项 | 说明 |
| --- | --- |
| `generic_api_url` | API 服务地址。直接填写根地址即可，例如 `http://10.10.10.99:3000` 或 `http://10.10.10.99:3000/`；插件会按模型路由自动选择 Gemini 或 OpenAI 格式并拼接路径，也兼容完整 OpenAI 端点 URL |
| `generic_api_keys` | Generic 模式 Key 池（可多条轮询）。当 `generic_api_url` 为服务根地址且 Gemini Key 池为空时，Gemini 路由自动复用本池 |
| `request_user_agent` | 发送 Generic/Gemini 生图 API 请求时使用的 User-Agent，默认 `Codex Desktop/0.145.0-alpha.30 (Ubuntu 22.4.0; x86_64) xterm-256color (Codex Desktop; 26.715.72359)`；留空使用 HTTP 客户端默认值，不影响图片下载请求 |
| `gemini_api_url` | 旧版兼容配置。仅当 `generic_api_url` 填写完整 OpenAI 端点 URL 时，Gemini 路由才回退使用它；默认 https://generativelanguage.googleapis.com |
| `gemini_api_keys` | Gemini 路由 Key 池 |
| `max_output_tokens` | Gemini 和 Generic Chat Completions 的最大输出/思考 Token 默认值；`0`（默认）不发送限制参数，即不限制；模型级正数可覆盖 |
| `model_list` | 可用模型 ID 列表，默认包含 nano-banana 等 |
| `gemini_model_list` | 命中后走 Gemini 端点的模型列表 |
| `command_model_list` | 触发指令和模型名绑定列表，不改变默认触发指令 |
| `model_prompt_template_list` | 预设提示词列表，按模型指定最终发送给绘图接口的提示词模板 |
| `model_parameter_list` | 模型参数设置列表；模型名、扣次和最大输出/思考 Token 始终生效，GPT/Gemini 图片参数必须开启各自开关后才会发送 |
| `model_mapping_list` | 模型热备映射列表；选择源模型后按映射项的优先权重从高到低请求实际模型，同权重按列表顺序 |
| `chat_completions_model_list` | 走 `/v1/chat/completions` 的模型列表。端点模型列表为空或模型未匹配时，也默认走该端点 |
| `images_generations_model_list` | 走 `/v1/images/generations` 的模型列表。常用于文生图 |
| `images_edits_model_list` | 走 `/v1/images/edits` 的模型列表。常用于带图请求，插件会用 multipart/form-data 上传图片 |
| `model` | 默认模型（需在模型列表中存在），示例：gemini-2.5-flash-preview-image |
| `show_model_info` | 是否在成功/失败消息中显示实际调用模型 |
| `debug_mode` | 调试模式，开启后报错会附加完整错误内容 |
| `send_error_reason` | 默认报错中展示上游返回的简短原因 |
| `send_error_context` | 默认报错中展示 request_id、endpoint、model 等定位信息 |
| `error_default_message` / `error_400_message` 等 | 自定义错误模板。支持 `{status_code}`、`{elapsed}`、`{detail}`、`{provider_message}`、`{model}`、`{endpoint_type}`、`{endpoint}`、`{request_id}` 等变量 |
| `content_policy_warning_message` | 上游判定内容违规、安全拦截或命中配置错误码时发送的独立警告内容；支持成功提示模板的通用变量和批量变量 |
| `prefix` | 是否需要命令前缀或 @ 才触发 |
| `maintenance_mode` | 维护模式开关。开启后拦截所有插件命令；不调用上游 API，也不扣除次数，可在后台关闭恢复使用 |
| `maintenance_message` | 维护模式下返回给用户的提示文本；留空时使用默认提示 |
| `extra_prefix` | 自定义提示词前缀（如 bnn，用 bnn <prompt> 调用） |
| `preset_list_command` | 预设/提示词列表触发指令，默认 `手办化列表`；修改后仅新配置的命令可触发，`lm列表`、`lmlist`、`预设列表` 不再兼容 |
| `preset_list_template` | 预设列表图片模板，默认读取 `templates/preset_list.html` |
| `batch_multiplier_symbol` | 批量生成倍率符号，默认 `*`，如 `#bnn*2 一只小猫` |
| `resolution_symbol` | 临时覆盖模型自适应分辨率的符号，默认 `x`；`x1/x2/x4` 对应 `1K/2K/4K`，可与批量倍率、比例任意排序组合 |
| `aspect_ratio_symbol` | 临时覆盖参考图比例的符号，默认 `=`；格式为 `=宽:高`，支持英文 `:` 和中文 `：`，可与批量倍率、分辨率级别任意排序组合；可用于 GPT 或 Gemini 的自适应比例 |
| `default_batch_count` | 未写倍率时的默认批量数，默认 1 |
| `max_batch_multiplier` | 单次指令最大生成倍率，可填 1-100，扣次按实际倍率计算 |
| `max_batch_concurrency` | 批量生成最大并发数，可填 1-20，倍率更大时会排队执行 |
| `use_proxy` / `proxy_url` | 启用代理与代理地址（支持 `http(s)://` 或 `socks5://` 等格式） |
| `timeout` | 请求超时（秒），默认 120 |
| `use_stream` | Generic 模式是否走流式请求。仅 `chat/completions` 路径有效，`images/generations` 不使用流式 |
| `download_retries` | 图片下载重试次数 |
| `help_command` | 帮助菜单触发命令，默认 `手办化帮助`；不需要写 `#`，`lmh` 和 `lm帮助` 不再作为别名 |
| `help_text` | 自定义帮助菜单显示文本，支持 Markdown；可使用变量 `{custom_command_model_bindings}`，会自动替换为自定义提示词前缀与模型的绑定列表，未绑定时显示默认模型，箭头会按最长触发词自动对齐 |
| `user_whitelist` / `user_blacklist` | 用户白/黑名单 |
| `group_whitelist` / `group_blacklist` | 群聊白/黑名单，白名单群不限制次数；全局管理员可无视群黑名单 |
| `enable_user_limit` / `enable_group_limit` | 是否启用用户/群组次数限制 |
| `resolution_1k_cost` / `resolution_2k_cost` / `resolution_4k_cost` | 分辨率档位的扣除次数；“1K超限自动转2K”实际升级时使用 `resolution_2k_cost`，默认分别为 1、2、4 |
| `failure_deduction_status_codes` | 触发违规警告和失败扣次判断的 HTTP 状态码列表，默认仅 `400`；命中后会停止模型热备切换 |
| `deduct_on_failure_status_codes` | 错误码是否扣除次数，默认开启；关闭后命中错误码仍会警告但不扣次 |
| `enable_checkin` | 是否启用每日签到获取次数 |
| `checkin_fixed_reward` | 签到固定奖励（未开启随机时） |
| `enable_random_checkin` / `checkin_random_reward_max` | 签到随机奖励开关与最大值 |
| `prompt_list` | 预设提示词列表，每项包含 `指令` / `提示词`；旧 `触发词:提示词` 会自动迁移 |

> 路径说明：`generic_api_url` 填写服务根地址（如 `http://10.10.10.99:3000` 或 `http://10.10.10.99:3000/`）时，命中 `gemini_model_list` 的模型自动请求 `/v1beta/models/{model}:generateContent`，并优先使用 Gemini Key 池，空时复用 Generic Key 池；其余模型按端点模型列表拼接 OpenAI `/v1/chat/completions`、`/v1/images/generations` 或 `/v1/images/edits`。未匹配的模型默认走 Chat Completions。同一模型同时配置在 `images_generations_model_list` 和 `images_edits_model_list` 时，文生图走 `/v1/images/generations`，图生图走 `/v1/images/edits`。填写完整 OpenAI 端点 URL 时，Generic 行为保持兼容，Gemini 继续使用旧版独立地址。

> 默认生成开始消息会显示当前调用端点简名，例如 `端点: edits`。自定义 `custom_img2img_start_message` 或 `custom_text2img_start_message` 只有包含 `{endpoint}` 时才显示端点，不再自动追加。

> 每次提交生图请求前，插件日志会输出请求方式、最终 URL、路由、请求头和全部非图片生图参数。API Key 会显示为 `<redacted>`，图片字段仅显示 `<image omitted>`，不会输出图片内容、Base64 或图片摘要。Images Edits 的其他 multipart 字段会逐项展开记录。

> 使用 SOCKS 代理时，需要在 AstrBot 的 Python 环境中先执行 `pip install aiohttp_socks`。

### 按模型预设提示词模板

`model_prompt_template_list` 用于给不同模型配置不同的最终提示词模板。命中模型后，插件会用该模板替换默认英文包装提示词；未命中模型时保持原逻辑。

可用变量：

- `{prompt}`：用户输入或预设展开后的提示词内容。
- `{model}`：本次实际调用模型。
- `{mode}`：生成模式，值为 `文生图` 或 `图生图`。
- `{image_count}`：输入图片数量。
- `{default_prompt}`：插件原本默认包装后的完整提示词。

配置示例：

| 模型 | 提示词模板 |
| --- | --- |
| `gemini-2.5-flash-image` | `请根据以下内容直接生成图片，不要解释：{prompt}` |
| `nano-banana` | `{default_prompt}` |

### 模型热备映射

`model_mapping_list` 使用与“模型参数设置”相同的可添加列表形式。每项填写 `源模型`、`映射模型` 和可选的 `优先权重`；源模型是逻辑选择入口，只要存在映射项，插件就直接调用第一条映射模型，不会先请求源模型。

同一源模型可以添加多条映射项。优先权重默认为 `0`，数值越大越优先；权重相同时按后台列表从上到下保持原顺序。未设置权重的旧配置与填写 `0` 的配置效果相同。例如按以下顺序配置后，选择 `model-1` 时会先请求 `model-3`，再请求 `model-2`，最后请求 `model-4`：

| 源模型 | 映射模型 | 优先权重 |
| --- | --- | ---: |
| `model-1` | `model-2` | `0` |
| `model-1` | `model-3` | `10` |
| `model-1` | `model-4` | `0` |

收到请求的开始消息显示源模型，便于对应用户选择；生成成功或最终失败消息中的模型名显示实际请求的映射模型。

任何未成功生成图片的结果，包括上游 HTTP 错误、超时、响应无图片或图片下载失败，都会继续切换到下一条热备模型；但上游判定内容违规/安全拦截，或 HTTP 状态码命中 `failure_deduction_status_codes` 时会立即停止热备，并单独发送违规内容警告，不再请求后续模型。全部模型失败后才向用户返回最后一次失败结果；中间失败不会单独扣次。

每次尝试均以实际调用模型为准：其 `model_prompt_template_list`、Gemini/Generic 路由、端点列表和 `model_parameter_list` 都会重新计算。因此映射模型在“模型参数设置”中配置了扣次、Token、GPT 图片参数或 Gemini 图片参数时，切换后严格遵循映射模型自身的设置。成功、最终失败和失败扣次也按照实际调用模型结算；配额预检会按所有候选模型中的最高单次扣次检查，避免备用模型扣次更高时超额。

端点路由列表同样必须填写实际调用的映射模型名：例如 `model-1 -> model-2` 且 `model-2` 应走 Images Edits 时，`images_edits_model_list` 必须填写 `model-2`，不能只填写 `model-1`。Images Generations、Chat Completions 与 Gemini 路由列表也遵循同一规则；只有未配置映射项的源模型才需要自行配置实际路由。

### 模型参数设置

`model_parameter_list` 使用与“触发指令模型绑定”相同的列表对象形式。模型名、扣除次数、最大输出/思考 Token、默认分辨率和“默认传递 size”位于每项最上方，且不受下方 GPT/Gemini 参数开关影响。

- `模型`：填写模型列表或端点模型列表中的模型名。
- `该模型扣除次数`：每次生成扣除多少次，默认 `1`；不受 GPT/Gemini 参数开关影响，批量生成还会乘以实际生成倍率。
- `最大输出/思考 Token`：仅 Gemini 和 Generic Chat Completions 路由生效。大于 `0` 时覆盖全局 `max_output_tokens`；填 `0` 时继承全局设置。全局也为 `0`（默认）时不发送限制参数，即不限制。Gemini 使用 `maxOutputTokens`，Generic Chat Completions 使用 `max_tokens`；不受 GPT/Gemini 参数开关影响。
- `默认分辨率`：独立设置，启用“默认传递 size”后，在自适应比例未启动或未生成有效尺寸时发送的 `size` 参数，默认 `auto`；可填写供应商支持的其他 `size` 值。
- `默认传递 size`：独立设置，默认关闭。开启后，Images Generations / Edits 请求在未生成自适应尺寸时发送“默认分辨率”（默认 `size=auto`）；关闭时不传递兜底 `size`。不受 GPT/Gemini 参数开关影响。
- `GPT参数设置`：默认关闭。只有开启后，下面的质量、审核、自适应比例、自适应比例分辨率和强制限制分辨率才会生效；关闭时即使已填写也不会发送这些参数。
- `质量`：可选 `low`、`medium`、`high`、`auto`，默认 `auto`。
- `审核`：可选 `auto`、`low`。`auto` 为标准过滤；`low` 的过滤限制较少。
- `自适应比例`：默认关闭。开启后，带图的 Images 请求会读取第一张图片宽高比并计算满足 Images API 约束的 `size` 参数。
- `自适应比例分辨率`：可选 `1K`、`2K`、`4K`，默认 `1K`。开启自适应比例后默认使用此值；命令中的 `x1/x2/x4` 可临时覆盖。
- `1K超限自动转2K`：默认关闭。开启后，当有效自适应分辨率为 `1K` 且初次计算出的 `size` 任一边超过 `1024` 时，插件会改按 `2K` 重新计算并提交 `size`；本次请求也按全局 `resolution_2k_cost` 结算。它与“强制限制分辨率”互斥；两项同时开启时，本项优先并自动按关闭强制限制处理。
- `比例设置符号`：默认 `=`。命令中的 `=16:9` 或 `=16：9` 可临时覆盖参考图比例；比例参数会与 `x1/x2/x4`、批量倍率任意排序，例如 `#bnnx4*2=16:9`、`#bnn=16:9x4*2`。未传比例时继续使用参考图比例。
- `强制限制分辨率`：默认关闭。开启后按供应商的最长边计费档位限制输出 `size`：`1K` 不超过 `1024`、`2K` 不超过 `2048`、`4K` 不超过 `3840`；宽高仍保持为 16 的倍数且总像素不少于 `655,360`。原始比例无法同时满足时会增大短边，1K 的宽屏或竖屏比例最多为 `8:5`。开启“1K超限自动转2K”时，本项自动失效。
- `Gemini参数设置`：默认关闭。只有模型走 Gemini 路由且该开关开启时，下面的 Gemini 图片配置才会发送到上游。
- `Gemini分辨率`：可选 `auto`、`1K`、`2K`、`4K`，默认 `auto`。选择 `auto` 时不发送 `imageSize`，由上游使用默认值；选择 `1K`、`2K` 或 `4K` 时发送对应值。
- `Gemini自适应比例`：默认关闭。开启后，带图请求会读取第一张图片比例，或使用命令 `=宽:高` 指定的比例，并按比例距离映射为 Gemini 官方 `ImageConfig.aspectRatio` 枚举中最接近的一项后发送。它只控制比例，不会修改 Gemini 分辨率；优先级高于“Gemini图片比例”。
- `Gemini图片比例`：可选 `auto`、`1:1`、`1:4`、`4:1`、`1:8`、`8:1`、`2:3`、`3:2`、`3:4`、`4:3`、`4:5`、`5:4`、`9:16`、`16:9`、`21:9`，默认 `auto`。选择 `auto` 时不发送 `aspectRatio`，官方端点会在有参考图时依据参考图决定比例，无参考图时使用模型默认比例；选择其他值时发送对应比例。开启“Gemini自适应比例”且读取到首图时，本项会被自适应结果覆盖。不同 Gemini 模型支持的比例可能不同。

Gemini 图片配置使用官方 `generateContent` 请求结构 `generationConfig.imageConfig`：

  ```json
  {
    "generationConfig": {
      "imageConfig": {
        "imageSize": "4K",
        "aspectRatio": "16:9"
      }
    }
  }
  ```

自适应尺寸默认遵守以下规则：宽高都是 16 的倍数、最长边不超过 3840、长短边比例不超过 3:1，总像素不少于 655,360 且不超过 8,294,400。`1K` 和 `2K` 分别以 1024、2048 为目标最长边，`4K` 以 3840 为目标最长边；若目标尺寸超出总像素范围，会等比放大或缩小后再对齐到 16。开启“强制限制分辨率”后，最长边限制与最小总像素规则同时生效；比例无法同时满足时会调整短边。

例如第一张图是 `1920x1080`（16:9）：模型配置为 `1K` 且关闭“强制限制分辨率”时提交 `size=1088x608`（`1024x576` 低于最小总像素，因此自动放大）；若开启“1K超限自动转2K”（即使强制限制仍被配置为开启，也会自动失效），会改按 2K 提交 `size=2048x1152` 并按 `resolution_2k_cost` 扣次。单独开启“强制限制分辨率”后提交 `size=1024x640`，既不超过最长边 1024，也满足最小总像素，但比例会从 16:9 调整为 8:5。使用 `#bnnx2` 临时覆盖后提交 `size=2048x1152`；使用 `#bnnx4` 提交 `size=3840x2160`。命令带 `=9:16` 时会覆盖参考图比例并按竖图方向生成对应 `size`。竖图会交换宽高；方图 4K 会受最大总像素限制，提交 `2880x2880`。比例超过 3:1 的参考图或命令比例会按 3:1 上限计算。插件只增加请求参数，不会缩放或修改上传的原图。

当前模型开启“GPT参数设置”后，插件才会向 `/v1/images/generations` 或 `/v1/images/edits` 发送质量、审核和自适应计算出的 `size`。请求携带图片且开启“自适应比例”时，会按模型配置的分辨率增加计算后的 `size`；命令带 `x1/x2/x4` 时覆盖分辨率级别，命令带 `=宽:高` / `=宽：高` 时覆盖参考图比例。“默认传递 size”独立生效：自适应比例未启动、无图片或未生成有效尺寸时，开启该开关才会发送“默认分辨率”（默认 `size=auto`）；该开关默认关闭。Chat Completions 和 Gemini 路由不提交 Generic 的 `size`；Gemini 的图片大小和比例由“Gemini参数设置”独立控制，使用官方 `generationConfig.imageConfig` 字段。开启“Gemini自适应比例”后，带图请求会把首图（或 `=宽:高`）映射为最接近的 Gemini 官方 `aspectRatio` 并提交，不影响其 `imageSize` 或扣次。自适应分辨率通常只影响请求参数；仅启用“1K超限自动转2K”并实际升级时，扣次改用 `resolution_2k_cost`。

每次请求通常按模型的“该模型扣除次数”扣次，最后再乘批量倍率。例如模型配置为每次扣除 `3` 次时，`#bnn*2x4` 共扣 `3 x 2 = 6` 次。仅当“1K超限自动转2K”实际升级时，单次扣除次数改为全局 `resolution_2k_cost`；配额预检和最终成功/失败结算均使用同一规则。

次数按每个请求的实际结果结算：生成成功正常扣次。上游判定内容违规或触发安全拦截时，或 HTTP 状态码命中 `failure_deduction_status_codes` 时，均会单独发送 `content_policy_warning_message`。命中错误码是否扣次由 `deduct_on_failure_status_codes` 决定，默认扣次；关闭后仍会警告但不扣次。未命中错误码的普通失败不扣次，并继续模型热备切换。批量生成逐个结果结算。

`content_policy_warning_message` 支持与 `custom_success_message` 相同的通用变量：`{model}`、`{label}`、`{image_count}`、`{elapsed}`、`{remaining}`、`{prompt}`；另有 `{reason}`，会替换为上游返回的主要错误原因（例如安全拦截说明）。批量、预设和 `#bnn` 入口还支持 `{batch_count}`、`{batch_index}`、`{max_batch_concurrency}`。独立 `#文生图` 保持成功模板的现有行为，不替换这三个批量变量。默认警告会提示更换模型、提示词或参考图，并显示实际模型与任务序号。

旧版 `deduct_on_content_policy_violation` 配置会在首次加载时自动迁移为 `deduct_on_failure_status_codes`，已存在新配置时不会覆盖。

配置示例：

| 模型 | 扣除次数 | 最大输出/思考 Token | GPT参数设置 | GPT 分辨率 | Gemini参数设置 | Gemini分辨率 |
| --- | --- | --- | --- | --- | --- | --- |
| `gpt-image-1` | `1` | `0` | 开启 | 自适应 `2K` | 关闭 | `auto`（不生效） |
| `gemini-2.5-flash-image` | `1` | `8192` | 关闭 | 不发送 | 开启 | `4K` |

## 使用方法

- **发送图片**并使用命令。
- **引用**含有图片的消息并使用命令。
- **@某人**并使用命令 (将使用该用户的头像)。

---
## 新增

新增签到系统，文生图功能和自定义模型。

### 本次更新

- 新增维护模式开关和可配置提示内容；开启后会拦截所有插件命令，不调用 API 或扣次。
- 模型参数设置新增 GPT/Gemini 参数开关；模型名、模型扣次和最大输出/思考 Token 始终生效，其余参数须开启对应开关。Gemini 支持 `auto`、`1K`、`2K`、`4K` 分辨率。
- Gemini 参数设置新增图片比例，可设为 `auto` 或官方 `ImageConfig` 支持的比例；图片大小和比例改按官方 `generationConfig.imageConfig` 请求结构提交，并支持按首图自适应比例。
- 后台新增触发指令与模型绑定列表，配置项位于最上方。
- 后台新增按模型配置的预设提示词列表，可覆盖不同模型最终收到的提示词模板。
- 后台新增模型参数设置列表，支持为 Images Generations / Edits 模型指定质量与审核参数。
- 后台新增模型热备映射列表；同一源模型可按优先权重配置多个备用模型，权重从高到低调用，同权重按列表顺序，失败后自动切换，且备用模型遵循自身的模型参数设置。
- 模型参数设置新增自适应比例开关和 `1K/2K/4K` 默认分辨率；分辨率符号（默认 `x`）可用 `x1/x2/x4` 临时覆盖，并支持与批量倍率、比例任意排序组合。
- 模型参数设置新增独立的“默认传递 size”开关，默认关闭；仅开启时才会在未生成自适应尺寸的场景提交默认 `size=auto`。
- GPT 参数设置新增“1K超限自动转2K”开关：1K 自适应尺寸任一边超过 `1024` 时，改按 2K 计算并使用 `2K` 扣次。
- 新增比例设置符号（默认 `=`），支持 `=16:9` / `=16：9` 覆盖参考图比例，且参数可乱序组合。
- 新增 `1K/2K/4K` 扣次配置和模型默认扣次配置。
- 新增失败扣次错误码列表，默认仅 HTTP `400` 失败会扣次。
- 新增错误码是否扣次开关；命中错误码时可控制是否扣次。
- 新增违规内容警告文本；上游安全拦截或命中配置错误码时，均单独发送该警告并停止模型热备切换。
- 生图请求发送前记录全部非图片参数，API Key 脱敏且图片字段不输出内容。
- 移除后台 `gemini_official` / API 模式切换，改为 `gemini_model_list` 自动路由。
- 预设列表改为 HTML 模板渲染，模板位于 `templates/preset_list.html`。
- `prompt_list` 改为对象列表，并自动兼容/导入旧字符串格式。
- 新增默认批量数 `default_batch_count`，默认 1。
## 📖 命令列表

### 基础与新增命令（合并）

| 命令 | 功能说明 |
| :--- | :--- |
| `#文生图 <描述>` | 文生图：输入描述生成图片 |
| `#自定义 <提示词>` | 搭配图片使用自定义提示词进行图生图（回复/携带图片） |
| `#手办化` | 生成角色的手办造型，偏向立体模型展示 |
| `#手办化2` | 生成另一种风格的手办造型，可能是细节或比例的不同 |
| `#手办化3` | 生成不同版本的手办展示，更偏系列感 |
| `#手办化4` | 生成手办化第四种风格，可能是更精致或特殊造型 |
| `#手办化5` | 生成另一种改良版手办造型 |
| `#手办化6` | 生成手办化的第六种衍生风格 |
| `#Q版化` | 生成Q版（可爱简化比例）的角色形象 |
| `#痛屋化` | 生成痛屋（贴满角色元素装饰的房间）场景 |
| `#痛屋化2` | 生成改良版痛屋场景，更丰富或现代感 |
| `#痛车化` | 生成痛车（贴有角色图案的车辆）造型 |
| `#cos化` | 生成角色cosplay化的照片风格 |
| `#cos自拍` | 生成角色自拍风格的cos照片 |
| `#孤独的我` | 生成孤独、滑稽或小丑化的意境图 |
| `#第三视角` | 生成第三人称视角场景，看起来像他人在看角色 |
| `#鬼图` | 生成灵异鬼图风格照片，带恐怖氛围 |
| `#第一视角` | 生成第一人称视角场景，沉浸感强 |
| `#贴纸化` | 生成贴纸风格的小图，方便做表情或周边 |
| `#玉足` | 生成角色玉足相关的画面或细节 |
| `#fumo化` | 生成毛绒玩偶（fumo）风格角色 |
| `#cos相遇` | 生成两位cos角色相遇的场景 |
| `#三视图` | 生成角色三视图（正面、侧面、背面） |
| `#穿搭拆解` | 生成角色服装穿搭的详细拆解图 |
| `#拆解图` | 生成模型拆解或零件展示图 |
| `#角色界面` | 生成类似游戏中角色信息界面的画面 |
| `#角色设定` | 生成角色设定图，包含全身、武器、细节等 |
| `#3D打印` | 生成适合3D打印的模型预览图 |
| `#微型化` | 生成微缩模型、小比例角色形象 |
| `#挂件化` | 生成挂件、钥匙扣风格的角色造型 |
| `#姿势表` | 生成角色姿势参考表，多种动作合集 |
| `#高清修复` | 对画面进行高清化、细节修复 |
| `#人物转身` | 生成人物转身动作的连续画面 |
| `#绘画四宫格` | 生成四宫格绘画对比图或进度展示 |
| `#发型九宫格` | 生成九种不同发型的对比图 |
| `#头像九宫格` | 生成九个不同风格的头像合集 |
| `#表情九宫格` | 生成角色九种不同表情合集 |
| `#多机位` | 生成多机位拍摄的场景视角合集 |
| `#电影分镜` | 生成电影风格的分镜图 |
| `#动漫分镜` | 生成动漫风格的分镜图 |
| `#真人化` | 生成角色的真人化形象（真实感较强） |
| `#真人化2` | 生成另一种风格的真人化形象 |
| `#半真人` | 生成半写实半动漫的混合风格 |
| `#半融合` | 生成角色与其他元素融合的半融合风格 |

### 自定义与查询

| 命令 | 功能说明 |
| :--- | :--- |
| `#bnn <提示词>` | 使用自定义提示词生成 |
| `#bnn*2 <提示词>` | 使用倍率并发生成 2 次，倍率符号可在后台配置 |
| `#bnnx4 <提示词>` | 临时覆盖为 4K 分辨率；仅在当前模型开启自适应比例且请求带图时生效 |
| `#bnn*2x4` / `#bnnx4*2` | 使用 4K 分辨率并批量生成 2 次，两种顺序都支持 |
| `#bnn=16:9` / `#bnn=16：9` | 覆盖参考图比例为 16:9；仅在当前模型开启 GPT 或 Gemini 自适应比例且请求带图时生效 |
| `#bnnx4*2=16:9` / `#bnn=16:9x4*2` | 使用 16:9 比例、4K 分辨率并批量生成 2 次，参数顺序可调整 |
| `#手办化帮助` | 显示帮助菜单；默认命令，可通过 `help_command` 自定义 |
| `#手办化查询次数` | 查询自己的剩余次数 |

### 👑 管理命令 (仅主人)

| 命令 | 功能说明 |
| :--- | :--- |
| `#手办化添加key <key1>...` | 添加一个或多个API密钥 |
| `#手办化key列表` | 查看API密钥列表 |
| `#手办化删除key <序号\|all>` | 删除API密钥 |
| `#手办化增加次数 <QQ号> <次数>` | 为用户增加使用次数 |
| `#手办化查询次数 <QQ号>` | 查询指定用户剩余次数 |

---

## 🎨 效果展示

*以下图片均为插件实际生成效果。*

| `#手办化` | `#cos化` |
| :---: | :---: |
| <img src="./images/figurine_demo.png" width="400"> | <img src="./images/cos_demo.png" width="400"> |
| **#第一视角** | **#第三视角** |
| <img src="./images/pov1_demo.png" width="400"> | <img src="./images/pov3_demo.png" width="400"> |

## 注
本插件是基于维拉大佬写的js改的。 感谢维拉大佬的支持
