/** 金额与时间格式化工具。后端金额一律以「厘」(1 厘 = 0.001 元) 整数存储。 */

/** 厘 → 元文本（去尾零）：50 → "0.05"，1234 → "1.234"，0 → "0" */
export function fmtYuan(milli: number | null | undefined): string {
  const value = (Number(milli) || 0) / 1000;
  return String(Number(value.toFixed(3)));
}

/** 厘 → 金额文本（带单位）："0.05 元" */
export function fmtYuanText(milli: number | null | undefined): string {
  return `${fmtYuan(milli)} 元`;
}

/** 元输入 → 厘整数（后端存储单位），非法输入返回 0 */
export function yuanToMilli(value: number | string | null | undefined): number {
  const num = typeof value === "number" ? value : parseFloat(String(value ?? ""));
  if (!Number.isFinite(num)) return 0;
  return Math.round(num * 1000);
}

/** ISO 时间 → "MM-DD HH:mm:ss" */
export function formatDateTime(iso: string | null | undefined): string {
  const text = String(iso || "");
  if (!text) return "-";
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2}:\d{2})/);
  if (!match) return text;
  return `${match[2]}-${match[3]} ${match[4]}`;
}

/** 隐藏手机号/邮箱等无关紧要的中间细节（备用） */
export function truncateMiddle(text: string, max = 40): string {
  if (!text || text.length <= max) return text;
  return `${text.slice(0, Math.ceil(max / 2))}…${text.slice(-Math.floor(max / 2))}`;
}
