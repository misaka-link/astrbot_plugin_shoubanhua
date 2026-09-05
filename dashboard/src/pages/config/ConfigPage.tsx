import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Collapse,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
  App as AntApp,
} from "antd";
import { DeleteOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import { apiGet, apiPost } from "@/shared/api/bridge";

const { Text } = Typography;

type Json = Record<string, any>;

interface FieldOption {
  value: string;
  label: string;
}

interface ParamField {
  name: string;
  label: string;
  group: string;
  type: string;
  default: any;
  min?: number;
  max?: number;
  step?: number;
  float?: boolean;
  options?: (string | FieldOption)[];
  max_length?: number;
  hint?: string;
}

interface SettingMeta {
  key: string;
  label: string;
  group: string;
  hint: string;
  type: string;
  default: any;
  min?: number | null;
  max?: number | null;
  step?: number | null;
  options?: string[];
  item_type?: string;
  reload_required?: boolean;
  write_only?: boolean;
}

interface SensitiveState {
  generic_api_keys?: { configured: boolean; count: number };
  generic_api_url?: { configured: boolean; write_only: boolean };
  proxy_url?: { configured: boolean; write_only: boolean };
}

interface ConfigValues {
  model: string;
  model_list: string[];
  generic_api_url: string;
  gemini_model_list: string[];
  chat_completions_model_list: string[];
  images_generations_model_list: string[];
  images_edits_model_list: string[];
  extra_prefix: string[];
  command_model_list: { command: string; model: string }[];
  model_mapping_list: { model: string; mapped_model: string; priority: number }[];
  model_prompt_template_list: { model: string; prompt_template: string }[];
  model_parameter_list: Json[];
  settings: Record<string, any>;
}

interface Metadata {
  model_parameter_fields: ParamField[];
  parameter_modes: { value: string; label: string }[];
  settings: SettingMeta[];
}

const AUTO_DERIVED_FIELDS = new Set([
  "enable_gpt_parameters",
  "enable_gemini_parameters",
  "enable_grok_parameters",
  "enable_seedream_parameters",
]);

const GROUP_TO_MODE: Record<string, string> = {
  GPT: "gpt",
  Gemini: "gemini",
  Grok: "grok",
  Seedream: "seedream",
};

function optionValue(option: string | FieldOption): string {
  return typeof option === "object" ? option.value : option;
}

function optionLabel(option: string | FieldOption): string {
  return typeof option === "object" ? option.label || option.value : option;
}

function asOptions(options: (string | FieldOption)[] | undefined) {
  return (options || []).map((option) => ({ value: optionValue(option), label: optionLabel(option) }));
}

export default function ConfigPage({ refreshSignal }: { refreshSignal: number }) {
  const { message, modal } = AntApp.useApp();
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState("");
  const [config, setConfig] = useState<ConfigValues | null>(null);
  const [metadata, setMetadata] = useState<Metadata | null>(null);
  const [sensitive, setSensitive] = useState<SensitiveState | null>(null);
  const [dirty, setDirty] = useState(false);
  const [activeSection, setActiveSection] = useState("models");

  // 敏感配置输入
  const [keyInput, setKeyInput] = useState("");
  const [sensitiveUrl, setSensitiveUrl] = useState("");
  const [sensitiveProxy, setSensitiveProxy] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await apiGet<{
        revision: string;
        config: ConfigValues;
        sensitive: SensitiveState;
        metadata: Metadata;
      }>("configuration");
      setRevision(data.revision || "");
      setConfig(data.config);
      setSensitive(data.sensitive || null);
      setMetadata(data.metadata || null);
      setDirty(false);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, refreshSignal]);

  if (!config || !metadata) {
    return (
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {error && <Alert type="error" showIcon message={error} />}
        <Card size="small" loading={loading} />
      </Space>
    );
  }

  const update = (patch: Partial<ConfigValues>) => {
    setConfig((prev) => (prev ? { ...prev, ...patch } : prev));
    setDirty(true);
  };

  const updateSettings = (key: string, value: unknown) => {
    setConfig((prev) => (prev ? { ...prev, settings: { ...prev.settings, [key]: value } } : prev));
    setDirty(true);
  };

  const handleReload = () => {
    modal.confirm({
      title: "重新加载配置？",
      content: "将丢弃当前未保存的更改并从服务器重新拉取。",
      okText: "重新加载",
      cancelText: "取消",
      onOk: load,
    });
  };

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      const data = await apiPost<{ revision: string; config: ConfigValues; message?: string }>(
        "configuration",
        {
          revision,
          config: {
            model: config.model,
            model_list: config.model_list,
            generic_api_url: config.generic_api_url,
            gemini_model_list: config.gemini_model_list,
            chat_completions_model_list: config.chat_completions_model_list,
            images_generations_model_list: config.images_generations_model_list,
            images_edits_model_list: config.images_edits_model_list,
            extra_prefix: config.extra_prefix,
            command_model_list: config.command_model_list,
            model_mapping_list: config.model_mapping_list,
            model_prompt_template_list: config.model_prompt_template_list,
            model_parameter_list: config.model_parameter_list,
            settings: config.settings,
          },
        }
      );
      setRevision(data.revision || "");
      setConfig(data.config);
      setDirty(false);
      message.success(data.message || "仪表盘配置已保存");
    } catch (err) {
      const text = (err as Error).message;
      setError(text);
      if (text.includes("已被其他页面修改")) {
        modal.confirm({
          title: "配置已过期",
          content: "配置已被其他页面修改，是否重新加载最新配置？未保存的更改将丢失。",
          okText: "重新加载",
          cancelText: "留在当前",
          onOk: load,
        });
      }
    } finally {
      setSaving(false);
    }
  };

  const submitSensitive = async (
    target: "generic_api_keys" | "generic_api_url" | "proxy_url",
    action: "append" | "replace" | "clear",
    body: Record<string, unknown>
  ) => {
    setError("");
    try {
      const data = await apiPost<{ message?: string; sensitive?: SensitiveState }>(
        "configuration/sensitive",
        { revision, action, target, ...body }
      );
      if (data.sensitive) setSensitive(data.sensitive);
      message.success(data.message || "敏感配置已更新");
      const fresh = await apiGet<{ revision: string; sensitive: SensitiveState }>("configuration");
      setRevision(fresh.revision || "");
      setSensitive(fresh.sensitive || null);
    } catch (err) {
      message.error((err as Error).message);
    }
  };

  // ---------- 模型参数卡片辅助 ----------
  const paramFields = metadata.model_parameter_fields;
  const modeOptions = metadata.parameter_modes;

  const updateParam = (index: number, key: string, value: unknown) => {
    setConfig((prev) => {
      if (!prev) return prev;
      const list = prev.model_parameter_list.map((item, i) =>
        i === index ? { ...item, [key]: value } : item
      );
      return { ...prev, model_parameter_list: list };
    });
    setDirty(true);
  };

  const addParamCard = () => {
    const used = new Set(config.model_parameter_list.map((item) => String(item.model || "")));
    const nextModel = config.model_list.find((name) => !used.has(name)) || "";
    if (!nextModel) {
      message.warning("所有模型均已配置参数条目");
      return;
    }
    const defaults: Json = { model: nextModel, parameter_mode: "none" };
    for (const field of paramFields) {
      if (AUTO_DERIVED_FIELDS.has(field.name)) continue;
      defaults[field.name] = field.default;
    }
    update({ model_parameter_list: [...config.model_parameter_list, defaults] });
  };

  const removeParamCard = (index: number) => {
    update({
      model_parameter_list: config.model_parameter_list.filter((_, i) => i !== index),
    });
  };

  const renderParamField = (entry: Json, index: number, field: ParamField) => {
    const value = entry[field.name];
    const hint = field.hint ? { extra: field.hint } : {};
    if (field.type === "boolean") {
      return (
        <Form.Item key={field.name} label={field.label} {...hint}>
          <Switch checked={!!value} onChange={(checked) => updateParam(index, field.name, checked)} />
        </Form.Item>
      );
    }
    if (field.type === "select") {
      return (
        <Form.Item key={field.name} label={field.label} {...hint}>
          <Select
            value={String(value ?? field.default ?? "")}
            options={asOptions(field.options)}
            onChange={(selected) => updateParam(index, field.name, selected)}
          />
        </Form.Item>
      );
    }
    if (field.type === "number") {
      return (
        <Form.Item key={field.name} label={field.label} {...hint}>
          <InputNumber
            style={{ width: "100%" }}
            value={Number(value ?? field.default ?? 0)}
            min={field.min}
            max={field.max}
            step={field.step ?? (field.float ? 0.001 : 1)}
            onChange={(next) => updateParam(index, field.name, next ?? 0)}
          />
        </Form.Item>
      );
    }
    return (
      <Form.Item key={field.name} label={field.label} {...hint}>
        <Input
          value={String(value ?? field.default ?? "")}
          maxLength={field.max_length}
          onChange={(event) => updateParam(index, field.name, event.target.value)}
        />
      </Form.Item>
    );
  };

  const renderParamCard = (entry: Json, index: number) => {
    const mode = String(entry.parameter_mode || "none");
    const visibleFields = paramFields.filter((field) => {
      if (AUTO_DERIVED_FIELDS.has(field.name)) return false;
      if (field.group === "基础与额度") return true;
      return GROUP_TO_MODE[field.group] === mode;
    });
    return (
      <Card
        key={String(entry.model) + index}
        size="small"
        title={
          <Space wrap>
            <Select
              size="small"
              style={{ minWidth: 200 }}
              value={String(entry.model || "")}
              options={config.model_list.map((name) => ({ value: name, label: name }))}
              onChange={(selected) => updateParam(index, "model", selected)}
              showSearch
            />
            <Select
              size="small"
              style={{ minWidth: 160 }}
              value={mode}
              options={modeOptions}
              onChange={(selected) => updateParam(index, "parameter_mode", selected)}
            />
          </Space>
        }
        extra={
          <Button
            size="small"
            danger
            type="text"
            icon={<DeleteOutlined />}
            onClick={() => removeParamCard(index)}
          >
            移除
          </Button>
        }
      >
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))",
            gap: "0 16px",
          }}
        >
          {visibleFields.map((field) => renderParamField(entry, index, field))}
        </div>
      </Card>
    );
  };

  // ---------- 功能设置分组 ----------
  const settingGroups = metadata.settings.reduce<Record<string, SettingMeta[]>>((acc, setting) => {
    const group = setting.group || "其他";
    (acc[group] = acc[group] || []).push(setting);
    return acc;
  }, {});

  const renderSetting = (setting: SettingMeta) => {
    const value = config.settings?.[setting.key] ?? setting.default;
    const labelNode = (
      <Space size={6} wrap>
        <span>{setting.label}</span>
        {setting.reload_required && <Tag color="orange">需重载</Tag>}
      </Space>
    );
    if (setting.write_only) return null;
    if (setting.type === "bool") {
      return (
        <Form.Item key={setting.key} label={labelNode} extra={setting.hint}>
          <Switch checked={!!value} onChange={(checked) => updateSettings(setting.key, checked)} />
        </Form.Item>
      );
    }
    if (setting.type === "int" || setting.type === "float") {
      return (
        <Form.Item key={setting.key} label={labelNode} extra={setting.hint}>
          <InputNumber
            style={{ width: "100%" }}
            value={Number(value ?? setting.default ?? 0)}
            min={setting.min ?? undefined}
            max={setting.max ?? undefined}
            step={setting.step ?? (setting.type === "float" ? 0.001 : 1)}
            onChange={(next) => updateSettings(setting.key, next ?? 0)}
          />
        </Form.Item>
      );
    }
    if (setting.type === "text") {
      return (
        <Form.Item key={setting.key} label={labelNode} extra={setting.hint}>
          <Input.TextArea
            value={String(value ?? "")}
            autoSize={{ minRows: 3, maxRows: 12 }}
            onChange={(event) => updateSettings(setting.key, event.target.value)}
          />
        </Form.Item>
      );
    }
    if (setting.type === "list") {
      const items = Array.isArray(value) ? value.map((item: unknown) => String(item)) : [];
      const numeric = setting.item_type === "int";
      return (
        <Form.Item key={setting.key} label={labelNode} extra={setting.hint}>
          <Select
            mode="tags"
            style={{ width: "100%" }}
            value={items}
            tokenSeparators={[",", "，", " ", "\n"]}
            onChange={(next) =>
              updateSettings(
                setting.key,
                numeric ? next.map((item) => Number(item)).filter((item) => Number.isFinite(item)) : next
              )
            }
            open={false}
            suffixIcon={null}
          />
        </Form.Item>
      );
    }
    return (
      <Form.Item key={setting.key} label={labelNode} extra={setting.hint}>
        <Input
          value={String(value ?? "")}
          onChange={(event) => updateSettings(setting.key, event.target.value)}
        />
      </Form.Item>
    );
  };

  // ---------- 各分区 ----------
  const modelOptions = config.model_list.map((name) => ({ value: name, label: name }));

  const sections = [
    {
      key: "models",
      label: "模型与路由",
      content: (
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Form layout="vertical">
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))", gap: "0 16px" }}>
              <Form.Item label="默认模型" extra="model_list 中的任意模型">
                <Select
                  value={config.model}
                  options={modelOptions}
                  showSearch
                  onChange={(value) => update({ model: value })}
                />
              </Form.Item>
              <Form.Item label="模型列表" extra="可用模型 ID，回车添加；所有路由与参数配置都基于该列表">
                <Select
                  mode="tags"
                  value={config.model_list}
                  tokenSeparators={[",", "，", " ", "\n"]}
                  open={false}
                  suffixIcon={null}
                  onChange={(value) => update({ model_list: value })}
                />
              </Form.Item>
              <Form.Item label="共享 API 地址" extra="generic_api_url：所有路由的共享服务地址，填根地址或 /v1">
                <Input
                  value={config.generic_api_url}
                  placeholder="http://10.10.10.99:3000"
                  onChange={(event) => update({ generic_api_url: event.target.value })}
                />
              </Form.Item>
              <Form.Item label="Gemini 路由模型列表" extra="命中后走 Gemini 官方格式端点">
                <Select
                  mode="multiple"
                  value={config.gemini_model_list}
                  options={modelOptions}
                  showSearch
                  onChange={(value) => update({ gemini_model_list: value })}
                />
              </Form.Item>
              <Form.Item label="Chat Completions 模型列表" extra="走 /v1/chat/completions；端点列表为空或未匹配时默认走该端点">
                <Select
                  mode="multiple"
                  value={config.chat_completions_model_list}
                  options={modelOptions}
                  showSearch
                  onChange={(value) => update({ chat_completions_model_list: value })}
                />
              </Form.Item>
              <Form.Item label="Images Generations 模型列表" extra="走 /v1/images/generations，常用于文生图">
                <Select
                  mode="multiple"
                  value={config.images_generations_model_list}
                  options={modelOptions}
                  showSearch
                  onChange={(value) => update({ images_generations_model_list: value })}
                />
              </Form.Item>
              <Form.Item label="Images Edits 模型列表" extra="走 /v1/images/edits，常用于带图请求">
                <Select
                  mode="multiple"
                  value={config.images_edits_model_list}
                  options={modelOptions}
                  showSearch
                  onChange={(value) => update({ images_edits_model_list: value })}
                />
              </Form.Item>
            </div>
          </Form>
        </Space>
      ),
    },
    {
      key: "bindings",
      label: "触发词与绑定",
      content: (
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Form layout="vertical">
            <Form.Item label="自定义触发词" extra="每个触发词是一条独立命令（如 bnn）；至少保留一个，不能与插件专用指令冲突">
              <Select
                mode="tags"
                value={config.extra_prefix}
                tokenSeparators={[",", "，", " ", "\n"]}
                open={false}
                suffixIcon={null}
                onChange={(value) => update({ extra_prefix: value })}
              />
            </Form.Item>
          </Form>
          <Card
            size="small"
            title="指令模型绑定"
            extra={
              <Button
                size="small"
                icon={<PlusOutlined />}
                onClick={() =>
                  update({
                    command_model_list: [
                      ...config.command_model_list,
                      { command: config.extra_prefix[0] || "", model: config.model_list[0] || "" },
                    ],
                  })
                }
              >
                添加绑定
              </Button>
            }
          >
            <Table
              rowKey={(_, index) => String(index)}
              size="small"
              dataSource={config.command_model_list}
              pagination={false}
              columns={[
                {
                  title: "指令",
                  dataIndex: "command",
                  render: (value: string, _: unknown, index: number) => (
                    <Select
                      style={{ width: "100%" }}
                      value={value || undefined}
                      options={config.extra_prefix.map((prefix) => ({ value: prefix, label: prefix }))}
                      onChange={(selected) => {
                        const list = config.command_model_list.map((item, i) =>
                          i === index ? { ...item, command: selected } : item
                        );
                        update({ command_model_list: list });
                      }}
                    />
                  ),
                },
                {
                  title: "模型",
                  dataIndex: "model",
                  render: (value: string, _: unknown, index: number) => (
                    <Select
                      style={{ width: "100%" }}
                      showSearch
                      value={value || undefined}
                      options={modelOptions}
                      onChange={(selected) => {
                        const list = config.command_model_list.map((item, i) =>
                          i === index ? { ...item, model: selected } : item
                        );
                        update({ command_model_list: list });
                      }}
                    />
                  ),
                },
                {
                  title: "操作",
                  key: "action",
                  width: 80,
                  render: (_: unknown, __: unknown, index: number) => (
                    <Button
                      size="small"
                      danger
                      type="text"
                      icon={<DeleteOutlined />}
                      onClick={() =>
                        update({
                          command_model_list: config.command_model_list.filter((_, i) => i !== index),
                        })
                      }
                    />
                  ),
                },
              ]}
            />
          </Card>
        </Space>
      ),
    },
    {
      key: "mappings",
      label: "热备映射",
      content: (
        <Card
          size="small"
          title="模型热备映射"
          extra={
            <Button
              size="small"
              icon={<PlusOutlined />}
              onClick={() =>
                update({
                  model_mapping_list: [
                    ...config.model_mapping_list,
                    { model: config.model_list[0] || "", mapped_model: config.model_list[1] || "", priority: 0 },
                  ],
                })
              }
            >
              添加映射
            </Button>
          }
        >
          <Table
            rowKey={(_, index) => String(index)}
            size="small"
            dataSource={config.model_mapping_list}
            pagination={false}
            columns={[
              {
                title: "源模型",
                dataIndex: "model",
                width: "30%",
                render: (value: string, _: unknown, index: number) => (
                  <Select
                    style={{ width: "100%" }}
                    showSearch
                    value={value || undefined}
                    options={modelOptions}
                    onChange={(selected) => {
                      const list = config.model_mapping_list.map((item, i) =>
                        i === index ? { ...item, model: selected } : item
                      );
                      update({ model_mapping_list: list });
                    }}
                  />
                ),
              },
              {
                title: "映射模型",
                dataIndex: "mapped_model",
                width: "30%",
                render: (value: string, _: unknown, index: number) => (
                  <Select
                    style={{ width: "100%" }}
                    showSearch
                    value={value || undefined}
                    options={modelOptions}
                    onChange={(selected) => {
                      const list = config.model_mapping_list.map((item, i) =>
                        i === index ? { ...item, mapped_model: selected } : item
                      );
                      update({ model_mapping_list: list });
                    }}
                  />
                ),
              },
              {
                title: "优先权重",
                dataIndex: "priority",
                width: "20%",
                render: (value: number, _: unknown, index: number) => (
                  <InputNumber
                    style={{ width: "100%" }}
                    value={value}
                    min={-1}
                    max={10000}
                    onChange={(next) => {
                      const list = config.model_mapping_list.map((item, i) =>
                        i === index ? { ...item, priority: Number(next ?? 0) } : item
                      );
                      update({ model_mapping_list: list });
                    }}
                  />
                ),
              },
              {
                title: "操作",
                key: "action",
                width: 80,
                render: (_: unknown, __: unknown, index: number) => (
                  <Button
                    size="small"
                    danger
                    type="text"
                    icon={<DeleteOutlined />}
                    onClick={() =>
                      update({
                        model_mapping_list: config.model_mapping_list.filter((_, i) => i !== index),
                      })
                    }
                  />
                ),
              },
            ]}
          />
          <Text type="secondary" style={{ fontSize: 11 }}>
            权重越大越优先；-1 表示该映射不参与调度。只要存在有效映射，请求将直接调用首选映射模型。
          </Text>
        </Card>
      ),
    },
    {
      key: "templates",
      label: "提示词模板",
      content: (
        <Card
          size="small"
          title="按模型提示词模板"
          extra={
            <Button
              size="small"
              icon={<PlusOutlined />}
              onClick={() =>
                update({
                  model_prompt_template_list: [
                    ...config.model_prompt_template_list,
                    { model: "ALL", prompt_template: "" },
                  ],
                })
              }
            >
              添加模板
            </Button>
          }
        >
          <Space direction="vertical" size={10} style={{ width: "100%" }}>
            {config.model_prompt_template_list.map((item, index) => (
              <Card key={index} size="small" styles={{ body: { padding: 10 } }}>
                <Space direction="vertical" size={6} style={{ width: "100%" }}>
                  <Space wrap>
                    <Select
                      style={{ minWidth: 220 }}
                      showSearch
                      value={item.model}
                      options={[{ value: "ALL", label: "ALL（所有模型兜底）" }, ...modelOptions]}
                      onChange={(selected) => {
                        const list = config.model_prompt_template_list.map((entry, i) =>
                          i === index ? { ...entry, model: selected } : entry
                        );
                        update({ model_prompt_template_list: list });
                      }}
                    />
                    <Button
                      size="small"
                      danger
                      icon={<DeleteOutlined />}
                      onClick={() =>
                        update({
                          model_prompt_template_list: config.model_prompt_template_list.filter(
                            (_, i) => i !== index
                          ),
                        })
                      }
                    >
                      删除
                    </Button>
                  </Space>
                  <Input.TextArea
                    value={item.prompt_template}
                    placeholder="最终发送给绘图接口的提示词模板，可用 {prompt} {model} {mode} {image_count} {default_prompt} 变量"
                    autoSize={{ minRows: 2, maxRows: 8 }}
                    onChange={(event) => {
                      const list = config.model_prompt_template_list.map((entry, i) =>
                        i === index ? { ...entry, prompt_template: event.target.value } : entry
                      );
                      update({ model_prompt_template_list: list });
                    }}
                  />
                </Space>
              </Card>
            ))}
            {config.model_prompt_template_list.length === 0 && (
              <div style={{ textAlign: "center", color: "#8c8c8c", padding: 16 }}>
                未配置模板时直接发送原始提示词
              </div>
            )}
          </Space>
        </Card>
      ),
    },
    {
      key: "params",
      label: "模型参数",
      content: (
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Button size="small" icon={<PlusOutlined />} onClick={addParamCard}>
            添加模型参数条目
          </Button>
          {config.model_parameter_list.map((entry, index) => renderParamCard(entry, index))}
          {config.model_parameter_list.length === 0 && (
            <div style={{ textAlign: "center", color: "#8c8c8c", padding: 16 }}>
              未配置模型时默认每次扣费 1 元；添加条目可按模型精细配置
            </div>
          )}
        </Space>
      ),
    },
    {
      key: "settings",
      label: "功能设置",
      content: (
        <Collapse
          defaultActiveKey={Object.keys(settingGroups).slice(0, 1)}
          items={Object.entries(settingGroups).map(([group, settings]) => ({
            key: group,
            label: group,
            children: (
              <Form layout="vertical">
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))",
                    gap: "0 16px",
                  }}
                >
                  {settings.map(renderSetting)}
                </div>
              </Form>
            ),
          }))}
        />
      ),
    },
    {
      key: "sensitive",
      label: "敏感配置",
      content: (
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Alert
            type="info"
            showIcon
            message="敏感配置只写不读"
            description="Key 池、认证地址与代理仅支持追加/替换/清除，页面不会回显明文。"
          />
          <Card size="small" title={`共享 Key 池（已配置 ${sensitive?.generic_api_keys?.count ?? 0} 条）`}>
            <Space direction="vertical" size={8} style={{ width: "100%" }}>
              <Input.TextArea
                rows={3}
                value={keyInput}
                placeholder="每行一个 Key，仅新增的 Key 会被追加"
                onChange={(event) => setKeyInput(event.target.value)}
              />
              <Space>
                <Button
                  size="small"
                  type="primary"
                  disabled={!keyInput.trim()}
                  onClick={() => {
                    const values = keyInput
                      .split(/\r?\n/)
                      .map((line) => line.trim())
                      .filter(Boolean);
                    submitSensitive("generic_api_keys", "append", { values }).then(() => setKeyInput(""));
                  }}
                >
                  追加
                </Button>
                <Button
                  size="small"
                  danger
                  onClick={() =>
                    modal.confirm({
                      title: "清空 Key 池？",
                      content: "所有共享 Key 将被删除，插件将无法请求上游 API。",
                      okText: "清空",
                      okButtonProps: { danger: true },
                      cancelText: "取消",
                      onOk: () => submitSensitive("generic_api_keys", "clear", {}),
                    })
                  }
                >
                  清空
                </Button>
              </Space>
            </Space>
          </Card>
          <Card
            size="small"
            title={`认证共享地址（${sensitive?.generic_api_url?.configured ? "已配置" : "未配置"}${
              sensitive?.generic_api_url?.write_only ? "，含认证信息" : ""
            }）`}
          >
            <Space direction="vertical" size={8} style={{ width: "100%" }}>
              <Input
                value={sensitiveUrl}
                placeholder="http://user:pass@host:port"
                onChange={(event) => setSensitiveUrl(event.target.value)}
              />
              <Space>
                <Button
                  size="small"
                  type="primary"
                  disabled={!sensitiveUrl.trim()}
                  onClick={() => {
                    submitSensitive("generic_api_url", "replace", { value: sensitiveUrl.trim() }).then(() =>
                      setSensitiveUrl("")
                    );
                  }}
                >
                  替换
                </Button>
                <Button
                  size="small"
                  danger
                  onClick={() =>
                    modal.confirm({
                      title: "清除共享地址？",
                      onOk: () => submitSensitive("generic_api_url", "clear", {}).then(() => setSensitiveUrl("")),
                    })
                  }
                >
                  清除
                </Button>
              </Space>
            </Space>
          </Card>
          <Card
            size="small"
            title={`认证代理（${sensitive?.proxy_url?.configured ? "已配置" : "未配置"}${
              sensitive?.proxy_url?.write_only ? "，含认证信息" : ""
            }）`}
          >
            <Space direction="vertical" size={8} style={{ width: "100%" }}>
              <Input
                value={sensitiveProxy}
                placeholder="socks5://user:pass@host:port"
                onChange={(event) => setSensitiveProxy(event.target.value)}
              />
              <Space>
                <Button
                  size="small"
                  type="primary"
                  disabled={!sensitiveProxy.trim()}
                  onClick={() => {
                    submitSensitive("proxy_url", "replace", { value: sensitiveProxy.trim() }).then(() =>
                      setSensitiveProxy("")
                    );
                  }}
                >
                  替换
                </Button>
                <Button
                  size="small"
                  danger
                  onClick={() =>
                    modal.confirm({
                      title: "清除认证代理？",
                      onOk: () => submitSensitive("proxy_url", "clear", {}).then(() => setSensitiveProxy("")),
                    })
                  }
                >
                  清除
                </Button>
              </Space>
            </Space>
          </Card>
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {error && <Alert type="error" showIcon message={error} closable onClose={() => setError("")} />}

      <Card
        size="small"
        title={
          <Space>
            插件配置管理
            {dirty && <Tag color="warning">有未保存更改</Tag>}
            {!dirty && <Tag>已同步</Tag>}
          </Space>
        }
        extra={
          <Space size={8}>
            <Button size="small" icon={<ReloadOutlined />} onClick={handleReload}>
              重新加载
            </Button>
            <Button size="small" type="primary" loading={saving} onClick={save} disabled={!dirty}>
              保存配置
            </Button>
          </Space>
        }
      >
        <Tabs
          tabPosition="left"
          activeKey={activeSection}
          onChange={setActiveSection}
          items={sections.map((section) => ({
            key: section.key,
            label: section.label,
            children: section.content,
          }))}
        />
      </Card>
    </Space>
  );
}
