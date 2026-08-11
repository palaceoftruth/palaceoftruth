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

// Vite's `secure` option means "verify the target's TLS certificate". Verify by
// default so a dev proxy pointed at staging or production cannot be silently
// intercepted. The local stack terminates TLS with a certificate Node does not
// trust, so the opt-out stays available - but it must be requested explicitly.
function shouldVerifyProxyTls(target: string, allowInsecure: string | undefined): boolean {
  if (!target.startsWith("https://")) {
    // `secure` is meaningless for plain HTTP targets.
    return false;
  }
  const optedOut = ["1", "true", "yes"].includes((allowInsecure ?? "").trim().toLowerCase());
  if (optedOut) {
    console.warn(
      `[vite] TLS certificate verification is DISABLED for ${target} ` +
        "(VITE_DEV_PROXY_ALLOW_INSECURE_TLS). Never use this against a real environment.",
    );
    return false;
  }
  return true;
}

export default defineConfig(async ({ command, mode }) => {
  const env = loadEnv(mode, "..", "");
  const apiProxyTarget = command === "serve"
    ? await resolveApiProxyTarget(env.VITE_API_PROXY_TARGET)
    : env.VITE_API_PROXY_TARGET || "https://api.palaceoftruth.test";
  const verifyTls = shouldVerifyProxyTls(apiProxyTarget, env.VITE_DEV_PROXY_ALLOW_INSECURE_TLS);

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
          secure: verifyTls,
        },
        "/redoc": {
          target: apiProxyTarget,
          changeOrigin: true,
          secure: verifyTls,
        },
        "/api/": {
          target: apiProxyTarget,
          changeOrigin: true,
          secure: verifyTls,
        },
      },
    },
  };
});
