import { createContext, useCallback, useContext, useState, type CSSProperties, type ReactNode } from "react";
import { Typography } from "antd";

/**
 * 隐私遮罩：双击切换某个遮罩键的显示/隐藏状态。
 * 与旧版仪表盘的 privacyMasks 行为保持一致。
 */

type MaskMap = Record<string, boolean>;

interface PrivacyContextValue {
  masks: MaskMap;
  toggle: (mask: string) => void;
}

const PrivacyContext = createContext<PrivacyContextValue>({
  masks: {},
  toggle: () => undefined,
});

export function PrivacyProvider({ children }: { children: ReactNode }) {
  const [masks, setMasks] = useState<MaskMap>({});
  const toggle = useCallback((mask: string) => {
    setMasks((prev) => ({ ...prev, [mask]: !prev[mask] }));
  }, []);
  return <PrivacyContext.Provider value={{ masks, toggle }}>{children}</PrivacyContext.Provider>;
}

export function usePrivacy(): PrivacyContextValue {
  return useContext(PrivacyContext);
}

interface PrivacyTextProps {
  value: string | number | null | undefined;
  mask: string;
  label?: string;
  style?: CSSProperties;
}

/** 受隐私遮罩保护的文本：默认明文，双击切换为 •••• */
export function PrivacyText({ value, mask, label, style }: PrivacyTextProps) {
  const { masks, toggle } = usePrivacy();
  const masked = !!masks[mask];
  const display = masked ? "••••••" : String(value ?? "-");
  return (
    <Typography.Text
      style={{ cursor: "pointer", userSelect: "none", ...style }}
      title={masked ? `双击显示${label || ""}` : `双击隐藏${label || ""}`}
      onDoubleClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        toggle(mask);
      }}
    >
      {display}
    </Typography.Text>
  );
}
