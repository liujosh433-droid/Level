import type { NextConfig } from "next";

/** Backend for /v1 rewrites — keeps session cookies first-party on :3000. */
const API_PROXY_TARGET =
  process.env.LEVEL_API_PROXY_TARGET?.replace(/\/$/, "") || "http://127.0.0.1:8080";

const nextConfig: NextConfig = {
  output: "standalone",
  reactStrictMode: true,
  async rewrites() {
    return [
      {
        source: "/v1/:path*",
        destination: `${API_PROXY_TARGET}/v1/:path*`,
      },
    ];
  },
};

export default nextConfig;
