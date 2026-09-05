import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Input,
  Space,
  Typography,
  App as AntApp,
} from "antd";
import { DeleteOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import { apiGet, apiPost } from "@/shared/api/bridge";

const { Text } = Typography;

interface PresetItem {
  command: string;
  prompt: string;
  legacy_alias?: string;
}

export default function PresetsPage({ refreshSignal }: { refreshSignal: number }) {
  const { message, modal } = AntApp.useApp();
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState("");
  const [items, setItems] = useState<PresetItem[]>([]);
  const [dirty, setDirty] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await apiGet<{ revision: string; presets: PresetItem[] }>("presets");
      setRevision(data.revision || "");
      setItems((data.presets || []).map((item) => ({ command: item.command, prompt: item.prompt })));
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

  const update = (index: number, patch: Partial<PresetItem>) => {
    setItems((prev) => prev.map((item, i) => (i === index ? { ...item, ...patch } : item)));
    setDirty(true);
  };

  const remove = (index: number) => {
    setItems((prev) => prev.filter((_, i) => i !== index));
    setDirty(true);
  };

  const add = () => {
    setItems((prev) => [...prev, { command: "", prompt: "" }]);
    setDirty(true);
  };

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      const data = await apiPost<{ revision: string; presets: PresetItem[]; message?: string }>("presets", {
        revision,
        presets: items.map((item) => ({ command: item.command.trim(), prompt: item.prompt })),
      });
      setRevision(data.revision || "");
      setItems((data.presets || []).map((item) => ({ command: item.command, prompt: item.prompt })));
      setDirty(false);
      message.success(data.message || "预设提示词已保存");
    } catch (err) {
      const text = (err as Error).message;
      setError(text);
      if (text.includes("已被其他页面或配置文件修改")) {
        modal.confirm({
          title: "预设配置已过期",
          content: "预设已被其他页面修改，是否重新加载最新内容？未保存的更改将丢失。",
          okText: "重新加载",
          cancelText: "留在当前",
          onOk: load,
        });
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {error && <Alert type="error" showIcon message={error} closable onClose={() => setError("")} />}

      <Card
        size="small"
        title={
          <Space>
            预设提示词管理
            {dirty && <Text type="warning" style={{ fontSize: 11 }}>（有未保存更改）</Text>}
          </Space>
        }
        extra={
          <Space size={8}>
            <Button size="small" icon={<ReloadOutlined />} onClick={load} />
            <Button size="small" icon={<PlusOutlined />} onClick={add}>
              添加预设
            </Button>
            <Button size="small" type="primary" loading={saving} onClick={save} disabled={!dirty}>
              保存
            </Button>
          </Space>
        }
        styles={{ body: { padding: 12 } }}
      >
        <Space direction="vertical" size={10} style={{ width: "100%" }}>
          {items.length === 0 && (
            <div style={{ textAlign: "center", color: "#8c8c8c", padding: 24 }}>
              暂无预设，点击右上角「添加预设」创建
            </div>
          )}
          {items.map((item, index) => (
            <Card
              key={index}
              size="small"
              styles={{ body: { padding: 10 } }}
              title={
                <Input
                  value={item.command}
                  status={!item.command.trim() ? "error" : undefined}
                  onChange={(event) => update(index, { command: event.target.value })}
                  placeholder="触发指令（不含 # 前缀，如 手办化）"
                  style={{ maxWidth: 320, fontWeight: 500 }}
                />
              }
              extra={
                <Button
                  size="small"
                  danger
                  type="text"
                  icon={<DeleteOutlined />}
                  onClick={() => remove(index)}
                >
                  删除
                </Button>
              }
            >
              <Input.TextArea
                value={item.prompt}
                onChange={(event) => update(index, { prompt: event.target.value })}
                placeholder="该指令发送给绘图接口的提示词内容"
                autoSize={{ minRows: 2, maxRows: 10 }}
                maxLength={20000}
              />
            </Card>
          ))}
        </Space>
      </Card>
    </Space>
  );
}
