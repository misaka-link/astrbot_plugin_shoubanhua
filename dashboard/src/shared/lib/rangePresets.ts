import dayjs, { type Dayjs } from "dayjs";

export interface DateRange {
  start: string;
  end: string;
}

/** 时间范围一键预设：今天 / 24小时 / 48小时 / 一周 / 一个月 */
export const RANGE_PRESETS: { label: string; value: [Dayjs, Dayjs] }[] = [
  { label: "今天", value: [dayjs(), dayjs()] },
  { label: "24小时", value: [dayjs().subtract(1, "day"), dayjs()] },
  { label: "48小时", value: [dayjs().subtract(2, "day"), dayjs()] },
  { label: "一周", value: [dayjs().subtract(6, "day"), dayjs()] },
  { label: "一个月", value: [dayjs().subtract(1, "month"), dayjs()] },
];

/** 把 RangePicker 选中的日期转换为后端 {start, end}（YYYY-MM-DD，end 为闭包日期） */
export function toRange(
  dates: [Dayjs | null, Dayjs | null] | null
): DateRange {
  return {
    start: dates?.[0]?.format("YYYY-MM-DD") || "",
    end: dates?.[1]?.format("YYYY-MM-DD") || "",
  };
}
