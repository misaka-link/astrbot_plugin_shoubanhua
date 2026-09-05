import { useCallback, useEffect, useState } from "react";
import {
  Card,
  Col,
  Row,
  Statistic,
  Table,
  Tag,
  DatePicker,
  Select,
  Space,
  Typography,
  Alert,
} from "antd";
import {
  ThunderboltOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  ExperimentOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { apiGet } from "@/shared/api/bridge";
import { fmtYuan, formatDateTime } from "@/shared/lib/format";
import { PrivacyText } from "@/shared/ui/PrivacyText";
import TrendChart, { type TrendPoint } from "./TrendChart";

interface OverviewSummary {
  successful_outputs: number;
  failed_charged_amount: number;
  charged_amount: number;
  unbilled_llm_outputs: number;
}

interface ModelRow {
  actual_model: string;
  api_route: string;
  endpoint_type: string;
  outputs: number;
  charged_amount: number;
  attempts: number;
}

interface EventRow {
  id: number;
  occurred_at: string;
  event_kind: string;
  user_id: string;
  group_id: string;
  user_nickname?: string;
  group_name?: string;
  logical_model: string;
  actual_model: string;
  api_route: string;
  endpoint_type: string;
  outcome: string;
  http_status: number;
  output_count: number;
  charged_amount: number;
  balance_delta: number | null;
  resulting_balance: number | null;
  note?: string;
}

const ENDPOINT_LABELS: Record<string, string> = {
  chat_completions: "chat",
  images_generations: "generations",
  images_edits: "edits",
  gemini_generate_content: "gemini",
};

const KIND_LABELS: Record<string, string> = {
  generation: "生成",
  adjustment: "额度调整",
  opening_balance: "建账",
};

function outcomeTag(outcome: string) {
  switch (outcome) {
    case "success":
      return <Tag color="success">成功</Tag>;
    case "failed":
      return <Tag color="error">失败</Tag>;
    case "applied":
      return <Tag color="cyan">已应用</Tag>;
    case "skipped":
      return <Tag>免扣费</Tag>;
    case "imported":
      return <Tag>旧版导入</Tag>;
    default:
      return <Tag>{outcome || "-"}</Tag>;
  }
}

const { Text } = Typography;

export default function OverviewPage({ refreshSignal }: { refreshSignal: number }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [summary, setSummary] = useState<OverviewSummary | null>(null);
  const [trend, setTrend] = useState<TrendPoint[]>([]);
  const [models, setModels] = useState<ModelRow[]>([]);
  const [range, setRange] = useState<{ start: string; end: string }>({ start: "", end: "" });

  // 事件列表独立状态
  const [events, setEvents] = useState<EventRow[]>([]);
  const [eventsTotal, setEventsTotal] = useState(0);
  const [eventPage, setEventPage] = useState(1);
  const [eventPageSize, setEventPageSize] = useState(15);
  const [eventDay, setEventDay] = useState<string>("");
  const [eventOutcome, setEventOutcome] = useState<string>("");

  const rangeParams = useCallback(
    (extra: Record<string, unknown> = {}) => ({
      ...(range.start ? { start: range.start } : {}),
      ...(range.end ? { end: range.end } : {}),
      ...extra,
    }),
    [range]
  );

  const loadOverview = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await apiGet<{
        summary: OverviewSummary;
        trend: TrendPoint[];
        models: ModelRow[];
      }>("usage/overview", rangeParams());
      setSummary(data.summary || null);
      setTrend(data.trend || []);
      setModels(data.models || []);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [rangeParams]);

  const loadEvents = useCallback(async () => {
    try {
      const base = eventDay
        ? { start: eventDay, end: eventDay }
        : rangeParams();
      const data = await apiGet<{ items: EventRow[]; total: number }>("usage/events", {
        ...base,
        outcome: eventOutcome,
        page: eventPage,
        page_size: eventPageSize,
      });
      setEvents(data.items || []);
      setEventsTotal(data.total || 0);
    } catch (err) {
      setError((err as Error).message);
    }
  }, [eventDay, eventOutcome, eventPage, eventPageSize, rangeParams]);

  useEffect(() => {
    loadOverview();
  }, [loadOverview, refreshSignal]);

  useEffect(() => {
    loadEvents();
  }, [loadEvents, refreshSignal]);

  const columns = [
    {
      title: "时间",
      dataIndex: "occurred_at",
      width: 130,
      render: (value: string) => <Text className="font-mono">{formatDateTime(value)}</Text>,
    },
    {
      title: "用户",
      dataIndex: "user_id",
      width: 150,
      render: (value: string, row: EventRow) =>
        value ? (
          <PrivacyText value={row.user_nickname ? `${row.user_nickname} (${value})` : value} mask="userId" />
        ) : (
          "-"
        ),
    },
    {
      title: "群组",
      dataIndex: "group_id",
      width: 150,
      render: (value: string, row: EventRow) =>
        value ? (
          <PrivacyText value={row.group_name ? `${row.group_name} (${value})` : value} mask="groupId" />
        ) : (
          "-"
        ),
    },
    {
      title: "事件",
      dataIndex: "event_kind",
      width: 90,
      render: (value: string) => KIND_LABELS[value] || value || "-",
    },
    {
      title: "模型 (实际)",
      dataIndex: "actual_model",
      width: 180,
      render: (value: string) => <Text className="font-mono">{value || "-"}</Text>,
    },
    {
      title: "路由",
      key: "route",
      width: 130,
      render: (_: unknown, row: EventRow) =>
        `${row.api_route || "-"} · ${ENDPOINT_LABELS[row.endpoint_type] || row.endpoint_type || "-"}`,
    },
    {
      title: "结果",
      dataIndex: "outcome",
      width: 110,
      render: (value: string, row: EventRow) => (
        <Space size={4}>
          {outcomeTag(value)}
          {row.http_status > 0 && <Text className="font-mono" type="secondary">{row.http_status}</Text>}
        </Space>
      ),
    },
    {
      title: "产出",
      dataIndex: "output_count",
      width: 70,
      align: "right" as const,
      render: (value: number) => <Text className="font-mono">{value}</Text>,
    },
    {
      title: "扣费(元)",
      dataIndex: "charged_amount",
      width: 100,
      align: "right" as const,
      render: (value: number) => <Text className="font-mono">{fmtYuan(value)}</Text>,
    },
    {
      title: "余额变动(元)",
      dataIndex: "balance_delta",
      width: 140,
      align: "right" as const,
      render: (value: number | null, row: EventRow) => {
        if (value === null || value === undefined) return "-";
        const sign = value > 0 ? "+" : "";
        return (
          <Space direction="vertical" size={0}>
            <PrivacyText
              mask="balance"
              value={`${sign}${fmtYuan(value)}`}
              style={{ fontVariantNumeric: "tabular-nums" }}
            />
            {row.resulting_balance !== null && row.resulting_balance !== undefined && (
              <Text type="secondary" className="font-mono" style={{ fontSize: 11 }}>
                余: {fmtYuan(row.resulting_balance)}
              </Text>
            )}
          </Space>
        );
      },
    },
  ];

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {error && <Alert type="error" showIcon message={error} closable onClose={() => setError("")} />}

      {/* 指标卡矩阵 */}
      <Row gutter={[10, 10]}>
        {[
          {
            title: "成功输出",
            value: summary?.successful_outputs ?? 0,
            icon: <CheckCircleOutlined style={{ color: "#2563eb" }} />,
            sub: "生成完成数",
          },
          {
            title: "本次消耗(元)",
            value: fmtYuan(summary?.charged_amount ?? 0),
            icon: <ThunderboltOutlined style={{ color: "#2563eb" }} />,
            sub: "所选范围累计消耗金额",
          },
          {
            title: "失败扣费(元)",
            value: fmtYuan(summary?.failed_charged_amount ?? 0),
            icon: <CloseCircleOutlined style={{ color: "#2563eb" }} />,
            sub: "失败且未返还",
          },
          {
            title: "LLM 工具免计费",
            value: summary?.unbilled_llm_outputs ?? 0,
            icon: <ExperimentOutlined style={{ color: "#2563eb" }} />,
            sub: "插件免计费调用",
          },
        ].map((metric) => (
          <Col xs={12} sm={12} md={6} key={metric.title}>
            <Card size="small" styles={{ body: { padding: "10px 14px", minHeight: 90 } }}>
              <div style={{ fontSize: 12, color: "#8c8c8c", marginBottom: 2 }}>
                {metric.icon} {metric.title}
              </div>
              <Statistic
                value={metric.value}
                valueStyle={{ fontSize: 18, fontWeight: 600, fontVariantNumeric: "tabular-nums", lineHeight: "24px" }}
              />
              <div style={{ marginTop: 4, fontSize: 11, color: "#8c8c8c", minHeight: 16 }}>{metric.sub}</div>
            </Card>
          </Col>
        ))}
      </Row>

      {/* 趋势看板 */}
      <Card
        size="small"
        title="每日趋势"
        extra={
          <Space size={12}>
            <span style={{ fontSize: 11, color: "#8c8c8c" }}>
              <span style={{ color: "#1677ff" }}>■</span> 成功输出
              <span style={{ color: "#fa8c16", marginLeft: 8 }}>■</span> 本次消耗(厘)
            </span>
            <DatePicker.RangePicker
              size="small"
              onChange={(dates) => {
                setRange({
                  start: dates?.[0]?.format("YYYY-MM-DD") || "",
                  end: dates?.[1]?.format("YYYY-MM-DD") || "",
                });
              }}
              allowEmpty={[true, true]}
            />
          </Space>
        }
      >
        <TrendChart items={trend} />
      </Card>

      {/* 模型与路由明细 */}
      <Card size="small" title="模型与路由明细" styles={{ body: { padding: 0 } }}>
        <Table<ModelRow>
          rowKey={(row) => `${row.actual_model}-${row.api_route}-${row.endpoint_type}`}
          size="small"
          loading={loading}
          dataSource={models}
          pagination={false}
          scroll={{ x: 700 }}
          columns={[
            { title: "实际模型", dataIndex: "actual_model", render: (v: string) => <Text className="font-mono">{v}</Text> },
            {
              title: "路由通道",
              key: "route",
              render: (_: unknown, row: ModelRow) =>
                `${row.api_route} · ${ENDPOINT_LABELS[row.endpoint_type] || row.endpoint_type}`,
            },
            {
              title: "成功输出",
              dataIndex: "outputs",
              align: "right" as const,
              render: (v: number) => <Text className="font-mono">{v}</Text>,
            },
            {
              title: "本次消耗(元)",
              dataIndex: "charged_amount",
              align: "right" as const,
              render: (v: number) => <Text className="font-mono">{fmtYuan(v)}</Text>,
            },
            {
              title: "尝试次数",
              dataIndex: "attempts",
              align: "right" as const,
              render: (v: number) => <Text className="font-mono">{v}</Text>,
            },
          ]}
        />
      </Card>

      {/* 最近活动记录 */}
      <Card
        size="small"
        title="最近活动记录"
        styles={{ body: { padding: 0 } }}
        extra={
          <Space size={8}>
            <DatePicker
              size="small"
              placeholder="按天筛选"
              onChange={(date) => {
                setEventPage(1);
                setEventDay(date ? date.format("YYYY-MM-DD") : "");
              }}
              allowClear
            />
            <Select
              size="small"
              style={{ width: 110 }}
              value={eventOutcome}
              onChange={(value) => {
                setEventPage(1);
                setEventOutcome(value);
              }}
              options={[
                { value: "", label: "全部结果" },
                { value: "success", label: "成功" },
                { value: "failed", label: "失败" },
              ]}
            />
            <Select
              size="small"
              style={{ width: 100 }}
              value={eventPageSize}
              onChange={(value) => {
                setEventPage(1);
                setEventPageSize(value);
              }}
              options={[15, 30, 50, 100].map((size) => ({ value: size, label: `${size} 条/页` }))}
            />
          </Space>
        }
      >
        <Table<EventRow>
          rowKey="id"
          size="small"
          dataSource={events}
          loading={loading}
          scroll={{ x: 1100 }}
          pagination={{
            current: eventPage,
            pageSize: eventPageSize,
            total: eventsTotal,
            showSizeChanger: false,
            showTotal: (total) => `共 ${total} 条`,
            onChange: (page, size) => {
              setEventPage(page);
              setEventPageSize(size);
            },
          }}
          columns={columns}
        />
      </Card>

      <div style={{ textAlign: "right" }}>
        <Text type="secondary" style={{ fontSize: 11 }}>
          <ReloadOutlined /> 数据随「刷新」按钮与筛选条件自动重新加载
        </Text>
      </div>
    </Space>
  );
}
