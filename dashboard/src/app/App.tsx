import { useState } from "react";
import { ConfigProvider, Tabs, theme, Button, Tooltip, App as AntApp } from "antd";
import zhCN from "antd/locale/zh_CN";
import {
  DashboardOutlined,
  TeamOutlined,
  FolderOpenOutlined,
  SettingOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import { useTheme } from "@/shared/lib/theme";
import { PrivacyProvider } from "@/shared/ui/PrivacyText";
import OverviewPage from "@/pages/overview/OverviewPage";
import SubjectsPage from "@/pages/subjects/SubjectsPage";
import PresetsPage from "@/pages/presets/PresetsPage";
import ConfigPage from "@/pages/config/ConfigPage";

type ViewKey = "overview" | "users" | "groups" | "presets" | "config";

function DashboardShell() {
  const { isDark } = useTheme();
  const [activeTab, setActiveTab] = useState<ViewKey>("overview");
  const [refreshSignal, setRefreshSignal] = useState(0);
  const { message } = AntApp.useApp();

  const handleRefresh = () => {
    setRefreshSignal((value) => value + 1);
  };

  const tabItems = [
    {
      key: "overview",
      label: (
        <span>
          <DashboardOutlined /> 用量概览
        </span>
      ),
      children: <OverviewPage refreshSignal={refreshSignal} />,
    },
    {
      key: "users",
      label: (
        <span>
          <TeamOutlined /> 用户用量
        </span>
      ),
      children: <SubjectsPage subjectType="user" refreshSignal={refreshSignal} />,
    },
    {
      key: "groups",
      label: (
        <span>
          <TeamOutlined /> 群组用量
        </span>
      ),
      children: <SubjectsPage subjectType="group" refreshSignal={refreshSignal} />,
    },
    {
      key: "presets",
      label: (
        <span>
          <FolderOpenOutlined /> 预设提示词
        </span>
      ),
      children: <PresetsPage refreshSignal={refreshSignal} />,
    },
    {
      key: "config",
      label: (
        <span>
          <SettingOutlined /> 配置管理
        </span>
      ),
      children: <ConfigPage refreshSignal={refreshSignal} />,
    },
  ];

  return (
    <PrivacyProvider>
      <div
        style={{
          minHeight: "100vh",
          background: isDark ? "#000000" : "#f5f5f5",
          padding: 12,
          color: isDark ? "#ffffff" : "#000000",
        }}
      >
        {/* 顶部页头 */}
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            padding: "10px 14px",
            background: isDark ? "#141414" : "#ffffff",
            border: `1px solid ${isDark ? "#303030" : "#f0f0f0"}`,
            borderRadius: 4,
            marginBottom: 12,
            flexWrap: "wrap",
            gap: 8,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <div
              style={{
                width: 28,
                height: 28,
                borderRadius: 6,
                background: "#1677ff",
                color: "#fff",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 15,
                fontWeight: 700,
              }}
            >
              手
            </div>
            <div>
              <div style={{ fontSize: 15, fontWeight: 600, lineHeight: "20px" }}>
                手办化Pro 控制台
              </div>
              <div style={{ fontSize: 11, color: "#8c8c8c", marginTop: 2 }}>
                用量、余额、预设与插件配置管理
              </div>
            </div>
          </div>
          <Tooltip title="重新加载当前页数据">
            <Button
              size="small"
              type="primary"
              icon={<ReloadOutlined />}
              onClick={() => {
                handleRefresh();
                message.success("已触发刷新");
              }}
            >
              刷新
            </Button>
          </Tooltip>
        </div>

        {/* 卡片式 Tab 导航与页面路由 */}
        <Tabs
          activeKey={activeTab}
          onChange={(key) => setActiveTab(key as ViewKey)}
          items={tabItems}
          type="card"
          size="small"
          destroyInactiveTabPane={false}
        />
      </div>
    </PrivacyProvider>
  );
}

export default function App() {
  const { isDark } = useTheme();
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm,
        token: {
          colorPrimary: "#1677ff",
          borderRadius: 4,
          fontFamily:
            "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif",
        },
      }}
    >
      <AntApp>
        <DashboardShell />
      </AntApp>
    </ConfigProvider>
  );
}
