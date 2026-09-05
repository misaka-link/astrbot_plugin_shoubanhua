import { useEffect, useState } from "react";
import { fetchContext, onHostContext, type AstrBotContext } from "@/shared/api/bridge";

const THEME_CACHE_KEY = "astrbot_plugin_theme_is_dark";

function getInitialTheme(): boolean {
  try {
    // 1. URL 参数最权威：AstrBot iframe 路由必带 ?theme=dark / light
    const params = new URLSearchParams(window.location.search);
    const themeVal = params.get("theme");
    if (themeVal === "dark" || params.get("isDark") === "true") return true;
    if (themeVal === "light" || params.get("isDark") === "false") return false;

    // 2. 本地缓存
    const cached = localStorage.getItem(THEME_CACHE_KEY);
    if (cached !== null) return cached === "true";

    // 3. 父级窗口暗色标记
    try {
      if (window.parent && window.parent.document) {
        const parentHtml = window.parent.document.documentElement;
        if (
          parentHtml.classList.contains("dark") ||
          parentHtml.getAttribute("data-theme") === "dark"
        ) {
          return true;
        }
      }
    } catch {
      // 跨域 iframe 拦截，忽略
    }
  } catch {
    // 忽略
  }
  return false;
}

export function persistTheme(isDark: boolean): void {
  try {
    localStorage.setItem(THEME_CACHE_KEY, String(isDark));
  } catch {
    // 忽略存储异常
  }
}

/** 跟随 AstrBot 宿主暗黑模式的全局主题 Hook */
export function useTheme(): { isDark: boolean } {
  const [isDark, setIsDark] = useState<boolean>(getInitialTheme());

  useEffect(() => {
    let cancelled = false;
    const apply = (dark: boolean) => {
      if (!cancelled) {
        setIsDark(dark);
        persistTheme(dark);
      }
    };

    fetchContext().then((ctx: AstrBotContext) => {
      if (ctx?.isDark !== undefined) apply(!!ctx.isDark);
    }).catch(() => undefined);

    const unsubscribe = onHostContext((ctx) => {
      if (ctx?.isDark !== undefined) apply(!!ctx.isDark);
    });

    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, []);

  return { isDark };
}
