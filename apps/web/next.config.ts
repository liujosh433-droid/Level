import type { NextConfig } from "next";

const apiTarget = process.env.LEVEL_API_PROXY_TARGET ?? "http://127.0.0.1:8080";

const config: NextConfig = {
  reactStrictMode: true,
  // Keep the Next.js "N" badge off the left data inspector rail.
  devIndicators: {
    position: "bottom-right",
  },
  async rewrites() {
    return [{ source: "/v1/:path*", destination: `${apiTarget}/v1/:path*` }];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "no-referrer" },
        ],
      },
    ];
  },
};

export default config;
