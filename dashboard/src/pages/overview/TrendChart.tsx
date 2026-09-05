import { useMemo } from "react";
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import { GridComponent, TooltipComponent, LegendComponent } from "echarts/components";
import { SVGRenderer } from "echarts/renderers";
import EChartsReactCore from "echarts-for-react/lib/core";
import { useTheme } from "@/shared/lib/theme";

export interface TrendPoint {
  date: string;
  outputs: number;
  charged_amount: number;
}

interface TrendChartProps {
  items: TrendPoint[];
  granularity?: "day" | "hour";
}

echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, SVGRenderer]);

/**
 * 每日/每小时成功输出与本次消耗双轴趋势（ECharts，样式对齐参考项目的可观测趋势看板）。
 * 左轴：成功输出（张）；右轴：本次消耗（元）。短范围自动切小时粒度。
 */
export default function TrendChart({ items, granularity = "day" }: TrendChartProps) {
  const { isDark } = useTheme();

  const axisLabelColor = isDark ? "#8b949e" : "#64748b";
  const axisLineColor = isDark ? "#30363d" : "#e2e8f0";
  const splitLineColor = isDark ? "#21262d" : "#f1f5f9";
  const tooltipBg = isDark ? "rgba(22, 27, 34, 0.96)" : "rgba(255, 255, 255, 0.98)";
  const tooltipBorder = isDark ? "#30363d" : "#e2e8f0";
  const tooltipText = isDark ? "#c9d1d9" : "#1e293b";
  const titleColor = isDark ? "#ffffff" : "#0f172a";

  const labels = useMemo(
    () =>
      items.map((item) =>
        granularity === "hour"
          ? `${item.date.slice(5, 10)} ${item.date.slice(11, 13)}时`
          : item.date.slice(5)
      ),
    [items, granularity]
  );

  const option = useMemo(() => {
    const outputs = items.map((item) => item.outputs);
    const yuan = items.map((item) => Number((item.charged_amount / 1000).toFixed(3)));
    const fullTitle = (index: number) => {
      const bucket = items[index]?.date || "";
      return granularity === "hour"
        ? `${bucket.slice(0, 10)} ${bucket.slice(11, 13)}:00`
        : bucket;
    };
    const symbolCount = items.length;

    return {
      backgroundColor: "transparent",
      tooltip: {
        trigger: "axis" as const,
        backgroundColor: tooltipBg,
        borderColor: tooltipBorder,
        borderWidth: 1,
        padding: [8, 12],
        textStyle: { color: tooltipText, fontSize: 12 },
        formatter: (params: Array<{ dataIndex: number }>) => {
          if (!params || params.length === 0) return "";
          const index = params[0].dataIndex;
          const item = items[index];
          if (!item) return "";
          const yuanText = Number((item.charged_amount / 1000).toFixed(3));
          return `
            <div style="font-weight:600;font-family:monospace;font-size:12px;margin-bottom:6px;color:${titleColor};">
              ${fullTitle(index)}
            </div>
            <div style="font-size:12px;color:#2563eb;margin-bottom:3px;">
              成功输出: <b>${item.outputs}</b> 张
            </div>
            <div style="font-size:12px;color:#fa8c16;">
              本次消耗: <b>${yuanText}</b> 元
            </div>
          `;
        },
      },
      legend: {
        top: 0,
        right: 0,
        itemWidth: 14,
        itemHeight: 8,
        textStyle: { color: axisLabelColor, fontSize: 11 },
      },
      grid: { top: 32, right: 56, bottom: 24, left: 44 },
      xAxis: {
        type: "category" as const,
        boundaryGap: false,
        data: labels,
        axisLine: { lineStyle: { color: axisLineColor } },
        axisTick: { show: false },
        axisLabel: {
          color: axisLabelColor,
          fontSize: 11,
          fontFamily: "'JetBrains Mono', Consolas, -apple-system, sans-serif",
        },
      },
      yAxis: [
        {
          type: "value" as const,
          minInterval: 1,
          splitLine: { lineStyle: { color: splitLineColor, type: "dashed" as const } },
          axisLabel: {
            color: axisLabelColor,
            fontSize: 11,
            fontFamily: "'JetBrains Mono', Consolas, -apple-system, sans-serif",
          },
        },
        {
          type: "value" as const,
          splitLine: { show: false },
          axisLabel: {
            color: axisLabelColor,
            fontSize: 11,
            fontFamily: "'JetBrains Mono', Consolas, -apple-system, sans-serif",
            formatter: (value: number) => Number(value.toFixed(3)),
          },
        },
      ],
      series: [
        {
          name: "成功输出(张)",
          type: "line" as const,
          smooth: true,
          showSymbol: symbolCount <= 40,
          symbolSize: 6,
          data: outputs,
          lineStyle: { width: 2, color: "#1677ff" },
          itemStyle: { color: "#1677ff" },
          areaStyle: {
            color: {
              type: "linear" as const,
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                { offset: 0, color: "rgba(22, 119, 255, 0.35)" },
                { offset: 0.8, color: "rgba(22, 119, 255, 0.06)" },
                { offset: 1, color: "rgba(22, 119, 255, 0)" },
              ],
            },
          },
        },
        {
          name: "本次消耗(元)",
          type: "line" as const,
          smooth: true,
          showSymbol: false,
          symbolSize: 6,
          yAxisIndex: 1,
          data: yuan,
          lineStyle: { width: 2, color: "#fa8c16" },
          itemStyle: { color: "#fa8c16" },
          areaStyle: {
            color: {
              type: "linear" as const,
              x: 0,
              y: 0,
              x2: 0,
              y2: 1,
              colorStops: [
                { offset: 0, color: "rgba(250, 140, 22, 0.25)" },
                { offset: 0.8, color: "rgba(250, 140, 22, 0.05)" },
                { offset: 1, color: "rgba(250, 140, 22, 0)" },
              ],
            },
          },
        },
      ],
    };
  }, [
    items,
    granularity,
    labels,
    tooltipBg,
    tooltipBorder,
    tooltipText,
    titleColor,
    axisLabelColor,
    axisLineColor,
    splitLineColor,
  ]);

  if (!items.length) {
    return (
      <div
        style={{
          height: 220,
          width: "100%",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: "#8c8c8c",
        }}
      >
        所选时间范围内暂无趋势统计数据
      </div>
    );
  }

  return (
    <EChartsReactCore
      echarts={echarts}
      option={option}
      notMerge
      style={{ height: 240, width: "100%" }}
      opts={{ renderer: "svg" }}
    />
  );
}
