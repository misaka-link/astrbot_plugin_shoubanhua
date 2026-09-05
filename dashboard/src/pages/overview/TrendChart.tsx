import { useMemo } from "react";

export interface TrendPoint {
  date: string;
  outputs: number;
  charged_amount: number;
}

interface TrendChartProps {
  items: TrendPoint[];
  granularity?: "day" | "hour";
}

const HEIGHT = 180;
const PADDING = { top: 16, right: 16, bottom: 30, left: 30 };

/** 每日成功输出与本次消耗双折线（纯 SVG，跟随参考项目的轻量趋势看板） */
export default function TrendChart({ items, granularity = "day" }: TrendChartProps) {
  const geometry = useMemo(() => {
    const perPoint = granularity === "hour" ? 34 : 56;
    const width = Math.max(360, items.length * perPoint + PADDING.left + PADDING.right);
    const plotWidth = width - PADDING.left - PADDING.right;
    const plotHeight = HEIGHT - PADDING.top - PADDING.bottom;
    const peak = Math.max(
      1,
      ...items.map((item) => Math.max(item.outputs, item.charged_amount))
    );
    const xAt = (index: number) =>
      items.length === 1
        ? PADDING.left + plotWidth / 2
        : PADDING.left + (plotWidth * index) / (items.length - 1);
    const yAt = (amount: number) => PADDING.top + plotHeight - (amount / peak) * plotHeight;
    return { width, peak, xAt, yAt };
  }, [items]);

  if (!items.length) {
    return (
      <div style={{ padding: "24px 0", textAlign: "center", color: "#8c8c8c" }}>
        所选时间范围内暂无趋势统计数据
      </div>
    );
  }

  const { width, peak, xAt, yAt } = geometry;
  const buildPoints = (key: "outputs" | "charged_amount") =>
    items.map((item, index) => `${xAt(index)},${yAt(item[key])}`).join(" ");

  const labelOf = (bucket: string) =>
    granularity === "hour"
      ? `${bucket.slice(5, 10)} ${bucket.slice(11, 13)}时`
      : bucket.slice(5);
  const tooltipOf = (bucket: string) =>
    granularity === "hour" ? `${bucket.slice(0, 10)} ${bucket.slice(11, 13)}:00` : bucket;

  return (
    <svg
      className="trend-chart"
      viewBox={`0 0 ${width} ${HEIGHT}`}
      role="img"
      aria-label="每日成功输出与本次消耗趋势"
      style={{ width: "100%", minWidth: width, display: "block" }}
    >
      {Array.from({ length: 5 }, (_, step) => {
        const y = PADDING.top + ((HEIGHT - PADDING.top - PADDING.bottom) * step) / 4;
        return (
          <line
            key={step}
            x1={PADDING.left}
            x2={width - PADDING.right}
            y1={y}
            y2={y}
            stroke="rgba(148, 163, 184, 0.25)"
            strokeDasharray="3 3"
          />
        );
      })}

      <polyline points={buildPoints("outputs")} fill="none" stroke="#1677ff" strokeWidth="1.8" />
      <polyline
        points={buildPoints("charged_amount")}
        fill="none"
        stroke="#fa8c16"
        strokeWidth="1.8"
      />

      {items.map((item, index) => {
        const x = xAt(index);
        const labelEvery = Math.max(1, Math.ceil(items.length / 8));
        return (
          <g key={item.date}>
            <title>
              {`${tooltipOf(item.date)}\n成功输出: ${item.outputs}\n本次消耗: ${Number(
                (item.charged_amount / 1000).toFixed(3)
              )} 元`}
            </title>
            <circle cx={x} cy={yAt(item.outputs)} r={3.5} fill="#1677ff" />
            <rect
              x={x - 3}
              y={yAt(item.charged_amount) - 3}
              width={6}
              height={6}
              rx={1}
              fill="#fa8c16"
            />
            {(index % labelEvery === 0 || index === items.length - 1) && (
              <text
                x={x}
                y={HEIGHT - 9}
                textAnchor="middle"
                fontSize={10}
                fill="#8c8c8c"
              >
                {labelOf(item.date)}
              </text>
            )}
          </g>
        );
      })}
      <text x={PADDING.left} y={12} fontSize={10} fill="#8c8c8c">
        峰值 {peak}
      </text>
    </svg>
  );
}
