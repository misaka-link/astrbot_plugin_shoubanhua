import { useEffect, useMemo, useRef, useState } from "react";

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
const PADDING = { top: 16, right: 16, bottom: 30, left: 34 };
const MIN_WIDTH = 320;

/**
 * 每日/每小时双折线趋势（纯 SVG）。
 * 宽度取自容器实测像素，绘制尺寸与 viewBox 一致，杜绝比例拉伸错位；
 * 容器过窄时横向滚动而不是压缩图形。
 */
export default function TrendChart({ items, granularity = "day" }: TrendChartProps) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [containerWidth, setContainerWidth] = useState(0);

  useEffect(() => {
    const element = wrapRef.current;
    if (!element) return;
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        setContainerWidth(entry.contentRect.width);
      }
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const width = Math.max(MIN_WIDTH, Math.floor(containerWidth));
  const plotWidth = width - PADDING.left - PADDING.right;
  const plotHeight = HEIGHT - PADDING.top - PADDING.bottom;
  const count = items.length;

  const geometry = useMemo(() => {
    const peak = Math.max(1, ...items.map((item) => Math.max(item.outputs, item.charged_amount)));
    const xAt = (index: number) =>
      count <= 1
        ? PADDING.left + plotWidth / 2
        : PADDING.left + (plotWidth * index) / (count - 1);
    const yAt = (amount: number) => PADDING.top + plotHeight - (amount / peak) * plotHeight;
    return { peak, xAt, yAt };
  }, [items, count, plotWidth, plotHeight]);

  const labelOf = (bucket: string) =>
    granularity === "hour"
      ? `${bucket.slice(5, 10)} ${bucket.slice(11, 13)}时`
      : bucket.slice(5);
  const tooltipOf = (bucket: string) =>
    granularity === "hour" ? `${bucket.slice(0, 10)} ${bucket.slice(11, 13)}:00` : bucket;

  if (!items.length) {
    return (
      <div ref={wrapRef} style={{ width: "100%", padding: "24px 0", textAlign: "center", color: "#8c8c8c" }}>
        所选时间范围内暂无趋势统计数据
      </div>
    );
  }

  const { peak, xAt, yAt } = geometry;
  const buildPoints = (key: "outputs" | "charged_amount") =>
    items.map((item, index) => `${xAt(index)},${yAt(item[key])}`).join(" ");
  const labelEvery = Math.max(1, Math.ceil(count / 8));

  return (
    <div ref={wrapRef} style={{ width: "100%", overflowX: "auto" }}>
      <svg
        width={width}
        height={HEIGHT}
        viewBox={`0 0 ${width} ${HEIGHT}`}
        role="img"
        aria-label="成功输出与本次消耗趋势"
        style={{ display: "block" }}
      >
        {Array.from({ length: 5 }, (_, step) => {
          const y = PADDING.top + (plotHeight * step) / 4;
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
              {(index % labelEvery === 0 || index === count - 1) && (
                <text x={x} y={HEIGHT - 9} textAnchor="middle" fontSize={10} fill="#8c8c8c">
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
    </div>
  );
}
