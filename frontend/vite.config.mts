import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

const DEV_PROXY_CANDIDATES = [
  "https://api.palaceoftruth.test",
  "http://backend:8000",
  "http://localhost:8000",
];

// Mirror the deployed nginx proxy in local Vite dev without injecting backend
// credentials into browser-originated requests.
async function isHealthyApiTarget(target: string): Promise<boolean> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 1000);

  try {
    const response = await fetch(`${target}/api/v1/health`, {
      signal: controller.signal,
    });
    if (!response.ok) return false;
    const body = await response.text();
    return body.includes('"status":"ok"');
  } catch {
    return false;
  } finally {
    clearTimeout(timeout);
  }
}

async function resolveApiProxyTarget(explicitTarget?: string) {
  if (explicitTarget) {
    return explicitTarget;
  }

  for (const candidate of DEV_PROXY_CANDIDATES) {
    if (await isHealthyApiTarget(candidate)) {
      return candidate;
    }
  }

  return "https://api.palaceoftruth.test";
}

export default defineConfig(async ({ command, mode }) => {
  const env = loadEnv(mode, "..", "");
  const apiProxyTarget = command === "serve"
    ? await resolveApiProxyTarget(env.VITE_API_PROXY_TARGET)
    : env.VITE_API_PROXY_TARGET || "https://api.palaceoftruth.test";

  console.log(`[vite] API proxy target: ${apiProxyTarget}`);

  return {
    envDir: "..",
    plugins: [react()],
    server: {
      port: 3000,
      proxy: {
        "/docs": {
          target: apiProxyTarget,
          changeOrigin: true,
          secure: !apiProxyTarget.startsWith("https://"),
        },
        "/redoc": {
          target: apiProxyTarget,
          changeOrigin: true,
          secure: !apiProxyTarget.startsWith("https://"),
        },
        "/api/": {
          target: apiProxyTarget,
          changeOrigin: true,
          secure: !apiProxyTarget.startsWith("https://"),
        },
      },
    },
  };
});
