/** @type {import('next').NextConfig} */
const backend = (process.env.BACKEND_URL || "http://localhost:8000").replace(/\/$/, "");

const nextConfig = {
  // Separate build dirs let a verification build run next to `next dev`.
  distDir: process.env.NEXT_DIST_DIR || ".next",
  poweredByHeader: false,
  // The browser only ever talks to /api on this origin, so the session
  // cookie is first-party. Next proxies to FastAPI.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/:path*` }];
  },
};

export default nextConfig;
