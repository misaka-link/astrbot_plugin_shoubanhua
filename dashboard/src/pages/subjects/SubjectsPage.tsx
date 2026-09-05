import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Avatar,
  Button,
  Card,
  DatePicker,
  Form,
  Input,
  InputNumber,
  Modal,
  Space,
  Table,
  Typography,
  App as AntApp,
} from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { apiGet, apiPost } from "@/shared/api/bridge";
import { RANGE_PRESETS, toRange } from "@/shared/lib/rangePresets";
import { fmtYuan } from "@/shared/lib/format";
import { PrivacyText } from "@/shared/ui/PrivacyText";

const { Text } = Typography;

interface SubjectRow {
  user_id?: string;
  group_id?: string;
  balance: number;
  platform: string;
  nickname?: string;
  name?: string;
  avatar_url?: string;
  outputs: number;
  charged_amount: number;
  active_users?: number;
}

interface AdjustFormValues {
  amount: number;
  note?: string;
}

const QQ_AVATAR = (id: string) => `https://q1.qlogo.cn/g?b=qq&nk=${id}&s=100`;

export default function SubjectsPage({
  subjectType,
  refreshSignal,
}: {
  subjectType: "user" | "group";
  refreshSignal: number;
}) {
  const { message } = AntApp.useApp();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [rows, setRows] = useState<SubjectRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(30);
  const [search, setSearch] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [range, setRange] = useState<{ start: string; end: string }>({ start: "", end: "" });

  // 调整余额弹窗
  const [adjustTarget, setAdjustTarget] = useState<SubjectRow | null>(null);
  const [adjustSubmitting, setAdjustSubmitting] = useState(false);
  const [form] = Form.useForm<AdjustFormValues>();

  const isUser = subjectType === "user";
  const idOf = (row: SubjectRow) => String((isUser ? row.user_id : row.group_id) || "");
  const nameOf = (row: SubjectRow) => String((isUser ? row.nickname : row.name) || "");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await apiGet<{ items: SubjectRow[]; total: number }>(
        isUser ? "usage/users" : "usage/groups",
        {
          search,
          page,
          page_size: pageSize,
          ...(range.start ? { start: range.start } : {}),
          ...(range.end ? { end: range.end } : {}),
        }
      );
      setRows(data.items || []);
      setTotal(data.total || 0);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [isUser, search, page, pageSize, range]);

  useEffect(() => {
    load();
  }, [load, refreshSignal]);

  const submitAdjust = async (values: AdjustFormValues) => {
    if (!adjustTarget) return;
    const amount = Math.round(Number(values.amount) * 1000) / 1000;
    if (!amount) {
      message.warning("变更金额不能为 0");
      return;
    }
    setAdjustSubmitting(true);
    try {
      await apiPost("usage/adjust", {
        subject_type: subjectType,
        subject_id: idOf(adjustTarget),
        amount,
        note: values.note?.trim() || "",
      });
      message.success("余额调整成功");
      setAdjustTarget(null);
      form.resetFields();
      load();
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setAdjustSubmitting(false);
    }
  };

  const columns = [
    {
      title: isUser ? "用户" : "群组信息",
      key: "identity",
      render: (_: unknown, row: SubjectRow) => {
        const id = idOf(row);
        const name = nameOf(row);
        return (
          <Space size={8}>
            {isUser ? (
              <Avatar size={28} src={row.avatar_url || QQ_AVATAR(id)}>
                {(name || id).slice(0, 1)}
              </Avatar>
            ) : null}
            <Space direction="vertical" size={0}>
              <Text strong>{name || (isUser ? `QQ ${id}` : `群 ${id}`)}</Text>
              <Text type="secondary" className="font-mono" style={{ fontSize: 11 }}>
                <PrivacyText value={id} mask={isUser ? "userId" : "groupId"} />
              </Text>
            </Space>
          </Space>
        );
      },
    },
    {
      title: "成功输出",
      dataIndex: "outputs",
      width: 100,
      align: "right" as const,
      render: (value: number) => <Text className="font-mono">{value}</Text>,
    },
    {
      title: "本次消耗(元)",
      dataIndex: "charged_amount",
      width: 120,
      align: "right" as const,
      render: (value: number) => <Text className="font-mono">{fmtYuan(value)}</Text>,
    },
    ...(isUser
      ? []
      : [
          {
            title: "活跃用户数",
            dataIndex: "active_users",
            width: 100,
            align: "right" as const,
            render: (value: number) => <Text className="font-mono">{value}</Text>,
          },
        ]),
    {
      title: isUser ? "个人余额(元)" : "群公用余额(元)",
      dataIndex: "balance",
      width: 130,
      align: "right" as const,
      render: (value: number) => (
        <PrivacyText mask="balance" value={fmtYuan(value)} style={{ fontVariantNumeric: "tabular-nums" }} />
      ),
    },
    {
      title: "操作",
      key: "action",
      width: 110,
      align: "center" as const,
      render: (_: unknown, row: SubjectRow) => (
        <Button
          size="small"
          type="primary"
          ghost
          onClick={() => {
            form.resetFields();
            setAdjustTarget(row);
          }}
        >
          调整余额
        </Button>
      ),
    },
  ];

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {error && <Alert type="error" showIcon message={error} closable onClose={() => setError("")} />}

      <Card
        size="small"
        title={isUser ? "用户用量与余额" : "群组用量与余额"}
        styles={{ body: { padding: 0 } }}
        extra={
          <Space size={8}>
            <Input.Search
              size="small"
              style={{ width: 220 }}
              placeholder={isUser ? "输入 QQ 号或昵称搜索" : "输入群号或群名称搜索"}
              value={searchInput}
              onChange={(event) => setSearchInput(event.target.value)}
              onSearch={(value) => {
                setPage(1);
                setSearch(value.trim());
              }}
              allowClear
            />
            <DatePicker.RangePicker
              size="small"
              presets={RANGE_PRESETS}
              onChange={(dates) => {
                setPage(1);
                setRange(toRange(dates));
              }}
              allowEmpty={[true, true]}
            />
            <Button size="small" icon={<ReloadOutlined />} onClick={load} />
          </Space>
        }
      >
        <Table<SubjectRow>
          rowKey={(row) => idOf(row)}
          size="small"
          loading={loading}
          dataSource={rows}
          columns={columns}
          scroll={{ x: 760 }}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: false,
            showTotal: (value) => `共 ${value} 条`,
            onChange: (nextPage, nextSize) => {
              setPage(nextPage);
              setPageSize(nextSize);
            },
          }}
        />
      </Card>

      <Modal
        open={!!adjustTarget}
        title={`调整${isUser ? "用户" : "群组"}余额`}
        onCancel={() => setAdjustTarget(null)}
        confirmLoading={adjustSubmitting}
        onOk={() => form.submit()}
        okText="提交"
        cancelText="取消"
        destroyOnClose
      >
        <div style={{ marginBottom: 12 }}>
          <Text type="secondary">调整目标：</Text>
          <Text strong className="font-mono">
            {adjustTarget ? `${nameOf(adjustTarget) || ""} (${idOf(adjustTarget)})` : "-"}
          </Text>
        </div>
        <Form<AdjustFormValues> form={form} layout="vertical" onFinish={submitAdjust}>
          <Form.Item
            name="amount"
            label="变更金额(元)"
            rules={[{ required: true, message: "请输入变更金额" }]}
            extra="正数为增加，负数为扣减，精确到 0.001"
          >
            <InputNumber
              style={{ width: "100%" }}
              step={0.001}
              min={-100000}
              max={100000}
              placeholder="如 0.5 或 -0.2"
            />
          </Form.Item>
          <Form.Item name="note" label="审计备注" extra="可选，记录在账本明细中">
            <Input.TextArea rows={3} maxLength={500} placeholder="填写本次余额调整原因，最多 500 字..." />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  );
}
